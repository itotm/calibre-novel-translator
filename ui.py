import os.path

from qt.core import QMenu, QSettings  # type: ignore
from calibre.gui2 import gprefs  # type: ignore
from calibre.utils.localization import _  # type: ignore
from calibre.gui2.actions import InterfaceAction  # type: ignore
from calibre.utils.config_base import plugin_dir  # type: ignore
from calibre.ebooks.conversion.config import (  # type: ignore
    get_input_format_for_book)
from . import NovelTranslatorPlugin
from .lib.ebook import Ebooks
from .lib.config import get_config
from .lib.conversion import ConversionWorker
from .setting import TranslationSetting
from .cache import CacheManager
from .about import AboutDialog
from .components import AlertMessage
from .lib.utils import uid
from .lib.cache import TranslationCache
from .lib.book_translations import BookTranslation, matching_format
from .novel import BookTranslations, NovelTranslation, apply_translation
from .text_view import CompareTranslations


load_translations()  # type: ignore


# The toolbars calibre lets an action sit in, as it stores their layouts.
TOOLBAR_LAYOUTS = ('action-layout-toolbar', 'action-layout-toolbar-device')


def place_on_toolbars(name, layouts):
    """Append ``name`` to every toolbar layout in ``layouts`` that does
    not carry it yet. Returns the layouts that were changed.

    calibre only offers to put a freshly installed plugin on the toolbar
    when the plugin is installed from its own dialog; installed from the
    command line or from a file, the action exists but sits in no
    toolbar until the user finds it under Preferences > Toolbars & menus.
    """
    changed = []
    for key in TOOLBAR_LAYOUTS:
        layout = list(layouts.get(key) or ())
        if name in layout:
            continue
        layout.append(name)
        layouts[key] = tuple(layout)
        changed.append(key)
    return changed


class NovelTranslatorGui(InterfaceAction):
    name = NovelTranslatorPlugin.name
    action_spec = (
        _('Translate Book'), None, _('Translate Ebook Content'), None)
    title = '%s - %s' % (NovelTranslatorPlugin.title, NovelTranslatorPlugin.__version__)
    ui_settings = QSettings(os.path.join(
        plugin_dir, NovelTranslatorPlugin.identifier, 'settings.ini'),
        QSettings.Format.IniFormat)

    class Status:
        jobs: dict[object, tuple] = {}
        windows: dict[str, object] = {}

    def genesis(self):
        try:
            self.icon = get_icons('images/icon.png', self.name)  # type: ignore
        except Exception:
            self.icon = get_icons('images/icon.png')  # type: ignore

        # On the toolbar the first time the plugin runs, and only then:
        # a user who takes the button off afterwards is not overruled.
        # genesis runs before calibre builds its toolbars, so the layout
        # written here is the one they are built from.
        config = get_config()
        if not config.get('toolbar_placed'):
            place_on_toolbars(self.name, gprefs)
            config.save(toolbar_placed=True)

        menu = QMenu(self.gui)
        menu.addAction(_('Translate'), self.show_novel_translation)
        menu.addSeparator()
        menu.addAction(_('Cache'), self.show_cache)
        menu.addSeparator()
        menu.addAction(_('Setting'), self.show_setting)
        menu.addAction(_('About'), self.show_about)

        self.qaction.setMenu(menu)
        self.qaction.setIcon(self.icon)
        self.qaction.triggered.connect(self.show_novel_translation)

        self.alert = AlertMessage(self.gui)

        if not getattr(self.gui, 'novel_translator', False):
            self.gui.novel_translator = self.Status()

    def novel_translation_window(self, ebook, cache_id):
        # One window per translation: two translations of the same book
        # can be open side by side, to compare them.
        name = 'novel_' + cache_id
        if self.show_window(name):
            return
        worker = ConversionWorker(self.gui, self.icon)
        window = NovelTranslation(
            self, self.gui, worker, ebook, cache_id, self.library_id())
        window.setMinimumWidth(1000)
        window.setMinimumHeight(640)
        window.setWindowTitle(self.title)
        window.setWindowIcon(self.icon)
        window.show()
        self.add_window(name, window)

    def show_novel_translation(self):
        ebooks = self.get_selected_ebooks()
        if len(ebooks) < 1:
            return self.alert.pop(
                _('Please choose a book.'), 'warning')
        if len(ebooks) > 1:
            return self.alert.pop(
                _('One book at a time: please select a single book.'),
                'warning')
        window = BookTranslations(
            self.gui, ebooks.first(), self.library_id(),
            is_open=self.cache_in_use)
        window.open_translation.connect(self.novel_translation_window)
        window.compare_translations.connect(self.compare_window)
        window.setModal(True)
        window.setMinimumWidth(760)
        window.setMinimumHeight(520)
        window.setWindowTitle(self.title)
        window.show()

    def compare_window(self, cache_ids):
        """Open the comparison of the translations ``cache_ids``."""
        name = 'compare_' + uid(*sorted(cache_ids))
        if self.show_window(name):
            return
        window = CompareTranslations(
            self.gui, cache_ids, busy=self.translation_running)
        window.setMinimumWidth(1000)
        window.setMinimumHeight(640)
        window.setWindowTitle('%s - %s' % (_('Compare'), self.title))
        window.setWindowIcon(self.icon)
        window.show()
        self.add_window(name, window)

    def translation_running(self, cache_id):
        """Whether a run is writing the translation ``cache_id`` now."""
        window = self.get_window('novel_' + cache_id)
        return bool(window is not None and getattr(
            window, 'translating', False))

    def cache_in_use(self, cache_id):
        """Whether a window shows the translation ``cache_id``: its own,
        or a comparison. It is not deleted from under it."""
        for name, window in self.gui.novel_translator.windows.items():
            if name == 'novel_' + cache_id or (
                    name.startswith('compare_')
                    and cache_id in getattr(window, 'cache_ids', ())):
                return True
        return False

    def library_id(self):
        """The id of the library open in calibre: book ids are only
        unique within one."""
        try:
            return self.gui.current_db.new_api.library_id
        except Exception:
            return None

    def show_setting(self):
        if self.has_running_jobs():
            self.alert.pop(_(
                'Cannot change setting while book(s) are under translation.'),
                'warning')
            return
        if self.show_window('setting'):
            return
        window = TranslationSetting(self, self.gui, self.icon)
        window.setModal(True)
        window.setMinimumWidth(600)
        window.setMinimumHeight(520)
        window.setWindowTitle('%s - %s' % (_('Setting'), self.title))
        window.setWindowIcon(self.icon)
        window.show()
        self.add_window('setting', window)

    def show_cache(self):
        if self.has_running_jobs():
            self.alert.pop(_(
                'Cannot manage cache while book(s) are under translation.'),
                'warning')
            return
        if self.show_window('cache'):
            return
        window = CacheManager(self, self.gui)
        window.setModal(True)
        window.setMinimumWidth(800)
        window.setMinimumHeight(620)
        window.setWindowTitle('%s - %s' % (_('Cache Manager'), self.title))
        window.setWindowIcon(self.icon)
        window.show()
        self.add_window('cache', window)

    def show_about(self):
        if self.show_window('about'):
            return
        window = AboutDialog(self, self.gui, self.icon)
        window.setMinimumWidth(600)
        window.setMinimumHeight(520)
        window.setWindowTitle('%s - %s' % (_('About'), self.title))
        window.setWindowIcon(self.icon)
        window.show()
        self.add_window('about', window)

    def add_window(self, name, window):
        identifier = name.split('_')[0]

        window_size = 'window_size/%s' % identifier
        size = self.ui_settings.value(window_size)
        if size:
            window.resize(size)

        window_position = 'window_position/%s' % identifier
        position = self.ui_settings.value(window_position)
        if position:
            window.restoreGeometry(position)

        windows = self.gui.novel_translator.windows
        windows[name] = window

        def setup_window():
            self.ui_settings.setValue(window_size, window.size())
            self.ui_settings.setValue(window_position, window.saveGeometry())
            windows.pop(name)
        window.finished.connect(setup_window)

    def get_window(self, name):
        return self.gui.novel_translator.windows.get(name)

    def show_window(self, name):
        window = self.get_window(name)
        if not window:
            return False
        window.raise_()
        return True

    def has_running_jobs(self):
        jobs = self.gui.novel_translator.jobs
        if len(jobs) > 0:
            return True
        windows = self.gui.novel_translator.windows
        for name in windows:
            if name.startswith('novel_'):
                return True
        return False

    def get_selected_ebooks(self):
        ebooks = Ebooks()
        rows = self.gui.library_view.selectionModel().selectedRows()
        model = self.gui.library_view.model()
        for row in rows:
            self.add_ebook(ebooks, model.id(row))
        return ebooks

    def add_ebook(self, ebooks, book_id):
        db = self.gui.current_db
        api = db.new_api
        book_metadata = api.get_proxy_metadata(book_id)
        fmt, fmts = get_input_format_for_book(db, book_id, 'epub')
        ebooks.add(
            book_id,  # Book ID in db
            api.field_for('title', book_id),  # Title
            # Format and path
            dict(zip(
                map(lambda fmt: fmt.lower(), fmts),
                map(lambda fmt: api.format_abspath(book_id, fmt), fmts),
            )),
            fmt.lower(),  # Input format
            book_metadata.language,  # Source language
            list(book_metadata.authors or []),  # Authors
        )

    def find_cached_translation(self, cache_id):
        """The book of the translation in cache ``cache_id``, set up to
        open it, for the cache manager. Returns ``(ebook, None)``, or
        ``(None, why not)``.

        The book is found by its id in this library, as the cache records
        it; a cache written before 1.3 records only the title, and is
        the book's of that title whose file it was made from.
        """
        cache = TranslationCache(cache_id)
        try:
            info = cache.all_info()
        finally:
            cache.close()
        api = self.gui.current_db.new_api
        library_id = self.library_id()
        book_id = info.get('book_id')
        recorded_library = info.get('library_id')
        if book_id and recorded_library and library_id \
                and recorded_library != str(library_id):
            return None, _(
                'This translation is of a book in another calibre library: '
                'switch to that library to open it.')
        if book_id:
            candidates = [int(book_id)]
        else:
            title = info.get('title')
            if not title:
                return None, _(
                    'This cache does not say which book it translates.')
            titles = api.all_field_for('title', api.all_book_ids())
            candidates = [
                candidate for candidate, value in titles.items()
                if value == title]
        for candidate in candidates:
            if not api.has_id(candidate):
                continue
            ebooks = Ebooks()
            try:
                self.add_ebook(ebooks, candidate)
            except Exception:
                continue
            ebook = ebooks.first()
            fmt = matching_format(info, cache_id, ebook, library_id)
            if fmt is None:
                continue
            error = apply_translation(
                ebook, BookTranslation(cache_id, info, fmt))
            return (None, error) if error else (ebook, None)
        return None, _(
            'The book this translation was made from is no longer in the '
            'library.')
