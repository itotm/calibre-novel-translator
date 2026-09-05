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

    # Lower than what the other engines default to. Translation is a
    # high-certainty task where diversity is noise: measured over six
    # temperatures, quality falls as it rises -- 4.3 COMET points from 0
    # to 1 on English to Chinese (arXiv:2303.13780). It also keeps a
    # model on the requested JSON shape where the provider does not
    # enforce it, and keeps names and terminology steady over a book.
    #
    # 0.3 rather than something lower because that is the value DeepSeek
    # runs its own product at, and its published table -- 1.3 for
    # translation -- is written on the API's remapped scale, not on the
    # raw one this gateway forwards. Raise it in the Fine-tuning section
    # if the prose comes out flat.
    temperature = 0.3

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

    # Filled in by the setting dialog from the model listing. See
    # GenAI.model_max_output_tokens and model_supported_parameters.
    model_max_output_tokens = 0
    model_supported_parameters: list = []

    # -- reasoning ------------------------------------------------------
    # 'default' omits the field and lets the model decide, 'none' turns
    # reasoning off, the rest ask for progressively more of it.
    #
    # 'none' is the shipped default. A chain of thought does not help a
    # translation the way it helps a maths problem -- the model is
    # rewriting prose it has already read, not deriving anything -- and
    # measured on a real chapter it dominated the bill: a third of all
    # the tokens billed over one chapter, up to three quarters of them
    # on the glossary call, which only has to list proper nouns. It also
    # costs wall-clock time invisibly, because `exclude` keeps those
    # tokens out of the stream: nothing at all reaches the plugin while
    # the model deliberates. Raise it per engine in the OpenRouter
    # section if a particular model needs it.
    reasoning_efforts = [
        'default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh']
    reasoning_effort = 'none'
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
    # A book is a long sequence of large, strictly sequential requests,
    # so the endpoint's speed is felt end to end: the same model behind
    # the same gateway was measured at 200 tokens/s on one request and
    # 30 on another a few minutes later. 'throughput' asks OpenRouter for
    # the fastest endpoint rather than its balanced default.
    provider_sort = 'throughput'
    provider_allow_fallbacks = True
    # Novel Mode asks for JSON with `response_format` and for reasoning
    # to be off; a provider that supports neither is free to ignore both
    # unless this is set, which is how the model ends up echoing the
    # source text back and burning thinking tokens anyway. Turn it off
    # if OpenRouter answers that no provider is available.
    provider_require_parameters = True
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
        'model_max_output_tokens', 'model_supported_parameters',
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
        """https://openrouter.ai/docs/api-reference/list-available-models

        The listing carries far more than the ids. For every model it
        reports the context window, the longest reply its top provider
        will write (``top_provider.max_completion_tokens``) and the
        parameters that provider honours. Those are kept in
        ``model_details`` so the setting dialog can show them and the
        novel pipeline can size its requests against a real number
        instead of a guess: a model that writes at most 4096 tokens
        cannot answer a chunk budgeted at 16000.

        The figures describe the provider OpenRouter would route to by
        default. Provider routing can land on a different endpoint with
        a lower ceiling, so they are an upper bound, not a promise.
        """
        endpoint = self.endpoint or ''
        model_endpoint = re.sub(
            r'/chat/completions/?$', '/models', endpoint)
        if model_endpoint == endpoint:
            model_endpoint = '%s/models' % endpoint.rstrip('/')
        response = request(
            model_endpoint, headers=self.get_headers(),
            proxy_uri=self.proxy_uri)
        details = {}
        for item in json.loads(response).get('data') or []:
            model_id = item.get('id')
            if not model_id:
                continue
            top_provider = item.get('top_provider') or {}
            parameters = item.get('supported_parameters') or []
            details[model_id] = {
                'context_length': (
                    top_provider.get('context_length')
                    or item.get('context_length')),
                'max_output_tokens': top_provider.get(
                    'max_completion_tokens'),
                'structured_output': (
                    'structured_outputs' in parameters
                    or 'response_format' in parameters),
                'supported_parameters': list(parameters),
            }
        # Assigned, never mutated in place: the empty dict on GenAI is
        # shared by every engine that does not publish a listing.
        type(self).model_details = details
        return sorted(details)

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

    # Optional knobs, as opposed to the request itself (model, messages,
    # stream). Each is dropped when the chosen model does not list it
    # among the parameters it accepts. A fifth of the catalogue takes no
    # `temperature` at all -- the GPT-6 and GPT-5.6 families, the newest
    # Claude models -- and a parameter a model cannot take is at best
    # ignored and at worst, with `require_parameters` on, leaves the
    # request with no provider to route to.
    #
    # `response_format` is deliberately not in the list: whether to ask
    # for a JSON schema is decided once, by `structured_output_mode`
    # below, and a user who overrides that decision in the Novel Mode
    # settings means it.
    optional_parameters = (
        'temperature', 'top_p', 'top_k', 'min_p', 'top_a',
        'frequency_penalty', 'presence_penalty', 'repetition_penalty',
        'max_tokens', 'seed', 'logit_bias', 'stop', 'reasoning')

    # Parameters OpenRouter names in more than one way in its listing.
    parameter_aliases = {
        'max_tokens': ('max_tokens', 'max_completion_tokens'),
        'response_format': ('response_format', 'structured_outputs'),
    }

    def get_supported_parameters(self) -> list[str]:
        """The parameters the chosen model accepts, empty when unknown.

        Taken from the listing when it has been fetched in this session,
        otherwise from what the setting dialog persisted when the model
        was picked. Empty means nothing is filtered and every parameter
        is sent, which is what happened before this existed.
        """
        listed = self.get_model_limits(self.model).get('supported_parameters')
        return list(listed or self.model_supported_parameters or [])

    def accepts_parameter(self, name, supported=None) -> bool:
        supported = self.get_supported_parameters() \
            if supported is None else supported
        if not supported:
            return True
        return any(alias in supported
                   for alias in self.parameter_aliases.get(name, (name,)))

    def drop_unsupported(self, body: dict[str, Any]) -> dict[str, Any]:
        """Remove the optional fields the chosen model does not accept."""
        supported = self.get_supported_parameters()
        if not supported:
            return body
        for key in self.optional_parameters:
            if key in body and not self.accepts_parameter(key, supported):
                del body[key]
        return body

    @property
    def structured_output_mode(self):
        """Whether Novel Mode may ask this model for a JSON schema.

        Inherited from the ChatGPT engine as a plain class attribute, it
        described the gateway. Behind the gateway sit hundreds of models
        and some of them accept no `response_format` at all, so the
        answer depends on the model that is configured.
        """
        if not self.accepts_parameter('response_format'):
            return None
        return 'schema'

    def get_reasoning(self) -> dict[str, Any]:
        """Build the unified `reasoning` object.

        https://openrouter.ai/docs/use-cases/reasoning-tokens
        """
        reasoning: dict[str, Any] = {}
        # The unified API turns reasoning off with `enabled: false`; there
        # is no effort level named 'none' to send, and `exclude` alone
        # would only hide the tokens while still paying for them.
        if self.reasoning_effort == 'none':
            return {'enabled': False}
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
        # Filtered before the escape hatch, which is how a field the
        # listing does not mention can still be forced through.
        self.drop_unsupported(body)
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
