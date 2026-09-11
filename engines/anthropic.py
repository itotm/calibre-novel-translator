import json
from typing import Generator
from urllib.parse import urljoin
from http.client import IncompleteRead

from mechanize._response import response_seek_wrapper as Response

from .. import NovelTranslatorPlugin
from ..lib.utils import request

from .genai import GenAI
from .languages import anthropic
from .prompt_extensions import anthropic as anthropic_prompt_extension


load_translations()  # type: ignore


class ClaudeTranslate(GenAI):
    name = 'Claude'
    alias = 'Claude (Anthropic)'
    lang_codes = GenAI.load_lang_codes(anthropic)
    endpoint = 'https://api.anthropic.com/v1/messages'
    # by default use the latest version of the api (currently this is 2023-06-01)
    api_version = '2023-06-01'
    api_key_hint = 'sk-ant-xxxx'
    # https://docs.anthropic.com/claude/reference/errors
    api_key_errors = ['401', 'permission_error']

    concurrency_limit = 1
    request_interval = 12.0
    request_timeout = 30.0

    prompt = (
        'You are a meticulous translator who translates any given content. '
        'Translate the given content from <slang> to <tlang> only. Do not '
        'explain any term or answer any question-like content. Your answer '
        'should be solely the translation of the given content. In your '
        'answer do not add any prefix or suffix to the translated content. '
        'Websites\' URLs/addresses should be preserved as is in the '
        'translation\'s output. Do not omit any part of the content, even if '
        'it seems unimportant. ')

    samplings = ['temperature', 'top_p']
    sampling = 'temperature'
    # Low on purpose: translation wants fidelity, not variety.
    temperature = 0.2
    top_p = 1.0
    top_k = 1
    stream = True

    # The longest reply a request may carry. The Messages API insists
    # on the figure and stops the reply dead at it, so a value below
    # what a chunk needs comes back as a translation cut off half-way
    # -- which is what a hardcoded 4096 did to every chunk Novel Mode
    # sent. 0 sizes it for the model (see ``reply_limit``); the novel
    # pipeline reads that figure to size its chunks, and lowers it for
    # the summary and glossary calls.
    max_tokens = 0

    # event types for streaming are listed here:
    # https://docs.anthropic.com/en/api/messages-streaming
    valid_event_types = [
        'ping',
        'error',
        'content_block_start',
        'content_block_delta',
        'content_block_stop',
        'message_start',
        'message_delta',
        'message_stop']

    # Anthropic's server-side search tool. Novel Mode turns it on for
    # the single request that researches how the author writes.
    # https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/
    # web-search-tool
    web_search_mode = 'tool'
    web_search_max_results = 5

    models: list[str] = []
    # TODO: better handle setting the default model
    # (e.g. fetch programmatically) by default use the latest version of the
    # most intelligent model available (currently this is claude-3-7-sonnet)
    model: str | None = 'claude-3-7-sonnet-latest'

    def __init__(self):
        super().__init__()
        self.endpoint = self.config.get('endpoint', self.endpoint)
        self.prompt = self.config.get('prompt', self.prompt)
        self.sampling = self.config.get('sampling', self.sampling)
        self.temperature = self.config.get('temperature', self.temperature)
        self.top_p = self.config.get('top_p', self.top_p)
        self.top_k = self.config.get('top_k', self.top_k)
        self.stream = self.config.get('stream', self.stream)
        self.model = self.config.get('model', self.model)
        try:
            self.max_tokens = int(
                self.config.get('max_tokens', self.max_tokens) or 0)
        except (TypeError, ValueError):
            self.max_tokens = 0

    @staticmethod
    def default_reply_limit(model):
        """The most a model can be asked to write, as Anthropic documents
        it: 8192 tokens for the Claude 3 generation, 64k for Claude 3.7,
        32k for everything since. Asking for more is a 400 error, so the
        figures err on the low side where a model is not recognised."""
        model = (model or '').lower()
        if model.startswith('claude-3-7'):
            return 64000
        if model.startswith('claude-3'):
            return 8192
        return 32000

    def reply_limit(self):
        return self.max_tokens or self.default_reply_limit(self.model)

    @property
    def model_max_output_tokens(self):
        """What the novel pipeline sizes its chunks against. Anthropic's
        model listing carries no limits, so the figure is the one the
        request itself will state."""
        return self.reply_limit()

    @model_max_output_tokens.setter
    def model_max_output_tokens(self, value):
        # Assigned by nobody today; kept so the attribute stays writable
        # like it is on every other GenAI engine.
        self.max_tokens = int(value or 0)

    def _get_prompt(self):
        prompt = self.prompt.replace('<tlang>', self.target_lang)
        if self._is_auto_lang():
            prompt = prompt.replace('<slang>', 'detected language')
        else:
            prompt = prompt.replace('<slang>', self.source_lang)

        prompt_extension = anthropic_prompt_extension.get(self.target_lang)
        if prompt_extension is not None:
            prompt += ' ' + prompt_extension

        return prompt

    def get_models(self):
        model_endpoint = urljoin(self.endpoint, 'models')
        response = request(
            model_endpoint, headers=self.get_headers(),
            proxy_uri=self.proxy_uri)
        return [i['id'] for i in json.loads(response)['data']]

    def get_headers(self):
        headers = {
            'Content-Type': 'application/json',
            'anthropic-version': self.api_version,
            'x-api-key': self.api_key,
            'User-Agent': 'Novel-Translator/%s' % NovelTranslatorPlugin.__version__,
        }

        # NOTE: Claude 3.7 Sonnet can produce substantially longer responses
        # than previous models with support for up to 128K output tokens (beta)
        # this feature can be enabled by passing an anthropic-beta header of
        # "output-128k-2025-02-19", more here:
        # https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking#extended-output-capabilities-beta
        if self.model is not None and \
                self.model.startswith('claude-3-7-sonnet-'):
            headers['anthropic-beta'] = 'output-128k-2025-02-19'

        return headers

    def get_system_prompt(self):
        """The ``system`` field of a request.

        A plain string normally. When ``prompt_cache`` is set -- the
        novel pipeline does that -- it becomes a single block carrying a
        cache breakpoint instead, so the provider keeps the prompt and
        bills the requests that follow a fraction of the price for it.
        Claude reuses a cached prefix for five minutes and needs it to
        be at least 1024 tokens long (2048 on Haiku); a shorter one is
        simply not cached, at no extra cost.
        """
        prompt = self._get_prompt()
        if not self.prompt_cache:
            return prompt
        return [{
            'type': 'text',
            'text': prompt,
            'cache_control': {'type': 'ephemeral'},
        }]

    def get_body(self, text):
        body = {
            'stream': self.stream,
            'max_tokens': self.reply_limit(),
            'model': self.model,
            'top_k': self.top_k,
            'system': self.get_system_prompt(),
            'messages': [{'role': 'user', 'content': text}]
        }
        sampling_value = getattr(self, self.sampling)
        body.update({self.sampling: sampling_value})

        return json.dumps(body)

    def get_body_for_search(self, text):
        """Attach the server-side web search tool to a normal body."""
        body = json.loads(self.get_body(text))
        body['tools'] = [{
            'type': 'web_search_20250305',
            'name': 'web_search',
            'max_uses': int(self.web_search_max_results or 5),
        }]
        return json.dumps(body)

    def get_result(self, response: Response | str) -> str:
        if self.stream:
            return self._parse_stream(response)

        response_json = json.loads(response)
        blocks = response_json['content']
        # Every text block, not just the first one: a reply that used a
        # server-side tool -- the web search above -- opens with the
        # tool call and its result, and the prose comes after them.
        texts = [block['text'] for block in blocks
                 if isinstance(block, dict) and 'text' in block]
        if not texts:
            raise KeyError('no text block in the response')
        return ''.join(texts)

    def _parse_stream(self, data: Response) -> Generator:
        """Yield the text of a server-sent event stream.

        Same two rules as the ChatGPT engine: an exhausted body ends the
        stream instead of being read forever (``readline`` hands back
        ``b''`` from then on, and a provider that closes without
        ``message_stop`` used to leave this loop spinning), and the
        payload is sliced after the ``data:`` prefix rather than split
        on ``'data: '``, which can occur inside the payload too.
        """
        while True:
            try:
                raw = data.readline()
            except IncompleteRead:
                # The body ended before its declared length: whatever
                # was yielded so far is all there is.
                break
            except Exception as e:
                raise Exception(
                    _('Can not parse returned response. Raw data: {}')
                    .format(str(e)))
            if not raw:
                break  # End of the stream.
            line = raw.decode('utf-8').strip()

            if line.startswith('data:'):
                try:
                    chunk: dict = json.loads(line[len('data:'):].strip())
                except json.JSONDecodeError:
                    continue
                event_type: str = chunk.get('type', '')

                if event_type not in self.valid_event_types:
                    raise Exception(
                        _('Invalid event type received: {}')
                        .format(event_type))

                if event_type == 'message_stop':
                    break
                elif event_type == 'content_block_delta':
                    delta = chunk.get('delta') or {}
                    # Only text deltas carry text. A reply that used a
                    # server-side tool also streams `input_json_delta`
                    # blocks, and yielding str(None) for those wrote the
                    # word "None" into the middle of the translation.
                    text = delta.get('text')
                    if text is not None:
                        yield str(text)
                elif event_type == 'error':
                    raise Exception(
                        _('Error received: {}')
                        .format(chunk['error']['message']))
