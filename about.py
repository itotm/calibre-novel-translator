import re
import zipfile

from qt.core import ( # type: ignore
    Qt, QLabel, QDialog, QWidget, QVBoxLayout, QTextBrowser, QTextDocument,
    QFont, QPalette)
from calibre.library.comments import markdown # type: ignore
from calibre.utils.localization import get_lang # type: ignore

from . import NovelTranslatorPlugin
from .components import Footer, MARGIN, secondary, tidy


load_translations() # type: ignore


class AboutDialog(QDialog):
    def __init__(self, plugin, parent, icon):
        QDialog.__init__(self, parent)
        self.plugin = plugin
        self.gui = parent
        self.icon = icon

        layout = QVBoxLayout(self)

        brand = QWidget()
        brand_layout = QVBoxLayout(brand)
        brand_layout.addStretch(1)
        logo = QLabel()
        logo.setPixmap(self.icon.pixmap(80, 80))
        logo.setAlignment(Qt.AlignCenter)
        brand_layout.addWidget(logo)
        # Sizes relative to the font of the desktop, not in pixels.
        name = QLabel(NovelTranslatorPlugin.title)
        font = QFont(name.font())
        if font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() * 1.6)
        else:  # a font set in pixels has no point size
            font.setPixelSize(round(font.pixelSize() * 1.6))
        font.setWeight(QFont.Weight.Light)
        name.setFont(font)
        name.setAlignment(Qt.AlignCenter)
        brand_layout.addWidget(name)
        version = secondary(QLabel(NovelTranslatorPlugin.__version__))
        version.setAlignment(Qt.AlignCenter)
        brand_layout.addWidget(version)
        brand_layout.addStretch(1)

        description = QTextBrowser()
        # Owned by the browser, so the two go together: a document of
        # its own could be freed first when the window is destroyed.
        document = QTextDocument(description)
        document.setDocumentMargin(2 * MARGIN)
        # The code background from the palette: a fixed light grey was a
        # glaring block on a dark theme.
        code = description.palette().color(QPalette.ColorRole.AlternateBase)
        document.setDefaultStyleSheet(
            'h1,h2{font-size:large;}p,body > ul{margin:12px 0;}'
            'ul ul {list-style:circle;}ul ul ul{list-style:square;}'
            'ul,ol{-qt-list-indent:0;margin-left:10px;}li{margin:4px 0;}'
            'ol{margin-left:15px;}pre{background-color:%s;}' % code.name())
        markup = re.sub(r'^!\[.*', '', self.get_readme(), flags=re.M)
        markup = re.sub(r'\|.*\\\*.*?\n\n', '', markup, flags=re.S)
        document.setHtml(markdown(markup))
        description.setDocument(document)
        description.setOpenExternalLinks(True)

        layout.addWidget(brand, 1)
        layout.addWidget(description, 2)
        layout.addWidget(Footer())
        tidy(self)

    def get_readme(self):
        default = 'README.md'
        foreign = default.replace('.', '.%s.' % get_lang().replace('_', '-'))
        resource = self.get_resource(foreign) or self.get_resource(default)
        return '' if resource is None else resource.decode('utf-8')

    def get_resource(self, filename):
        """Replace the built-in get_resources function because it cannot
        prevent reporting to STDERR in the old version..
        """
        with zipfile.ZipFile(self.plugin.plugin_path) as zf:
            try:
                return zf.read(filename)
            except Exception:
                return None
