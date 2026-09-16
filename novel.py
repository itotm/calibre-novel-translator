"""The translation window.

It runs the chapter-aware sequential pipeline defined in ``lib/novel.py``
on a single ebook. The model operates on whole chapters, so the window
shows chapter-level progress plus tabs for the running summaries, the
dynamic glossary, the author brief and the log.

"""
import time
import traceback
from types import MethodType

from qt.core import (  # type: ignore
    Qt, QObject, QDialog, QGroupBox, QWidget, QVBoxLayout, QHBoxLayout,
    QPlainTextEdit, QPushButton, QSplitter, QLabel, QThread, QGridLayout,
    QTextBrowser,
    QProgressBar, pyqtSignal, pyqtSlot, QPixmap, QListWidget,
    QListWidgetItem, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QSpacerItem, QStackedWidget, QComboBox, QMessageBox,
    QSizePolicy, QColor, QBrush, QAbstractItemView)
from calibre.constants import __version__  # type: ignore
from calibre.gui2 import I  # type: ignore
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
    novel_cache_id, INFO_NOVEL_CHAPTERS, INFO_NOVEL_LOG, INFO_NOVEL_STYLE,
    INFO_NOVEL_STYLE_NOTE, INFO_NOVEL_REPORT)
from .lib.conversion import get_novel_config
from .engines.genai import GenAI
from .components import (
    Footer, AlertMessage, SourceLang, TargetLang, InputFormat, OutputFormat)


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

    def __init__(self, engine_class, ebook):
        QObject.__init__(self)
        self.engine_class = engine_class
        self.ebook = ebook
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
        translator_name = self.engine_class.name
        encoding = ''
        if self.ebook.encoding.lower() != 'utf-8':
            encoding = self.ebook.encoding.lower()
        cache_id = novel_cache_id(
            input_path, translator_name, self.ebook.target_lang, encoding)

        cache = get_cache(cache_id)
        cache.set_info('title', self.ebook.title)
        # The author is asked about once per book. Written here so a
        # translation started from the background job -- which only
        # receives the title -- can still find it.
        cache.set_info('author', self.ebook.get_author())
        cache.set_info('engine_name', translator_name)
        cache.set_info('target_lang', self.ebook.target_lang)
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
            cache.close()


# ---------------------------------------------------------------------------
# CreateNovelProject: the setup dialog before the window opens
# ---------------------------------------------------------------------------


class CreateNovelProject(QDialog):
    start_translation = pyqtSignal(object)

    def __init__(self, parent, ebook):
        QDialog.__init__(self, parent)
        self.ebook = ebook
        self.alert = AlertMessage(self)

        layout = QVBoxLayout(self)
        self.choose_format = self.layout_format()
        self.start_button = QPushButton(_('&Start'))
        self.start_button.clicked.connect(self.show_novel)

        layout.addWidget(self.choose_format)
        layout.addWidget(self.start_button)

    def layout_format(self):
        engine_class = get_engine_class()
        widget = QWidget()
        layout = QGridLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        input_group = QGroupBox(_('Input Format'))
        input_layout = QGridLayout(input_group)
        input_format = InputFormat(self.ebook.files.keys())
        input_layout.addWidget(input_format)
        layout.addWidget(input_group, 1, 0, 1, 3)

        output_group = QGroupBox(_('Output Format'))
        output_layout = QGridLayout(output_group)
        output_format = OutputFormat()
        output_layout.addWidget(output_format)
        layout.addWidget(output_group, 1, 3, 1, 3)

        source_group = QGroupBox(_('Source Language'))
        source_layout = QVBoxLayout(source_group)
        source_lang = SourceLang()
        source_lang.setFixedWidth(150)
        source_layout.addWidget(source_lang)
        layout.addWidget(source_group, 2, 0, 1, 2)

        target_group = QGroupBox(_('Target Language'))
        target_layout = QVBoxLayout(target_group)
        target_lang = TargetLang()
        target_lang.setFixedWidth(150)
        target_layout.addWidget(target_lang)
        layout.addWidget(target_group, 2, 2, 1, 2)

        source_lang.refresh.emit(
            engine_class.lang_codes.get('source'),
            engine_class.config.get('source_lang'), True)
        target_lang.refresh.emit(
            engine_class.lang_codes.get('target'),
            engine_class.config.get('target_lang'))

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

        return widget

    @pyqtSlot()
    def show_novel(self):
        self.done(0)
        self.start_translation.emit(self.ebook)


# ---------------------------------------------------------------------------
# NovelTranslation: main dialog
# ---------------------------------------------------------------------------


class NovelTranslation(QDialog):
    """The main window.

    Layout:
      * Left column: cover thumbnail, book title, chapter list with
        per-chapter status icons.
      * Right column: progress bar + tabs (Summaries / Glossary / Log).
      * Bottom: Start / Pause / Cancel / Close buttons.
    """

    STATUS_PENDING = 0
    STATUS_RUNNING = 1
    STATUS_DONE = 2
    STATUS_ERROR = 3

    def __init__(self, plugin, parent, worker, ebook):
        QDialog.__init__(self, parent)
        self.ui_settings = plugin.ui_settings
        self.api = parent.current_db.new_api
        self.worker = worker
        self.ebook = ebook
        self.alert = AlertMessage(self)
        self.footer = Footer()

        self.config = get_config()
        self.current_engine = get_engine_class()

        self.cache_id = None
        self.chapters_meta = []
        self.chapter_items = {}   # index -> QListWidgetItem
        self.status_by_chapter = {}
        self.output_ready = False
        # Whether a worker is busy, and whether the window is waiting
        # for it to stop so it can close. See ``done``.
        self.preparing = True
        self.translating = False
        self.closing = False

        # One pair of threads per window. As class attributes they were
        # shared by every book open at once and never stopped, so the
        # work went on after the window was gone and calibre aborted on
        # exit with a thread still running.
        self.prep_thread = QThread()
        self.trans_thread = QThread()

        self.prep_worker = NovelPreparationWorker(
            self.current_engine, self.ebook)
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

        # Info line.
        info_row = QHBoxLayout()
        info_row.addWidget(QLabel('%s: %s' % (_('Engine'),
                                              self.current_engine.name)))
        info_row.addWidget(QLabel('%s: %s' % (_('Target'),
                                              self.ebook.target_lang)))
        info_row.addStretch(1)
        right_layout.addLayout(info_row)

        # Progress bar + status label.
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        right_layout.addWidget(self.progress_bar)
        self.progress_label = QLabel(_('Ready.'))
        right_layout.addWidget(self.progress_label)

        # Tabs.
        self.tabs = QTabWidget()

        # Author brief tab, first: it is what the book is translated
        # by. Read-only like the glossary: the worker writes it once,
        # before the first chapter, and reads it back on every request.
        self.style_view = QPlainTextEdit()
        self.style_view.setReadOnly(True)
        self.style_view.setPlaceholderText(_(
            'The brief on how this book is written appears here once the '
            'translation has started, or a note on why there is none. '
            'How it is obtained is set in the plugin settings ("How the '
            'author writes").'))
        self.tabs.addTab(self.style_view, _('Author'))

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
        # Read-only on purpose. The table is redrawn from scratch every
        # time a chapter completes, and the worker thread keeps its own
        # copy of the glossary that it writes back after each chapter:
        # an edit typed here during a run was overwritten without a word.
        self.glossary_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        glossary_actions = QHBoxLayout()
        reset_glossary_btn = QPushButton(_('Reset context'))
        reset_glossary_btn.clicked.connect(self._reset_context)
        glossary_actions.addWidget(reset_glossary_btn)
        glossary_actions.addStretch(1)
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
        self.close_button = QPushButton(_('Close'))
        self.close_button.clicked.connect(lambda: self.done(0))
        btn_row.addWidget(self.start_button)
        btn_row.addWidget(self.cancel_button)
        btn_row.addStretch(1)
        btn_row.addWidget(self.output_button)
        btn_row.addWidget(self.close_button)
        outer.addLayout(btn_row)

        # In-memory glossary accumulator. Updated incrementally from the
        # chapter_done signal so the UI never needs to read back from SQLite
        # (which can miss in-flight transactions from the worker thread).
        self._ui_glossary = {}

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
            # book before the first chapter.
            style = cache.get_info(INFO_NOVEL_STYLE)
            if not isinstance(style, str) or not style:
                # No brief: say why, rather than show an empty tab.
                style = cache.get_info(INFO_NOVEL_STYLE_NOTE)
            self.style_view.setPlainText(
                style if isinstance(style, str) else '')

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

    # -- controls ----------------------------------------------------------

    def _all_chapters_done(self):
        return bool(self.chapters_meta) and all(
            self.status_by_chapter.get(m['index']) == self.STATUS_DONE
            for m in self.chapters_meta)

    def _on_start(self):
        # The engine may have been changed in the settings meanwhile.
        self.current_engine = get_engine_class()
        retranslate = False
        if self._all_chapters_done():
            # "Re-run all". The button used to just start the worker,
            # which found every chapter done and stopped at once.
            action = self.alert.ask(_(
                'Every chapter is done. Translate the whole book again?\n\n'
                'The summaries, the glossary and the author brief are '
                'kept; every paragraph is sent to the model again.'))
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
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.output_button.setEnabled(False)
        self.translating = True
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
                self._ui_glossary[source] = {
                    'translation': translation,
                    'type': (item.get('type') or '').strip(),
                    'notes': (item.get('notes') or '').strip(),
                }
        self._refresh_context_views()

    def _on_worker_finished(self, success, message):
        self.translating = False
        if self.closing:
            self.done(0)
            return
        self._refresh_report_from_cache()
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
            self.ebook.set_output_format(
                self.ebook.output_format or 'epub')
            self.worker.translate_ebook(self.ebook, cache_only=True)
            self.alert.pop(_(
                'Building translated ebook in background. Watch the '
                'jobs panel for progress.'))
        except Exception as e:
            self.alert.pop(
                _('Failed to launch build job: {}').format(e), 'error')