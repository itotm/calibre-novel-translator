import re
import json
from typing import Any

from calibre.utils.localization import _  # type: ignore

from ..lib.utils import request

from .openai import ChatgptTranslate


load_translations()  # type: ignore


def parse_list(value) -> list[str]:
    """Turn a comma/newline separated string into a clean list of slugs."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in re.split(r'[,\n]', str(value))
            if item.strip()]


def parse_object(value) -> dict[str, Any]:
    """Return a dict from a JSON string, or an empty dict when unusable.

    Used for the free-form "extra" fields: a malformed snippet must never
    abort a translation, it is simply ignored (the setting dialog is
    responsible for warning the user about invalid JSON).
    """
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(value)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


class OpenRouterTranslate(ChatgptTranslate):
    """OpenRouter exposes hundreds of models behind a single OpenAI
    compatible endpoint, so it inherits everything from ChatgptTranslate
    and only adds the fields that are specific to the gateway: unified
    reasoning control, provider routing, the extra sampling knobs that
    OpenRouter normalizes across providers, and two escape hatches
    (extra headers / extra body) for anything not exposed in the UI.

    https://openrouter.ai/docs/api-reference/chat-completion
    https://openrouter.ai/docs/api-reference/parameters
    https://openrouter.ai/docs/use-cases/reasoning-tokens
    https://openrouter.ai/docs/features/provider-routing
    """

    name = 'OpenRouter'
    alias = 'OpenRouter'
    endpoint = 'https://openrouter.ai/api/v1/chat/completions'
    api_key_hint = 'sk-or-v1-xxx...xxx'
    # https://openrouter.ai/docs/api-reference/errors
    api_key_errors = [
        '401', '402', '403', 'unauthorized', 'quota', 'credits',
        'insufficient']

    using_tip = _(
        'Get an API key at <a href="https://openrouter.ai/keys">'
        'openrouter.ai/keys</a>. The model list is fetched from OpenRouter '
        'and covers every model it proxies; the "Fine-tuning" and '
        '"OpenRouter" sections below control reasoning, provider routing '
        'and sampling.')

    # OpenRouter has no per-minute interval for paid keys and queues
    # requests itself, so there is no reason to throttle by default.
    concurrency_limit = 1
    request_interval = 0.0
    request_timeout = 300.0

    models: list[str] = []
    model: str | None = 'deepseek/deepseek-v4-flash'

    # A book-length job is worth more consistency than invention: a low
    # temperature keeps names, terminology and register steady across
    # chapters and makes structured output align more reliably. Note that
    # DeepSeek's own guidance suggests a much higher value for one-off
    # translation, so raise it if the prose comes out flat.
    temperature = 0.2

    # -- extra sampling parameters -------------------------------------
    # Every value below is the neutral one: it is omitted from the request
    # so the upstream provider keeps applying its own default. OpenRouter
    # deliberately does not substitute defaults for absent parameters.
    top_k = 0
    frequency_penalty = 0.0
    presence_penalty = 0.0
    repetition_penalty = 1.0
    min_p = 0.0
    top_a = 0.0
    max_tokens = 0
    seed = 0

    # -- reasoning ------------------------------------------------------
    # 'default' omits the field and lets the model decide. 'minimal' is
    # the shipped default: it keeps a short chain of thought (which
    # measurably helps with idiomatic literary translation) without
    # paying for the long deliberation a reasoning model would do on its
    # own. 'none' disables reasoning entirely, which is what local models
    # served through Ollama usually want.
    reasoning_efforts = [
        'default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh']
    reasoning_effort = 'minimal'
    # An explicit token budget (Anthropic, Qwen) takes precedence over
    # the qualitative effort level. 0 means "use the effort level".
    reasoning_max_tokens = 0
    # Keep the chain of thought out of the response: the plugin only ever
    # reads `message.content`, so returning it would just burn bandwidth.
    reasoning_exclude = True

    # -- provider routing ----------------------------------------------
    provider_only = ''
    provider_order = ''
    provider_ignore = ''
    provider_quantizations = ''
    provider_sorts = ['default', 'price', 'throughput', 'latency']
    provider_sort = 'default'
    provider_allow_fallbacks = True
    provider_require_parameters = False
    provider_data_collections = ['allow', 'deny']
    provider_data_collection = 'allow'
    provider_zdr = False

    # -- attribution and escape hatches ---------------------------------
    app_referer = 'https://github.com/itotm/calibre-plugin-ebook-translator'
    app_title = 'Ebook Translator (Novel)'
    extra_headers = ''
    extra_body = ''

    # Names of the settings persisted verbatim in the engine preferences.
    # Declared once so __init__ and the setting dialog stay in sync.
    preference_keys = (
        'top_k', 'frequency_penalty', 'presence_penalty',
        'repetition_penalty', 'min_p', 'top_a', 'max_tokens', 'seed',
        'reasoning_effort', 'reasoning_max_tokens', 'reasoning_exclude',
        'provider_only', 'provider_order', 'provider_ignore',
        'provider_quantizations', 'provider_sort',
        'provider_allow_fallbacks', 'provider_require_parameters',
        'provider_data_collection', 'provider_zdr',
        'app_referer', 'app_title', 'extra_headers', 'extra_body')

    def __init__(self):
        super().__init__()
        for key in self.preference_keys:
            setattr(self, key, self.config.get(key, getattr(self, key)))

    def get_models(self):
        """https://openrouter.ai/docs/api-reference/list-available-models"""
        endpoint = self.endpoint or ''
        model_endpoint = re.sub(
            r'/chat/completions/?$', '/models', endpoint)
        if model_endpoint == endpoint:
            model_endpoint = '%s/models' % endpoint.rstrip('/')
        response = request(
            model_endpoint, headers=self.get_headers(),
            proxy_uri=self.proxy_uri)
        return sorted(
            item['id'] for item in json.loads(response).get('data') or [])

    def get_headers(self):
        headers = super().get_headers()
        # Optional attribution headers used by openrouter.ai/rankings.
        if self.app_referer:
            headers['HTTP-Referer'] = str(self.app_referer)
        if self.app_title:
            headers['X-Title'] = str(self.app_title)
        for name, value in parse_object(self.extra_headers).items():
            headers[str(name)] = str(value)
        return headers

    def get_reasoning(self) -> dict[str, Any]:
        """Build the unified `reasoning` object.

        https://openrouter.ai/docs/use-cases/reasoning-tokens
        """
        reasoning: dict[str, Any] = {}
        if int(self.reasoning_max_tokens or 0) > 0:
            reasoning['max_tokens'] = int(self.reasoning_max_tokens)
        elif self.reasoning_effort and self.reasoning_effort != 'default':
            reasoning['effort'] = self.reasoning_effort
        if reasoning and self.reasoning_exclude:
            reasoning['exclude'] = True
        return reasoning

    def get_provider_routing(self) -> dict[str, Any]:
        """Build the `provider` object.

        https://openrouter.ai/docs/features/provider-routing
        """
        provider: dict[str, Any] = {}
        for key, value in (
                ('only', self.provider_only),
                ('order', self.provider_order),
                ('ignore', self.provider_ignore),
                ('quantizations', self.provider_quantizations)):
            slugs = parse_list(value)
            if slugs:
                provider[key] = slugs
        if self.provider_sort and self.provider_sort != 'default':
            provider['sort'] = self.provider_sort
        if not self.provider_allow_fallbacks:
            provider['allow_fallbacks'] = False
        if self.provider_require_parameters:
            provider['require_parameters'] = True
        if self.provider_data_collection == 'deny':
            provider['data_collection'] = 'deny'
        if self.provider_zdr:
            provider['zdr'] = True
        return provider

    def extend_body(self, body: dict[str, Any]) -> dict[str, Any]:
        """Add the OpenRouter specific fields to an OpenAI-shaped body."""
        for key, neutral in (
                ('top_k', 0),
                ('frequency_penalty', 0.0),
                ('presence_penalty', 0.0),
                ('repetition_penalty', 1.0),
                ('min_p', 0.0),
                ('top_a', 0.0),
                ('max_tokens', 0),
                ('seed', 0)):
            value = getattr(self, key)
            if value in (None, ''):
                continue
            value = type(neutral)(value)
            if value != neutral:
                body[key] = value
        # `reasoning` supersedes the OpenAI-only `reasoning_effort` field
        # that ChatgptTranslate may have added.
        body.pop('reasoning_effort', None)
        reasoning = self.get_reasoning()
        if reasoning:
            body['reasoning'] = reasoning
        provider = self.get_provider_routing()
        if provider:
            body['provider'] = provider
        # Applied last so it can override anything computed above.
        body.update(parse_object(self.extra_body))
        return body

    def get_body(self, text):
        return json.dumps(self.extend_body(json.loads(super().get_body(text))))

    def get_body_for_structured(self, text, schema=None):
        return json.dumps(self.extend_body(
            json.loads(super().get_body_for_structured(text, schema))))

    def get_result(self, response):
        # OpenRouter reports upstream provider failures inside a 200 body,
        # which would otherwise surface as an opaque parsing error.
        if not self.stream:
            try:
                error = json.loads(response).get('error')
            except Exception:
                error = None
            if isinstance(error, dict):
                raise Exception(_('Error: {}').format(
                    error.get('message') or json.dumps(error)))
        return super().get_result(response)
