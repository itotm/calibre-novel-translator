from .base import Base
from .openrouter import OpenRouterTranslate
from .openai import ChatgptTranslate
from .anthropic import ClaudeTranslate
from .gemini import GeminiTranslate

# In the order the setting dialog lists them. OpenRouter first: it is
# the default, and the one the plugin is built around.
builtin_engines: tuple[type[Base], ...] = (
    OpenRouterTranslate, ChatgptTranslate, ClaudeTranslate, GeminiTranslate)
