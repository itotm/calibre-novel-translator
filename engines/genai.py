from abc import ABC, abstractmethod

from .base import Base


class GenAI(Base, ABC):
    """Each GenAI model should inherit this class to use specific methods."""

    prompt: str
    models: list[str]
    model: str | None
    samplings: list
    sampling: str
    temperature: float
    top_p: float
    top_k: int

    @abstractmethod
    def get_models(self) -> list[str]:
        """Automatically get the models for the engine."""

    # ------------------------------------------------------------------
    # Transient prompt override.
    # ------------------------------------------------------------------
    #
    # ``self.prompt`` is the system prompt as configured. The pipeline
    # instead wants to inject a *different* system prompt for each request
    # (containing the running summary and the dynamic glossary) without
    # permanently mutating the engine configuration.
    #
    # ``override_prompt`` saves the current prompt (once, so nested calls
    # remain safe) and swaps in the new one; ``restore_prompt`` puts the
    # original prompt back. Sub-classes that build the message payload from
    # ``self.prompt`` (ChatGPT, Claude, Gemini) do not need any further
    # change: they will naturally pick up the overridden value.

    structured_output_mode: str | None = None

    # Set by the novel pipeline when it wants the provider to keep the
    # prompt prefix in its cache: every chunk of a chapter is sent with
    # the same system prompt, a few thousand tokens of running summary
    # and glossary, and a prefix the provider already holds is billed at
    # a fraction of the price. Engines that cache on their own (OpenAI,
    # Gemini, DeepSeek) ignore this; the ones whose API needs an explicit
    # breakpoint read it in ``get_body``.
    prompt_cache: bool = False

    # What the provider says about each model it serves, keyed by model
    # id: the context window, the longest reply it will write and whether
    # it honours a JSON schema. Filled by ``get_models`` on engines whose
    # listing publishes the numbers, empty everywhere else.
    model_details: dict = {}

    # The longest reply the configured model will write, in tokens, as
    # the provider reported it when the model was chosen. Persisted in
    # the engine preferences so a translation can size its requests
    # against a real number without asking the provider again. 0 means
    # nobody ever said.
    model_max_output_tokens: int = 0

    # The request parameters the configured model accepts, as the
    # provider listed them when the model was chosen. Persisted next to
    # the reply limit, and empty when nobody ever said -- in which case
    # every parameter is sent, as it always was.
    model_supported_parameters: list = []

    @classmethod
    def get_model_limits(cls, model):
        """What the provider says ``model`` can do.

        Returns a dict with ``context_length``, ``max_output_tokens``,
        ``structured_output`` and ``supported_parameters`` -- any of them
        possibly None or empty when the provider is silent about it -- or
        an empty dict when the model is unknown, which is the case for
        every engine that does not publish a listing and for any engine
        whose listing was never fetched.
        """
        details = cls.model_details.get(model) if model else None
        return dict(details) if details else {}

    def get_body_for_structured(self, text, schema=None):
        """Return the request body with structured (JSON) output enabled.

        Default implementation is a graceful fallback: it returns the same
        body as :meth:`get_body`, so engines that do not override this
        method silently continue with unstructured output. The novel
        pipeline detects the lack of structured support via the
        ``structured_output_mode`` class attribute and routes such
        requests through the classical text-marker path instead.

        Subclasses that support structured output override this method to
        inject the provider-specific field (``response_format`` for
        OpenAI-compatible APIs, ``generationConfig.responseMimeType`` +
        ``responseSchema`` for Gemini, ...).

        :schema: an optional JSON Schema dict describing the expected
            response shape. Engines that support ``'schema'`` should
            enforce it; engines that only support ``'json'`` may ignore
            it and rely on prompt engineering to produce the right shape.
        """
        return self.get_body(text)

    def override_prompt(self, prompt_text):
        if not hasattr(self, '_prompt_stash'):
            self._prompt_stash = self.prompt
        self.prompt = prompt_text

    def restore_prompt(self):
        if hasattr(self, '_prompt_stash'):
            self.prompt = self._prompt_stash
            del self._prompt_stash
