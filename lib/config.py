import os
import os.path
import shutil
from typing import Any

from calibre.constants import config_dir  # type: ignore
from calibre.utils.config_base import plugin_dir  # type: ignore
from calibre.utils.config import JSONConfig  # type: ignore

from .. import EbookTranslator
from ..engines import (
    GoogleFreeTranslateNew, ChatgptTranslate, AzureChatgptTranslate)


defaults: dict[str, Any] = {
    'preferred_mode': None,
    'to_library': True,
    'output_path': None,
    'translate_engine': None,
    'engine_preferences': {},
    'proxy_enabled': False,
    'proxy_type': 'http',
    'proxy_setting': {},
    'cache_enabled': True,
    'cache_path': None,
    'log_translation': True,
    'show_notification': True,
    'translation_position': None,
    'column_gap': {
        '_type': 'percentage',
        'percentage': 10,
        'space_count': 6,
    },
    'original_color': None,
    'translation_color': None,
    'priority_rules': [],
    'rule_mode': 'normal',
    'filter_scope': 'text',
    'filter_rules': [],
    'ignore_rules': [],
    'reserve_rules': [],
    'custom_engines': {},
    'glossary_enabled': False,
    'glossary_path': None,
    'merge_enabled': False,
    'merge_length': 1800,
    'ebook_metadata': {},
    'search_paths': [],
    'novel_mode_enabled': False,
    # Show the sampling and penalty parameters of the OpenRouter section.
    # Off by default: specialist knobs, each already at the neutral value
    # that leaves it out of the request. Visibility only -- a value set
    # and then hidden is still sent.
    'openrouter_advanced_parameters': False,
    # Sized for the context windows current models actually have,
    # but capped by what a model can *write* rather than read: the
    # reply is about as long as the chunk, and output limits are far
    # lower than context windows (8k-32k tokens on most models). A
    # hundred paragraphs is a comfortable answer for any of them.
    'novel_chunk_tokens': 16000,
    'novel_max_paragraphs_per_chunk': 100,
    # Structured output policy for the novel translator.
    #   'auto'  -> use JSON structured output when the engine advertises
    #              support (see engines.genai.GenAI.structured_output_mode),
    #              otherwise fall back to text markers [N]. Default.
    #   'off'   -> always use text markers, even on capable engines.
    #   'force' -> always use JSON structured output, even on engines that
    #              don't advertise it
    'novel_structured_output': 'auto',
    'novel_overlap_paragraphs': 3,
    'novel_context_tokens': 4000,
    'novel_summary_tokens': 600,
    'novel_glossary_max_entries': 500,
    # Prompts carry only the glossary entries the chapter at hand
    # mentions, capped to this many. A glossary that keeps growing
    # otherwise ends up costing input tokens on every request and, in
    # the extraction call, invites the model to copy the list of names
    # it was told to skip back as new entries.
    'novel_glossary_relevant_only': True,
    'novel_glossary_prompt_max_entries': 150,
    # Hard cap on what the summary and glossary calls may write. Both
    # answers are short by nature, but a model that starts repeating
    # itself only stops at its own output limit: one glossary call was
    # measured writing 131072 tokens over eight minutes. 0 disables it.
    'novel_context_max_tokens': 4000,
    # Length above which a chapter summary is truncated before being
    # stored. A summary is re-read in every later chapter's prompt, so a
    # model that answers with the whole chapter instead of 150-350 words
    # fills the context budget for the rest of the book. 0 derives the
    # limit from novel_summary_tokens (twice the target size).
    'novel_summary_max_chars': 0,
    # Ask for the summary and the glossary in a single request. Both
    # read the chapter that was just translated, so two calls send it
    # twice: measured on a real book, the summary call carried 7000 to
    # 8000 tokens of chapter text the glossary call was about to send
    # again. A summary or glossary prompt typed by the user turns this
    # off by itself, so that prompt is not silently ignored.
    'novel_combined_context_call': True,
    'novel_context_prompt': None,
    # The last chapter's summary and glossary are read by nobody: the
    # context of a chapter exists for the chapters that follow it.
    'novel_skip_context_last_chapter': True,
    # Whether the summary and glossary calls may spend reasoning tokens.
    # Off by default: neither task is a reasoning task, and on a measured
    # chapter the glossary call spent three quarters of its output on
    # deliberation. Only ever turns an engine's reasoning down, never on.
    'novel_context_reasoning': False,
    # Chapters with fewer translated characters than this threshold are
    # translated normally but skip the summary + glossary extraction
    # LLM calls. Typical target: front/back matter (Copyright, Table of
    # Contents, About the Author, ...) which is not narrative content.
    'novel_min_chars_for_context': 300,
    # Keep the paragraphs the cache already holds instead of translating
    # them again. Progress only advances at the end of a chapter while
    # translations are stored after every chunk, so a run cancelled at
    # chunk 7 of 9 would otherwise pay for those seven chunks twice.
    'novel_reuse_translated_paragraphs': True,
    # Cap the chunk budget with the longest reply the chosen model can
    # write, as its provider reports it (OpenRouter publishes the figure
    # for every model it proxies). Reading room and writing room have
    # nothing to do with each other -- context windows run to hundreds of
    # thousands of tokens, reply limits start at 4096 -- and a chunk the
    # model cannot finish is paid for twice.
    'novel_output_aware_chunking': True,
    # Ask the engine to keep the prompt prefix in its cache. Every chunk
    # of a chapter carries the same system prompt -- role, languages,
    # running summary and glossary -- and a prefix the provider already
    # holds is billed at a fraction of the price. Only engines with
    # explicit cache breakpoints (Claude) read this; the ones that cache
    # on their own are unaffected either way.
    'novel_prompt_cache': True,
    'novel_chapter_source': 'toc_level_1',  # 'toc_level_1' | 'toc_level_2' | 'xhtml_file'
    # Pages whose total non-ignored text (in chars) is below this threshold
    # are treated as front/back matter (Cover, Titlepage, decorative pages)
    # and excluded from chapter narrative content. Set to 0 to disable.
    'novel_front_matter_min_chars': 100,
    'novel_translation_prompt': None,
    'novel_summary_prompt': None,
    'novel_glossary_prompt': None,
}


class Configuration:
    def __init__(self, config={}):
        self.preferences = config

    def get(self, key, default=None):
        """Get config value with dot flavor. e.g. get('a.b.c')"""
        if key is None:
            return default
        temp = self.preferences
        for key in key.split('.'):
            if isinstance(temp, dict) and key in temp:
                temp = temp.get(key)
                continue
            temp = defaults.get(key)
        return default if temp is None else temp

    def set(self, key, value):
        """Set config value with dot flavor. e.g. set('a.b.c', '1')"""
        temp = self.preferences
        keys = key.split('.')
        while len(keys) > 0:
            key = keys.pop(0)
            if len(keys) > 0:
                if key in temp and isinstance(temp.get(key), dict):
                    temp = temp[key]
                    continue
                temp[key] = {}
                temp = temp.get(key)
                continue
        temp[key] = value

    def update(self, *args, **kwargs):
        self.preferences.update(*args, **kwargs)

    def delete(self, key):
        if key in self.preferences:
            del self.preferences[key]
            return True
        return False

    def refresh(self):
        self.preferences.refresh()

    def commit(self):
        self.preferences.commit()

    def save(self, *args, **kwargs):
        self.update(*args, **kwargs)
        self.commit()


def get_config():
    preferences = JSONConfig('plugins/ebook_translator_novel')
    preferences.defaults = defaults
    return Configuration(preferences)


def upgrade_config():
    config = get_config()
    version = EbookTranslator.version
    if version >= (2, 0, 0):  # type: ignore
        ver200_upgrade(config)
    if version >= (2, 0, 3):  # type: ignore
        ver203_upgrade(config)
    if version >= (2, 0, 5):  # type: ignore
        ver205_upgrade(config)
    if version >= (2, 4, 0):  # type: ignore
        ver240_upgrade()


def ver200_upgrade(config):
    """Upgrade the configuration for version 2.0.0 or earlier."""
    if config.get('engine_preferences'):
        return

    engine_preferences = {}

    def get_engine_preference(engine_name):
        if engine_name not in engine_preferences:
            engine_preferences.update({engine_name: {}})
        return engine_preferences.get(engine_name)

    chatgpt_prompt = config.get('chatgpt_prompt')
    if chatgpt_prompt is not None:
        if len(chatgpt_prompt) > 0:
            preference = get_engine_preference(ChatgptTranslate.name)
            prompts = config.get('chatgpt_prompt')
            if preference is not None and 'lang' in chatgpt_prompt:
                preference.update(prompt=prompts.get('lang'))
        config.delete('chatgpt_prompt')

    languages = config.get('preferred_language')
    if languages is not None:
        for engine_name, language in languages.items():
            preference = get_engine_preference(engine_name)
            if preference is not None:
                preference.update(target_lang=language)
        config.delete('preferred_language')

    api_keys = config.get('api_key')
    if api_keys is not None:
        for engine_name, api_key in api_keys.items():
            preference = get_engine_preference(engine_name)
            if preference is not None:
                preference.update(api_keys=[api_key])
        config.delete('api_key')

    if len(engine_preferences) > 0:
        config.update(engine_preferences=engine_preferences)
        config.commit()


def ver203_upgrade(config):
    """Upgrade the configuration for version 2.0.3 or earlier."""
    engine_config = config.get('engine_preferences')
    azure_chatgpt = engine_config.get('ChatGPT(Azure)')
    if azure_chatgpt and 'model' in azure_chatgpt:
        model = azure_chatgpt.get('model')
        if model not in AzureChatgptTranslate.models:
            del azure_chatgpt['model']

    if len(engine_config) < 1:
        engine_config.update({GoogleFreeTranslateNew.name: {}})

    old_concurrency_limit = config.get('concurrency_limit')
    old_request_attempt = config.get('request_attempt')
    old_request_interval = config.get('request_interval')
    old_request_timeout = config.get('request_timeout')

    for data in engine_config.values():
        if old_concurrency_limit is not None and old_concurrency_limit != 1:
            data.update(concurrency_limit=old_concurrency_limit)
        if old_request_attempt is not None and old_request_attempt != 3:
            data.update(request_attempt=old_request_attempt)
        if old_request_interval is not None and old_request_interval != 5:
            data.update(request_interval=old_request_interval)
        if old_request_timeout is not None and old_request_timeout != 10:
            data.update(request_timeout=old_request_timeout)

    config.delete('concurrency_limit')
    config.delete('request_attempt')
    config.delete('request_interval')
    config.delete('request_timeout')

    config.commit()


def ver205_upgrade(config):
    """Upgrade the configuration for version 2.0.5 or earlier."""
    if config.get('translate_engine') in ('GeminiPro', 'GeminiFlash'):
        config.update(translate_engine='Gemini')
    preferences = config.get('engine_preferences')
    if 'GeminiPro' in preferences.keys():
        preferences['Gemini'] = preferences.pop('GeminiPro')
    if 'GeminiFlash' in preferences.keys():
        preferences['Gemini'] = preferences.pop('GeminiFlash')
        preferences['Gemini'].update(model='gemini-1.5-flash')
    config.commit()


def ver240_upgrade():
    """Move the pre-2.4.0 configuration to the current location.

    Only the upstream plugin ever wrote to the legacy directory, and
    the official plugin may well be installed next to this fork, so a
    fork must leave that directory to its owner instead of renaming
    it out from under the plugin that created it.
    """
    if EbookTranslator.identifier != 'ebook-translator':
        return
    old_config_path = os.path.join(config_dir, EbookTranslator.author)
    new_config_path = os.path.join(plugin_dir, EbookTranslator.identifier)
    if os.path.exists(new_config_path) and os.path.exists(old_config_path):
        shutil.rmtree(old_config_path)
    if os.path.exists(old_config_path):
        os.rename(old_config_path, new_config_path)
        os.rename(
            os.path.join(new_config_path, EbookTranslator.identifier + '.ini'),
            os.path.join(new_config_path, 'settings.ini'))
