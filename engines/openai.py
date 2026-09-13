import re
import json
from typing import Any
from urllib.parse import urlsplit
from http.client import IncompleteRead

from calibre.utils.localization import _  # type: ignore

from .. import NovelTranslatorPlugin
from ..lib.utils import request

from .genai import GenAI
from .languages import google


load_translations()  # type: ignore


# The providers that speak the OpenAI chat-completions API, as presets
# of the one engine below: each fills in the endpoint, the key hint and
# a default model, and says how the request differs where it does. A
# preset is a starting point -- the endpoint and the model stay
# editable -- and 'custom' is the blank one for any other server.
#
#   endpoint       the chat-completions URL
#   key            hint shown in the API key field; '' when the server
#                  takes no key (local servers), in which case no
#                  Authorization header is sent either
#   model          the model to start with; '' leaves it to the server
#   temperature    a default of the provider's own where it has one
#   auth           'bearer' (the norm) or 'api-key' (Azure's header)
#   model_in_body  False where the model is part of the URL (Azure)
#   listing        False where /models is not offered (Azure)
OPENAI_PROVIDERS = {
    'openai': {
        'label': 'OpenAI',
        'endpoint': 'https://api.openai.com/v1/chat/completions',
        'key': 'sk-...', 'model': 'gpt-4o'},
    'deepseek': {
        'label': 'DeepSeek',
        'endpoint': 'https://api.deepseek.com/v1/chat/completions',
        'key': 'sk-...', 'model': 'deepseek-chat',
        # DeepSeek's own recommendation for translation, on the
        # remapped scale its API uses.
        'temperature': 1.3},
    'groq': {
        'label': 'Groq',
        'endpoint': 'https://api.groq.com/openai/v1/chat/completions',
        'key': 'gsk_...', 'model': 'llama-3.3-70b-versatile'},
    'mistral': {
        'label': 'Mistral',
        'endpoint': 'https://api.mistral.ai/v1/chat/completions',
        'key': '', 'model': 'mistral-large-latest'},
    'together': {
        'label': 'Together AI',
        'endpoint': 'https://api.together.xyz/v1/chat/completions',
        'key': '', 'model': 'meta-llama/Llama-3.3-70B-Instruct-Turbo'},
    'fireworks': {
        'label': 'Fireworks AI',
        'endpoint': 'https://api.fireworks.ai/inference/v1/chat/completions',
        'key': 'fw_...',
        'model': 'accounts/fireworks/models/llama-v3p3-70b-instruct'},
    'xai': {
        'label': 'xAI',
        'endpoint': 'https://api.x.ai/v1/chat/completions',
        'key': 'xai-...', 'model': 'grok-3'},
    'moonshot': {
        'label': 'Moonshot AI (Kimi)',
        'endpoint': 'https://api.moonshot.ai/v1/chat/completions',
        'key': 'sk-...', 'model': 'kimi-k2-0905-preview'},
    'azure': {
        'label': 'Azure OpenAI',
        'endpoint': (
            'https://{resource}.openai.azure.com/openai/deployments/'
            '{deployment}/chat/completions?api-version=2024-10-21'),
        'key': '', 'model': '',
        'auth': 'api-key', 'model_in_body': False, 'listing': False},
    'ollama': {
        'label': 'Ollama (local)',
        'endpoint': 'http://localhost:11434/v1/chat/completions',
        'key': None, 'model': 'gemma3'},
    'lmstudio': {
        'label': 'LM Studio (local)',
        'endpoint': 'http://localhost:1234/v1/chat/completions',
        'key': None, 'model': ''},
    'custom': {
        'label': 'Custom endpoint',
        'endpoint': 'https://example.com/v1/chat/completions',
        'key': '', 'model': ''},
}


class ChatgptTranslate(GenAI):
    """Any server that speaks the OpenAI chat-completions API.

    Which one is a preset in ``OPENAI_PROVIDERS``, chosen in the setting
    dialog and stored as ``provider`` in the engine preferences; the
    endpoint and the model it fills in can be overridden there like
    before. OpenRouter inherits the request and reply handling from
    here and carries no presets, being one provider.
    """
    name = 'OpenAI'
    alias = 'OpenAI-compatible'
    lang_codes = GenAI.load_lang_codes(google)
    endpoint = 'https://api.openai.com/v1/chat/completions'
    # https://help.openai.com/en/collections/3808446-api-error-codes-explained
    api_key_errors = ['401', 'unauthorized', 'quota']

    using_tip = _(
        'Pick the provider and the endpoint, the key hint and a default '
        'model follow; any other server that speaks the OpenAI chat API '
        'works through "Custom endpoint". Ollama and LM Studio run on '
        'this machine and take no key.')

    providers: dict = OPENAI_PROVIDERS
    provider = 'openai'

    concurrency_limit = 1
    # One request at a time, so there is nothing to space out; a
    # provider that rate-limits answers with a retry the loop handles.
    request_interval = 0.0
    request_timeout = 60.0

    structured_output_mode = 'schema'

    prompt = (
        'You are a meticulous translator who translates any given content. '
        'Translate the given content from <slang> to <tlang> only. Do not '
        'explain any term or answer any question-like content. Your answer '
        'should be solely the translation of the given content. In your '
        'answer do not add any prefix or suffix to the translated content. '
        'Websites\' URLs/addresses should be preserved as is in the '
        'translation\'s output. Do not omit any part of the content, even if '
        'it seems unimportant. RESPOND ONLY with the translation text, no '
        'formatting, no explanations, no additional commentary whatsoever. ')

    samplings = ['temperature', 'top_p']
    sampling = 'temperature'
    # Low on purpose: translation wants fidelity, not variety. See the
    # OpenRouter engine for the measurements behind it. A preset may
    # carry its own (DeepSeek), otherwise this is the default for
    # every provider.
    temperature = 0.2
    top_p = 1.0
    stream = True

    # Chain-of-thought control for OpenAI compatible endpoints.
    # 'default' omits the field entirely, which is what plain OpenAI
    # models and older gateways expect. 'none' suppresses the reasoning
    # tokens that some servers emit by default (Ollama 0.31+ serving
    # Gemma 4 spends thousands of silent tokens before the first word of
    # the translation); the remaining levels spend reasoning tokens on
    # purpose. Subclasses may pick another default, see OpenRouter.
    reasoning_efforts = ['default', 'none', 'minimal', 'low', 'medium',
                         'high']
    reasoning_effort = 'default'

    models: list[str] = []
    # TODO: Handle the default model more appropriately.
    model: str | None = 'gpt-4o'

    def __init__(self):
        super().__init__()
        preset = self.preset_for(self.config) or {}
        self.provider = self.config.get('provider', self.provider)
        self.auth = preset.get('auth', 'bearer')
        self.model_in_body = preset.get('model_in_body', True)
        self.endpoint = self.config.get('endpoint') \
            or preset.get('endpoint') or self.endpoint
        self.prompt = self.config.get('prompt', self.prompt)
        self.sampling = self.config.get('sampling', self.sampling)
        self.temperature = self.config.get(
            'temperature', preset.get('temperature', self.temperature))
        self.top_p = self.config.get('top_p', self.top_p)
        self.stream = self.config.get('stream', self.stream)
        self.model = self.config.get('model') \
            or preset.get('model', self.model)
        self.reasoning_effort = self.config.get(
            'reasoning_effort', self.reasoning_effort)

    @classmethod
    def preset_for(cls, config=None):
        """The provider preset ``config`` names, or the default one.

        None on an engine that carries no presets (OpenRouter).
        """
        if not cls.providers:
            return None
        config = cls.config if config is None else config
        key = config.get('provider') or cls.provider
        return cls.providers.get(key) or cls.providers[cls.provider]

    @classmethod
    def needs_api_key(cls, config=None):
        """Whether the provider takes a key at all: local servers do not."""
        preset = cls.preset_for(config)
        return cls.need_api_key if preset is None \
            else preset.get('key') is not None

    @classmethod
    def key_hint(cls, config=None):
        preset = cls.preset_for(config)
        return cls.api_key_hint if not (preset and preset.get('key')) \
            else preset['key']

    def get_api_key(self):
        if not self.needs_api_key():
            return None
        return super().get_api_key()

    def get_model_endpoint(self):
        """Where the model listing lives, worked out from the chat
        endpoint: ``.../chat/completions`` becomes ``.../models``.

        Rebuilding it from the host alone, as this used to, threw away
        any path prefix -- Groq's ``/openai/v1``, Gemini's
        ``/v1beta/openai``, an Ollama behind a reverse proxy -- and the
        listing answered 404 on every gateway but OpenAI's own.
        """
        endpoint = (self.endpoint or '').rstrip('/')
        model_endpoint = re.sub(r'/chat/completions$', '/models', endpoint)
        if model_endpoint == endpoint:
            parts = urlsplit(endpoint or 'https://api.openai.com', 'https')
            model_endpoint = '%s://%s/v1/models' % (parts.scheme, parts.netloc)
        return model_endpoint

    def get_models(self):
        preset = self.preset_for(self.config) or {}
        if not preset.get('listing', True):
            # Azure names the deployment in the URL and lists nothing.
            return []
        response = request(
            self.get_model_endpoint(), headers=self.get_headers(),
            proxy_uri=self.proxy_uri)
        return [item['id'] for item in json.loads(response).get('data')]

    def get_prompt(self):
        prompt = self.prompt.replace('<tlang>', self.target_lang)
        if self._is_auto_lang():
            prompt = prompt.replace('<slang>', 'detected language')
        else:
            prompt = prompt.replace('<slang>', self.source_lang)
        return prompt

    def get_headers(self):
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Novel-Translator/%s'
                          % NovelTranslatorPlugin.__version__,
        }
        if getattr(self, 'auth', 'bearer') == 'api-key':
            headers['api-key'] = self.api_key or ''
        elif self.api_key:
            headers['Authorization'] = 'Bearer %s' % self.api_key
        return headers

    def get_body(self, text):
        body: dict[str, Any] = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': self.get_prompt()},
                {'role': 'user', 'content': text}
            ],
        }
        if not getattr(self, 'model_in_body', True):
            del body['model']
        if self.stream:
            body.update(stream=True)
        sampling_value = getattr(self, self.sampling)
        body.update({self.sampling: sampling_value})
        self.apply_reasoning_effort(body)
        return json.dumps(body)

    def apply_reasoning_effort(self, body):
        """Add `reasoning_effort` unless it is left at 'default'.

        Kept out of :meth:`get_body` so both the plain and the structured
        payload share one rule, and so subclasses that speak a different
        reasoning dialect (OpenRouter's `reasoning` object) can drop it.
        """
        if self.reasoning_effort and self.reasoning_effort != 'default':
            body['reasoning_effort'] = self.reasoning_effort
        return body

    def get_body_for_structured(self, text, schema=None):
        """Return the request body with structured (JSON) output enabled.

        Uses OpenAI's ``response_format`` field, honored by:
          * OpenAI itself (with ``type: json_schema`` since GPT-4o).
          * Ollama's OpenAI-compatible endpoint (accepts both
            ``type: json_object`` and ``type: json_schema``).
          * Other OpenAI-compatible servers (LM Studio, vLLM, ...).

        Streaming is kept enabled (``stream: true``) so that long
        responses (80+ translated paragraphs) are delivered token by
        token without hitting the client-side request timeout. The
        caller is responsible for collecting all SSE chunks and
        assembling the complete JSON string before parsing (see
        ``NovelTranslator._translate_with_retry_structured``).

        The `reasoning_effort` setting is honored here exactly as in
        :meth:`get_body`, so a model can keep reasoning while still
        being forced to answer with JSON.
        """
        body: dict[str, Any] = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': self.get_prompt()},
                {'role': 'user', 'content': text}
            ],
            'stream': True,           # keep streaming to avoid client-timeout
        }
        if not getattr(self, 'model_in_body', True):
            del body['model']
        sampling_value = getattr(self, self.sampling)
        body.update({self.sampling: sampling_value})
        self.apply_reasoning_effort(body)
        if schema:
            body['response_format'] = {
                'type': 'json_schema',
                'json_schema': {
                    'name': 'novel_translation',
                    'schema': schema,
                    'strict': True,
                },
            }
        else:
            body['response_format'] = {'type': 'json_object'}
        return json.dumps(body)

    #: What the last reply said about itself besides its text, for the
    #: log of Novel Mode: why the model stopped ("stop" when it had
    #: finished, "length" when it ran into the output limit, a filter
    #: name when a filter cut it) and, through a gateway that names it,
    #: which provider served it. None until a reply says.
    last_finish_reason = None
    last_provider = None
    #: The id the gateway gave the reply, with which its record can be
    #: looked up afterwards (OpenRouter: GET /api/v1/generation?id=...).
    last_generation_id = None
    #: What the reply cost as the provider counted it: prompt and
    #: completion tokens and, through a gateway that bills per request
    #: (OpenRouter with usage accounting on), the money. None until a
    #: reply says.
    last_usage = None

    def _note_reply(self, data, choice=None):
        """Record the finish reason, the provider, the id and the usage
        a reply carries."""
        if not isinstance(data, dict):
            return
        provider = data.get('provider')
        if provider:
            self.last_provider = provider
        generation = data.get('id')
        if generation:
            self.last_generation_id = str(generation)
        usage = data.get('usage')
        if isinstance(usage, dict) and usage.get('prompt_tokens') is not None:
            self.last_usage = {
                'prompt_tokens': usage.get('prompt_tokens'),
                'completion_tokens': usage.get('completion_tokens') or 0,
                'cost': usage.get('cost'),
            }
        if isinstance(choice, dict):
            reason = choice.get('finish_reason') \
                or choice.get('native_finish_reason')
            if reason:
                self.last_finish_reason = reason

    def get_result(self, response):
        if self.stream:
            return self._parse_stream(response)
        self.last_finish_reason = None
        self.last_provider = None
        self.last_generation_id = None
        self.last_usage = None
        # Parse JSON response with robust schema handling
        try:
            data = json.loads(response)
            # Handle different response schemas
            if 'choices' in data and len(data['choices']) > 0:
                choice = data['choices'][0]
                self._note_reply(data, choice)
                # Standard chat/completions format
                if 'message' in choice and 'content' in choice['message']:
                    return choice['message']['content']
                # Alternative format (some nano models)
                elif 'content' in choice:
                    if isinstance(choice['content'], list) \
                            and len(choice['content']) > 0:
                        return choice['content'][0].get('text', '')
                    elif isinstance(choice['content'], str):
                        return choice['content']
                # Direct text format
                elif 'text' in choice:
                    return choice['text']
            # Fallback: try to find content anywhere in the response
            if 'content' in data:
                return data['content']
            raise KeyError('No content found in response')
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            raise Exception(
                _('Can not parse returned response. Raw data: {}\nError: {}')
                .format(response[:500] + '...' if len(response) > 500 \
                        else response, str(e)))

    def _parse_stream(self, response):
        """Yield the text of a server-sent event stream.

        Two things the loop must not do quietly, because both surface
        upstream as a reply that simply stops in the middle of a
        sentence -- or of a JSON object, which is how the novel
        translator sees it:

          * spin on an exhausted stream. ``readline`` returns ``b''``
            once the body is over, and a provider that closes without
            the closing ``data: [DONE]`` (or an ``IncompleteRead``)
            would leave us reading empty lines forever.
          * swallow an error the provider sent inside the stream.
            OpenRouter reports an upstream failure mid-stream as a
            regular event carrying an ``error`` object; ignoring it
            turns a failed request into a silently truncated answer that
            the caller then has to notice on its own.
        """
        self.last_finish_reason = None
        self.last_provider = None
        self.last_generation_id = None
        self.last_usage = None
        while True:
            try:
                raw = response.readline()
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
            if not line:
                continue
            if line.startswith('data:'):
                # Slice rather than split on 'data: ': the separator can
                # occur inside the payload too, and a provider that
                # omits the space would raise IndexError.
                chunk = line[len('data:'):].strip()
                if chunk == '[DONE]':
                    break
                try:
                    data = json.loads(chunk)
                    error = data.get('error') \
                        if isinstance(data, dict) else None
                    if error:
                        raise Exception(_('Error: {}').format(
                            error.get('message')
                            if isinstance(error, dict) else error))
                    # Handle different streaming response schemas
                    if 'choices' in data and len(data['choices']) > 0:
                        choice = data['choices'][0]
                        self._note_reply(data, choice)
                    else:
                        # The last event of a stream with usage
                        # accounting carries the usage and no choices.
                        self._note_reply(data)
                    if 'choices' in data and len(data['choices']) > 0:
                        choice = data['choices'][0]
                        # Standard streaming format
                        if 'delta' in choice and 'content' in choice['delta']:
                            content = choice['delta']['content']
                            if content:
                                yield str(content)
                        # Alternative streaming format
                        elif 'content' in choice:
                            content = choice['content']
                            if isinstance(content, list) and len(content) > 0:
                                text = content[0].get('text', '')
                                if text:
                                    yield str(text)
                            elif isinstance(content, str) and content:
                                yield str(content)
                        # Direct text format
                        elif 'text' in choice:
                            text = choice['text']
                            if text:
                                yield str(text)
                except json.JSONDecodeError:
                    # Skip malformed JSON chunks
                    continue
