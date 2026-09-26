"""The text of a book beside its translations, to read and correct.

``TranslationText`` shows, paragraph by paragraph, the original and one
or more translations of the same book. With one translation it is the
Text tab of the translation window; with several it is the comparison
window, ``CompareTranslations``, where a paragraph can be corrected in
any of them and copied from one into another.

The translations are lined up on the first one: its chapters are the
chapters shown, and a paragraph of another translation is the one with
the same text at the same place among its equals (see
``lib.book_translations.alignment_keys``).
"""
import json

from qt.core import (  # type: ignore
    Qt, QDialog, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPlainTextEdit, QPushButton, QSplitter, QLabel, QLineEdit, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QListWidget, QListWidgetItem, QColor, QBrush, QMenu, QToolButton,
    QApplication, QEvent, QObject, QTimer, QPalette)
from calibre.utils.localization import _  # type: ignore

from .lib.cache import get_cache
from .lib.novel import INFO_NOVEL_CHAPTERS
from .lib.book_translations import (
    alignment_keys, compare_labels, edited_paragraphs, save_translation)
from .components import (
    AlertMessage, NEGATIVE, heading, secondary, with_icon, tidy)


load_translations()  # type: ignore


class EnterFilter(QObject):
    """Keep Enter in a field from the dialog, which would press its
    default button with it, and run ``action`` instead, if any."""

    def __init__(self, action, parent):
        QObject.__init__(self, parent)
        self.action = action

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress and event.key() in (
                Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.action is not None:
                self.action()
            return True
        return False


class TranslationText(QWidget):
    """The paragraphs of a chapter, or those a search finds in the whole
    book, with the original and every translation side by side, and an
    editor below for the one selected.

    A translation cannot be corrected while a run writes it: the run
    writes the same paragraphs, and a resume would not know to keep a
    correction it had not read.
    """

    # The section that stands for what no chapter holds: metadata,
    # table of contents, front matter.
    AUX_SECTION = -1
    # At most this many paragraphs found in the whole book are shown:
    # beyond that the search has to be narrowed, not scrolled.
    MAX_ROWS = 1000

    def __init__(self, sources, chapters_meta, alert, busy=None,
                 parent=None):
        """
        :sources: ``(cache_id, label)`` for each translation, the first
            one being the translation the chapters are taken from.
        :chapters_meta: the chapters of the first translation, as the
            preparation stored them.
        :busy: callable(cache_id) -> bool, whether a run is writing that
            translation now.
        """
        QWidget.__init__(self, parent)
        self.sources = list(sources)
        self.chapters_meta = chapters_meta or []
        self.alert = alert
        self.busy = busy or (lambda cache_id: False)
        self.locked = False

        self._section = None
        self._scope = 'chapter'   # or 'book': what the table shows
        self._needle = ''         # the text searched for, '' for none
        self._data = None         # what was read from the caches
        self._rows = []           # (section, position, base, [paragraph])
        self._current = None      # the row in the editor

        layout = QVBoxLayout(self)
        layout.addLayout(self._layout_search())
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._layout_table())
        splitter.addWidget(self._layout_editor())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        self._set_editor(None)

    # -- layout --------------------------------------------------------------

    def _layout_search(self):
        # The search runs when asked, with the button or Enter, never
        # while typing or on changing the scope: a whole book is
        # thousands of paragraphs to read and lay out.
        top = QHBoxLayout()
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText(_(
            'Find in the original or in the translations'
            if len(self.sources) > 1
            else 'Find in the original or in the translation'))
        self.search_field.setClearButtonEnabled(True)
        self.search_field.installEventFilter(
            EnterFilter(self.search, self))
        self.search_scope = QComboBox()
        self.search_scope.addItem(_('This chapter'), 'chapter')
        self.search_scope.addItem(_('Whole book'), 'book')
        search = with_icon(QPushButton(_('Search')), 'search')
        search.setAutoDefault(False)
        search.clicked.connect(self.search)
        self.count_label = secondary(QLabel())
        top.addWidget(self.search_field, 1)
        top.addWidget(self.search_scope)
        top.addWidget(search)
        top.addWidget(self.count_label)
        return top

    def _layout_table(self):
        columns = ['#', _('Original')] + [
            label for _cache_id, label in self.sources]
        self.table = QTableWidget(0, len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        # Sized once after the rows are in: ResizeToContents measures
        # the whole column again at every row added.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        for column in range(1, len(columns)):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch)
        self.table.currentCellChanged.connect(self._edit_row)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        return self.table

    def _layout_editor(self):
        editor = QWidget()
        layout = QGridLayout(editor)
        panes = QSplitter(Qt.Orientation.Horizontal)
        panes.setChildrenCollapsible(False)

        original = QWidget()
        original_layout = QVBoxLayout(original)
        original_layout.addWidget(heading(_('Original')))
        self.original_edit = QPlainTextEdit()
        self.original_edit.setReadOnly(True)
        original_layout.addWidget(self.original_edit, 1)
        panes.addWidget(original)

        self.editors = []
        self.reverts = []
        for index, (_cache_id, label) in enumerate(self.sources):
            pane = QWidget()
            pane_layout = QVBoxLayout(pane)
            title = heading(label if len(self.sources) > 1
                            else _('Translation'))
            pane_layout.addWidget(title)
            edit = QPlainTextEdit()
            edit.textChanged.connect(self._update_state)
            pane_layout.addWidget(edit, 1)
            buttons = QHBoxLayout()
            copy = with_icon(QToolButton(), 'edit-copy')
            copy.setText(_('Copy'))
            copy.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            copy.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            copy.setMenu(self._copy_menu(index, copy))
            revert = with_icon(QPushButton(_('Revert')), 'edit-undo')
            revert.setAutoDefault(False)
            revert.clicked.connect(
                lambda checked=False, index=index: self._revert(index))
            buttons.addWidget(copy)
            buttons.addStretch(1)
            buttons.addWidget(revert)
            pane_layout.addLayout(buttons)
            panes.addWidget(pane)
            self.editors.append(edit)
            self.reverts.append(revert)
        self._edit_hint = _(
            'Select a paragraph above to read it here and correct its '
            'translation.')
        layout.addWidget(panes, 0, 0, 1, 2)

        self.note = secondary(QLabel())
        self.note.setWordWrap(True)
        self.save_button = with_icon(QPushButton(
            _('Save corrections') if len(self.sources) > 1
            else _('Save correction')), 'save')
        self.save_button.setAutoDefault(False)
        self.save_button.setToolTip(_(
            'Write what is changed into the cache of each translation. A '
            'book is built from its cache: build it again to have the '
            'correction in it. A correction survives a resume; '
            'translating the whole book again replaces it, and says so '
            'before it does.'))
        self.save_button.clicked.connect(self.save)
        layout.addWidget(self.note, 1, 0)
        layout.addWidget(self.save_button, 1, 1)
        layout.setColumnStretch(0, 1)
        return editor

    def _copy_menu(self, index, parent):
        """Copy the text of translation ``index`` to the clipboard, or
        into the editor of another translation, where it is saved with
        the rest."""
        menu = QMenu(parent)
        menu.addAction(_('To the clipboard'), lambda: QApplication.clipboard()
                       .setText(self.editors[index].toPlainText()))
        if len(self.sources) > 1:
            menu.addSeparator()
            for other, (_cache_id, label) in enumerate(self.sources):
                if other != index:
                    menu.addAction(
                        _('Into {}').format(label),
                        lambda other=other: self._copy_into(index, other))
        return menu

    def _copy_into(self, source, target):
        if self.editors[target].isReadOnly():
            self.alert.pop(_(
                'That translation cannot be changed now: see the note '
                'below the editors.'), 'warning')
            return
        self.editors[target].setPlainText(self.editors[source].toPlainText())

    def _table_menu(self, position):
        row = self.table.rowAt(position.y())
        if not 0 <= row < len(self._rows):
            return
        _section, _position, base, paragraphs = self._rows[row]
        menu = QMenu(self.table)
        clipboard = QApplication.clipboard()
        menu.addAction(_('Copy the original'),
                       lambda: clipboard.setText(base.original or ''))
        for (_cache_id, label), paragraph in zip(self.sources, paragraphs):
            if paragraph is None:
                continue
            text = paragraph.translation or ''
            menu.addAction(
                _('Copy the translation') if len(self.sources) == 1
                else _('Copy {}').format(label),
                lambda text=text: clipboard.setText(text))
        menu.exec(self.table.viewport().mapToGlobal(position))

    # -- what is shown -------------------------------------------------------

    def show_section(self, section):
        """Show the whole of chapter ``section`` (or AUX_SECTION).

        Called on a click as well as on a change of the chapter list,
        so that a click on the chapter already selected brings it back
        after a search; one that is already on show is left alone.
        Returns False when the user chose to stay on an unsaved
        correction, and nothing changed.
        """
        if section == self._section and self._scope == 'chapter' \
                and not self._needle and self._data is not None:
            return True
        if not self.confirm_discard():
            return False
        self._section = section
        self.search_scope.setCurrentIndex(0)
        self._scope = 'chapter'
        self._needle = ''
        self._show()
        return True

    def bind_chapter_list(self, chapters):
        """Show the chapter chosen in the QListWidget ``chapters``, whose
        items carry their section in Qt.UserRole; put the choice back
        when the user stays on an unsaved correction."""
        def changed(current, previous):
            if current is None:
                return
            if not self.show_section(current.data(Qt.UserRole)) \
                    and previous is not None:
                chapters.blockSignals(True)
                chapters.setCurrentItem(previous)
                chapters.blockSignals(False)

        def clicked(item):
            # A click on the chapter already selected brings it back
            # after a search. One on another chapter was just handled
            # by ``changed``, or refused there.
            if item is chapters.currentItem():
                self.show_section(item.data(Qt.UserRole))
        chapters.currentItemChanged.connect(changed)
        chapters.itemClicked.connect(clicked)

    def search(self):
        """Search the chapter or the whole book, as the scope says. The
        whole book is only ever searched, never listed whole."""
        scope = self.search_scope.currentData()
        needle = self.search_field.text().strip()
        if scope == 'book' and not needle:
            self.count_label.setText(_(
                'Type what to look for in the whole book.'))
            return
        if not self.confirm_discard():
            return
        self._scope = scope
        self._needle = needle
        self._show()

    def reload(self):
        """Read the caches again and show what was shown."""
        if not self.confirm_discard():
            return
        self._data = None
        self._show()

    def chapter_changed(self, section):
        """A run has just written chapter ``section``: what was read is
        stale, and read again at once when that chapter is on show."""
        self._data = None
        if self._scope == 'chapter' and self._section == section:
            self._show()

    def set_locked(self, locked):
        """No correction at all: a run is writing the translation."""
        self.locked = bool(locked)
        self._update_state()

    def refresh_state(self):
        """Look again at which translations a run is writing."""
        self._update_state()

    def _load(self):
        """Read every translation, and line their paragraphs up on the
        first one's."""
        translations = []
        for cache_id, _label in self.sources:
            cache = get_cache(cache_id)
            try:
                paragraphs = cache.texts()
                edited = edited_paragraphs(cache)
            finally:
                cache.close()
            keys = alignment_keys(paragraphs)
            by_key = {keys[p.id]: p for p in paragraphs}
            translations.append((paragraphs, keys, by_key, edited))
        base = translations[0][0]
        base_keys = translations[0][1]
        self._data = {
            'base': {p.id: p for p in base},
            'keys': base_keys,
            'lookups': [by_key for _p, _k, by_key, _e in translations],
            'edited': [edited for _p, _k, _b, edited in translations],
        }

    def _sections(self, section=None):
        """The paragraphs of the first translation by section, in
        reading order: what no chapter holds first, as the pipeline
        translates it, then the chapters. Only ``section`` when given."""
        by_id = self._data['base']
        sections = []
        placed = set()
        for meta in self.chapters_meta:
            ids = meta.get('paragraph_ids') or []
            placed.update(ids)
            if section is None or meta['index'] == section:
                sections.append((meta['index'], [
                    by_id[pid] for pid in ids if pid in by_id]))
        if section is None or section == self.AUX_SECTION:
            aux = [p for pid, p in sorted(by_id.items())
                   if pid not in placed]
            sections.insert(0, (self.AUX_SECTION, aux))
        return sections

    @staticmethod
    def _cell(text):
        """One line of ``text`` for a table cell; the editor below shows
        it whole."""
        line = ' '.join((text or '').split())
        return line if len(line) <= 300 else line[:300] + '…'

    def _show(self):
        if self._data is None:
            self._load()
        whole = self._scope == 'book'
        needle = self._needle.casefold()
        keys = self._data['keys']
        lookups = self._data['lookups']
        rows = []
        for index, paragraphs in self._sections(
                None if whole else self._section):
            for position, base in enumerate(paragraphs, start=1):
                others = [lookup.get(keys[base.id]) for lookup in lookups]
                if needle and needle not in (base.original or '').casefold() \
                        and not any(
                            needle in (p.translation or '').casefold()
                            for p in others if p is not None):
                    continue
                rows.append((index, position, base, others))
        total = len(rows)
        if whole:
            rows = rows[:self.MAX_ROWS]
        self._rows = rows
        self._set_editor(None)
        table = self.table
        table.blockSignals(True)
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(0)
            table.setRowCount(len(rows))
            for row in range(len(rows)):
                self._fill_row(row)
            table.resizeColumnToContents(0)
        finally:
            table.setUpdatesEnabled(True)
            table.blockSignals(False)
        table.clearSelection()
        table.setCurrentCell(-1, -1)
        if total > len(rows):
            count = _('the first {shown} of {total} found: narrow the '
                      'search').format(shown=len(rows), total=total)
        elif needle:
            count = _('{} found').format(total)
        else:
            count = _('{} paragraph(s)').format(total)
        self.count_label.setText(count)

    def _edited_by(self, paragraphs):
        """The labels of the translations whose paragraph among
        ``paragraphs`` was corrected by hand."""
        return [
            label for (_cache_id, label), paragraph, edited in zip(
                self.sources, paragraphs, self._data['edited'])
            if paragraph is not None and paragraph.id in edited]

    def _fill_row(self, row):
        index, position, base, paragraphs = self._rows[row]
        number = '%s.%d' % (max(index, 0), position) \
            if self._scope == 'book' else str(position)
        edited = self._edited_by(paragraphs)
        item = QTableWidgetItem(number + (' ✎' if edited else ''))
        if edited:
            item.setToolTip(_('Corrected by hand: {}').format(
                ', '.join(edited)))
        self.table.setItem(row, 0, item)
        self.table.setItem(row, 1, QTableWidgetItem(self._cell(base.original)))
        for column, paragraph in enumerate(paragraphs, start=2):
            if paragraph is None:
                item = QTableWidgetItem(_('(not in this translation)'))
                item.setForeground(self.table.palette().brush(
                    QPalette.ColorRole.PlaceholderText))
            elif not (paragraph.translation or '').strip():
                item = QTableWidgetItem(_('(not translated yet)'))
                item.setForeground(QBrush(QColor(NEGATIVE)))
            else:
                item = QTableWidgetItem(self._cell(paragraph.translation))
            self.table.setItem(row, column, item)

    # -- the editor ----------------------------------------------------------

    def _edit_row(self, row, column=-1, previous_row=-1, previous_column=-1):
        # Another cell of the same row is the same paragraph: the editor
        # keeps what is typed in it.
        if row == previous_row:
            return
        if not self.confirm_discard():
            self.table.blockSignals(True)
            self.table.setCurrentCell(previous_row, max(previous_column, 0))
            self.table.blockSignals(False)
            return
        self._set_editor(row if 0 <= row < len(self._rows) else None)

    def _set_editor(self, row):
        self._current = row
        paragraphs = self._rows[row][3] if row is not None \
            else [None] * len(self.sources)
        self.original_edit.setPlainText(
            self._rows[row][2].original or '' if row is not None else '')
        for edit, paragraph in zip(self.editors, paragraphs):
            edit.blockSignals(True)
            edit.setPlainText(
                paragraph.translation or '' if paragraph is not None else '')
            edit.blockSignals(False)
        self._update_state()

    def _paragraphs(self):
        return self._rows[self._current][3] if self._current is not None \
            else [None] * len(self.sources)

    def _changed(self):
        """The indexes of the translations whose editor differs from the
        cache."""
        return [
            index for index, (edit, paragraph) in enumerate(
                zip(self.editors, self._paragraphs()))
            if paragraph is not None
            and edit.toPlainText() != (paragraph.translation or '')]

    def _dirty(self):
        return bool(self._changed())

    def _writable(self, index):
        paragraph = self._paragraphs()[index]
        return paragraph is not None and not self.locked \
            and not self.busy(self.sources[index][0])

    def _update_state(self):
        paragraphs = self._paragraphs()
        changed = self._changed()
        running = []
        for index, (edit, revert) in enumerate(
                zip(self.editors, self.reverts)):
            edit.setReadOnly(not self._writable(index))
            revert.setEnabled(index in changed)
            if self._current is None:
                edit.setPlaceholderText(self._edit_hint if index == 0 else '')
            elif paragraphs[index] is None:
                edit.setPlaceholderText(_('(not in this translation)'))
            else:
                edit.setPlaceholderText(_('(not translated yet)'))
            if self._current is not None and self.busy(self.sources[index][0]):
                running.append(self.sources[index][1])
        self.save_button.setEnabled(
            any(self._writable(index) for index in changed))
        if self._current is None:
            note = ''
        elif self.locked:
            note = _('Corrections wait for the run to end: it writes the '
                     'same paragraphs.')
        elif running:
            note = _('A run is writing {}: it cannot be corrected until '
                     'it ends.').format(', '.join(running))
        elif changed:
            note = _('Changed, not saved.')
        else:
            edited = self._edited_by(paragraphs)
            note = _('Corrected by hand.') if edited and len(
                self.sources) == 1 else (
                    _('Corrected by hand: {}.').format(', '.join(edited))
                    if edited else '')
        self.note.setText(note)

    def _revert(self, index):
        paragraph = self._paragraphs()[index]
        if paragraph is not None:
            self.editors[index].setPlainText(paragraph.translation or '')

    def save(self):
        """Write every changed translation of the paragraph in the
        editor into its cache."""
        if self._current is None:
            return
        paragraphs = self._paragraphs()
        skipped = []
        for index in self._changed():
            label = self.sources[index][1]
            if not self._writable(index):
                skipped.append(label)
                continue
            paragraph = paragraphs[index]
            text = self.editors[index].toPlainText()
            cache = get_cache(self.sources[index][0])
            try:
                # Another window may have saved this paragraph since it
                # was read here: it is not overwritten unasked.
                stored = cache.paragraph(paragraph.id).translation
                if (stored or '') != (paragraph.translation or '') \
                        and self.alert.ask(_(
                            'The translation of this paragraph in {} was '
                            'changed elsewhere since it was shown here. '
                            'Replace it with yours?').format(label)) != 'yes':
                    paragraph.translation = stored
                    continue
                save_translation(cache, paragraph.id, text)
            except Exception as e:
                self.alert.pop(_(
                    'Could not save the correction to {}: {}').format(
                        self.sources[index][1], e), 'error')
                continue
            finally:
                cache.close()
            paragraph.translation = text
            self._data['edited'][index].add(paragraph.id)
        self._fill_row(self._current)
        self._update_state()
        if skipped:
            self.alert.pop(_(
                'Not saved, because a run is writing it now: {}.').format(
                    ', '.join(skipped)), 'warning')

    def confirm_discard(self):
        """Whether what the editor shows may go: nothing unsaved in it,
        or the user saved it or dropped it. False when the user chose to
        stay, and the caller must leave everything as it is."""
        if not self._dirty():
            return True
        action = self.alert.ask_save(_(
            'The paragraph you were editing has corrections that are not '
            'saved.'))
        if action == 'save':
            self.save()
            return True
        if action == 'discard':
            self._set_editor(self._current)
            return True
        return False


class CompareTranslations(QDialog):
    """Two or more translations of one book side by side, chapter by
    chapter, with the original: to read them against each other, correct
    any of them, and copy a paragraph from one into another."""

    def __init__(self, parent, cache_ids, busy=None):
        QDialog.__init__(self, parent)
        self.alert = AlertMessage(self)
        self.cache_ids = list(cache_ids)
        infos = []
        for cache_id in self.cache_ids:
            cache = get_cache(cache_id)
            try:
                infos.append(cache.all_info())
            finally:
                cache.close()
        # The chapters come from a translation that has them: one
        # created and never opened has not been prepared yet.
        order = sorted(range(len(infos)), key=lambda i: not infos[i].get(
            INFO_NOVEL_CHAPTERS))
        self.cache_ids = [self.cache_ids[i] for i in order]
        infos = [infos[i] for i in order]
        try:
            chapters = json.loads(infos[0].get(INFO_NOVEL_CHAPTERS) or '[]')
        except (TypeError, ValueError):
            chapters = []
        self.title = infos[0].get('title') or ''

        labels = compare_labels(infos)
        self.text = TranslationText(
            list(zip(self.cache_ids, labels)), chapters, self.alert, busy)

        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(heading(_('Chapters')))
        self.chapter_list = QListWidget()
        self.chapter_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.chapter_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        aux = QListWidgetItem(_('Metadata, contents and front matter'))
        aux.setData(Qt.UserRole, TranslationText.AUX_SECTION)
        aux.setForeground(self.chapter_list.palette().brush(
            QPalette.ColorRole.PlaceholderText))
        aux.setToolTip(aux.text())
        self.chapter_list.addItem(aux)
        for meta in chapters:
            item = QListWidgetItem('%d. %s' % (
                meta['index'], meta.get('title') or '?'))
            item.setData(Qt.UserRole, meta['index'])
            item.setToolTip(meta.get('title') or '')
            self.chapter_list.addItem(item)
        self.text.bind_chapter_list(self.chapter_list)
        left_layout.addWidget(self.chapter_list, 1)
        splitter.addWidget(left)
        splitter.addWidget(self.text)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([220, 780])
        layout.addWidget(splitter, 1)

        buttons = QHBoxLayout()
        title = secondary(QLabel(self.title))
        reload_button = with_icon(QPushButton(_('Reload')), 'view-refresh')
        reload_button.setAutoDefault(False)
        reload_button.setToolTip(_(
            'Read the translations again from the cache: a run may have '
            'translated more of them since this window opened.'))
        reload_button.clicked.connect(self.text.reload)
        close_button = with_icon(QPushButton(_('Close')), 'window-close')
        close_button.setAutoDefault(False)
        close_button.clicked.connect(lambda: self.done(0))
        buttons.addWidget(title, 1)
        buttons.addWidget(reload_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        tidy(self)
        self.chapter_list.setCurrentRow(
            1 if self.chapter_list.count() > 1 else 0)

        # A run started or ended in another window changes which
        # translations can be corrected here.
        self.busy_timer = QTimer(self)
        self.busy_timer.setInterval(1000)
        self.busy_timer.timeout.connect(self.text.refresh_state)
        self.busy_timer.start()

    def done(self, result):
        if not self.text.confirm_discard():
            return
        self.busy_timer.stop()
        QDialog.done(self, result)
