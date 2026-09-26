"""How the windows of the plugin look, in one place.

Modelled on the KDE Human Interface Guidelines and kept compact: one
margin around the content of a window, a tab page or a section, one
spacing between the widgets of a layout, and nothing added by the
containers in between; headings instead of frames inside frames; the
secondary text in the colour the palette gives it, so that it follows a
light or a dark theme; messages in the manner of KMessageWidget; icons
on the buttons, from calibre's own set so they match calibre.

``tidy`` applies the margins and the spacing to a whole window at once,
after it is built: the layouts themselves say only what they hold.
"""
from qt.core import (  # type: ignore
    Qt, QWidget, QFrame, QLabel, QHBoxLayout, QLayout, QBoxLayout,
    QGridLayout, QFormLayout, QGroupBox, QTabWidget, QStackedWidget,
    QScrollArea, QAbstractItemView, QTableView, QTableWidget, QPalette,
    QIcon, QPainter, QFont, QFontMetrics, QStyle, QColor)


# The units of the layout: around the content of a window, a tab page or
# a section; between the widgets of a layout; between a heading or a
# field and what goes with it. Breeze uses 6 between widgets; the margins
# are kept at 8 rather than its 10, for a compact window.
MARGIN = 8
SPACING = 6
TIGHT = 4

# The semantic colours of Breeze, readable on a light and a dark
# background alike.
POSITIVE = '#27ae60'
NEUTRAL = '#f67400'
NEGATIVE = '#da4453'
ACTIVE = '#3daee9'


def icon(name):
    """A calibre icon (``name`` without the extension), which follows
    calibre's light or dark theme; an empty icon when there is none."""
    try:
        return QIcon.ic(name + '.png')
    except Exception:
        return QIcon()


def with_icon(button, name):
    button.setIcon(icon(name))
    return button


def secondary(label):
    """Show ``label`` in the colour of secondary text: hints, notes,
    figures that go with a field."""
    palette = label.palette()
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        palette.setColor(
            group, QPalette.ColorRole.WindowText,
            palette.color(group, QPalette.ColorRole.PlaceholderText))
    label.setPalette(palette)
    return label


def heading(text=''):
    """A bold label heading a list or a pane."""
    label = QLabel(text)
    font = QFont(label.font())
    font.setBold(True)
    label.setFont(font)
    return label


class Section(QWidget):
    """A titled part of a window: a bold heading over its content, with
    no frame around it. It takes the place of a QGroupBox -- a layout is
    set on it the same way -- because frames nested in a tab page nested
    in a window are what the KDE guidelines ask to avoid."""

    def __init__(self, title='', parent=None):
        QWidget.__init__(self, parent)
        self._title = title
        self._padding = SPACING
        # A painted title is not read out as a group box title is.
        self.setAccessibleName(title)
        self._update_margins()

    def title(self):
        return self._title

    def setTitle(self, title):
        self._title = title
        self.setAccessibleName(title)
        self._update_margins()
        self.update()

    def set_first(self, first):
        """No room above the heading of the first section of a page."""
        self._padding = 0 if first else SPACING
        self._update_margins()

    def _heading_font(self):
        font = QFont(self.font())
        font.setBold(True)
        return font

    def _update_margins(self):
        top = self._padding
        if self._title:
            top += self.fontMetrics().height() + TIGHT
        self.setContentsMargins(0, top, 0, 0)

    def changeEvent(self, event):
        QWidget.changeEvent(self, event)
        self._update_margins()

    def _with_title(self, hint):
        # A group box is never narrower than its title; nor is this.
        if self._title:
            width = QFontMetrics(self._heading_font()).horizontalAdvance(
                self._title)
            hint.setWidth(max(hint.width(), width))
        return hint

    def minimumSizeHint(self):
        return self._with_title(QWidget.minimumSizeHint(self))

    def sizeHint(self):
        return self._with_title(QWidget.sizeHint(self))

    def paintEvent(self, event):
        if not self._title:
            return
        painter = QPainter(self)
        painter.setFont(self._heading_font())
        group = QPalette.ColorGroup.Active if self.isEnabled() \
            else QPalette.ColorGroup.Disabled
        painter.setPen(self.palette().color(
            group, QPalette.ColorRole.WindowText))
        height = self.fontMetrics().height()
        painter.drawText(
            0, self._padding, self.width(), height,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._title)
        painter.end()


class MessageBanner(QFrame):
    """A message inside the window, in the manner of KDE's
    KMessageWidget: a tinted box with an icon, for what the user should
    know without being stopped by a dialog."""

    KINDS = {
        'information': (ACTIVE, 'dialog_information'),
        'positive': (POSITIVE, 'ok'),
        'warning': (NEUTRAL, 'dialog_warning'),
        'error': (NEGATIVE, 'dialog_error'),
    }

    def __init__(self, text='', kind='information', parent=None):
        QFrame.__init__(self, parent)
        self.setObjectName('message_banner')
        self.icon = QLabel()
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACING, TIGHT, SPACING, TIGHT)
        layout.setSpacing(SPACING)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.label, 1)
        self.set_kind(kind)

    def set_kind(self, kind):
        colour, name = self.KINDS.get(kind, self.KINDS['information'])
        size = self.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize)
        self.icon.setPixmap(icon(name).pixmap(size, size))
        rgb = QColor(colour)
        self.setStyleSheet(
            '#message_banner{background-color:rgba(%d,%d,%d,40);'
            'border:1px solid rgba(%d,%d,%d,160);border-radius:4px;}' % (
                rgb.red(), rgb.green(), rgb.blue(),
                rgb.red(), rgb.green(), rgb.blue()))

    def setText(self, text):
        self.label.setText(text)

    def text(self):
        return self.label.text()


def tidy_table(view):
    """A table the way KDE lists read: alternate rows, no grid, no row
    numbers, compact rows."""
    view.setAlternatingRowColors(True)
    view.setShowGrid(False)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(
        view.fontMetrics().height() + 2 * TIGHT)
    view.horizontalHeader().setHighlightSections(False)
    view.horizontalHeader().setDefaultAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return view


def _page_margin(widget):
    """The margin of ``widget`` when it is a page of a tab widget, where
    a window's content starts again, 'scroll' when it is the content of
    a scroll area, None otherwise. The pages of flat tabs (document
    mode) sit flush under the tab bar, like the ones that are a single
    editor."""
    parent = widget.parentWidget()
    if isinstance(parent, QStackedWidget) \
            and isinstance(parent.parentWidget(), QTabWidget):
        return 0 if parent.parentWidget().documentMode() else MARGIN
    area = parent.parentWidget() if parent is not None else None
    if isinstance(area, QScrollArea) and area.widget() is widget:
        return 'scroll'
    return None


def _set_spacing(layout):
    if isinstance(layout, (QGridLayout, QFormLayout)):
        layout.setHorizontalSpacing(SPACING)
        layout.setVerticalSpacing(SPACING)
    else:
        layout.setSpacing(SPACING)
    if isinstance(layout, QFormLayout):
        layout.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)


def tidy(root):
    """Give every layout of the window ``root`` the same margins and
    spacing: MARGIN around a window, a tab page, a scroll area's content
    or a group box, none around any other container, SPACING between
    widgets. The first section of a page loses the room above its
    heading. Layouts of other windows (dialogs parented to this one)
    are left alone."""
    window = root.window()
    layouts = root.findChildren(QLayout)
    # QWidget.layout, not root.layout: some dialogs shadow the method
    # with an attribute or a method of their own.
    own = QWidget.layout(root)
    if own is not None and own not in layouts:
        layouts.append(own)
    for layout in layouts:
        owner = layout.parentWidget()
        if owner is None or owner.window() is not window:
            continue
        # Qt's own parts (the scroll bar containers of every scroll
        # area) and the banners keep their layouts as they are.
        if isinstance(owner, MessageBanner) \
                or owner.objectName().startswith('qt_'):
            continue
        if isinstance(layout, (QBoxLayout, QGridLayout, QFormLayout)):
            _set_spacing(layout)
        if QWidget.layout(owner) is not layout:
            continue  # a layout nested in another: no margins of its own
        margin = _page_margin(owner)
        if margin == 'scroll':
            # The page around the scroll area has the margin already;
            # the content keeps clear of the scroll bar only.
            layout.setContentsMargins(0, 0, SPACING, 0)
        else:
            if margin is None:
                margin = MARGIN if owner.isWindow() \
                    or isinstance(owner, QGroupBox) else 0
            layout.setContentsMargins(margin, margin, margin, margin)
        if isinstance(layout, QBoxLayout):
            # Side by side, sections line their headings up; one above
            # the other, only the first one starts at the top.
            vertical = layout.direction() in (
                QBoxLayout.Direction.TopToBottom,
                QBoxLayout.Direction.BottomToTop)
            first = vertical
            for index in range(layout.count()):
                widget = layout.itemAt(index).widget()
                if isinstance(widget, Section):
                    widget.set_first(first)
                if widget is not None or layout.itemAt(index).layout():
                    first = False
    for view in root.findChildren(QAbstractItemView):
        if isinstance(view, (QTableView, QTableWidget)) \
                and view.window() is window:
            tidy_table(view)
