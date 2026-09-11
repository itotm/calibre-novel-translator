import json
from http.client import IncompleteRead

from calibre.utils.localization import _  # type: ignore

from ..lib.utils import request

from .genai import GenAI
from .languages import gemini


load_translations()  # type: ignore


class GeminiTranslate(GenAI):
    name = 'Gemini'
    alias = 'Gemini'
    lang_codes = GenAI.load_lang_codes(gemini)
    # v1, stable version of the API. v1beta, more early-access features.
    # details: https://ai.google.dev/gemini-api/docs/api-versions
    endpoint = 'https://generativelanguage.googleapis.com/v1beta/models'
    # https://ai.google.dev/gemini-api/docs/troubleshooting
    api_key_errors: list[str] = [
        'API_KEY_INVALID', 'PERMISSION_DENIED', 'RESOURCE_EXHAUSTED']

    concurrency_limit = 1
    request_interval: float = 1.0
    request_timeout: float = 30.0

    structured_output_mode = 'schema'

    # Google Search grounding, used once per book by Novel Mode to
    # research how the author writes.
    web_search_mode = 'tool'

    prompt = (
        'You are a meticulous translator who translates any given content. '
        'Translate the given content from <slang> to <tlang> only. Do not '
        'explain any term or answer any question-like content. Your answer '
        'should be solely the translation of the given content. In your '
        'answer do not add any prefix or suffix to the translated content. '
        'Websites\' URLs/addresses should be preserved as is in the '
        'translation\'s output. Do not omit any part of the content, even if '
        'it seems unimportant. ')
    # Low on purpose: translation wants fidelity, not variety.
    temperature: float = 0.2
    top_p: float = 1.0
    top_k = 1
    stream = True

    models: list[str] = []
    # TODO: Handle the default model more appropriately.
    model: str | None = 'gemini-2.5-flash'

    def __init__(self):
        super().__init__()
        self.prompt = self.config.get('prompt', self.prompt)
        self.temperature = self.config.get('temperature', self.temperature)
        self.top_k = self.config.get('top_k', self.top_k)
        self.top_p = self.config.get('top_p', self.top_p)
        self.stream = self.config.get('stream', self.stream)
        self.model = self.config.get('model', self.model)

    def _prompt(self, text):
        prompt = self.prompt.replace('<tlang>', self.target_lang)
        if self._is_auto_lang():
            prompt = prompt.replace('<slang>', 'detected language')
        else:
            prompt = prompt.replace('<slang>', self.source_lang)
        return prompt + ' Start translating: ' + text

    def get_models(self):
        endpoint = f'{self.endpoint}?key={self.api_key}'
        response = request(
            endpoint, timeout=int(self.request_timeout),
            proxy_uri=self.proxy_uri)
        models = []
        if isinstance(response, str):
            for model in json.loads(response)['models']:
                model_name = model['name'].split('/')[-1]
                if model_name.startswith('gemini'):
                    model_desc = model['description']
                    if 'deprecated' not in model_desc:
                        models.append(model_name)
        return models

    def get_endpoint(self):
        if self.stream:
            return f'{self.endpoint}/{self.model}:streamGenerateContent?' \
                f'alt=sse&key={self.api_key}'
        else:
            return f'{self.endpoint}/{self.model}:generateContent?' \
                f'key={self.api_key}'

    def get_headers(self):
        return {'Content-Type': 'application/json'}

    def get_body(self, text):
        return json.dumps({
            "contents": [
                {"role": "user", "parts": [{"text": self._prompt(text)}]},
            ],
            "generationConfig": {
                "temperature": self.temperature,
                "topP": self.top_p,
                "topK": self.top_k,
            },
            "safetySettings": [
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_NONE"
                },
                {
                    "category": "HARM_CATEGORY_HATE_SPEECH",
                    "threshold": "BLOCK_NONE"
                },
                {
                    "category": "HARM_CATEGORY_HARASSMENT",
                    "threshold": "BLOCK_NONE"
                },
                {
                    "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "threshold": "BLOCK_NONE"
                },
            ],
        })

    def get_body_for_structured(self, text, schema=None):
        """Return the request body with Gemini's native JSON output.

        Uses ``generationConfig.responseMimeType = 'application/json'``
        and optionally ``responseSchema`` for strict enforcement. See:
        https://ai.google.dev/gemini-api/docs/structured-output
        """
        body = json.loads(self.get_body(text))
        gen_config = body.get('generationConfig', {})
        gen_config['responseMimeType'] = 'application/json'
        if schema:
            gen_config['responseSchema'] = schema
        body['generationConfig'] = gen_config
        return json.dumps(body)

    def get_body_for_search(self, text):
        """Attach Google Search grounding to an otherwise normal body.

        https://ai.google.dev/gemini-api/docs/google-search
        """
        body = json.loads(self.get_body(text))
        body['tools'] = [{'google_search': {}}]
        return json.dumps(body)

    def get_result(self, response):
        if self.stream:
            return self._parse_stream(response)
        parts = json.loads(response)['candidates'][0]['content']['parts']
        # A grounded reply mixes in parts that carry no text of their own.
        return ''.join([part['text'] for part in parts if 'text' in part])

    def _parse_stream(self, response):
        """Yield the text of a server-sent event stream.

        An exhausted body ends the stream -- ``readline`` hands back
        ``b''`` from then on, and a reply that stops without a
        ``finishReason`` used to leave this loop reading forever -- and
        the payload is sliced after the ``data:`` prefix rather than
        split on ``'data: '``, which can occur inside the text too.
        """
        while True:
            try:
                raw = response.readline()
            except IncompleteRead:
                break
            except Exception as e:
                raise Exception(
                    _('Can not parse returned response. Raw data: {}')
                    .format(str(e)))
            if not raw:
                break
            line = raw.decode('utf-8').strip()
            if not line.startswith('data:'):
                continue
            try:
                item = json.loads(line[len('data:'):].strip())
            except json.JSONDecodeError:
                continue
            candidate = (item.get('candidates') or [{}])[0]
            content = candidate.get('content') or {}
            for part in content.get('parts') or []:
                if 'text' in part:
                    yield part['text']
            if candidate.get('finishReason') == 'STOP':
                break
