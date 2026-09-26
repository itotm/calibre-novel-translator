from qt.core import QWidget, QHBoxLayout, QLabel  # type: ignore
from calibre_plugins.novel_translator import NovelTranslatorPlugin  # type: ignore

from .style import SPACING, secondary


load_translations()  # type: ignore


class Footer(QWidget):

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.status = QLabel()

        homepage = NovelTranslatorPlugin.homepage
        link = QLabel(
            '{0} {1} · <a href="{2}">GitHub</a> · '
            '<a href="{2}/issues">{3}</a>'.format(
                NovelTranslatorPlugin.title,
                NovelTranslatorPlugin.__version__, homepage, _('Feedback')))
        secondary(link)
        link.setOpenExternalLinks(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACING)
        layout.addWidget(self.status)
        layout.addStretch(1)
        layout.addWidget(link)
