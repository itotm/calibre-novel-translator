from ..engines import builtin_engines, OpenRouterTranslate
from ..engines.base import Base

from .config import get_config


def get_engine_class(engine_name=None) -> type[Base]:
    """The engine class the settings name, configured with its preferences.

    OpenRouter stands in for a name that matches nothing: it is the
    default engine, and the one a fresh configuration starts with.
    """
    config = get_config()
    engine_name = engine_name or config.get('translate_engine')
    engines: dict[str, type[Base]] = {
        engine.name: engine for engine in builtin_engines
        if engine.name is not None}
    engine_class = engines.get(engine_name, OpenRouterTranslate)
    engine_preferences = config.get('engine_preferences') or {}
    engine_class.set_config(engine_preferences.get(engine_class.name) or {})
    return engine_class


def get_translator(engine_class=None) -> Base:
    config = get_config()
    engine_class = engine_class or get_engine_class()
    translator = engine_class()
    if config.get('proxy_enabled'):
        proxy_type: str | None = config.get('proxy_type')
        proxy_setting: dict[str, list] | None = config.get('proxy_setting')
        if proxy_type is not None and proxy_setting is not None:
            # Compatible with old proxy settings stored as a list.
            if isinstance(proxy_setting, list):
                proxy_setting = {'http': proxy_setting}
            host, port = proxy_setting.get(proxy_type) or ['', '']
            translator.set_proxy(proxy_type, host, port)
    return translator
