import os
import os.path
from types import MethodType

from calibre import sanitize_file_name  # type: ignore
from calibre.gui2 import Dispatcher  # type: ignore
from calibre.constants import DEBUG, __version__  # type: ignore
from calibre.utils.localization import _  # type: ignore
from calibre.ebooks.conversion.plumber import (  # type: ignore
    Plumber, CompositeProgressReporter)
from calibre.ptempfile import PersistentTemporaryFile  # type: ignore
from calibre.ebooks.metadata.meta import (  # type: ignore
    get_metadata, set_metadata)

from .. import NovelTranslatorPlugin

from .config import get_config
from .utils import log, sep
from .cache import get_cache
from .element import (
    get_element_handler, get_toc_elements, get_page_elements,
    get_metadata_elements)
from .translation import get_translator
from .novel import (
    ChapterBuilder, ContextManager, NovelTranslator, novel_cache_id)


load_translations()  # type: ignore


def convert_book(
        input_path, output_path, translator, element_handler, cache,
        debug_info, encoding, notification,
        chapter_source='toc_level_1',
        novel_config=None,
        log_callback=None,
        progress_callback=None,
        cancel_request=None,
        chapter_started=None,
        chapter_done=None,
        cache_only=False) -> None:
    """Translate the book, or rebuild it from the cache, through Plumber.

    Translates the book sequentially, chapter by chapter, maintaining a
    running summary and glossary through a ``ContextManager`` persisted
    in the same SQLite cache. The metadata, the table of contents and the
    front matter are translated apart from the narrative, by the same
    translator.

    :translator: an already-configured engine instance (source_lang and
        target_lang must be set by the caller).
    :chapter_source: strategy passed to ``ChapterBuilder`` ('toc_level_1'
        or 'xhtml_file').
    :novel_config: dict of ``novel_*`` config keys read from
        ``lib.config.get_config``.
    :log_callback: callable(str, bool=False). Logging function reused by
        the UI (mirrors ``lib.translation.Translation.set_logging``).
    :progress_callback: callable(float 0..1, str message).
    :cancel_request: callable() -> bool. If True, the pipeline raises
        ``TranslationCanceled`` at the next safe point.
    :chapter_started / chapter_done: UI callbacks (see NovelTranslator).
    :cache_only: If True, do NOT translate anything. Just use existing
        translations from the SQLite cache to rebuild the output ebook.
        This is the mode used by the UI "Build translated ebook" button
        after the user has already run the pipeline interactively.
    """
    log_callback = log_callback or (lambda *a, **k: None)
    progress_callback = progress_callback or (lambda *a, **k: None)
    cancel_request = cancel_request or (lambda: False)

    plumber = Plumber(
        input_path, output_path, log=log, report_progress=notification)
    _convert = plumber.output_plugin.convert
    elements = []

    def convert(self, oeb, output_path, input_plugin, opts, log):
        backup_progress = self.report_progress.global_min
        self.report_progress = CompositeProgressReporter(0, 1, notification)
        log.info('Translating ebook content...')
        log.info(debug_info)

        # 1. Extract the elements: the DOM handler finds the paragraphs
        #    and, at the end, puts the translations back in.
        elements.extend(get_metadata_elements(oeb.metadata))
        elements.extend(get_toc_elements(oeb.toc.nodes, []))
        elements.extend(get_page_elements(oeb.manifest.items))
        original_group = element_handler.prepare_original(elements)
        # ``save`` is idempotent: it only writes when the cache is fresh.
        cache.save(original_group)

        paragraphs = cache.all_paragraphs()

        if cache_only:
            # The window already ran the pipeline and filled the cache;
            # the translations go back into the DOM and Plumber writes
            # the ebook. The model is not asked anything.
            log_callback(_(
                'Building from the cache: reusing {} cached paragraph(s).')
                .format(len(paragraphs)))
        else:
            # 2. Compute the ordered spine (xhtml pages only) so
            #    ChapterBuilder can walk the book in reading order. Same
            #    key as ``Extraction.get_sorted_pages``.
            import re as _re
            from .utils import sorted_mixed_keys
            page_pattern = _re.compile(r'\.(xhtml|html|htm|xml|xht)$')
            xhtml_items = [item for item in oeb.manifest.items
                           if page_pattern.search(item.href or '')]
            xhtml_items.sort(
                key=lambda item: sorted_mixed_keys(item.href or ''))
            ordered_page_ids = [item.id for item in xhtml_items]

            # 3. Build chapters.
            builder = ChapterBuilder(
                ordered_page_ids, oeb.toc.nodes,
                list(oeb.manifest.items), paragraphs,
                source=chapter_source,
                front_matter_min_chars=int(
                    (novel_config or {}).get(
                        'novel_front_matter_min_chars', 100) or 0))
            chapters = builder.build()
            log_callback(
                _('{} chapter(s) detected.')
                .format(len(chapters)))

            # 4. Load the context manager (summaries + glossary + progress).
            ctx = ContextManager(
                cache,
                glossary_max_entries=int(
                    (novel_config or {}).get(
                        'novel_glossary_max_entries', 500) or 0),
            ).load()

            # 5. Translate: first what sits outside the chapters (the
            #    metadata, the TOC titles, the front matter), then the
            #    chapters themselves.
            novel_translator = NovelTranslator(
                translator, chapters, ctx, cache,
                config=novel_config or {},
                aux_paragraphs=builder.auxiliary_paragraphs())
            novel_translator.set_logging(log_callback)
            novel_translator.set_progress(progress_callback)
            novel_translator.set_cancel_request(cancel_request)
            if chapter_started is not None:
                novel_translator.set_chapter_started(chapter_started)
            if chapter_done is not None:
                novel_translator.set_chapter_done(chapter_done)
            novel_translator.run()

        # 6. Reload paragraphs from cache (they were mutated during
        #    translation via update_paragraph) and reinject into the DOM.
        paragraphs = cache.all_paragraphs()
        element_handler.add_translations(paragraphs)

        log.info(sep())
        log.info(_('Start to convert ebook format...'))
        log.info(sep())

        self.report_progress = CompositeProgressReporter(
            backup_progress, 1, notification)
        self.report_progress(0., _('Outputting ebook file...'))
        _convert(oeb, output_path, input_plugin, opts, log)

    plumber.output_plugin.convert = MethodType(convert, plumber.output_plugin)
    plumber.run()


def get_novel_config():
    """Return a dict of all ``novel_*`` settings read from the plugin config.

    Single source of truth for the novel-mode configuration dict. Both
    the interactive UI worker (``NovelTranslationWorker`` in ``novel.py``)
    and the background job entry point (``convert_item_novel``) call this
    function so that any future additions to the config surface
    automatically in both code paths without risking drift.
    """
    config = get_config()
    return {
        'novel_chunk_tokens': config.get('novel_chunk_tokens', 16000),
        'novel_max_paragraphs_per_chunk': config.get(
            'novel_max_paragraphs_per_chunk', 50),
        'novel_overlap_paragraphs': config.get(
            'novel_overlap_paragraphs', 5),
        'novel_structured_output': config.get(
            'novel_structured_output', 'auto'),
        'novel_front_matter_min_chars': config.get(
            'novel_front_matter_min_chars', 100),
        'novel_context_tokens': config.get('novel_context_tokens', 4000),
        'novel_summary_tokens': config.get('novel_summary_tokens', 600),
        'novel_glossary_max_entries': config.get(
            'novel_glossary_max_entries', 500),
        'novel_glossary_relevant_only': config.get(
            'novel_glossary_relevant_only', True),
        'novel_glossary_prompt_max_entries': config.get(
            'novel_glossary_prompt_max_entries', 150),
        'novel_context_max_tokens': config.get(
            'novel_context_max_tokens', 4000),
        'novel_context_reasoning': config.get(
            'novel_context_reasoning', False),
        'novel_min_chars_for_context': config.get(
            'novel_min_chars_for_context', 300),
        'novel_reuse_translated_paragraphs': config.get(
            'novel_reuse_translated_paragraphs', True),
        'novel_verify_alignment': config.get(
            'novel_verify_alignment', True),
        'novel_log_reply_excerpt': config.get(
            'novel_log_reply_excerpt', 300),
        'novel_realign_shifted_replies': config.get(
            'novel_realign_shifted_replies', True),
        'novel_retry_split': config.get('novel_retry_split', True),
        'novel_provider_failures_before_exclusion': config.get(
            'novel_provider_failures_before_exclusion', 2),
        'novel_front_matter_titles': config.get(
            'novel_front_matter_titles', None),
        'novel_untranslated_titles': config.get(
            'novel_untranslated_titles', None),
        'novel_context_timing': config.get(
            'novel_context_timing', 'before'),
        'novel_parallel_chunks': config.get('novel_parallel_chunks', 1),
        'novel_balanced_chunks': config.get('novel_balanced_chunks', True),
        'novel_chunk_context': config.get(
            'novel_chunk_context', 'translated'),
        'novel_source_context_paragraphs': config.get(
            'novel_source_context_paragraphs', 5),
        'novel_on_missing_paragraphs': config.get(
            'novel_on_missing_paragraphs', 'stop'),
        'novel_rate_limit_max_wait': config.get(
            'novel_rate_limit_max_wait', 600),
        'novel_reply_max_tokens': config.get(
            'novel_reply_max_tokens', 16384),
        'novel_prompt_cache': config.get('novel_prompt_cache', True),
        'novel_output_aware_chunking': config.get(
            'novel_output_aware_chunking', True),
        'novel_summary_input_max_chars': config.get(
            'novel_summary_input_max_chars', 40000),
        'novel_summary_max_chars': config.get(
            'novel_summary_max_chars', 0),
        'novel_combined_context_call': config.get(
            'novel_combined_context_call', True),
        'novel_context_narrative_only': config.get(
            'novel_context_narrative_only', True),
        'novel_skip_context_last_chapter': config.get(
            'novel_skip_context_last_chapter', True),
        'novel_translation_prompt': config.get(
            'novel_translation_prompt', None),
        'novel_author_style': config.get('novel_author_style', 'model'),
        'novel_author_check': config.get('novel_author_check', True),
        'novel_dialogue_convention': config.get(
            'novel_dialogue_convention', 'auto'),
        'novel_dialogue_rules': config.get('novel_dialogue_rules', None),
    }


def convert_item(
        ebook_title, input_path, output_path, source_lang, target_lang,
        cache_only, format, encoding, direction, notification):
    """The background job: translate one book, or build it from the cache.

    Calibre's ``arbitrary_n`` job runner injects ``notification`` as the
    last positional argument of the callable, so it stays last.

    :cache_only: If True, do not ask the model anything. Reuse the cache
        the window filled and only rebuild the output ebook through
        Plumber. What the "Build translated ebook" button does.
    """
    translator = get_translator()
    translator.set_source_lang(source_lang)
    translator.set_target_lang(target_lang)

    element_handler = get_element_handler(
        translator.placeholder, translator.separator, direction)
    element_handler.set_translation_lang(
        translator.get_iso639_target_code(target_lang))

    _encoding = ''
    if encoding.lower() != 'utf-8':
        _encoding = encoding.lower()
    cache_id = novel_cache_id(
        input_path, translator.name, target_lang, _encoding)
    cache = get_cache(cache_id)
    cache.set_cache_only(cache_only)
    cache.set_info('title', ebook_title)
    cache.set_info('engine_name', translator.name)
    cache.set_info('target_lang', target_lang)
    cache.set_info('plugin_version', NovelTranslatorPlugin.__version__)
    cache.set_info('calibre_version', __version__)
    cache.set_info('novel_mode', '1')

    debug_info = '{0}\n| Diagnosis Information\n{0}'.format(sep())
    debug_info += '\n| Calibre Version: %s\n' % __version__
    debug_info += '| Plugin Version: %s\n' % NovelTranslatorPlugin.__version__
    debug_info += '| Translation Engine: %s\n' % translator.name
    debug_info += '| Source Language: %s\n' % source_lang
    debug_info += '| Target Language: %s\n' % target_lang
    debug_info += '| Encoding: %s\n' % encoding
    debug_info += '| Cache Enabled: %s\n' % cache.is_persistence()
    debug_info += '| Cache-only: %s\n' % ('yes' if cache_only else 'no')
    debug_info += '| Input Path: %s\n' % input_path
    debug_info += '| Output Path: %s' % output_path

    novel_config = get_novel_config()
    chapter_source = get_config().get(
        'novel_chapter_source', 'toc_level_1') or 'toc_level_1'

    convert_book(
        input_path, output_path, translator, element_handler, cache,
        debug_info, encoding, notification,
        chapter_source=chapter_source,
        novel_config=novel_config,
        log_callback=lambda text, error=False: log.info(text),
        progress_callback=notification,
        cache_only=cache_only,
    )
    cache.done()


class ConversionWorker:
    def __init__(self, gui, icon):
        self.gui = gui
        self.icon = icon
        self.config = get_config()
        self.db = gui.current_db
        self.api = self.db.new_api
        self.working_jobs = self.gui.novel_translator.jobs

    def translate_ebook(self, ebook, cache_only=True):
        """Launch the background job.

        Used with ``cache_only=True`` to rebuild the output ebook after
        the window has filled the cache.
        """
        input_path = ebook.get_input_path()
        if not self.config.get('to_library'):
            filename = sanitize_file_name(ebook.title[:200])
            output_path = self.config.get('output_path')
            if output_path is None or not os.path.isdir(output_path):
                raise Exception(
                    _('Please set a valid output path.'))
            output_path = os.path.join(
                output_path, f'{filename}.{ebook.output_format}')
        else:
            output_path = PersistentTemporaryFile(
                suffix='.' + ebook.output_format).name
        job = self.gui.job_manager.run_job(
            Dispatcher(self.translate_done),
            'arbitrary_n',
            args=(
                'calibre_plugins.novel_translator.lib.conversion',
                'convert_item',
                (ebook.title, input_path, output_path, ebook.source_lang,
                 ebook.target_lang, cache_only, ebook.input_format,
                 ebook.encoding, ebook.target_direction)),
            description=(_('[{} > {}] Building "{}"').format(
                ebook.source_lang, ebook.target_lang, ebook.title)))
        self.working_jobs[job] = (ebook, output_path)

    def translate_done(self, job):
        ebook, output_path = self.working_jobs.pop(job)

        if job.failed:
            if not DEBUG:
                self.gui.job_exception(
                    job, dialog_title=_('Translation job failed'))
            return

        # TODO: Try to use the calibre generated metadata file.
        ebook_metadata_config = self.config.get('ebook_metadata') or {}
        with open(output_path, 'r+b') as file:
            metadata = get_metadata(file, ebook.output_format)
            ebook_title = metadata.title
            if ebook.custom_title is not None:
                ebook_title = ebook.custom_title
            if ebook_metadata_config.get('lang_mark'):
                ebook_title = '%s [%s]' % (ebook_title, ebook.target_lang)
            metadata.title = ebook_title
            if ebook_metadata_config.get('lang_code'):
                metadata.language = ebook.lang_code
            # Only the subjects the user asked for: the book carries no
            # mention of the plugin that translated it.
            subjects = ebook_metadata_config.get('subjects')
            metadata.tags += subjects or []
            set_metadata(file, metadata, ebook.output_format)

        if self.config.get('to_library'):
            book_id = self.db.create_book_entry(metadata)
            self.api.add_format(
                book_id, ebook.output_format, output_path, run_hooks=False)
            self.gui.library_view.model().books_added(1)
            output_path = self.api.format_abspath(book_id, ebook.output_format)
        else:
            dirname = os.path.dirname(output_path)
            filename = sanitize_file_name(ebook_title[:200])
            new_output_path = os.path.join(
                dirname, '%s.%s' % (filename, ebook.output_format))
            os.rename(output_path, new_output_path)
            output_path = new_output_path

        self.gui.status_bar.show_message(
            job.description + ' ' + _('completed'), 5000)

        def callback(payload):
            kwargs = {'args': ['ebook-viewer', output_path]}
            payload('ebook-viewer', kwargs=kwargs)

        if self.config.get('show_notification', True):
            self.gui.proceed_question(
                callback,
                self.gui.job_manager.launch_gui_app,
                job.log_path,
                _('Ebook Translation Log'), _('Translation Completed'),
                _('The translation of "{}" was completed. Do you want to '
                  'open the book?').format(ebook_title),
                log_is_file=True, icon=self.icon, auto_hide_after=10)
