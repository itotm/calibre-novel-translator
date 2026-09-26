import re
import json
from typing import Any

from calibre.utils.localization import _  # type: ignore

from .. import NovelTranslatorPlugin
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
    # One provider, no presets: the gateway is the whole point. The
    # endpoint is not a setting either -- another server that speaks the
    # same API is a provider of the OpenAI-compatible engine.
    providers: dict = {}
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

    # Translation is a high-certainty task where diversity is noise:
    # measured over six temperatures, quality falls as it rises -- 4.3
    # COMET points from 0 to 1 on English to Chinese (arXiv:2303.13780).
    # A low value also keeps a model on the requested JSON shape where
    # the provider does not enforce it, and keeps names and terminology
    # steady over a book. Raise it in the Fine-tuning section if the
    # prose comes out flat.
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
    # A book is hundreds of large requests, so the endpoint's price is
    # felt end to end; 'price' asks OpenRouter for the cheapest endpoint
    # serving the model rather than its balanced default. 'throughput'
    # is the one to pick when the run is too slow: the same model behind
    # the same gateway was measured at 200 tokens/s on one endpoint and
    # 30 on another.
    provider_sort = 'price'
    provider_allow_fallbacks = True
    # The pipeline asks for JSON with `response_format` and for reasoning
    # to be off; a provider that supports neither is free to ignore both
    # unless this is set, which is how the model ends up echoing the
    # source text back and burning thinking tokens anyway. Turn it off
    # if OpenRouter answers that no provider is available.
    provider_require_parameters = True
    provider_data_collections = ['allow', 'deny']
    provider_data_collection = 'allow'
    provider_zdr = False

    # -- service tier ----------------------------------------------------
    # 'default' omits the field and the request is served at the
    # standard price. 'flex' is a discounted tier that trades latency and
    # availability for price, and never falls back to the standard tier:
    # with no flex capacity the request fails. 'priority' costs more for
    # faster, steadier service, and does fall back, billed at the tier
    # that served it. Only some providers offer them (OpenAI, Google,
    # Anthropic for priority); the reply says which tier served it. Left at the default because a tier is a choice about
    # money and time that only the user can make, and it is the one
    # setting worth changing per translation, not per engine.
    # https://openrouter.ai/docs/guides/features/service-tiers
    service_tiers = ['default', 'flex', 'priority']
    service_tier = 'default'
    # Whether a new translation starts on flex when its model has a flex
    # endpoint (see model_has_flex), whatever the tier above says. A book
    # is hundreds of requests with nobody waiting on each, which is what
    # flex is priced for; a model without flex keeps the tier above, or
    # the default when that is flex, since flex would fail every request.
    flex_when_available = True
    # A flex request may wait in the provider's queue before its first
    # token: OpenAI asks for a timeout of fifteen minutes on that tier.
    flex_request_timeout = 900.0

    # -- usage accounting ------------------------------------------------
    # Ask OpenRouter to say, with every reply, what it cost: the tokens
    # as the provider counted them and the money. It is one more event
    # at the end of the stream and nothing on the bill; it is what the
    # report of a run adds up.
    # https://openrouter.ai/docs/use-cases/usage-accounting
    usage_accounting = True

    # -- attribution and escape hatches ---------------------------------
    app_referer = NovelTranslatorPlugin.homepage
    app_title = NovelTranslatorPlugin.name
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
        'provider_data_collection', 'provider_zdr', 'service_tier',
        'flex_when_available', 'usage_accounting', 'app_referer',
        'app_title', 'extra_headers', 'extra_body')

    def __init__(self):
        super().__init__()
        self.endpoint = type(self).endpoint
        for key in self.preference_keys:
            setattr(self, key, self.config.get(key, getattr(self, key)))
        # The timeout the settings ask for, which flex raises and any
        # other tier gives back.
        self.base_request_timeout = self.request_timeout
        self.set_service_tier(self.service_tier)

    def set_service_tier(self, tier):
        """Ask for ``tier`` ('default', 'flex' or 'priority').

        A translation can name its own tier, set after the engine is
        built; this is where the timeout follows it, so a flex request
        waiting in the queue is not given up on as a dead connection.
        """
        tier = tier if tier in self.service_tiers else 'default'
        self.service_tier = tier
        base = float(getattr(self, 'base_request_timeout', None)
                     or self.request_timeout or 0)
        self.request_timeout = max(base, self.flex_request_timeout) \
            if tier == 'flex' else base

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
        response = request(
            self.get_model_endpoint(), headers=self.get_headers(),
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

    @staticmethod
    def header_value(value) -> str:
        """An HTTP header value the client can send.

        ``http.client`` encodes header values as latin-1 and raises on
        anything outside it, so a title typed in Japanese or Chinese
        used to fail every request. Such characters are replaced.
        """
        return str(value).encode('latin-1', 'replace').decode('latin-1')

    def get_headers(self):
        headers = super().get_headers()
        # Optional attribution headers used by openrouter.ai/rankings.
        if self.app_referer:
            headers['HTTP-Referer'] = self.header_value(self.app_referer)
        if self.app_title:
            headers['X-Title'] = self.header_value(self.app_title)
        for name, value in parse_object(self.extra_headers).items():
            headers[self.header_value(name)] = self.header_value(value)
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
    # below, and a user who overrides that decision in the settings
    # means it.
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
        """Whether the pipeline may ask this model for a JSON schema.

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

    def relax_parameters(self) -> bool:
        """Stop insisting that the provider honour every parameter.

        With ``require_parameters`` on, a provider list narrowed down to
        one that does not take some parameter -- a JSON schema, a
        reasoning field -- leaves OpenRouter with nothing to route to
        and a 404 that says so. The pipeline calls this once when that
        happens and asks again. Returns False when there was nothing to
        relax.
        """
        if not self.provider_require_parameters:
            return False
        self.provider_require_parameters = False
        return True

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
        if self.service_tier and self.service_tier != 'default':
            body['service_tier'] = self.service_tier
        if self.usage_accounting:
            body.setdefault('usage', {'include': True})
        # Filtered before the escape hatch, which is how a field the
        # listing does not mention can still be forced through.
        self.drop_unsupported(body)
        # Applied last so it can override anything computed above.
        body.update(parse_object(self.extra_body))
        return body

    def exclude_provider(self, name) -> bool:
        """Keep OpenRouter from routing to ``name`` for the life of this
        engine instance: the pipeline calls it when a provider has given
        too many unreliable replies. The setting on disk is not touched
        -- a provider bad with one model today is not bad with every
        model always.

        Routing takes the provider's slug, the reply names it by its
        display name; the model's endpoint listing maps one to the
        other, and the name itself is the fallback. Returns False when
        the provider was already excluded.

        A provider the settings pin the request to (``provider_only``)
        is taken off that list when others remain on it. When it is the
        only one, the exclusion is refused with a ValueError: ignoring
        the one provider allowed left OpenRouter nothing to route to,
        and every request of the rest of the run failed with "All
        providers have been ignored".
        """
        slug = self.provider_slug(name)
        only = parse_list(self.provider_only)
        if only:
            remaining = [
                entry for entry in only
                if not self._same_provider(entry, slug, name)]
            if not remaining:
                raise ValueError(_(
                    'it is the only provider the settings allow '
                    '(provider_only), so the run keeps using it'))
            self.provider_only = ', '.join(remaining)
        current = parse_list(self.provider_ignore)
        if any(self._same_provider(entry, slug, name) for entry in current):
            return False
        self.provider_ignore = ', '.join(current + [slug])
        return True

    @staticmethod
    def _same_provider(entry, slug, name) -> bool:
        """Whether a routing list ``entry`` -- a slug, possibly with a
        quantization suffix ("deepinfra/fp8") -- names the provider
        with this ``slug`` or display ``name``."""
        head = str(entry).split('/')[0].strip().casefold()
        return head in (str(slug).casefold(), str(name).strip().casefold())

    # Display name -> routing slug, per model, so that the copies of
    # this engine reading a chapter's chunks together do not each ask
    # the listing for the same answer, one after the other, under the
    # lock that holds the other chunks up.
    _provider_slugs: dict[tuple, str] = {}

    def provider_slug(self, name) -> str:
        """The routing slug of the provider called ``name`` in replies,
        from the endpoint listing of the configured model; the name
        lower-cased when the listing does not say."""
        wanted = str(name or '').strip().casefold()
        key = (self.model, wanted)
        cached = self._provider_slugs.get(key)
        if cached:
            return cached
        slug = ''
        try:
            response = request(
                'https://openrouter.ai/api/v1/models/%s/endpoints'
                % self.model, headers=self.get_headers(),
                proxy_uri=self.proxy_uri)
            endpoints = (json.loads(response).get('data') or {}).get(
                'endpoints') or []
            for endpoint in endpoints:
                if str(endpoint.get('provider_name', '')).casefold() \
                        == wanted:
                    slug = str(endpoint.get('tag') or '').split('/')[0]
                    if slug:
                        break
        except Exception:
            pass
        slug = slug or str(name or '').strip().lower().replace(' ', '-')
        self._provider_slugs[key] = slug
        return slug

    # Model -> its endpoints, as (tag, provider name), for the life of
    # calibre: the dialog asks every time the model is chosen, and the
    # routing settings of the moment are applied to them on each answer.
    _model_endpoints: dict[str, list] = {}

    def model_has_flex(self, model) -> bool | None:
        """Whether ``model`` has an endpoint that serves the flex tier and
        that the routing settings let a request reach; None when the
        listing could not be read.

        The model listing does not say; the endpoint listing of the
        model does, in the tag of each endpoint ("openai/flex",
        "google-vertex/global/flex"), whose first part is the routing
        slug of the provider. A flex endpoint of a provider that
        ``provider_only`` leaves out or ``provider_ignore`` names is no
        use: routed away from it, every flex request fails.
        """
        model = str(model or '').strip()
        if not model:
            return None
        endpoints = self._model_endpoints.get(model)
        if endpoints is None:
            try:
                response = request(
                    'https://openrouter.ai/api/v1/models/%s/endpoints'
                    % model, headers=self.get_headers(),
                    proxy_uri=self.proxy_uri)
                listed = (json.loads(response).get('data') or {}).get(
                    'endpoints') or []
            except Exception:
                return None
            endpoints = [
                (str(item.get('tag') or ''),
                 str(item.get('provider_name') or ''))
                for item in listed if isinstance(item, dict)]
            self._model_endpoints[model] = endpoints
        only = parse_list(self.provider_only)
        ignore = parse_list(self.provider_ignore)
        for tag, name in endpoints:
            if tag.rsplit('/', 1)[-1] != 'flex':
                continue
            slug = tag.split('/')[0]
            if only and not any(
                    self._same_provider(entry, slug, name)
                    for entry in only):
                continue
            if any(self._same_provider(entry, slug, name)
                   for entry in ignore):
                continue
            return True
        return False

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
