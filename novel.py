"""The translation window.

It runs the chapter-aware sequential pipeline defined in ``lib/novel.py``
on a single ebook. The model operates on whole chapters, so the window
shows chapter-level progress plus tabs for the running summaries, the
dynamic glossary, the author brief and the log.

"""
import time
import traceback
from html import escape
from types import MethodType

from qt.core import (  # type: ignore
    Qt, QObject, QDialog, QGroupBox, QWidget, QVBoxLayout, QHBoxLayout,
    QPlainTextEdit, QPushButton, QSplitter, QLabel, QThread, QGridLayout,
    QTextBrowser,
    QProgressBar, pyqtSignal, pyqtSlot, QPixmap, QListWidget,
    QListWidgetItem, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QSpacerItem, QStackedWidget, QComboBox, QMessageBox,
    QSizePolicy, QColor, QBrush, QAbstractItemView, QCompleter, QTimer)
from calibre.constants import __version__  # type: ignore
from calibre.gui2 import I, error_dialog  # type: ignore
from calibre.utils.localization import _  # type: ignore
from calibre.ebooks.conversion.plumber import (  # type: ignore
    Plumber, CompositeProgressReporter)
from calibre.ptempfile import PersistentTemporaryFile  # type: ignore

from . import NovelTranslatorPlugin
from .lib.utils import log, sep, uid, traceback_error
from .lib.config import get_config
from .lib.cache import get_cache
from .lib.element import (
    get_element_handler, get_page_elements, get_toc_elements,
    get_metadata_elements)
from .lib.translation import get_engine_class, get_translator
from .lib.exception import TranslationCanceled, TranslationFailed
from .lib.novel import (
    Chapter, ChapterBuilder, ContextManager, NovelTranslator,
    INFO_NOVEL_CHAPTERS, INFO_NOVEL_LOG, INFO_NOVEL_STYLE,
    INFO_NOVEL_STYLE_NOTE, INFO_NOVEL_REPORT)
from .lib.conversion import get_novel_config
from .lib.book_translations import (
    INFO_MODEL, INFO_PROVIDER, INFO_SERVICE_TIER, configure_translator,
    edited_texts, engine_class_for, engine_provider,
    create_translation, delete_translation, edited_paragraphs,
    find_translations, forget_edits, model_limits, comparison_problem,
    stamp_book)
from .engines.genai import GenAI
from .components import (
    Footer, AlertMessage, SourceLang, TargetLang, InputFormat, OutputFormat,
    ModelWorker, FlexWorker)
from .text_view import EnterFilter, TranslationText


load_translations()  # type: ignore


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


class NovelPreparationWorker(QObject):
    """Extract chapters, load context, and populate the cache.

    Runs entirely off the Qt main thread. Emits ``finished(cache_id)`` when
    ready to translate, or ``failed(str)`` on any error.
    """

    start = pyqtSignal()
    progress_message = pyqtSignal(str)
    progress_detail = pyqtSignal(str)
    finished = pyqtSignal(str, object)   # cache_id, list[dict chapters meta]
    failed = pyqtSignal(str)

    def __init__(self, engine_class, ebook, cache_id, library_id=None):
        """
        :cache_id: the translation to prepare, created by the dialog
            that lists the translations of the book (see
            ``lib.book_translations``).
        """
        QObject.__init__(self)
        self.engine_class = engine_class
        self.ebook = ebook
        self.cache_id = cache_id
        self.library_id = library_id
        self.canceled = False
        self.start.connect(self.run)

    def set_canceled(self, value):
        self.canceled = value

    @pyqtSlot()
    def run(self):
        try:
            self._do_run()
        except Exception as e:
            log.error('Novel prep failed: %s' % traceback.format_exc())
            self.failed.emit(str(e))

    def _do_run(self):
        input_path = self.ebook.get_input_path()
        cache_id = self.cache_id

        cache = get_cache(cache_id)
        # Again on every opening: calibre moves the files of a book whose
        # title or author changed, and a cache written before 1.3 learns
        # here which book it belongs to.
        stamp_book(cache, self.ebook, self.library_id)
        if not cache.get_info('engine_name'):
            cache.set_info('engine_name', self.engine_class.name)
        if not cache.get_info('target_lang'):
            cache.set_info('target_lang', self.ebook.target_lang)
        if not cache.get_info('source_lang'):
            cache.set_info('source_lang', self.ebook.source_lang or '')
        cache.set_info('plugin_version', NovelTranslatorPlugin.__version__)
        cache.set_info('calibre_version', __version__)

        chapters_meta = []
        # A cache without its chapter list is one a previous preparation
        # left half-written (the paragraphs are saved, then the chapters
        # are worked out); reading it as "no chapters" locked the book
        # out until the cache was deleted by hand.
        if cache.is_fresh() or not cache.is_persistence() \
                or not cache.get_info(INFO_NOVEL_CHAPTERS):
            self.progress_message.emit(_('Extracting ebook content...'))
            # Convert through Plumber and keep the element handler around
            # so its prepare_original can fill the cache.
            element_handler = get_element_handler(
                self.engine_class.placeholder, self.engine_class.separator,
                self.ebook.target_direction)
            element_handler.set_translation_lang(
                self.engine_class.get_iso639_target_code(
                    self.ebook.target_lang))
            output_path = PersistentTemporaryFile(suffix='.epub').name
            oeb_holder = {}
            plumber = Plumber(input_path, output_path, log=log)

            def convert(pself, oeb, output_path, input_plugin, opts, plog):
                oeb_holder['oeb'] = oeb
                elements = []
                elements.extend(get_metadata_elements(oeb.metadata))
                elements.extend(get_toc_elements(oeb.toc.nodes, []))
                elements.extend(get_page_elements(oeb.manifest.items))
                original_group = element_handler.prepare_original(elements)
                cache.save(original_group)
                # Trigger the abort so plumber does not proceed to output.
                from .lib.exception import ConversionAbort
                raise ConversionAbort()

            plumber.output_plugin.convert = MethodType(
                convert, plumber.output_plugin)
            try:
                plumber.run()
            except Exception as e:
                from .lib.exception import ConversionAbort
                if not isinstance(e, ConversionAbort):
                    raise

            oeb = oeb_holder.get('oeb')
            if oeb is None:
                raise RuntimeError(_(
                    'Failed to load ebook: OEB not available.'))

            self.progress_message.emit(_('Building chapters...'))
            paragraphs = cache.all_paragraphs()
            import re as _re
            from .lib.utils import sorted_mixed_keys
            page_pat = _re.compile(r'\.(xhtml|html|htm|xml|xht)$')
            xhtml_items = [it for it in oeb.manifest.items
                           if page_pat.search(it.href or '')]
            xhtml_items.sort(key=lambda it: sorted_mixed_keys(it.href or ''))
            page_ids = [it.id for it in xhtml_items]
            source = get_config().get(
                'novel_chapter_source', 'toc_level_1') or 'toc_level_1'
            front_matter_min = int(
                get_config().get('novel_front_matter_min_chars', 100) or 0)
            builder = ChapterBuilder(
                page_ids, oeb.toc.nodes,
                list(oeb.manifest.items), paragraphs,
                source=source,
                front_matter_min_chars=front_matter_min)
            chapters = builder.build()
            for ch in chapters:
                chapters_meta.append({
                    'index': ch.index,
                    'title': ch.title,
                    'char_count': ch.char_count,
                    'paragraphs': len(ch.paragraphs),
                    # Which pages and which cached paragraphs the chapter
                    # is made of. With these the translation worker
                    # rebuilds the chapters straight from the cache
                    # instead of converting the whole ebook a second
                    # time. See NovelTranslationWorker._chapters_from_meta.
                    'page_ids': list(ch.page_ids),
                    'paragraph_ids': [p.id for p in ch.paragraphs],
                })
            # Persist chapter metadata so subsequent openings can reuse it
            # without re-running Plumber.
            import json as _json
            cache.set_info(INFO_NOVEL_CHAPTERS, _json.dumps(chapters_meta))
        else:
            self.progress_detail.emit(_(
                'Loading data from cache and preparing user interface...'))
            import json as _json
            raw = cache.get_info(INFO_NOVEL_CHAPTERS)
            try:
                chapters_meta = _json.loads(raw) if raw else []
            except (ValueError, TypeError):
                chapters_meta = []

        cache.close()
        self.finished.emit(cache_id, chapters_meta)


class NovelTranslationWorker(QObject):
    """Run the sequential translation pipeline off the Qt main thread."""

    start = pyqtSignal()
    logging = pyqtSignal(str, bool)
    progress = pyqtSignal(float, str)
    chapter_started = pyqtSignal(int)          # chapter index
    chapter_done = pyqtSignal(int, str, list)  # index, summary, glossary
    report = pyqtSignal(str)                   # the report, rewritten
    finished = pyqtSignal(bool, str)           # success, message

    def __init__(self, engine_class, ebook, cache_id, retranslate=False):
        """
        :retranslate: send every paragraph to the model again instead of
            keeping the translations the cache holds. What "Re-run all"
            asks for once a book is done.
        """
        QObject.__init__(self)
        self.engine_class = engine_class
        self.ebook = ebook
        self.cache_id = cache_id
        self.retranslate = retranslate
        self.canceled = False
        self.start.connect(self.run)

    def cancel_request(self):
        return self.canceled

    def set_canceled(self, value):
        self.canceled = value
        translator = getattr(self, 'translator', None)
        if value and translator is not None:
            # Do not wait for the model: cut the request short.
            translator.abort()

    @pyqtSlot()
    def run(self):
        try:
            self._do_run()
        except TranslationCanceled:
            self.finished.emit(False, _('Translation canceled.'))
        except Exception as e:
            log.error('Novel translation failed: %s'
                      % traceback.format_exc())
            self.logging.emit(traceback.format_exc(), True)
            self.finished.emit(False, str(e))
        else:
            self.finished.emit(True, _('Translation completed.'))

    def _chapters_from_meta(self, cache, chapters_meta):
        """Rebuild the chapters from what the preparation worker stored.

        Preparation already converted the whole ebook to find the chapter
        boundaries, and wrote down which pages and which cached
        paragraphs every chapter ended up with. Rebuilding from that is
        one query; the alternative, running Plumber again, is a second
        full conversion of the book on every Start or Resume.

        Returns None when the metadata predates those two fields, so the
        caller falls back to reading the ebook.
        """
        if not chapters_meta:
            return None
        if any('paragraph_ids' not in meta for meta in chapters_meta):
            return None
        by_id = {p.id: p for p in cache.all_paragraphs()}
        chapters = []
        placed = set()
        for meta in chapters_meta:
            chapters.append(Chapter(
                index=meta['index'],
                title=meta.get('title') or '',
                page_ids=meta.get('page_ids') or [],
                paragraphs=[by_id[pid] for pid in meta['paragraph_ids']
                            if pid in by_id]))
            placed.update(meta['paragraph_ids'])
        # Whatever no chapter claimed -- metadata, table of contents,
        # the pages the front-matter filter set aside -- is translated
        # apart from the narrative.
        aux = [p for pid, p in by_id.items() if pid not in placed]
        return chapters, aux

    def _chapters_from_ebook(self, cache):
        """Rebuild the chapters by converting the ebook again.

        The fallback for caches whose metadata predates
        :meth:`_chapters_from_meta`. It repeats exactly what the
        preparation worker does, Plumber run included.
        """
        input_path = self.ebook.get_input_path()
        plumber = Plumber(input_path, PersistentTemporaryFile(
            suffix='.epub').name, log=log)
        oeb_holder = {}

        def convert(pself, oeb, output_path, input_plugin, opts, plog):
            oeb_holder['oeb'] = oeb
            from .lib.exception import ConversionAbort
            raise ConversionAbort()

        from .lib.exception import ConversionAbort
        plumber.output_plugin.convert = MethodType(
            convert, plumber.output_plugin)
        try:
            plumber.run()
        except ConversionAbort:
            pass
        oeb = oeb_holder.get('oeb')
        paragraphs = cache.all_paragraphs()

        import re as _re
        from .lib.utils import sorted_mixed_keys
        page_pat = _re.compile(r'\.(xhtml|html|htm|xml|xht)$')
        xhtml_items = [it for it in oeb.manifest.items
                       if page_pat.search(it.href or '')]
        xhtml_items.sort(key=lambda it: sorted_mixed_keys(it.href or ''))
        page_ids = [it.id for it in xhtml_items]
        source = get_config().get(
            'novel_chapter_source', 'toc_level_1') or 'toc_level_1'
        front_matter_min = int(
            get_config().get('novel_front_matter_min_chars', 100) or 0)
        builder = ChapterBuilder(
            page_ids, oeb.toc.nodes, list(oeb.manifest.items),
            paragraphs, source=source,
            front_matter_min_chars=front_matter_min)
        chapters = builder.build()
        return chapters, builder.auxiliary_paragraphs()

    def _do_run(self):
        cache = get_cache(self.cache_id)
        translator = get_translator(self.engine_class)
        translator.set_source_lang(self.ebook.source_lang)
        translator.set_target_lang(self.ebook.target_lang)
        # The model and the tier are the translation's, not the
        # settings': a book can be translated by several models.
        info = cache.all_info()
        configure_translator(translator, info)
        model = getattr(translator, 'model', None)
        # Written before 1.3, which did not record them: from now on the
        # list of translations can say what this one runs on, and it
        # keeps running on it whatever the settings say.
        if model and not info.get(INFO_MODEL):
            cache.set_info(INFO_MODEL, model)
        tier = getattr(translator, 'service_tier', None)
        if tier and not info.get(INFO_SERVICE_TIER):
            cache.set_info(INFO_SERVICE_TIER, tier)
        provider = engine_provider(self.engine_class)
        if provider and not info.get(INFO_PROVIDER):
            cache.set_info(INFO_PROVIDER, provider)
        # The corrections a new translation of every paragraph replaces
        # are forgotten when it is over, and only those it did replace:
        # a run that stops half way leaves the others marked.
        corrected = edited_texts(cache) if self.retranslate else None
        self.translator = translator

        # Rebuild the chapters the preparation worker had already built.
        import json as _json
        raw = cache.get_info(INFO_NOVEL_CHAPTERS)
        try:
            chapters_meta = _json.loads(raw) if raw else []
        except (ValueError, TypeError):
            chapters_meta = []

        rebuilt = self._chapters_from_meta(cache, chapters_meta)
        if rebuilt is None:
            self.logging.emit(_(
                'Chapter metadata was written by an older version: '
                'reading the ebook again to rebuild the chapters.'), False)
            rebuilt = self._chapters_from_ebook(cache)
            # Written down the way preparation writes it now, so the
            # next start does not convert the book again and the Text
            # tab can tell the chapters apart.
            cache.set_info(INFO_NOVEL_CHAPTERS, _json.dumps([{
                'index': ch.index,
                'title': ch.title,
                'char_count': ch.char_count,
                'paragraphs': len(ch.paragraphs),
                'page_ids': list(ch.page_ids),
                'paragraph_ids': [p.id for p in ch.paragraphs],
            } for ch in rebuilt[0]]))
        chapters, aux_paragraphs = rebuilt

        ctx = ContextManager(
            cache,
            glossary_max_entries=int(
                get_config().get('novel_glossary_max_entries', 500) or 0),
        ).load()

        novel_config = get_novel_config()
        # Not part of the settings: these two describe the book being
        # translated, not the way the pipeline works.
        novel_config.update({
            'novel_book_title': (
                self.ebook.custom_title or self.ebook.title or ''),
            'novel_book_author': self.ebook.get_author(),
        })
        if self.retranslate:
            novel_config['novel_reuse_translated_paragraphs'] = False

        translator_novel = NovelTranslator(
            translator, chapters, ctx, cache, config=novel_config,
            aux_paragraphs=aux_paragraphs)
        # Every line goes to the window and to the cache -- after every
        # chapter and at the end -- so the log of a run survives the
        # window being closed, and most of it survives calibre being
        # killed: the run that froze the window took its log with it,
        # and the cache held the log of the run before.
        lines = [cache.get_info(INFO_NOVEL_LOG) or '', '=' * 38,
                 time.strftime('%Y-%m-%d %H:%M:%S')]

        def logging(text, error=False):
            lines.append(('[ERROR] ' if error else '') + text)
            self.logging.emit(text, error)

        def store_log():
            # The last 300 KB: enough for the run that matters, not a
            # transcript of every attempt ever made on the book.
            cache.set_info(INFO_NOVEL_LOG, '\n'.join(
                line for line in lines if line)[-300000:])

        def chapter_done(chapter, summary, delta):
            self.chapter_done.emit(chapter.index, summary, delta or [])
            store_log()
        logging(_('Engine: {engine}, model: {model}{tier}.').format(
            engine=translator.name, model=model or _('(none)'),
            tier=_(', service tier: {}').format(tier)
            if tier and tier != 'default' else ''))
        translator_novel.set_logging(logging)
        translator_novel.set_progress(
            lambda frac, msg: self.progress.emit(frac, msg))
        translator_novel.set_cancel_request(self.cancel_request)
        translator_novel.set_chapter_started(
            lambda chapter: self.chapter_started.emit(chapter.index))
        translator_novel.set_chapter_done(chapter_done)
        translator_novel.set_report(lambda text: self.report.emit(text))

        try:
            translator_novel.run()
        finally:
            store_log()
            if corrected:
                forget_edits(cache, corrected)
            cache.close()


# ---------------------------------------------------------------------------
# BookTranslations: the translations of a book, and a new one
# ---------------------------------------------------------------------------


def apply_translation(ebook, translation, source_lang=None):
    """Set ``ebook`` up as ``translation`` was made: the input format,
    the languages, the encoding. Returns a message saying why it cannot
    be opened, or None.

    :source_lang: for a translation written before 1.3, which did not
        record it; the engine's setting, or auto-detection, otherwise.
    """
    fmt = translation.input_format
    if fmt not in ebook.files:
        return _(
            'This translation was made from the {} file of the book, '
            'which is no longer in the library.').format(
                (fmt or '?').upper())
    engine_class = get_engine_class(translation.engine_name)
    ebook.set_input_format(fmt)
    ebook.set_source_lang(
        translation.source_lang or source_lang
        or engine_class.config.get('source_lang') or _('Auto detect'))
    if translation.encoding:
        ebook.set_encoding(translation.encoding)
    ebook.set_target_lang(translation.target_lang)
    try:
        ebook.set_lang_code(
            engine_class.get_iso639_target_code(translation.target_lang))
    except Exception:
        ebook.set_lang_code(None)
    return None


class BookTranslations(QDialog):
    """The translations of one book: open one to continue it, read it or
    correct it; delete one; or start another, with a model of its own.

    A book can be translated several times -- by another model, into
    another language, at another service tier -- and each translation
    is a cache of its own. Everything else about a new translation is
    what the settings say: the model is the one thing worth choosing
    per translation, and the tier goes with it.
    """

    open_translation = pyqtSignal(object, str)   # ebook, cache id
    compare_translations = pyqtSignal(list)      # cache ids

    COLUMNS = ('model', 'tier', 'engine', 'language', 'progress', 'edited',
               'modified')

    def __init__(self, parent, ebook, library_id=None, is_open=None):
        """
        :is_open: callable(cache_id) -> bool, whether a window runs that
            translation now: it cannot be deleted while it does.
        """
        QDialog.__init__(self, parent)
        self.ebook = ebook
        self.library_id = library_id
        self.is_open = is_open or (lambda cache_id: False)
        self.alert = AlertMessage(self)
        self.engine_class = get_engine_class()
        self.configured_model = self._configured_model()
        self.translations = []

        self.model_thread = QThread()
        self.model_worker = ModelWorker()
        self.model_worker.moveToThread(self.model_thread)
        self.model_thread.finished.connect(self.model_worker.deleteLater)
        self.model_thread.start()
        self.model_worker.finished.connect(self._fill_models)
        self.model_worker.success.connect(self._models_fetched)
        # Whether the model chosen has flex, asked on the same thread a
        # moment after the choice settles: the field changes with every
        # key typed into it.
        self.flex_worker = FlexWorker()
        self.flex_worker.moveToThread(self.model_thread)
        self.model_thread.finished.connect(self.flex_worker.deleteLater)
        self.flex_worker.checked.connect(self._flex_checked)
        self.flex_timer = QTimer(self)
        self.flex_timer.setSingleShot(True)
        self.flex_timer.setInterval(400)
        self.flex_timer.timeout.connect(self._check_flex)
        # Once the user picks a tier, the model no longer picks it.
        self.tier_touched = False

        layout = QVBoxLayout(self)
        layout.addWidget(self._layout_list(), 1)
        layout.addWidget(self._layout_new())
        self.refresh()
        self._fill_models()
        if not self.engine_class.models and self._has_api_key():
            self._fetch_models()

    def _configured_model(self):
        try:
            return getattr(get_translator(self.engine_class), 'model', '') \
                or ''
        except Exception:
            return self.engine_class.config.get('model') \
                or getattr(self.engine_class, 'model', '') or ''

    def _has_api_key(self):
        needs = getattr(self.engine_class, 'needs_api_key', None)
        needs_key = needs() if callable(needs) \
            else getattr(self.engine_class, 'need_api_key', True)
        return bool(self.engine_class.config.get('api_keys')) \
            or not needs_key

    # -- the list ----------------------------------------------------------

    def _layout_list(self):
        group = QGroupBox(_('Translations of this book'))
        layout = QVBoxLayout(group)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([
            _('Model'), _('Service tier'), _('Engine'), _('Language'),
            _('Chapters'), _('Corrected'), _('Last change')])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        # Several rows at once: two or more are compared.
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(lambda index: self._open_selected())
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)

        self.empty_label = QLabel(_(
            'This book has not been translated yet: start the first '
            'translation below.'))
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)

        buttons = QHBoxLayout()
        self.delete_button = QPushButton(_('Delete'))
        self.delete_button.setToolTip(_(
            'Delete this translation from the cache: its text, summaries, '
            'glossary, log and report. Books already built are not '
            'touched.'))
        self.delete_button.clicked.connect(self._delete_selected)
        self.open_button = QPushButton(_('&Open'))
        self.open_button.setToolTip(_(
            'Open the translation to continue it, read and correct its '
            'text, or build the book from it.'))
        self.open_button.clicked.connect(self._open_selected)
        self.compare_button = QPushButton(_('&Compare'))
        self.compare_button.clicked.connect(self._compare_selected)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        buttons.addWidget(self.compare_button)
        buttons.addWidget(self.open_button)
        layout.addLayout(buttons)
        return group

    def refresh(self):
        self.translations = find_translations(self.ebook, self.library_id)
        self.table.setRowCount(len(self.translations))
        for row, translation in enumerate(self.translations):
            values = (
                translation.model_label(), translation.tier_label(),
                translation.engine_label(), translation.target_lang,
                translation.progress_label(),
                str(translation.edited) if translation.edited else '',
                translation.modified)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setToolTip(_(
                        'Created {created} from the {fmt} file.').format(
                            created=translation.created or '?',
                            fmt=(translation.input_format or '?').upper()))
                self.table.setItem(row, column, item)
        self.table.setVisible(bool(self.translations))
        self.empty_label.setVisible(not self.translations)
        if self.translations:
            self.table.selectRow(0)
        self._selection_changed()

    def _selected_all(self):
        rows = self.table.selectionModel().selectedRows() \
            if self.table.selectionModel() else []
        return [self.translations[row.row()] for row in sorted(
            rows, key=lambda row: row.row())
            if 0 <= row.row() < len(self.translations)]

    def _selected(self):
        """The translation selected, when it is only one."""
        selected = self._selected_all()
        return selected[0] if len(selected) == 1 else None

    def _selection_changed(self):
        selected = self._selected() is not None
        self.open_button.setEnabled(selected)
        self.delete_button.setEnabled(selected)
        self.open_button.setDefault(selected)
        several = self._selected_all()
        problem = comparison_problem(self._compare_infos(several))
        self.compare_button.setEnabled(problem is None)
        self.compare_button.setToolTip(problem or _(
            'Show the translations selected side by side with the '
            'original, to read, correct and copy paragraphs between '
            'them.'))

    @staticmethod
    def _compare_infos(translations):
        """The info tables, with the file each translation was made from
        filled in: one written before 1.3 does not record it, and here,
        with the book at hand, it is known."""
        return [dict(item.info, input_format=item.input_format)
                for item in translations]

    def _compare_selected(self):
        several = self._selected_all()
        if comparison_problem(self._compare_infos(several)):
            return
        self.done(0)
        self.compare_translations.emit([item.cache_id for item in several])

    def _open_selected(self):
        translation = self._selected()
        if translation is None:
            return
        error = apply_translation(
            self.ebook, translation, self.source_lang.currentText())
        if error:
            self.alert.pop(error, 'warning')
            return
        self.done(0)
        self.open_translation.emit(self.ebook, translation.cache_id)

    def _delete_selected(self):
        translation = self._selected()
        if translation is None:
            return
        if self.is_open(translation.cache_id):
            self.alert.pop(_(
                'This translation is open in a window: close it first.'),
                'warning')
            return
        action = self.alert.ask(_(
            'Delete the translation by {model} into {lang}? Its text, '
            'summaries, glossary, log and report are removed from the '
            'cache.').format(
                model=translation.model_label(),
                lang=translation.target_lang))
        if action != 'yes':
            return
        delete_translation(translation.cache_id)
        self.refresh()

    # -- a new translation -------------------------------------------------

    def _layout_new(self):
        group = QGroupBox(_('New translation'))
        layout = QGridLayout(group)
        engine_class = self.engine_class

        input_format = InputFormat(self.ebook.files.keys())
        output_format = OutputFormat()
        source_lang = SourceLang()
        target_lang = TargetLang()
        self.source_lang = source_lang
        source_lang.refresh.emit(
            engine_class.lang_codes.get('source'),
            engine_class.config.get('source_lang'), True)
        target_lang.refresh.emit(
            engine_class.lang_codes.get('target'),
            engine_class.config.get('target_lang'))

        layout.addWidget(QLabel(_('Input Format')), 0, 0)
        layout.addWidget(input_format, 0, 1)
        layout.addWidget(QLabel(_('Output Format')), 0, 2)
        layout.addWidget(output_format, 0, 3)
        layout.addWidget(QLabel(_('Source Language')), 1, 0)
        layout.addWidget(source_lang, 1, 1)
        layout.addWidget(QLabel(_('Target Language')), 1, 2)
        layout.addWidget(target_lang, 1, 3)

        # The model: the listing the settings fetched, searchable, and
        # open to a model it does not carry.
        self.model_box = QComboBox()
        self.model_box.setEditable(True)
        self.model_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_box.setMinimumWidth(320)
        self.model_box.wheelEvent = lambda event: None
        # Enter in the field does not press Open, the default button:
        # the dialog would open the translation selected above.
        self.model_box.lineEdit().installEventFilter(
            EnterFilter(None, self))
        completer = QCompleter(self.model_box.model(), self.model_box)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.model_box.setCompleter(completer)
        self.model_box.setToolTip(_(
            'The model that translates this book, summaries, glossary and '
            'author brief included. Everything else comes from the '
            'settings of the engine, as they stand when the translation '
            'runs. The model chosen there is preselected; type to search '
            'the listing, or type a model it does not carry.'))
        self.model_box.currentTextChanged.connect(self._model_changed)
        self.model_fetch = QPushButton(_('Fetch models'))
        self.model_fetch.clicked.connect(self._fetch_models)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_box, 1)
        model_row.addWidget(self.model_fetch)
        layout.addWidget(QLabel(_('Model')), 2, 0)
        layout.addLayout(model_row, 2, 1, 1, 3)

        self.model_limits = QLabel()
        self.model_limits.setStyleSheet('color:gray;')
        layout.addWidget(self.model_limits, 3, 1, 1, 3)

        # The tier, where the engine has one.
        self.tier_box = QComboBox()
        self.tier_box.wheelEvent = lambda event: None
        tiers = getattr(engine_class, 'service_tiers', None) or []
        for tier in tiers:
            self.tier_box.addItem(
                _('Default') if tier == 'default' else tier, tier)
        if tiers:
            self._set_tier(self._settings_tier())
        self.tier_box.activated.connect(self._tier_chosen)
        self.tier_box.setToolTip(_(
            'The price and speed the requests are served at. flex is '
            'discounted and slower, and only some providers offer it '
            '(OpenAI, Google): with no flex capacity a request fails '
            'rather than fall back to the standard tier. priority costs '
            'more. The reply says which tier served it, and the log and '
            'the report say it too. Preselected from the engine '
            'settings, and flex when the model has it (unless the '
            'settings say otherwise).'))
        tier_label = QLabel(_('Service tier'))
        layout.addWidget(tier_label, 4, 0)
        layout.addWidget(self.tier_box, 4, 1)
        self.flex_note = QLabel()
        self.flex_note.setStyleSheet('color:gray;')
        layout.addWidget(self.flex_note, 4, 2, 1, 2)
        tier_label.setVisible(bool(tiers))
        self.tier_box.setVisible(bool(tiers))
        self.flex_note.setVisible(bool(tiers))

        engine_label = QLabel(_(
            'Engine: {} (the one chosen in the settings)').format(
                engine_class.alias or engine_class.name))
        engine_label.setStyleSheet('color:gray;')
        layout.addWidget(engine_label, 5, 0, 1, 3)
        self.start_button = QPushButton(_('&Start new translation'))
        self.start_button.clicked.connect(self._start_new)
        layout.addWidget(self.start_button, 5, 3)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)

        def change_input_format(fmt):
            self.ebook.set_input_format(fmt)
        change_input_format(input_format.currentText())
        input_format.currentTextChanged.connect(change_input_format)

        def change_output_format(fmt):
            self.ebook.set_output_format(fmt)
        change_output_format(output_format.currentText())
        output_format.currentTextChanged.connect(change_output_format)

        def change_source_lang(lang):
            self.ebook.set_source_lang(lang)
        change_source_lang(source_lang.currentText())
        source_lang.currentTextChanged.connect(change_source_lang)

        def change_target_lang(lang):
            self.ebook.set_target_lang(lang)
            self.ebook.set_lang_code(
                engine_class.get_iso639_target_code(lang))
        change_target_lang(target_lang.currentText())
        target_lang.currentTextChanged.connect(change_target_lang)

        return group

    def _fill_models(self):
        current = self.model_box.currentText().strip() \
            or self.configured_model
        models = list(self.engine_class.models or [])
        self.model_box.blockSignals(True)
        self.model_box.clear()
        self.model_box.addItems(models)
        if current and current not in models:
            self.model_box.insertItem(0, current)
        self.model_box.setCurrentText(current)
        self.model_box.blockSignals(False)
        self.model_box.setEnabled(True)
        self.model_fetch.setEnabled(True)
        self.model_fetch.setText(_('Fetch models'))
        self.model_fetch.setVisible(not models)
        self._model_changed(current)

    def _fetch_models(self):
        if not self._has_api_key():
            self.alert.pop(_('You need to provide an API key to proceed.'))
            return
        self.model_fetch.setEnabled(False)
        self.model_fetch.setText(_('Fetching...'))
        self.model_worker.start.emit(self.engine_class)

    def _model_changed(self, model):
        self._show_limits(model)
        if 'flex' in (getattr(self.engine_class, 'service_tiers', None)
                      or []):
            self.flex_note.setText('')
            self.flex_timer.start()

    def _settings_tier(self):
        return self.engine_class.config.get(
            'service_tier', getattr(self.engine_class, 'service_tier', ''))

    def _set_tier(self, tier):
        self.tier_box.setCurrentIndex(max(self.tier_box.findData(tier), 0))

    def _tier_chosen(self, index):
        self.tier_touched = True

    def _check_flex(self):
        model = self.model_box.currentText().strip()
        if model:
            self.flex_worker.start.emit(self.engine_class, model)

    def _flex_checked(self, model, found):
        """Preselect flex for a model that has it, and never for one
        that does not: flex would fail every request."""
        if model != self.model_box.currentText().strip():
            return  # the answer for a model since changed
        self.flex_note.setText(
            _('flex: available for this model') if found else
            _('flex: not offered for this model') if found is False
            else '')
        if self.tier_touched:
            return
        tier = self._settings_tier()
        flex_first = self.engine_class.config.get(
            'flex_when_available',
            getattr(self.engine_class, 'flex_when_available', False))
        if found and flex_first:
            tier = 'flex'
        elif found is False and tier == 'flex':
            tier = 'default'
        self._set_tier(tier)

    def _show_limits(self, model):
        limits = model_limits(
            self.engine_class, (model or '').strip(), self.configured_model)
        if not limits:
            self.model_limits.setText('')
            self.model_limits.setVisible(False)
            return
        output = limits.get('max_output_tokens')
        self.model_limits.setText(_('Longest reply: {} tokens').format(
            '{:,}'.format(int(output)).replace(',', ' ')
            if output else _('not stated')))
        self.model_limits.setVisible(True)

    def _start_new(self):
        model = self.model_box.currentText().strip()
        if not model:
            self.alert.pop(_('Choose a model.'), 'warning')
            return
        tier = self.tier_box.currentData() \
            if self.tier_box.count() else None
        cache_id = create_translation(
            self.ebook, self.engine_class.name, model, tier,
            model_limits(self.engine_class, model, self.configured_model),
            self.library_id, engine_provider(self.engine_class))
        self.done(0)
        self.open_translation.emit(self.ebook, cache_id)

    def _models_fetched(self, success, message):
        if not success:
            error_dialog(self, _("Can't fetch model list"), _(
                "Can't fetch model list, please check and try again."),
                message, show=True)

    # Threads still fetching a listing when their dialog closed, kept
    # here until they end: waiting for them froze calibre for as long
    # as the request took, and a QThread let go while it runs aborts.
    _lingering = []

    def done(self, result):
        self.flex_timer.stop()
        try:
            self.model_worker.finished.disconnect(self._fill_models)
            self.model_worker.success.disconnect(self._models_fetched)
            self.flex_worker.checked.disconnect(self._flex_checked)
        except (TypeError, RuntimeError):
            pass
        thread = self.model_thread
        thread.quit()
        if not thread.wait(200):
            lingering = type(self)._lingering
            pair = (thread, self.model_worker, self.flex_worker)
            lingering.append(pair)
            thread.finished.connect(lambda: pair in lingering
                                    and lingering.remove(pair))
        QDialog.done(self, result)


# ---------------------------------------------------------------------------
# NovelTranslation: main dialog
# ---------------------------------------------------------------------------


class NovelTranslation(QDialog):
    """The window of one translation of a book.

    Layout:
      * Left column: the chapter list with per-chapter status marks,
        preceded by what is translated apart from the chapters (the
        metadata, the table of contents, the front matter).
      * Right column: the engine, model and tier of the translation, the
        progress bar and the tabs: the text, to read and correct; the
        author brief, the summaries, the glossary, the log, the report.
      * Bottom: Start / Cancel, the output format, Build, Close.
    """

    STATUS_PENDING = 0
    STATUS_RUNNING = 1
    STATUS_DONE = 2
    STATUS_ERROR = 3

    # The entry of the chapter list that stands for what no chapter
    # holds: metadata, table of contents, front matter.
    AUX_SECTION = TranslationText.AUX_SECTION

    def __init__(self, plugin, parent, worker, ebook, cache_id,
                 library_id=None):
        QDialog.__init__(self, parent)
        self.ui_settings = plugin.ui_settings
        self.api = parent.current_db.new_api
        self.worker = worker
        self.ebook = ebook
        self.alert = AlertMessage(self)
        self.footer = Footer()

        self.config = get_config()
        self.cache_id = cache_id
        self.info = self._read_info()
        # The engine the translation was made with, whatever the
        # settings name now: its model is that engine's.
        self.current_engine = engine_class_for(
            get_engine_class(self.info.get('engine_name')), self.info)

        self.chapters_meta = []
        self.chapter_items = {}   # index -> QListWidgetItem
        self.status_by_chapter = {}
        self.output_ready = False
        # Whether a worker is busy, and whether the window is waiting
        # for it to stop so it can close. See ``done``.
        self.preparing = True
        self.translating = False
        self.closing = False
        # The author brief and the glossary can be edited between runs.
        # ``_loading_context`` is set while the window itself fills them,
        # so that only the user's changes count as edits; whether any
        # paragraph is translated yet decides whether an edit is worth a
        # warning, and once it is, it stays so.
        self._loading_context = False
        self._stored_style = ''
        self._started = False

        # One pair of threads per window. As class attributes they were
        # shared by every book open at once and never stopped, so the
        # work went on after the window was gone and calibre aborted on
        # exit with a thread still running.
        self.prep_thread = QThread()
        self.trans_thread = QThread()

        self.prep_worker = NovelPreparationWorker(
            self.current_engine, self.ebook, cache_id, library_id)
        self.prep_worker.moveToThread(self.prep_thread)
        self.prep_thread.finished.connect(self.prep_worker.deleteLater)
        self.prep_thread.start()

        self.trans_worker = None  # created after preparation completes.

        layout = QVBoxLayout(self)
        self.waiting = self._layout_progress()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.waiting)
        layout.addWidget(self.stack)
        layout.addWidget(self.footer)

        self.prep_worker.progress_message.connect(self._prep_label.setText)
        self.prep_worker.progress_detail.connect(
            self._prep_detail.appendPlainText)
        self.prep_worker.finished.connect(self._on_prep_finished)
        self.prep_worker.failed.connect(self._on_prep_failed)
        self.prep_worker.start.emit()

    def _read_info(self):
        cache = get_cache(self.cache_id)
        try:
            return cache.all_info()
        finally:
            cache.close()

    def _model_label(self):
        """The model of this translation; for one written before 1.3,
        which did not record it, the model the settings name, which is
        the one a run would use."""
        model = self.info.get(INFO_MODEL)
        if model:
            return model
        try:
            model = getattr(get_translator(self.current_engine), 'model', '')
        except Exception:
            model = ''
        return _('{} (from the settings)').format(model) if model \
            else _('(not recorded)')

    def _tier_label(self):
        tier = self.info.get(INFO_SERVICE_TIER)
        if not tier:
            tier = self.current_engine.config.get(
                'service_tier', getattr(self.current_engine,
                                        'service_tier', ''))
        return '' if tier in (None, '', 'default') else tier

    def _refresh_info_line(self):
        self.info = self._read_info()
        provider = engine_provider(self.current_engine)
        engine = self.current_engine.name + (
            ' (%s)' % provider if provider else '')
        parts = ['%s: %s' % (_('Engine'), escape(engine)),
                 '%s: <b>%s</b>' % (_('Model'), escape(self._model_label()))]
        tier = self._tier_label()
        if tier:
            parts.append('%s: %s' % (_('Service tier'), escape(tier)))
        parts.append('%s: %s' % (_('Target'), escape(
            self.ebook.target_lang or '')))
        self.info_label.setText(' \u00b7 '.join(parts))

    # -- layout: preparation view -----------------------------------------

    def _layout_progress(self):
        widget = QWidget()
        layout = QGridLayout(widget)

        try:
            cover_image = self.api.cover(self.ebook.id, as_pixmap=True)
        except Exception:
            cover_image = QPixmap(
                self.api.cover(self.ebook.id, as_image=True))
        if cover_image is None or cover_image.isNull():
            cover_image = QPixmap(I('default_cover.png'))
        cover_image = cover_image.scaledToHeight(
            400, Qt.TransformationMode.SmoothTransformation)

        cover = QLabel()
        cover.setAlignment(Qt.AlignCenter)
        cover.setPixmap(cover_image)

        title = QLabel(self.ebook.title)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet('font-weight:bold;font-size:16px;')

        progress_bar = QProgressBar()
        progress_bar.setRange(0, 0)  # indeterminate
        progress_bar.setValue(0)

        self._prep_label = QLabel(_('Loading ebook data, please wait...'))
        self._prep_label.setAlignment(Qt.AlignCenter)

        self._prep_detail = QPlainTextEdit()
        self._prep_detail.setReadOnly(True)

        layout.addWidget(cover, 0, 0)
        layout.addWidget(title, 1, 0)
        layout.addItem(QSpacerItem(0, 20), 2, 0)
        layout.addWidget(progress_bar, 3, 0)
        layout.addWidget(self._prep_label, 4, 0)
        layout.addItem(QSpacerItem(10, 0), 0, 1, 6, 1)
        layout.addWidget(self._prep_detail, 0, 2, 6, 1)
        layout.setRowStretch(2, 1)
        layout.setColumnStretch(2, 1)

        return widget

    # -- prep -> main transition ------------------------------------------

    @pyqtSlot(str, object)
    def _on_prep_finished(self, cache_id, chapters_meta):
        self.preparing = False
        self.cache_id = cache_id
        self.chapters_meta = chapters_meta or []
        if not self.chapters_meta:
            self.alert.pop(
                _('No translatable content detected in this book.'),
                'warning')
            self.done(0)
            return
        self.main_panel = self._layout_main()
        self.stack.addWidget(self.main_panel)
        self.stack.setCurrentWidget(self.main_panel)
        self._refresh_chapter_list_from_cache()

    @pyqtSlot(str)
    def _on_prep_failed(self, message):
        self.preparing = False
        self.alert.pop(
            _('Failed to prepare the book: {}').format(message), 'error')
        self.done(0)

    # -- layout: main view -------------------------------------------------

    def _layout_main(self):
        widget = QWidget()
        outer = QVBoxLayout(widget)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left side: chapter list.
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel(_('Chapters')))
        self.chapter_list = QListWidget()
        aux = QListWidgetItem(_('Metadata, contents and front matter'))
        aux.setData(Qt.UserRole, self.AUX_SECTION)
        aux.setForeground(QColor('gray'))
        self.chapter_list.addItem(aux)
        for ch in self.chapters_meta:
            item = QListWidgetItem(self._chapter_label(ch, self.STATUS_PENDING))
            item.setData(Qt.UserRole, ch['index'])
            self.chapter_list.addItem(item)
            self.chapter_items[ch['index']] = item
            self.status_by_chapter[ch['index']] = self.STATUS_PENDING
        left_layout.addWidget(self.chapter_list, 1)

        # Right side: progress + tabs.
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Info line: which translation of the book this is.
        self.info_label = QLabel()
        self.info_label.setTextFormat(Qt.TextFormat.RichText)
        self.info_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        right_layout.addWidget(self.info_label)
        self._refresh_info_line()

        # Progress bar + status label.
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        right_layout.addWidget(self.progress_bar)
        self.progress_label = QLabel(_('Ready.'))
        right_layout.addWidget(self.progress_label)

        # Shown when the brief or the glossary is edited once paragraphs
        # are already translated, and until the translation starts again.
        self.context_warning = QLabel(_(
            'You changed the author brief or the glossary after the '
            'translation had started. What is already translated followed '
            'the previous version, so the book may not read consistently. '
            'Once every chapter is done, "Re-run all" translates it again '
            'with what you wrote.'))
        self.context_warning.setWordWrap(True)
        self.context_warning.setStyleSheet(
            'background:#fff3cd;color:#664d03;border:1px solid #ffda6a;'
            'border-radius:4px;padding:6px;')
        self.context_warning.setVisible(False)
        right_layout.addWidget(self.context_warning)

        # Tabs.
        self.tabs = QTabWidget()

        # Text tab, first: the book itself, to read and correct before
        # it is built.
        self.text_view = TranslationText(
            [(self.cache_id, self._model_label())], self.chapters_meta,
            self.alert)
        self.tabs.addTab(self.text_view, _('Text'))

        # Author brief tab: what the book is translated by. The worker
        # reads it once, before the first chapter, and asks the model for
        # one only when it is empty; so it can be written or corrected
        # here before a run and between runs, never during one, when the
        # worker holds its own copy. Saved as it is typed.
        style_tab = QWidget()
        style_layout = QVBoxLayout(style_tab)
        style_layout.setContentsMargins(0, 0, 0, 0)
        # Why there is no brief, when the last run found none.
        self.style_note = QLabel()
        self.style_note.setWordWrap(True)
        self.style_note.setStyleSheet('color:gray;')
        self.style_note.setVisible(False)
        style_layout.addWidget(self.style_note)
        self.style_view = QPlainTextEdit()
        self.style_view.setPlaceholderText(_(
            'No brief yet. Write one here and every chapter is translated '
            'by it. Leave it empty and the model is asked for one when '
            'the translation starts, unless the plugin settings say "Do '
            'not ask" ("How the author writes").'))
        self.style_view.textChanged.connect(self._on_style_edited)
        style_layout.addWidget(self.style_view, 1)
        self.tabs.addTab(style_tab, _('Author'))
        self._style_timer = QTimer(self)
        self._style_timer.setSingleShot(True)
        self._style_timer.setInterval(800)
        self._style_timer.timeout.connect(self._save_style)

        # Summaries tab.
        self.summaries_view = QPlainTextEdit()
        self.summaries_view.setReadOnly(True)
        self.tabs.addTab(self.summaries_view, _('Summaries'))

        # Glossary tab.
        self.glossary_table = QTableWidget()
        self.glossary_table.setColumnCount(4)
        self.glossary_table.setHorizontalHeaderLabels(
            [_('Source'), _('Translation'), _('Type'), _('Notes')])
        self.glossary_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.glossary_table.verticalHeader().setVisible(False)
        self.glossary_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        # Editable between runs only (see _set_context_locked). The table
        # is redrawn from scratch every time a chapter completes, and the
        # worker thread keeps its own copy of the glossary that it writes
        # back after each chapter: an edit typed here during a run was
        # overwritten without a word. An edit is saved when it is made.
        self.glossary_table.itemChanged.connect(self._on_glossary_edited)
        # Enter edits the entry selected; let through, it pressed the
        # dialog's default button and started the translation.
        self.glossary_table.installEventFilter(
            EnterFilter(self._edit_glossary_entry, self))
        glossary_actions = QHBoxLayout()
        self.glossary_add_button = QPushButton(_('Add entry'))
        self.glossary_add_button.setToolTip(_(
            'Add a name and how to translate it. An entry written or '
            'corrected here is used as it is: the model never changes it, '
            'and the glossary size limit never drops it.'))
        self.glossary_add_button.clicked.connect(self._add_glossary_entry)
        self.glossary_remove_button = QPushButton(_('Remove'))
        self.glossary_remove_button.setToolTip(_(
            'Remove the entries selected. The model may list a name '
            'again when a later chapter introduces it.'))
        self.glossary_remove_button.clicked.connect(
            self._remove_glossary_entries)
        self.reset_context_button = QPushButton(_('Reset context'))
        self.reset_context_button.clicked.connect(self._reset_context)
        glossary_actions.addWidget(self.glossary_add_button)
        glossary_actions.addWidget(self.glossary_remove_button)
        glossary_actions.addStretch(1)
        glossary_actions.addWidget(self.reset_context_button)
        glossary_wrap = QWidget()
        glossary_wrap_layout = QVBoxLayout(glossary_wrap)
        glossary_wrap_layout.setContentsMargins(0, 0, 0, 0)
        glossary_wrap_layout.addWidget(self.glossary_table, 1)
        glossary_wrap_layout.addLayout(glossary_actions)
        # The table is added to the wrapper and only the wrapper becomes a
        # tab. Adding the table as a page first and swapping it afterwards
        # would leave it permanently invisible: QStackedLayout calls hide()
        # on every page that is not the current one, and reparenting a
        # widget into another layout never undoes an explicit hide() -- the
        # Glossary tab would then show its buttons and no table at all.
        self.tabs.addTab(glossary_wrap, _('Glossary'))

        # Log tab.
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.tabs.addTab(self.log_view, _('Log'))

        # Report tab, last: what the book costs and takes, how the
        # providers behave, what to do about it. Apart from the log,
        # where those lines would scroll away under everything else,
        # and as HTML so the columns of the table line up.
        self.report_view = QTextBrowser()
        self.report_view.setOpenExternalLinks(False)
        self.report_view.setPlaceholderText(_(
            'Requests, tokens, cost and time of this book, the providers '
            'that served it and how reliably, and what to change if one '
            'of them misbehaved: written after every chapter.'))
        self.tabs.addTab(self.report_view, _('Report'))

        right_layout.addWidget(self.tabs, 1)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        outer.addWidget(splitter, 1)

        # Buttons row.
        btn_row = QHBoxLayout()
        self.start_button = QPushButton(_('Start / Resume'))
        self.start_button.clicked.connect(self._on_start)
        self.cancel_button = QPushButton(_('Cancel'))
        self.cancel_button.clicked.connect(self._on_cancel)
        self.cancel_button.setEnabled(False)
        self.output_button = QPushButton(_('Build translated ebook'))
        self.output_button.setEnabled(False)
        self.output_button.clicked.connect(self._on_build_output)
        output_format = OutputFormat()
        if self.ebook.output_format:
            output_format.setCurrentText(self.ebook.output_format)
        self.ebook.set_output_format(output_format.currentText())
        output_format.currentTextChanged.connect(
            self.ebook.set_output_format)
        self.close_button = QPushButton(_('Close'))
        self.close_button.clicked.connect(lambda: self.done(0))
        btn_row.addWidget(self.start_button)
        btn_row.addWidget(self.cancel_button)
        btn_row.addStretch(1)
        btn_row.addWidget(QLabel(_('Output Format')))
        btn_row.addWidget(output_format)
        btn_row.addWidget(self.output_button)
        btn_row.addWidget(self.close_button)
        outer.addLayout(btn_row)

        # In-memory glossary accumulator. Updated incrementally from the
        # chapter_done signal so the UI never needs to read back from SQLite
        # (which can miss in-flight transactions from the worker thread).
        self._ui_glossary = {}

        self.text_view.bind_chapter_list(self.chapter_list)
        self._set_context_locked(False)

        return widget

    # -- chapter list rendering -------------------------------------------

    _STATUS_SYMBOL = {
        STATUS_PENDING: '•',
        STATUS_RUNNING: '▶',
        STATUS_DONE: '✓',
        STATUS_ERROR: '✗',
    }
    _STATUS_COLOR = {
        STATUS_PENDING: None,
        STATUS_RUNNING: '#4169e1',   # royalblue
        STATUS_DONE: '#2e8b57',      # seagreen
        STATUS_ERROR: '#dc143c',     # crimson
    }

    def _chapter_label(self, chapter_meta, status):
        symbol = self._STATUS_SYMBOL.get(status, '•')
        return '%s  %d. %s' % (
            symbol, chapter_meta['index'], chapter_meta['title'] or '?')

    def _set_chapter_status(self, index, status):
        self.status_by_chapter[index] = status
        item = self.chapter_items.get(index)
        if item is None:
            return
        meta = next((m for m in self.chapters_meta
                     if m['index'] == index), None)
        if meta is None:
            return
        item.setText(self._chapter_label(meta, status))
        color_hex = self._STATUS_COLOR.get(status)
        # A pending chapter gets the default colour back: after "Reset
        # context" the list used to stay green with pending markers.
        item.setForeground(QColor(color_hex) if color_hex else QBrush())

    def _refresh_chapter_list_from_cache(self):
        """Restore chapter statuses from cache progress (for resume)."""
        cache = get_cache(self.cache_id)
        try:
            progress = int(cache.get_info('novel_progress') or 0)
        except (ValueError, TypeError):
            progress = 0
        for meta in self.chapters_meta:
            if meta['index'] <= progress:
                self._set_chapter_status(meta['index'], self.STATUS_DONE)
        self._refresh_context_views(cache)
        # The first chapter, not the front matter: it is what a reader
        # looks at first.
        self.chapter_list.setCurrentRow(
            1 if self.chapter_list.count() > 1 else 0)
        stored_log = cache.get_info(INFO_NOVEL_LOG)
        if stored_log:
            self.log_view.setPlainText(stored_log)
        stored_report = cache.get_info(INFO_NOVEL_REPORT)
        if stored_report:
            self.report_view.setHtml(stored_report)
        cache.close()

        completed = sum(1 for s in self.status_by_chapter.values()
                        if s == self.STATUS_DONE)
        total = len(self.chapters_meta)
        # The bar follows the text, not the chapters: the pages before
        # the story are chapters too, and small ones.
        total_chars = sum(
            int(m.get('char_count') or 0) for m in self.chapters_meta) or 1
        done_chars = sum(
            int(m.get('char_count') or 0) for m in self.chapters_meta
            if self.status_by_chapter.get(m['index']) == self.STATUS_DONE)
        pct = int(100 * done_chars / total_chars)
        self.progress_bar.setValue(pct)
        self.progress_label.setText(_(
            '{done}/{total} chapters done, {pct}% of the text.').format(
                done=completed, total=total, pct=pct))
        if completed >= total > 0:
            self.output_button.setEnabled(True)
            self.start_button.setText(_('Re-run all'))

    def _refresh_context_views(self, cache=None):
        """Refresh the Summaries, Author and Glossary tabs.

        Summaries are always read from the SQLite cache (they are long
        strings, writing them into the signal payload would be wasteful).

        The glossary is read from ``self._ui_glossary``, an in-memory dict
        that is updated incrementally by :meth:`_on_chapter_done` every
        time the worker emits a ``chapter_done`` signal. This avoids the
        SQLite transaction-visibility race that caused the Glossary tab to
        appear empty during a live translation run: the worker's open
        connection commits after each chapter, but the secondary connection
        opened here would sometimes read stale data depending on SQLite's
        WAL checkpoint timing.

        When ``cache`` is supplied (e.g. from the initial load in
        :meth:`_refresh_chapter_list_from_cache`), both summaries AND the
        glossary initial state are read from it so the UI is correctly
        restored on re-open.
        """
        should_close = False
        if cache is None:
            cache = get_cache(self.cache_id)
            should_close = True
        try:
            import json as _json
            # --- Summaries: always from cache ---
            raw = cache.get_info('novel_summaries')
            summaries = []
            try:
                summaries = _json.loads(raw) if raw else []
            except (ValueError, TypeError):
                summaries = []
            self.summaries_view.clear()
            for s in summaries:
                self.summaries_view.appendPlainText(
                    '=== %s: %s ===' % (
                        _('Chapter {}').format(s.get('chapter', '?')),
                        s.get('title', '')))
                self.summaries_view.appendPlainText(s.get('summary', ''))
                self.summaries_view.appendPlainText('')

            # --- Author brief: always from cache, written once per
            # book before the first chapter or typed in the tab.
            style = cache.get_info(INFO_NOVEL_STYLE)
            style = style if isinstance(style, str) else ''
            self._stored_style = style.strip()
            if self.style_view.toPlainText() != style:
                self._loading_context = True
                try:
                    self.style_view.setPlainText(style)
                finally:
                    self._loading_context = False
            # No brief: say why, rather than show an empty tab.
            note = cache.get_info(INFO_NOVEL_STYLE_NOTE)
            self.style_note.setText(note if isinstance(note, str) else '')
            self._show_style_note()

            # --- Glossary: prefer in-memory accumulator; seed from cache
            # on the initial load (when _ui_glossary is still empty).
            if not self._ui_glossary:
                raw = cache.get_info('novel_glossary')
                try:
                    self._ui_glossary = _json.loads(raw) if raw else {}
                    if not isinstance(self._ui_glossary, dict):
                        self._ui_glossary = {}
                except (ValueError, TypeError):
                    self._ui_glossary = {}

            self._redraw_glossary_table()
        finally:
            if should_close:
                cache.close()

    def _redraw_glossary_table(self):
        """Repopulate the glossary QTableWidget from ``self._ui_glossary``."""
        glossary = self._ui_glossary
        self._loading_context = True
        try:
            self.glossary_table.setRowCount(len(glossary))
            for row, (source, entry) in enumerate(glossary.items()):
                self.glossary_table.setItem(
                    row, 0, QTableWidgetItem(source))
                self.glossary_table.setItem(
                    row, 1, QTableWidgetItem(entry.get('translation', '')))
                self.glossary_table.setItem(
                    row, 2, QTableWidgetItem(entry.get('type', '')))
                self.glossary_table.setItem(
                    row, 3, QTableWidgetItem(entry.get('notes', '')))
                if entry.get('user'):
                    self._mark_user_row(row)
        finally:
            self._loading_context = False

    # -- editing the brief and the glossary ------------------------------

    def _set_context_locked(self, locked):
        """The brief and the glossary are the worker's while it runs,
        and the user's the rest of the time."""
        self.style_view.setReadOnly(locked)
        triggers = QAbstractItemView.EditTrigger
        self.glossary_table.setEditTriggers(
            triggers.NoEditTriggers if locked else (
                triggers.DoubleClicked | triggers.EditKeyPressed
                | triggers.AnyKeyPressed))
        for button in (self.glossary_add_button,
                       self.glossary_remove_button,
                       self.reset_context_button):
            button.setEnabled(not locked)

    def _translation_started(self):
        """Whether anything was translated with the brief and the
        glossary as they were."""
        if not self._started:
            if any(status == self.STATUS_DONE
                   for status in self.status_by_chapter.values()):
                self._started = True
            else:
                cache = get_cache(self.cache_id)
                try:
                    self._started = cache.has_translation()
                finally:
                    cache.close()
        return self._started

    def _context_edited(self):
        """The user changed the brief or the glossary: say what it means
        for a translation already under way. The warning stays until the
        translation starts again."""
        if not self.context_warning.isVisible() \
                and self._translation_started():
            self.context_warning.setVisible(True)

    def _show_style_note(self):
        self.style_note.setVisible(
            bool(self.style_note.text())
            and not self.style_view.toPlainText().strip())

    def _on_style_edited(self):
        if self._loading_context:
            return
        self._show_style_note()
        self._style_timer.start()
        if self.style_view.toPlainText().strip() != self._stored_style:
            self._context_edited()

    def _save_style(self):
        self._style_timer.stop()
        cache = get_cache(self.cache_id)
        try:
            self._stored_style = ContextManager(cache).load().set_style(
                self.style_view.toPlainText())
        finally:
            cache.close()

    def _flush_style(self):
        """Save the brief now if it was typed and not saved yet."""
        if self._style_timer.isActive():
            self._save_style()

    def _mark_user_row(self, row):
        """Mark ``row`` as written by the user, whose entries the model
        never changes."""
        loading, self._loading_context = self._loading_context, True
        try:
            for column in range(self.glossary_table.columnCount()):
                if self.glossary_table.item(row, column) is None:
                    self.glossary_table.setItem(
                        row, column, QTableWidgetItem(''))
            head = self.glossary_table.item(row, 0)
            head.setData(Qt.UserRole, True)
            head.setToolTip(_(
                'Written or corrected by you: the model never changes it.'))
        finally:
            self._loading_context = loading

    @staticmethod
    def _clean_glossary(glossary):
        """``glossary`` as it is saved: no empty fields."""
        return {source: {key: value for key, value in entry.items()
                         if value}
                for source, entry in glossary.items()}

    def _table_glossary(self):
        """The glossary the table shows. A row without a name or a
        translation is left out until it has both; of two rows with the
        same name, the lower one counts."""
        glossary = {}
        table = self.glossary_table
        for row in range(table.rowCount()):
            cells = [table.item(row, column) for column in range(4)]
            source, translation, gtype, notes = [
                item.text().strip() if item is not None else ''
                for item in cells]
            if not source or not translation:
                continue
            entry = {'translation': translation, 'type': gtype,
                     'notes': notes}
            if cells[0].data(Qt.UserRole):
                entry['user'] = True
            glossary.pop(source, None)
            glossary[source] = entry
        return self._clean_glossary(glossary)

    def _save_glossary(self):
        """Write the table into the cache; whether that changed it."""
        glossary = self._table_glossary()
        if glossary == self._clean_glossary(self._ui_glossary):
            return False
        cache = get_cache(self.cache_id)
        try:
            ContextManager(cache).load().replace_glossary(glossary)
        finally:
            cache.close()
        self._ui_glossary = glossary
        return True

    def _on_glossary_edited(self, item):
        if self._loading_context:
            return
        self._mark_user_row(item.row())
        if self._save_glossary():
            self._context_edited()

    def _edit_glossary_entry(self):
        item = self.glossary_table.currentItem()
        if item is not None and not self.translating:
            self.glossary_table.editItem(item)

    def _add_glossary_entry(self):
        table = self.glossary_table
        row = table.rowCount()
        self._loading_context = True
        try:
            table.insertRow(row)
        finally:
            self._loading_context = False
        self._mark_user_row(row)
        table.scrollToBottom()
        table.setCurrentCell(row, 0)
        table.editItem(table.item(row, 0))

    def _remove_glossary_entries(self):
        table = self.glossary_table
        rows = sorted({index.row() for index in
                       table.selectionModel().selectedRows()},
                      reverse=True)
        if not rows:
            return
        self._loading_context = True
        try:
            for row in rows:
                table.removeRow(row)
        finally:
            self._loading_context = False
        if self._save_glossary():
            self._context_edited()

    # -- controls ----------------------------------------------------------

    def _all_chapters_done(self):
        return bool(self.chapters_meta) and all(
            self.status_by_chapter.get(m['index']) == self.STATUS_DONE
            for m in self.chapters_meta)

    def _on_start(self):
        # The engine preferences may have been changed in the settings
        # meanwhile; the engine is still the translation's.
        self.current_engine = engine_class_for(
            get_engine_class(self.info.get('engine_name')), self.info)
        if not self.text_view.confirm_discard():
            return
        retranslate = False
        if self._all_chapters_done():
            # "Re-run all". The button used to just start the worker,
            # which found every chapter done and stopped at once.
            question = _(
                'Every chapter is done. Translate the whole book again?\n\n'
                'The summaries, the glossary and the author brief are '
                'kept; every paragraph is sent to the model again.')
            cache = get_cache(self.cache_id)
            try:
                edited = len(edited_paragraphs(cache))
            finally:
                cache.close()
            if edited:
                question += '\n\n' + _(
                    '{} paragraph(s) corrected by hand are translated '
                    'again too, and the corrections lost. To keep them '
                    'and compare, start a new translation of the book '
                    'instead.').format(edited)
            action = self.alert.ask(question)
            if action != 'yes':
                return
            cache = get_cache(self.cache_id)
            try:
                ContextManager(cache).load().reset_progress()
            finally:
                cache.close()
            for index in list(self.status_by_chapter):
                self._set_chapter_status(index, self.STATUS_PENDING)
            self.progress_bar.setValue(0)
            self.start_button.setText(_('Start / Resume'))
            retranslate = True
        self._start_translation_worker(retranslate)

    def _start_translation_worker(self, retranslate=False):
        # Terminate any previous worker: disconnect its signals so we don't
        # accidentally receive events for it after we start a new one.
        if self.trans_worker is not None:
            try:
                self.trans_worker.logging.disconnect()
                self.trans_worker.progress.disconnect()
                self.trans_worker.chapter_started.disconnect()
                self.trans_worker.chapter_done.disconnect()
                self.trans_worker.report.disconnect()
                self.trans_worker.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            try:
                self.trans_worker.deleteLater()
            except RuntimeError:
                pass
        if not self.trans_thread.isRunning():
            self.trans_thread.start()
        self.trans_worker = NovelTranslationWorker(
            self.current_engine, self.ebook, self.cache_id, retranslate)
        self.trans_worker.moveToThread(self.trans_thread)
        self.trans_worker.logging.connect(self._on_log)
        self.trans_worker.progress.connect(self._on_progress)
        self.trans_worker.chapter_started.connect(
            lambda idx: self._set_chapter_status(idx, self.STATUS_RUNNING))
        self.trans_worker.chapter_done.connect(self._on_chapter_done)
        self.trans_worker.report.connect(self.report_view.setHtml)
        self.trans_worker.finished.connect(self._on_worker_finished)
        # What was typed in the Author tab goes into this run.
        self._flush_style()
        self._set_context_locked(True)
        self.context_warning.setVisible(False)
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.output_button.setEnabled(False)
        self.translating = True
        self.text_view.set_locked(True)
        self.trans_worker.start.emit()

    def _on_cancel(self):
        if self.trans_worker is None:
            return
        self.trans_worker.set_canceled(True)
        self.cancel_button.setEnabled(False)
        self.progress_label.setText(_('Cancel requested...'))

    def _on_log(self, text, is_error):
        prefix = '[ERROR] ' if is_error else ''
        self.log_view.appendPlainText(prefix + text)

    def _on_progress(self, fraction, message):
        try:
            self.progress_bar.setValue(int(100 * float(fraction)))
        except (TypeError, ValueError):
            pass
        if message:
            self.progress_label.setText(message)

    def _refresh_report_from_cache(self):
        """Show the report the worker wrote last. It also arrives by
        signal; reading it back here is what keeps the tab current when
        the signal does not reach the widget."""
        cache = get_cache(self.cache_id)
        try:
            stored = cache.get_info(INFO_NOVEL_REPORT)
        finally:
            cache.close()
        if stored:
            self.report_view.setHtml(stored)

    def _on_chapter_done(self, index, summary, glossary_delta):
        self._set_chapter_status(index, self.STATUS_DONE)
        self._refresh_report_from_cache()
        # Merge the glossary delta received directly from the worker signal
        # into the in-memory accumulator. This avoids the SQLite read-back
        # race: the delta is already the freshly extracted data, so no need
        # to reopen the cache connection.
        if glossary_delta:
            for item in glossary_delta:
                if not isinstance(item, dict):
                    continue
                source = (item.get('source') or '').strip()
                translation = (item.get('translation') or '').strip()
                if not source or not translation:
                    continue
                if self._ui_glossary.get(source, {}).get('user'):
                    continue  # the worker keeps it too
                self._ui_glossary[source] = {
                    'translation': translation,
                    'type': (item.get('type') or '').strip(),
                    'notes': (item.get('notes') or '').strip(),
                }
        self._refresh_context_views()
        # The chapter on show is read again when it is the one just
        # translated; any other view is left as the reader has it.
        self.text_view.chapter_changed(index)

    def _on_worker_finished(self, success, message):
        self.translating = False
        if self.closing:
            self.done(0)
            return
        self._refresh_report_from_cache()
        self._refresh_info_line()
        # The glossary as the worker left it, not as the signals rebuilt
        # it: the cap may have dropped entries they never mentioned.
        # Edits start from what is actually stored.
        self._ui_glossary = {}
        self._refresh_context_views()
        self._set_context_locked(False)
        self.text_view.set_locked(False)
        self.text_view.reload()
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_label.setText(message)
        # Any chapter still marked "running" is now uncertain: leave as-is.
        if success:
            # Enable output only if all chapters are done.
            done_count = sum(
                1 for s in self.status_by_chapter.values()
                if s == self.STATUS_DONE)
            if done_count >= len(self.chapters_meta):
                self.output_button.setEnabled(True)
                self.start_button.setText(_('Re-run all'))
                self.alert.pop(_(
                    'All chapters translated. Click "Build translated '
                    'ebook" to produce the output.'))
        else:
            self.alert.pop(message, 'warning')

    # -- closing -------------------------------------------------------------

    def done(self, result):
        """Close only once nothing is running any more.

        Closing used to leave the translation going on in the background
        with no window to cancel it from, and the plugin thinking no job
        was running. A translation in flight is cancelled first and the
        window closes when the worker reports back; the preparation is
        short and cannot be interrupted, so it is waited for.
        """
        if self.preparing:
            self.alert.pop(_(
                'The book is still being prepared. Please wait a moment.'),
                'warning')
            return
        if hasattr(self, 'text_view') and not self.translating \
                and not self.text_view.confirm_discard():
            return
        if hasattr(self, 'style_view') and not self.translating:
            self._flush_style()
        if self.translating:
            if not self.closing:
                action = self.alert.ask(_(
                    'The translation is still running. Stop it and close '
                    'the window? What is already translated stays in the '
                    'cache and the next start resumes from there.'))
                if action != 'yes':
                    return
                self.closing = True
                self.trans_worker.set_canceled(True)
                self.cancel_button.setEnabled(False)
                self.close_button.setEnabled(False)
                self.progress_label.setText(_('Stopping...'))
            return
        for thread in (self.prep_thread, self.trans_thread):
            thread.quit()
            thread.wait()
        # With the cache turned off the work lives in a temporary
        # database; it goes with the window, as the other modes do.
        if self.cache_id is not None:
            cache = get_cache(self.cache_id)
            if cache.is_persistence():
                cache.close()
            else:
                cache.destroy()
        QDialog.done(self, result)

    # -- context -----------------------------------------------------------

    def _reset_context(self):
        ret = QMessageBox.question(
            self, _('Reset context'),
            _('This will discard all summaries, the glossary and the '
              'author brief, and reset chapter progress to 0. The '
              'already-translated paragraphs will remain in cache. '
              'Continue?'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        # A brief typed and not saved yet goes with the rest.
        self._style_timer.stop()
        cache = get_cache(self.cache_id)
        try:
            ContextManager(cache).load().reset()
        finally:
            cache.close()
        self._ui_glossary = {}
        for idx in list(self.status_by_chapter.keys()):
            self._set_chapter_status(idx, self.STATUS_PENDING)
        self.progress_bar.setValue(0)
        self.output_button.setEnabled(False)
        self._refresh_context_views()

    # -- final ebook build -------------------------------------------------

    def _on_build_output(self):
        # The cache is already fully populated by the interactive
        # pipeline, so the job only puts the translations back into the
        # DOM and lets Plumber write the ebook.
        try:
            if not self.text_view.confirm_discard():
                return
            self.ebook.set_output_format(
                self.ebook.output_format or 'epub')
            self.worker.translate_ebook(
                self.ebook, self.cache_id, cache_only=True,
                label=self._model_label())
            self.alert.pop(_(
                'Building translated ebook in background. Watch the '
                'jobs panel for progress.'))
        except Exception as e:
            self.alert.pop(
                _('Failed to launch build job: {}').format(e), 'error')