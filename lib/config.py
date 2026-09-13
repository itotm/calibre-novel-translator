from typing import Any

from calibre.utils.config import JSONConfig  # type: ignore


defaults: dict[str, Any] = {
    # Whether the button has been put on calibre's toolbars once. The
    # plugin does that on its first run; after that the toolbars are the
    # user's to arrange.
    'toolbar_placed': False,
    'to_library': True,
    'output_path': None,
    'translate_engine': 'OpenRouter',
    'engine_preferences': {},
    'proxy_enabled': False,
    'proxy_type': 'http',
    'proxy_setting': {},
    'cache_enabled': True,
    'cache_path': None,
    'log_translation': True,
    'show_notification': True,
    # Where the translation sits relative to the original in the output:
    # 'below', 'above', 'right', 'left', or 'only' for a translated book
    # with no original left in it, which is what a novel wants.
    'translation_position': 'only',
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
    # What the output book's metadata gets. 'lang_code' stamps the
    # target language on the book, so calibre and readers treat it as
    # what it is: a book in that language.
    'ebook_metadata': {'lang_code': True},
    # Show the sampling and penalty parameters of the OpenRouter section.
    # Off by default: specialist knobs, each already at the neutral value
    # that leaves it out of the request. Visibility only -- a value set
    # and then hidden is still sent.
    'openrouter_advanced_parameters': False,
    # Sized for the context windows current models actually have,
    # but capped by what a model can *write* rather than read: the
    # reply is about as long as the chunk, and output limits are far
    # lower than context windows (8k-32k tokens on most models).
    # Seventy-five paragraphs is a comfortable answer for any of them.
    'novel_chunk_tokens': 16000,
    'novel_max_paragraphs_per_chunk': 50,
    # Structured output policy for the novel translator.
    #   'auto'  -> use JSON structured output when the engine advertises
    #              support (see engines.genai.GenAI.structured_output_mode),
    #              otherwise fall back to text markers [N]. Default.
    #   'off'   -> always use text markers, even on capable engines.
    #   'force' -> always use JSON structured output, even on engines that
    #              don't advertise it
    'novel_structured_output': 'auto',
    'novel_overlap_paragraphs': 5,
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
    # Characters of chapter text handed to the summary and glossary
    # calls, for the source and for the translation each. Their prompts
    # open with the head of the chapter, where the characters and the
    # setting are introduced; a whole chapter would cost input tokens
    # the answer does not need. At 60000 a chapter's call was measured
    # at 30000 tokens of input, every chapter; 40000 keeps the whole of
    # most chapters and a third off the rest.
    'novel_summary_input_max_chars': 40000,
    # Ask for the summary and the glossary in a single request. Both
    # read the chapter that was just translated, so two calls send it
    # twice: measured on a real book, the summary call carried 7000 to
    # 8000 tokens of chapter text the glossary call was about to send
    # again. A summary or glossary prompt typed by the user turns this
    # off by itself, so that prompt is not silently ignored.
    'novel_combined_context_call': True,
    # Keep the summary and the glossary only for chapters that belong
    # to the story. The model that summarises a chapter says whether it
    # is one; a copyright page, a list of the author's other books, a
    # preface or a note would otherwise be carried into every later
    # prompt as if it were plot.
    'novel_context_narrative_only': True,
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
    # Check the translations of a reply against their paragraphs before
    # keeping them -- an ellipsis in place of text, placeholders unlike
    # the source, the same text under two numbers, dialogue where the
    # source has none, a length out of proportion -- and ask again for
    # the ones that fail. The number a model puts on a translation is
    # the only thing that pairs it with its paragraph, and a model that
    # skips one paragraph and numbers on from there files everything
    # after it under the wrong number.
    'novel_verify_alignment': True,
    # How many characters of a reply that covered fewer paragraphs than
    # asked go into the log, so what the model did instead can be seen.
    # 0 logs the count only.
    'novel_log_reply_excerpt': 300,
    # A reply whose numbers slipped -- the model skipped a paragraph and
    # numbered on -- is read as it was meant instead of asked for again,
    # when the translations make sense one or more places further on and
    # nowhere else. Logged as a warning either way: it means the model or
    # the provider loses count at this chunk size.
    'novel_realign_shifted_replies': True,
    # A retry asks for the missing paragraphs in two halves rather than
    # all at once: what the model could not manage at one size it seldom
    # manages again at the same size.
    'novel_retry_split': True,
    # How many unreliable replies (shifted numbers, less than half of
    # what was asked) a provider may give before the engine is told not
    # to route to it for the rest of the run. 0 never excludes anyone.
    # Only engines that route between providers (OpenRouter) act on it,
    # and only for the run: the setting on disk is not touched.
    'novel_provider_failures_before_exclusion': 2,
    # Words a chapter title carries when the chapter is not part of the
    # story: it is translated, but no summary or glossary is asked for
    # it. Comma-separated, matched as whole words, case-insensitive.
    # None means the shipped list (see lib.novel.FRONT_MATTER_TITLES).
    'novel_front_matter_titles': None,
    # Words a chapter title carries when the chapter stays in its
    # original language, such as the list of the author's other books.
    # None means the shipped list (see lib.novel.UNTRANSLATED_TITLES).
    'novel_untranslated_titles': None,
    # When the summary and the glossary of a chapter are asked for:
    #   'before' -> from the source, before the chapter is translated:
    #               half the tokens, and the glossary already guides
    #               every chunk of the chapter itself. Default.
    #   'after'  -> from source and translation together, once the
    #               chapter is done.
    'novel_context_timing': 'before',
    # How many chunks of one chapter may be in flight at once. 1 is the
    # sequential pipeline. More sends the chunks of a chapter together,
    # each on its own copy of the engine, with the source text around
    # each chunk as context (the translated overlap needs the previous
    # chunk to be done). Chapters stay sequential whatever the value.
    'novel_parallel_chunks': 1,
    # Chunks in flight together are cut to about the same size: the
    # chapter takes as long as its longest chunk, and 3637 + 1945 + 674
    # tokens waits for the first while 2085 + 2085 + 2086 is done in
    # two thirds of the time. Sequential chunks are filled to the cap
    # instead, which is the fewest requests.
    'novel_balanced_chunks': True,
    # What a chunk is shown of its surroundings: 'translated', the last
    # paragraphs of the previous chunk as the model rendered them (the
    # overlap above; sequential only), or 'source', the source text of
    # the paragraphs before and after it, which also shows what comes
    # next. Forced to 'source' when chunks are in flight together.
    'novel_chunk_context': 'translated',
    # Source paragraphs shown before and after a chunk with 'source'.
    'novel_source_context_paragraphs': 5,
    # What to do with paragraphs the model never returned, after the
    # retries inside a chunk and one more pass in smaller chunks.
    #   'stop'     -> end the run with the chapter unfinished; a resume
    #                 asks for exactly those paragraphs again. Default.
    #   'continue' -> log them and go on to the next chapter; they keep
    #                 their source text and nothing comes back for them.
    'novel_on_missing_paragraphs': 'stop',
    # How long a rate-limited request may be waited out, in seconds,
    # before it counts as a failure. A 429 says "not now", not
    # "never": the attempts used to be spent in a second on a provider
    # that asked for one second of patience. 0 treats it as an error.
    'novel_rate_limit_max_wait': 600,
    # The most a translation request may ask the model to write, in
    # tokens, when the engine leaves the figure to the provider (0 on
    # OpenRouter means "not sent"). Left out, many providers apply 4096
    # and a chunk comes back cut at a third. Each chunk asks for twice
    # its own size plus room for the JSON, up to this. 0 sends nothing.
    'novel_reply_max_tokens': 16384,
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
    # Research once per book, before the first chapter, how its author
    # writes, and repeat the answer in the prompt of every chapter. A
    # chapter is translated by requests that know the story so far but
    # nothing about the manner it was told in, and the result drifts
    # towards neutral prose.
    #   'auto'  -> search the web on engines that can (OpenRouter, Claude,
    #              Gemini), fall back to what the model knows on the rest.
    #   'model' -> never search, ask the model only. Default: a search
    #              is billed per result and a model knows the published
    #              authors well enough.
    #   'off'   -> do not ask at all.
    'novel_author_style': 'model',
    # How direct speech is punctuated. 'auto' reads it off the source,
    # chapter by chapter, and states the answer in every request; a key
    # of lib.novel.DIALOGUE_CONVENTIONS prescribes that convention;
    # 'off' leaves the choice to the model, which is how the same book
    # came back with guillemets in one chapter and quotation marks in
    # the next. A rule typed in novel_dialogue_rules replaces either.
    'novel_dialogue_convention': 'auto',
    'novel_dialogue_rules': None,
    'novel_translation_prompt': None,
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
    preferences = JSONConfig('plugins/novel_translator')
    preferences.defaults = defaults
    return Configuration(preferences)
