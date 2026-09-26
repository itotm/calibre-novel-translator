import time
from types import GeneratorType

from qt.core import (  # type: ignore
    Qt, pyqtSignal, pyqtSlot, QDialog, QThread, QGridLayout, QPushButton,
    QPlainTextEdit, QObject, QTextCursor, QLabel, QComboBox)

from ..lib.utils import log, traceback_error
from ..lib.translation import get_translator
from ..engines import builtin_engines
from ..engines.genai import GenAI

from .lang import SourceLang, TargetLang
from .style import secondary, with_icon, tidy


load_translations()  # type: ignore


class EngineList(QComboBox):
    def __init__(self, default=None):
        QComboBox.__init__(self)
        self.default = default
        self.wheelEvent = lambda event: None
        self.refresh()

    def layout(self):
        for engine in builtin_engines:
            self.addItem(_(engine.alias), engine.name)
        if self.default:
            self.setCurrentIndex(max(0, self.findData(self.default)))

    def refresh(self):
        self.clear()
        self.layout()


class ModelWorker(QObject):
    """Fetch the model listing of an engine off the main thread. The
    listing lands on the engine class (``models``, and ``model_details``
    where the provider publishes them)."""
    start = pyqtSignal(object)
    success = pyqtSignal(bool, str)
    finished = pyqtSignal()

    def __init__(self):
        QObject.__init__(self)
        self.log = log
        self.start.connect(self.get_models)

    @pyqtSlot(object)
    def get_models(self, engine_class):
        try:
            engine = get_translator(engine_class)
            if not isinstance(engine, GenAI):
                raise Exception(f'{engine.__class__} is not a GenAI instance.')
            engine_class.models = engine.get_models()
            self.success.emit(True, '')
        except Exception:
            error = traceback_error()
            self.log.error('Failed to fetch models: %s' % error)
            self.success.emit(False, error)
        self.finished.emit()


class FlexWorker(QObject):
    """Find out off the main thread whether a model has a flex
    endpoint (see ``OpenRouterTranslate.model_has_flex``)."""
    start = pyqtSignal(object, str)
    checked = pyqtSignal(str, object)   # model, True/False/None

    def __init__(self):
        QObject.__init__(self)
        self.start.connect(self.check)

    @pyqtSlot(object, str)
    def check(self, engine_class, model):
        found = None
        try:
            has_flex = getattr(get_translator(engine_class),
                               'model_has_flex', None)
            if callable(has_flex):
                found = has_flex(model)
        except Exception:
            log.error('Failed to look for a flex endpoint: %s'
                      % traceback_error())
        self.checked.emit(model, found)


class EngineWorker(QObject):
    clear = pyqtSignal()
    translate = pyqtSignal(str)
    result = pyqtSignal(str)
    complete = pyqtSignal()
    check = pyqtSignal()
    usage = pyqtSignal(object)

    def __init__(self, translator):
        QObject.__init__(self)
        self.translator = translator
        self.translate.connect(self.translate_text)
        self.check.connect(self.check_usage)

    @pyqtSlot(str)
    def translate_text(self, text):
        self.clear.emit()
        self.result.emit(_('Translating...'))
        try:
            translation = self.translator.translate(text)
            if isinstance(translation, GeneratorType):
                clear = True
                for text in translation:
                    if clear:
                        self.clear.emit()
                        clear = False
                    self.result.emit(text)
                    time.sleep(0.05)
            else:
                self.clear.emit()
                self.result.emit(translation)
            self.complete.emit()
        except Exception:
            self.clear.emit()
            error_message = traceback_error()
            self.result.emit(error_message)
            log.error(error_message)

    @pyqtSlot()
    def check_usage(self):
        self.usage.emit(self.translator.get_usage())


class EngineTester(QDialog):
    usage_thread = QThread()
    translation_thread = QThread()

    def __init__(self, parent, translator):
        QDialog.__init__(self, parent)
        self.parent = parent
        self.translator = translator
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(_('Test Translation Engine'))
        self.setModal(True)
        self.setMinimumWidth(500)
        # self.setMaximumHeight(300)
        self.layout()
        self.show()

    def layout(self):
        layout = QGridLayout(self)

        source = QPlainTextEdit()
        source.setPlainText('Hello World!')
        cursor = source.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        source.setTextCursor(cursor)
        layout.addWidget(source, 0, 0, 1, 3)

        target = QPlainTextEdit()
        layout.addWidget(target, 1, 0, 1, 3)

        source_lang = SourceLang()
        source_lang.set_codes(self.translator.lang_codes.get('source'))
        layout.addWidget(source_lang, 2, 0)

        def change_source_lang(lang):
            self.translator.set_source_lang(lang)
        change_source_lang(source_lang.currentText())
        source_lang.currentTextChanged.connect(change_source_lang)

        target_lang = TargetLang()
        target_lang.set_codes(
            self.translator.lang_codes.get('target'),
            self.parent.target_lang.currentText())
        layout.addWidget(target_lang, 2, 1)

        def change_target_lang(lang):
            self.translator.set_target_lang(lang)
        change_target_lang(target_lang.currentText())
        target_lang.currentTextChanged.connect(change_target_lang)

        translate = with_icon(QPushButton(_('Translate')), 'forward')
        layout.addWidget(translate, 2, 2)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)

        usage = secondary(QLabel(_('Usage: checking...')))
        usage.setVisible(False)
        layout.addWidget(usage, 3, 0, 1, 3)

        self.usage_worker = EngineWorker(self.translator)
        self.usage_worker.moveToThread(self.usage_thread)
        self.usage_thread.finished.connect(self.usage_worker.deleteLater)
        self.usage_thread.start()

        def check_usage(text):
            if text is not None:
                usage.setText(_('Usage: {}').format(text))
                usage.setVisible(True)
            else:
                usage.setVisible(False)
        self.usage_worker.usage.connect(check_usage)
        self.usage_worker.check.emit()

        self.translate_worker = EngineWorker(self.translator)
        self.translate_worker.moveToThread(self.translation_thread)
        self.translation_thread.finished.connect(
            self.translate_worker.deleteLater)
        self.translation_thread.start()

        self.translate_worker.clear.connect(target.clear)
        self.translate_worker.result.connect(target.insertPlainText)
        self.translate_worker.complete.connect(self.usage_worker.check.emit)

        def test_translate():
            self.translate_worker.translate.emit(source.toPlainText())
        translate.clicked.connect(test_translate)
        tidy(self)

    def done(self, result):
        QDialog.done(self, result)
        self.usage_thread.quit()
        self.usage_thread.wait()
        self.translation_thread.quit()
        self.translation_thread.wait()
