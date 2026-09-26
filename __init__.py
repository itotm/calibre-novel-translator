import os

from calibre.constants import DEBUG  # type: ignore
from calibre.customize import InterfaceActionBase  # type: ignore
from calibre.utils.localization import _  # type: ignore


__license__ = 'GPL v3'
# Novel Translator is a fork of Ebook Translator, whose code is still
# most of what runs here; the upstream notice stays, as the licence asks.
__copyright__ = ('2023, bookfere.com <bookfere@gmail.com>; '
                 '2026, itotm')
__docformat__ = 'restructuredtext en'

load_translations()  # type: ignore


# To prevent update errors, avoid importing anything from plugin modules.
def _z(message): return message


class NovelTranslatorPlugin(InterfaceActionBase):
    """Translate a novel with a language model, chapter by chapter.

    Development environment requirements for the project:
    - Calibre version: >= 7.0.0
    - Python version: >= 3.11
    """
    name = _z('Novel Translator')
    title = _(name)
    supported_platforms = ['windows', 'osx', 'linux']
    identifier = 'novel-translator'
    author = 'itotm'
    version = (1, 3, 3)
    __version__ = 'v' + '.'.join(map(str, version))
    description = _(
        'Translate a novel with a language model, chapter by chapter, '
        'keeping a running summary and a glossary of names so the whole '
        'book reads consistently. OpenRouter, OpenAI-compatible '
        'providers, Claude and Gemini.')
    # Where the plugin lives. Read by the footer, the About dialog and
    # the OpenRouter attribution headers, so it is stated once.
    homepage = 'https://github.com/itotm/calibre-plugin-ebook-translator'
    # The sources use runtime `X | Y` annotations (Python 3.10) and
    # QFormLayout.setRowVisible (Qt 6.4); calibre 7 ships both.
    minimum_calibre_version = (7, 0, 0)

    actual_plugin = 'calibre_plugins.novel_translator.ui:NovelTranslatorGui'

    # The DEBUG constant cannot be shared with new worker processes.
    # To ensure that it is available, add it to the OS environment.
    if DEBUG:
        os.environ.update(CALIBRE_DEBUG=str(DEBUG))

    def is_customizable(self):
        return False
