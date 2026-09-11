import re
import os
import json
import os.path

from qt.core import (  # type: ignore
    Qt, QLabel, QDialog, QWidget, QLineEdit, QPushButton, QPlainTextEdit,
    QTabWidget, QHBoxLayout, QVBoxLayout, QGroupBox, QFileDialog, QColor,
    QIntValidator, QScrollArea, QRadioButton, QGridLayout, QCheckBox, QObject,
    QButtonGroup, QColorDialog, QSpinBox, QPalette, QApplication, QFrame,
    QComboBox, QRegularExpression, pyqtSignal, QFormLayout, QDoubleSpinBox,
    QSpacerItem, QRegularExpressionValidator, QBoxLayout, QThread, pyqtSlot)
from calibre.gui2 import error_dialog  # type: ignore
from calibre.utils.localization import _  # type: ignore

from . import NovelTranslatorPlugin
from .lib.config import get_config
from .lib.utils import (
    log, css, is_proxy_available, traceback_error, socks_proxy)
from .lib.translation import get_engine_class, get_translator
from .lib.novel import DIALOGUE_CONVENTIONS
from .engines import (
    builtin_engines, GeminiTranslate, ChatgptTranslate, OpenRouterTranslate)
from .engines.genai import GenAI
from .components import (
    Footer, AlertMessage, TargetLang, SourceLang, EngineList, EngineTester,
    InputFormat, OutputFormat, set_shortcut)


load_translations()  # type: ignore


class ModelWorker(QObject):
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


def layout_scroll_area(name):
    def decorator(func):
        def scroll_widget(dialog):
            widget = QWidget()
            layout = QVBoxLayout(widget)

            scroll_area = QScrollArea(widget)
            scroll_area.setWidgetResizable(True)
            if not QApplication.instance().is_dark_theme:
                scroll_area.setBackgroundRole(QPalette.Light)
            scroll_area.setWidget(func(dialog))
            layout.addWidget(scroll_area, 1)

            save_button = QPushButton(_('&Save'))
            save_button.setObjectName(name)
            layout.addWidget(save_button)

            def save_current_config():
                dialog.save_config.emit(dialog.tabs.currentIndex())
            save_button.clicked.connect(save_current_config)
            set_shortcut(
                save_button, 'save', save_current_config, save_button.text())
            return widget
        return scroll_widget
    return decorator


class TranslationSetting(QDialog):
    save_config = pyqtSignal(int)
    fetch_models = pyqtSignal()
    model_thread = QThread()

    def __init__(self, plugin, parent, icon):
        QDialog.__init__(self, parent)
        self.plugin = plugin
        self.icon = icon
        self.alert = AlertMessage(self)

        self.config = get_config()
        self.current_engine = get_engine_class()

        self.model_worker = ModelWorker()
        self.model_worker.moveToThread(self.model_thread)
        self.model_thread.finished.connect(self.model_worker.deleteLater)
        self.model_thread.start()

        self.main_layout()

    def _divider(self):
        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setFrameShadow(QFrame.Sunken)
        # divider.setFrameStyle(QFrame.HLine | QFrame.Sunken)
        return divider

    def main_layout(self):
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        general_index = self.tabs.addTab(self.layout_general(), _('General'))
        engine_index = self.tabs.addTab(self.layout_engine(), _('Engine'))
        content_index = self.tabs.addTab(self.layout_content(), _('Content'))
        self.tabs.setStyleSheet('QTabBar::tab {min-width:120px;}')

        layout.addWidget(self.tabs)
        layout.addWidget(Footer())

        def save_setting(index):
            actions = {
                general_index: self.update_general_config,
                engine_index: self.update_engine_config,
                content_index: self.update_content_config,
            }
            if actions[index]():
                self.config.commit()
                self.alert.pop(_('The setting has been saved.'))
        self.save_config.connect(save_setting)

        def disable_button(disabled):
            save_button = self.findChild(QPushButton, 'engine')
            save_button.setDisabled(disabled)
        self.model_worker.start.connect(lambda: disable_button(True))
        self.model_worker.finished.connect(lambda: disable_button(False))

        def change_tab_index(index):
            self.config.refresh()
            if index == engine_index and \
                    issubclass(self.current_engine, GenAI):
                self.fetch_models.emit()
        self.tabs.currentChanged.connect(change_tab_index)

    @layout_scroll_area('general')
    def layout_general(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Output Path
        radio_group = QGroupBox(_('Output Path'))
        radio_layout = QHBoxLayout()
        library_radio = QRadioButton(_('Library'))
        self.path_radio = QRadioButton(_('Path'))
        radio_layout.addWidget(library_radio)
        radio_layout.addWidget(self.path_radio)
        self.output_path_entry = QLineEdit()
        self.output_path_entry.setPlaceholderText(
            _('Choose a path to store translated book(s)'))
        self.output_path_entry.setText(self.config.get('output_path'))
        radio_layout.addWidget(self.output_path_entry)
        output_path_button = QPushButton(_('Choose'))

        radio_layout.addWidget(output_path_button)
        radio_group.setLayout(radio_layout)
        layout.addWidget(radio_group)

        def choose_output_type(checked):
            output_path_button.setDisabled(checked)
            self.output_path_entry.setDisabled(checked)
            self.config.update(to_library=checked)
        library_radio.toggled.connect(choose_output_type)

        if self.config.get('to_library'):
            library_radio.setChecked(True)
        else:
            self.path_radio.setChecked(True)
        choose_output_type(library_radio.isChecked())

        def choose_output_path():
            path = QFileDialog.getExistingDirectory()
            self.output_path_entry.setText(path)
        output_path_button.clicked.connect(choose_output_path)

        # preferred Format
        format_group = QGroupBox(_('Preferred Format'))
        format_layout = QFormLayout(format_group)
        input_format = InputFormat()
        output_format = OutputFormat()
        format_layout.addRow(_('Input Format'), input_format)
        format_layout.addRow(_('Output Format'), output_format)
        layout.addWidget(format_group)

        self.apply_form_layout_policy(format_layout)

        input_format.setCurrentText(self.config.get('input_format'))
        output_format.setCurrentText(self.config.get('output_format'))

        def change_input_format(format):
            if format == _('Ebook Specific'):
                self.config.delete('input_format')
            else:
                self.config.update(input_format=format)
        input_format.currentTextChanged.connect(change_input_format)
        output_format.currentTextChanged.connect(
            lambda format: self.config.update(output_format=format))

        # Network Proxy
        proxy_group = QGroupBox(_('Network Proxy'))
        proxy_layout = QHBoxLayout()

        is_proxy_enabled = self.config.get('proxy_enabled', False)

        self.proxy_enabled = QCheckBox(_('Enable'))
        self.proxy_enabled.setChecked(is_proxy_enabled)
        proxy_layout.addWidget(self.proxy_enabled)

        self.proxy_type = QComboBox()
        self.proxy_type.addItems(['http', 'socks5'])
        self.proxy_type.setStyleSheet('text-transform:uppercase;')
        self.proxy_type.setEnabled(is_proxy_enabled)
        proxy_layout.addWidget(self.proxy_type)

        current_proxy_type = self.config.get('proxy_type')
        if current_proxy_type is not None:
            self.proxy_type.setCurrentText(current_proxy_type)

        self.proxy_host = QLineEdit()
        rule = r'^(http://|)([a-zA-Z\d]+:[a-zA-Z\d]+@|)' \
               r'(([a-zA-Z\d]|-)*[a-zA-Z\d]\.){1,}[a-zA-Z\d]+$'
        self.host_validator = QRegularExpressionValidator(
            QRegularExpression(rule))
        self.proxy_host.setPlaceholderText(
            _('Host') + ' (127.0.0.1, user:pass@127.0.0.1)')
        self.proxy_host.setEnabled(is_proxy_enabled)
        proxy_layout.addWidget(self.proxy_host, 4)

        self.proxy_port = QLineEdit()
        self.proxy_port.setPlaceholderText(_('Port'))
        port_validator = QIntValidator()
        port_validator.setRange(0, 65536)
        self.proxy_port.setValidator(port_validator)
        self.proxy_port.setEnabled(is_proxy_enabled)
        proxy_layout.addWidget(self.proxy_port, 1)

        self.proxy_port.textChanged.connect(
            lambda num: self.proxy_port.setText(
                num if not num or int(num) < port_validator.top()
                else str(port_validator.top())))

        proxy_test = QPushButton(_('Test'))
        proxy_test.clicked.connect(self.test_proxy_connection)
        proxy_layout.addWidget(proxy_test)

        proxy_group.setLayout(proxy_layout)
        layout.addWidget(proxy_group)

        def fill_proxy_setting(proxy_type):
            self.proxy_host.clear()
            self.proxy_port.clear()
            proxy_setting = self.config.get('proxy_setting') or {}
            # Compatible with old proxy settings stored as a list.
            if isinstance(proxy_setting, list):
                proxy_setting = {'http': proxy_setting}
            host, port = proxy_setting.get(proxy_type) or ['', '']
            self.proxy_host.setText(host)
            self.proxy_port.setText(str(port))
        fill_proxy_setting(self.proxy_type.currentText())
        self.proxy_type.currentTextChanged.connect(fill_proxy_setting)

        def enable_network_proxy(enable):
            self.proxy_type.setEnabled(enable)
            self.proxy_host.setEnabled(enable)
            self.proxy_port.setEnabled(enable)
            self.config.update(proxy_enabled=enable)
        self.proxy_enabled.toggled.connect(enable_network_proxy)

        misc_widget = QWidget()
        misc_layout = QHBoxLayout(misc_widget)
        misc_layout.setContentsMargins(0, 0, 0, 0)

        # Cache
        cache_group = QGroupBox(_('Cache'))
        cache_layout = QHBoxLayout(cache_group)
        cache_enabled = QCheckBox(_('Enable'))
        cache_manage = QLabel(_('Manage'))
        cache_layout.addWidget(cache_enabled)
        cache_layout.addStretch(1)
        cache_layout.addWidget(cache_manage)
        misc_layout.addWidget(cache_group, 1)

        cache_manage.setStyleSheet('color:blue;text-decoration:underline;')
        cursor = cache_manage.cursor()
        cursor.setShape(Qt.PointingHandCursor)
        cache_manage.setCursor(cursor)
        cache_manage.mouseReleaseEvent = lambda event: self.plugin.show_cache()

        cache_enabled.setChecked(self.config.get('cache_enabled'))
        cache_enabled.toggled.connect(
            lambda checked: self.config.update(cache_enabled=checked))

        # Job Log
        log_group = QGroupBox(_('Job Log'))
        log_translation = QCheckBox(_('Show translation'))
        log_layout = QVBoxLayout(log_group)
        log_layout.addWidget(log_translation)
        log_layout.addStretch(1)
        misc_layout.addWidget(log_group, 1)

        # Notification
        notice_group = QGroupBox(_('Notification'))
        notice_layout = QHBoxLayout(notice_group)
        notice = QCheckBox(_('Enable'))
        notice_layout.addWidget(notice)
        misc_layout.addWidget(notice_group, 1)

        layout.addWidget(misc_widget)

        log_translation.setChecked(self.config.get('log_translation', True))
        log_translation.toggled.connect(
            lambda checked: self.config.update(log_translation=checked))

        notice.setChecked(self.config.get('show_notification', True))
        notice.toggled.connect(
            lambda checked: self.config.update(show_notification=checked))

        layout.addStretch(1)

        return widget

    @layout_scroll_area('engine')
    def layout_engine(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Translate Engine
        engine_group = QGroupBox(_('Translation Engine'))
        engine_layout = QHBoxLayout(engine_group)
        engine_list = EngineList(self.current_engine.name)
        engine_test = QPushButton(_('Test'))
        engine_layout.addWidget(engine_list, 1)
        engine_layout.addWidget(engine_test)
        layout.addWidget(engine_group)

        self.model_worker.start.connect(
            lambda: engine_group.setDisabled(True))
        self.model_worker.finished.connect(
            lambda: engine_group.setDisabled(False))

        # Using Tip
        self.tip_group = QGroupBox(_('Usage Tip'))
        tip_layout = QVBoxLayout(self.tip_group)
        self.using_tip = QLabel()
        self.using_tip.setTextFormat(Qt.RichText)
        self.using_tip.setWordWrap(True)
        self.using_tip.setOpenExternalLinks(True)
        tip_layout.addWidget(self.using_tip)
        layout.addWidget(self.tip_group)

        # API Keys
        self.keys_group = QGroupBox(_('API Keys'))
        keys_layout = QVBoxLayout(self.keys_group)
        self.api_keys = QPlainTextEdit()
        self.api_keys.setFixedHeight(100)
        auto_change = QLabel('%s %s' % (_('Tip: '), _(
            'API keys will auto-switch if the previous one is unavailable.')))
        auto_change.setVisible(False)
        keys_layout.addWidget(self.api_keys)
        keys_layout.addWidget(auto_change)
        layout.addWidget(self.keys_group)

        self.api_keys.textChanged.connect(lambda: auto_change.setVisible(
            len(self.api_keys.toPlainText().strip().split('\n')) > 1))

        # preferred Language
        language_group = QGroupBox(_('Preferred Language'))
        language_layout = QFormLayout(language_group)
        self.source_lang = SourceLang()
        self.target_lang = TargetLang()
        language_layout.addRow(_('Source Language'), self.source_lang)
        language_layout.addRow(_('Target Language'), self.target_lang)
        layout.addWidget(language_group)

        self.apply_form_layout_policy(language_layout)

        # Network Request
        request_group = QGroupBox(_('HTTP Request'))
        request_interval = QDoubleSpinBox()
        request_interval.setRange(0, 999999)
        request_interval.setDecimals(1)
        request_attempt = QSpinBox()
        request_attempt.setRange(0, 999999)
        request_timeout = QDoubleSpinBox()
        request_timeout.setRange(0, 999999)
        request_timeout.setDecimals(1)
        request_layout = QFormLayout(request_group)
        request_layout.addRow(_('Interval (seconds)'), request_interval)
        request_layout.addRow(_('Attempt times'), request_attempt)
        request_layout.addRow(_('Timeout (seconds)'), request_timeout)
        layout.addWidget(request_group)

        # Abort Translation
        abort_translation_group = QGroupBox(_('Abort Translation'))
        abort_translation_layout = QHBoxLayout(abort_translation_group)
        max_error_count = QSpinBox()
        max_error_count.setMinimum(0)
        abort_translation_layout.addWidget(QLabel(_('Max errors')))
        abort_translation_layout.addWidget(max_error_count)
        abort_translation_layout.addWidget(QLabel(
            _('The number of consecutive errors to abort translation.')), 1)
        layout.addWidget(abort_translation_group)

        self.disable_wheel_event(max_error_count)

        self.apply_form_layout_policy(request_layout)
        self.disable_wheel_event(request_attempt)
        self.disable_wheel_event(request_interval)
        self.disable_wheel_event(request_timeout)

        # GenAI Setting
        genai_group = QGroupBox(_('Fine-tuning'))
        genai_group.setVisible(False)
        genai_layout = QFormLayout(genai_group)
        self.apply_form_layout_policy(genai_layout)

        provider_label = QLabel(_('Provider'))
        provider_list = QComboBox()
        provider_list.setToolTip(_(
            'Which server to talk to. Every one of them speaks the OpenAI '
            'chat API; the preset fills in the endpoint, the key hint and '
            'a model to start with, and both stay editable below. The API '
            'keys, the endpoint and the model are kept per provider, so '
            'switching back finds them again.'))
        genai_layout.addRow(provider_label, provider_list)
        self.disable_wheel_event(provider_list)

        self.genai_endpoint = QLineEdit()
        endpoint_label = QLabel(_('Endpoint'))
        genai_layout.addRow(endpoint_label, self.genai_endpoint)

        genai_model = QWidget()
        genai_model_layout = QHBoxLayout(genai_model)
        genai_model_layout.setContentsMargins(0, 0, 0, 0)
        genai_model_refresh = QPushButton(_('Refresh'))
        genai_model_list = QComboBox()
        genai_model_input = QLineEdit()
        genai_model_input.setPlaceholderText(_('A model name'))
        genai_model_input.setVisible(False)
        genai_model_layout.addWidget(genai_model_refresh)
        genai_model_layout.addWidget(genai_model_list, 1)
        genai_model_layout.addWidget(genai_model_input, 3)
        genai_layout.addRow(_('Model'), genai_model)

        genai_model_limits = QLabel()
        genai_model_limits.setWordWrap(True)
        genai_model_limits.setStyleSheet('color:grey;')
        genai_model_limits.setToolTip(_(
            'What the provider says about the selected model. The reply '
            'limit is the one that matters most in Novel Mode: a chunk '
            'is answered with a translation about as long as the chunk '
            'itself, so it is what a request can be sized against.\n\n'
            'Shown for engines whose model listing publishes the '
            'figures (OpenRouter today), and only after the list has '
            'been fetched.'))
        genai_layout.addRow('', genai_model_limits)

        self.disable_wheel_event(genai_model_list)

        sampling_widget = QWidget()
        sampling_layout = QHBoxLayout(sampling_widget)
        sampling_layout.setContentsMargins(0, 0, 0, 0)
        temperature = QRadioButton()
        temperature_label = QLabel('temperature')
        temperature_value = QDoubleSpinBox()
        temperature_value.setDecimals(1)
        temperature_value.setSingleStep(0.1)
        top_p = QRadioButton()
        top_p_label = QLabel('top_p')
        top_p_value = QDoubleSpinBox()
        top_p_value.setDecimals(1)
        top_p_value.setSingleStep(0.1)
        top_p_value.setRange(0, 1)
        top_k = QLabel('top_k')
        top_k_value = QSpinBox()
        top_k_value.setSingleStep(1)
        top_k_value.setRange(1, 40)
        sampling_layout.addWidget(temperature)
        sampling_layout.addWidget(temperature_label)
        sampling_layout.addWidget(temperature_value)
        sampling_layout.addSpacing(20)
        sampling_layout.addWidget(top_p)
        sampling_layout.addWidget(top_p_label)
        sampling_layout.addWidget(top_p_value)
        sampling_layout.addSpacing(20)
        sampling_layout.addWidget(top_k)
        sampling_layout.addWidget(top_k_value)
        sampling_layout.addStretch(1)
        genai_layout.addRow(_('Sampling'), sampling_widget)

        self.disable_wheel_event(temperature_value)
        self.disable_wheel_event(top_p_value)

        stream_enabled = QCheckBox(_('Enable streaming response'))
        genai_layout.addRow(_('Stream'), stream_enabled)

        reasoning_label = QLabel(_('Reasoning'))
        reasoning_effort_list = QComboBox()
        for effort in ChatgptTranslate.reasoning_efforts:
            reasoning_effort_list.addItem(
                _('Default') if effort == 'default' else effort, effort)
        reasoning_effort_list.setToolTip(_(
            'Chain-of-thought budget, sent as "reasoning_effort".\n\n'
            '- Default: omit the field, which is what plain OpenAI models '
            'and older gateways expect.\n'
            '- none: suppress the reasoning tokens some servers emit '
            'before every answer (Ollama 0.31+ with Gemma 4 spends '
            'thousands of silent tokens that make requests look stalled).\n'
            '- minimal/low/medium/high: spend reasoning tokens on purpose. '
            'Models without reasoning support ignore the field.'))
        genai_layout.addRow(reasoning_label, reasoning_effort_list)

        self.disable_wheel_event(reasoning_effort_list)

        reasoning_effort_list.currentIndexChanged.connect(
            lambda: self.current_engine.config.update(
                reasoning_effort=reasoning_effort_list.currentData()))

        sampling_btn_group = QButtonGroup(sampling_widget)
        sampling_btn_group.addButton(temperature, 0)
        sampling_btn_group.addButton(top_p, 1)

        labels = {
            temperature: temperature_label.text(),
            top_p: top_p_label.text()}

        sampling_btn_group.buttonClicked.connect(
            lambda button: self.current_engine.config
            .update(sampling=labels[button]))

        layout.addWidget(genai_group)

        # OpenRouter Setting (visible only for the OpenRouter engine).
        openrouter_group = QGroupBox(_('OpenRouter'))
        openrouter_group.setVisible(False)
        openrouter_layout = QFormLayout(openrouter_group)
        self.apply_form_layout_policy(openrouter_layout)

        # Widgets register themselves here by preference key. Values are
        # loaded in a single pass by show_openrouter_preferences() with
        # `openrouter_loading` set so the change signals do not write
        # back; that lets every signal be connected exactly once instead
        # of being re-connected each time the engine changes.
        openrouter_widgets = {}
        openrouter_loading = []

        def openrouter_save(key, value):
            if openrouter_loading:
                return
            if issubclass(self.current_engine, OpenRouterTranslate):
                self.current_engine.config.update({key: value})

        def openrouter_spin(key, title, minimum, maximum, decimals=0,
                            step=1, tooltip=None):
            widget = QDoubleSpinBox() if decimals else QSpinBox()
            if decimals:
                widget.setDecimals(decimals)
                widget.setRange(float(minimum), float(maximum))
                widget.setSingleStep(float(step))
                widget.valueChanged.connect(
                    lambda value: openrouter_save(key, round(value, decimals)))
            else:
                widget.setRange(int(minimum), int(maximum))
                widget.setSingleStep(int(step))
                widget.valueChanged.connect(
                    lambda value: openrouter_save(key, int(value)))
            widget.setPrefix('%s: ' % title)
            if tooltip is not None:
                widget.setToolTip(tooltip)
            self.disable_wheel_event(widget)
            openrouter_widgets[key] = widget
            return widget

        def openrouter_combo(key, values, tooltip=None):
            widget = QComboBox()
            for value in values:
                widget.addItem(
                    _('Default') if value == 'default' else value, value)
            if tooltip is not None:
                widget.setToolTip(tooltip)
            widget.currentIndexChanged.connect(
                lambda: openrouter_save(key, widget.currentData()))
            self.disable_wheel_event(widget)
            openrouter_widgets[key] = widget
            return widget

        def openrouter_check(key, title, tooltip=None):
            widget = QCheckBox(title)
            if tooltip is not None:
                widget.setToolTip(tooltip)
            widget.toggled.connect(
                lambda checked: openrouter_save(key, checked))
            openrouter_widgets[key] = widget
            return widget

        def openrouter_edit(key, placeholder=None, tooltip=None):
            widget = QLineEdit()
            if placeholder is not None:
                widget.setPlaceholderText(placeholder)
            if tooltip is not None:
                widget.setToolTip(tooltip)
            widget.textChanged.connect(
                lambda text: openrouter_save(key, text.strip()))
            openrouter_widgets[key] = widget
            return widget

        def openrouter_json(key, placeholder=None, tooltip=None):
            widget = QPlainTextEdit()
            widget.setFixedHeight(60)
            if placeholder is not None:
                widget.setPlaceholderText(placeholder)
            if tooltip is not None:
                widget.setToolTip(tooltip)

            def on_text_changed():
                text = widget.toPlainText().strip()
                valid = True
                if text:
                    try:
                        valid = isinstance(json.loads(text), dict)
                    except ValueError:
                        valid = False
                # Invalid snippets are ignored by the engine, so flag them
                # here instead of silently dropping them at request time.
                widget.setStyleSheet(
                    '' if valid else 'border: 1px solid red;')
                openrouter_save(key, text)
            widget.textChanged.connect(on_text_changed)
            openrouter_widgets[key] = widget
            return widget

        def openrouter_row(label, *widgets):
            """Add one form row; returns its index so it can be hidden."""
            index = openrouter_layout.rowCount()
            if len(widgets) == 1:
                openrouter_layout.addRow(label, widgets[0])
                return index
            container = QWidget()
            container_layout = QHBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
            for widget in widgets:
                container_layout.addWidget(widget)
            container_layout.addStretch(1)
            openrouter_layout.addRow(label, container)
            return index

        openrouter_row(
            _('Reasoning'),
            openrouter_combo(
                'reasoning_effort', OpenRouterTranslate.reasoning_efforts,
                _('How much thinking the model may do before answering. '
                  '"Default" omits the field and lets the model decide; '
                  '"none", the default here, turns reasoning off with '
                  '"reasoning.enabled: false"; the other levels are sent '
                  'as "reasoning.effort".\n\n'
                  'Reasoning is off because translation is not a '
                  'reasoning task and the tokens are far from free: over '
                  'one measured chapter they were a third of everything '
                  'billed. They also cost time invisibly, since "exclude" '
                  'keeps them out of the stream and nothing reaches the '
                  'plugin while the model thinks. Raise it if a '
                  'particular model needs it.')),
            openrouter_spin(
                'reasoning_max_tokens', 'max_tokens', 0, 128000, step=256,
                tooltip=_(
                    'An explicit reasoning budget ("reasoning.max_tokens"), '
                    'used by Anthropic and Qwen models. Above 0 it replaces '
                    'the effort level; 0 means "use the effort level".')),
            openrouter_check(
                'reasoning_exclude', 'exclude',
                _('Reason internally but leave the chain of thought out of '
                  'the response. Recommended: the plugin only reads the '
                  'translated text, so returning it just costs bandwidth.')))

        openrouter_advanced = QCheckBox(_('Show'))
        openrouter_advanced.setToolTip(_(
            'Show the sampling and penalty parameters.\n\n'
            'They are hidden by default: they are specialist knobs, each '
            'already at the neutral value that leaves it out of the '
            'request, and a book-length translation has no use for them. '
            'Hiding them does not change what is sent -- a value set here '
            'and then hidden is still part of every request.'))
        openrouter_layout.addRow(
            _('Advanced parameters'), openrouter_advanced)

        advanced_rows = []
        advanced_rows.append(openrouter_row(
            _('Sampling'),
            openrouter_spin(
                'top_k', 'top_k', 0, 200,
                tooltip=_('Keep only the K most likely tokens. '
                          '0 disables it.')),
            openrouter_spin(
                'min_p', 'min_p', 0, 1, decimals=2, step=0.05,
                tooltip=_('Drop tokens less likely than this fraction of '
                          'the top token. 0 disables it.')),
            openrouter_spin(
                'top_a', 'top_a', 0, 1, decimals=2, step=0.05,
                tooltip=_('Dynamic filtering relative to the top token '
                          'probability. 0 disables it.'))))

        advanced_rows.append(openrouter_row(
            _('Penalties'),
            openrouter_spin(
                'frequency_penalty', 'frequency', -2, 2,
                decimals=1, step=0.1,
                tooltip=_('Penalize tokens by how often they already '
                          'appeared. 0 disables it.')),
            openrouter_spin(
                'presence_penalty', 'presence', -2, 2,
                decimals=1, step=0.1,
                tooltip=_('Penalize tokens that appeared at all. '
                          '0 disables it.')),
            openrouter_spin(
                'repetition_penalty', 'repetition', 0, 2,
                decimals=2, step=0.05,
                tooltip=_('Scale down tokens already present in the input. '
                          '1 disables it.'))))

        def show_advanced_parameters(visible):
            for row in advanced_rows:
                openrouter_layout.setRowVisible(row, bool(visible))
        openrouter_advanced.setChecked(bool(self.config.get(
            'openrouter_advanced_parameters', False)))
        openrouter_advanced.toggled.connect(
            lambda checked: self.config.update(
                openrouter_advanced_parameters=bool(checked)))
        openrouter_advanced.toggled.connect(show_advanced_parameters)
        show_advanced_parameters(openrouter_advanced.isChecked())

        openrouter_row(
            _('Limits'),
            openrouter_spin(
                'max_tokens', 'max_tokens', 0, 1000000, step=256,
                tooltip=_('Cap the length of the answer. 0 lets the model '
                          'use its own limit -- keep it at 0 for Novel '
                          'Mode, where a whole chunk is translated in one '
                          'answer.')),
            openrouter_spin(
                'seed', 'seed', 0, 2147483647,
                tooltip=_('Ask for deterministic sampling. 0 disables it. '
                          'Only some providers honor it.')))

        openrouter_row(
            _('Provider: only'),
            openrouter_edit(
                'provider_only', 'baidu/fp8, deepinfra',
                _('Comma separated list of provider slugs that may serve '
                  'the request. Pin it when one provider gives you the '
                  'quality, price or quantization you want. Leave empty to '
                  'let OpenRouter choose.')))
        openrouter_row(
            _('Provider: order'),
            openrouter_edit(
                'provider_order', 'deepinfra, together',
                _('Comma separated providers to try in this order before '
                  'falling back to the rest.')))
        openrouter_row(
            _('Provider: ignore'),
            openrouter_edit(
                'provider_ignore', 'novita, targon',
                _('Comma separated providers that must never serve the '
                  'request.')))
        openrouter_row(
            _('Provider: quantizations'),
            openrouter_edit(
                'provider_quantizations', 'fp8, bf16',
                _('Comma separated quantization levels to accept '
                  '(int4, int8, fp8, fp16, bf16, fp32). Useful to avoid '
                  'heavily quantized endpoints that degrade long-form '
                  'translation.')))

        openrouter_row(
            _('Provider: routing'),
            QLabel(_('sort by')),
            openrouter_combo(
                'provider_sort', OpenRouterTranslate.provider_sorts,
                _('Pick the endpoint by price, throughput or latency '
                  'instead of OpenRouter\'s balanced order. Defaults to '
                  'price: a book is hundreds of large requests, and the '
                  'cheapest endpoint serving the model is felt end to '
                  'end. Pick throughput when the run is too slow: the '
                  'same model behind the same gateway has been measured '
                  'at 200 tokens/s on one endpoint and 30 on another.')),
            QLabel(_('data collection')),
            openrouter_combo(
                'provider_data_collection',
                OpenRouterTranslate.provider_data_collections,
                _('Whether the provider may store your prompts: "deny" '
                  'only routes to providers that do not.')))

        openrouter_row(
            _('Provider: policy'),
            openrouter_check(
                'provider_allow_fallbacks', 'allow_fallbacks',
                _('Let OpenRouter fall back to another provider when the '
                  'selected one fails. Turn it off to fail loudly instead '
                  'of silently changing model quality mid-book.')),
            openrouter_check(
                'provider_require_parameters', 'require_parameters',
                _('Only route to providers that support every parameter '
                  'in the request. On by default: a provider free to drop '
                  '"response_format" is a provider that answers with the '
                  'source text copied next to the translation, and one '
                  'free to drop "reasoning" bills for thinking you asked '
                  'it not to do.\n\n'
                  'The request now carries only the parameters the chosen '
                  'model accepts, which is what used to make this setting '
                  'risky. Turn it off anyway if OpenRouter answers that '
                  'no provider is available for your model.')),
            openrouter_check(
                'provider_zdr', 'zdr',
                _('Only route to Zero Data Retention endpoints.')))

        openrouter_row(
            _('Attribution'),
            openrouter_edit(
                'app_referer', 'https://example.com',
                _('Sent as the HTTP-Referer header, used by the OpenRouter '
                  'rankings. Optional.')),
            openrouter_edit(
                'app_title', NovelTranslatorPlugin.name,
                _('Sent as the X-Title header, used by the OpenRouter '
                  'rankings. Optional.')))

        openrouter_row(
            _('Extra headers'),
            openrouter_json(
                'extra_headers', '{"X-Session-Id": "calibre-translation"}',
                _('A JSON object merged into the request headers, for '
                  'anything not covered above.')))
        openrouter_row(
            _('Extra body'),
            openrouter_json(
                'extra_body',
                '{"plugins": [{"id": "web"}], "usage": {"include": true}}',
                _('A JSON object merged into the request body, applied '
                  'last so it can override any field computed above. Use '
                  'it for parameters the plugin does not expose '
                  '(logit_bias, stop, transforms, plugins, ...).')))

        layout.addWidget(openrouter_group)

        def show_openrouter_preferences(config):
            openrouter_loading.append(True)
            try:
                for key, widget in openrouter_widgets.items():
                    value = config.get(
                        key, getattr(OpenRouterTranslate, key))
                    if isinstance(widget, QComboBox):
                        widget.setCurrentIndex(max(widget.findData(value), 0))
                    elif isinstance(widget, QCheckBox):
                        widget.setChecked(bool(value))
                    elif isinstance(widget, QDoubleSpinBox):
                        widget.setValue(float(value or 0))
                    elif isinstance(widget, QSpinBox):
                        widget.setValue(int(value or 0))
                    elif isinstance(widget, QPlainTextEdit):
                        widget.setPlainText(str(value or ''))
                    else:
                        widget.setText(str(value or ''))
                        widget.setCursorPosition(0)
            finally:
                openrouter_loading.clear()

        # Novel Mode settings (visible only for GenAI engines).
        novel_group = QGroupBox(_('Novel Mode'))
        novel_group.setVisible(False)
        novel_layout = QFormLayout(novel_group)
        self.apply_form_layout_policy(novel_layout)

        novel_chapter_source = QComboBox()
        novel_chapter_source.addItem(
            _('Table of Contents (level 1)'), 'toc_level_1')
        novel_chapter_source.addItem(
            _('Table of Contents (level 2)'), 'toc_level_2')
        novel_chapter_source.addItem(
            _('One XHTML file per chapter'), 'xhtml_file')
        novel_chapter_source.setToolTip(_(
            'How to detect chapter boundaries.\n\n'
            '- TOC level 1: use the top-level Table of Contents entries as '
            'chapter boundaries. Best for single-book EPUBs.\n'
            '- TOC level 2: use the second-level TOC entries (sub-chapters). '
            'Recommended for multi-book anthologies (e.g. a complete series '
            'in one EPUB) where level 1 maps each full novel to a single '
            '"chapter". Level 2 maps individual chapters, giving the LLM '
            'the correct chapter title and preventing front-matter pages '
            '(Cover, Titlepage) from polluting the first translation chunk.\n'
            '- One XHTML file: each XHTML file in the spine becomes one '
            'chapter. Useful when the TOC is missing or unreliable.'))
        novel_layout.addRow(
            _('Chapter detection'), novel_chapter_source)
        self.disable_wheel_event(novel_chapter_source)

        novel_front_matter_min = QSpinBox()
        novel_front_matter_min.setRange(0, 5000)
        novel_front_matter_min.setSingleStep(50)
        novel_front_matter_min.setToolTip(_(
            'XHTML pages whose total text content is shorter than this '
            'many characters are treated as front/back matter (Cover, '
            'Titlepage, decorative pages) and excluded from chapter '
            'narrative content. Their paragraphs are still translated by '
            'the auxiliary pipeline (metadata/TOC titles).\n\n'
            'A typical Titlepage in a multi-book anthology contains only '
            'the book title as a single heading (~25 chars) -- well below '
            'the default of 100. Set to 0 to include all pages.'))
        novel_layout.addRow(
            _('Skip front matter under (chars)'), novel_front_matter_min)
        self.disable_wheel_event(novel_front_matter_min)

        novel_chunk_tokens = QSpinBox()
        novel_chunk_tokens.setRange(1024, 1000000)
        novel_chunk_tokens.setSingleStep(1024)
        novel_chunk_tokens.setToolTip(_(
            'Maximum estimated tokens per translation chunk. Together '
            'with "Max paragraphs per chunk" it forms a dual-cap: the '
            'chunk is closed as soon as either limit is reached.\n\n'
            'The limit that bites is not the context window but how much '
            'the model may write in one reply: it has to emit the whole '
            'chunk translated, which is as long as the chunk itself or '
            'longer. Recent hosted models allow 64k output tokens and up, '
            'while smaller and local ones often stop at 8k-16k and would '
            'truncate a large chunk mid-chapter.'))
        novel_layout.addRow(
            _('Max tokens per chunk'), novel_chunk_tokens)
        self.disable_wheel_event(novel_chunk_tokens)

        novel_max_paragraphs = QSpinBox()
        novel_max_paragraphs.setRange(0, 5000)
        novel_max_paragraphs.setSingleStep(10)
        novel_max_paragraphs.setToolTip(_(
            'Maximum number of paragraphs per translation chunk. '
            'The chunk is closed as soon as either the token budget or '
            'this paragraph cap is reached, whichever comes first.\n\n'
            'On a novel this is the cap that fires first, and it is the '
            'one that matters: the token budget above limits what the '
            'model has to read, but the translation costs about as much '
            'again to write, and a model that reads 200k tokens will '
            'only write 8k to 32k of them. Too many paragraphs in one '
            'chunk means a reply the model cannot finish, which arrives '
            'truncated. It also keeps the LLM from losing track of the '
            '[N] alignment markers when paragraphs are short (dialogue, '
            'TOC lists).\n\n'
            'Lower it to 40-80 for a small or local model, which loses '
            'markers well before a hosted one does. '
            'Set to 0 to disable and use only the token budget.'))
        novel_layout.addRow(
            _('Max paragraphs per chunk'), novel_max_paragraphs)
        self.disable_wheel_event(novel_max_paragraphs)

        novel_overlap = QSpinBox()
        novel_overlap.setRange(0, 50)
        novel_overlap.setSingleStep(1)
        novel_overlap.setToolTip(_(
            'Number of already-translated paragraphs from the previous '
            'chunk to replay as context for the next chunk. Similar to '
            'the sliding-window overlap used in RAG chunking, this '
            'preserves dialogue threads, pronoun referents and stylistic '
            'continuity across chunk boundaries. The overlap paragraphs '
            'are shown to the LLM as already-translated text (do NOT '
            'retranslate), so they consume some of the token budget but '
            'do not cause double translations.\n\n'
            'Recommended: 5 for most novels; up to 10 for dense narrative '
            'with long scenes. Set to 0 to disable overlap.'))
        novel_layout.addRow(
            _('Overlap paragraphs'), novel_overlap)
        self.disable_wheel_event(novel_overlap)

        novel_output_aware = QCheckBox(_('Cap it with the model limit'))
        novel_output_aware.setToolTip(_(
            'Never ask for a chunk longer than the model can answer.\n\n'
            'On by default. The token cap above says how much to send; '
            'it says nothing about how much the model is able to write '
            'back, and the two are unrelated -- context windows run to '
            'hundreds of thousands of tokens while reply limits start at '
            '4096. A chunk that cannot be finished is cut mid-answer and '
            'the lost paragraphs are asked for again, so the request is '
            'paid twice.\n\n'
            'The reply room is the lower of the limit the provider '
            'reported when the model was picked in the Model row above '
            '(known for engines that publish it, OpenRouter today) and '
            '"Reply room per chunk" below; a chunk carries about a 2.2th '
            'of it in source, the rest being what the translation and '
            'the JSON around it add. With neither figure, the cap above '
            'is used as it stands.'))
        novel_layout.addRow(
            _('Chunk size against the reply limit'), novel_output_aware)

        novel_reuse_paragraphs = QCheckBox(_('Keep'))
        novel_reuse_paragraphs.setToolTip(_(
            'Keep the paragraphs already translated in the cache instead '
            'of sending them to the model again.\n\n'
            'On by default. Translations are written after every chunk '
            'while the chapter counter only moves once a chapter is '
            'over, so a run cancelled at chunk 7 of 9 pays for those '
            'seven chunks a second time when it resumes. Turn it off to '
            'translate every pending chapter from scratch.'))
        novel_layout.addRow(
            _('Paragraphs already translated'), novel_reuse_paragraphs)

        novel_prompt_cache = QCheckBox(_('Ask the engine to cache it'))
        novel_prompt_cache.setToolTip(_(
            'Ask the engine to keep the prompt prefix in its cache.\n\n'
            'On by default. Every chunk of a chapter is sent with the '
            'same system prompt -- role, languages, running summary and '
            'glossary -- and a prefix the provider already holds is '
            'billed at a fraction of the price.\n\n'
            'Only engines whose API needs an explicit cache breakpoint '
            '(Claude) read this. The ones that cache on their own '
            '(OpenAI, Gemini, DeepSeek, OpenRouter) do it either way.'))
        novel_layout.addRow(
            _('Prompt prefix'), novel_prompt_cache)

        novel_structured = QComboBox()
        novel_structured.addItem(_('Auto (recommended)'), 'auto')
        novel_structured.addItem(_('Off (text markers [N])'), 'off')
        novel_structured.addItem(_('Force JSON on any engine'), 'force')
        novel_structured.setToolTip(_(
            'How to format the LLM response for paragraph alignment.\n\n'
            '- Auto: use native JSON structured output when the engine '
            'supports it (OpenAI, Gemini, Ollama via OpenAI-compatible '
            'endpoint). Falls back to text markers [N] otherwise. '
            'Recommended.\n'
            '- Off: always use text markers [N]. Slightly less reliable '
            'above 60-80 paragraphs per chunk on medium models but '
            'works everywhere.\n'
            '- Force: always request JSON output, even from engines that '
            'do not advertise support. Useful for custom OpenAI-compatible '
            'endpoints (Ollama exotics, LM Studio, vLLM) that accept '
            '"response_format" but are not recognised by the plugin.\n\n'
            'Structured output removes the marker-dropping limit, but '
            'not the model\'s output limit: however the response is '
            'formatted, a chunk is only translatable if the model can '
            'write the whole reply. Raise "Max paragraphs per chunk" '
            'past 100 only if the log shows no truncated responses.'))
        novel_layout.addRow(
            _('Structured output'), novel_structured)
        self.disable_wheel_event(novel_structured)

        novel_context_tokens = QSpinBox()
        novel_context_tokens.setRange(0, 100000)
        novel_context_tokens.setSingleStep(256)
        novel_context_tokens.setToolTip(_(
            'How much room the running context (summaries + glossary) '
            'may take. It is both the size the context block is trimmed '
            'to and the amount held back from the chunk budget.\n\n'
            'The glossary sent with a chapter now holds only the names '
            'that chapter uses, so this no longer has to cover a '
            'glossary that grows with the book. Raise it if the log '
            'shows the oldest summaries being dropped sooner than you '
            'would like.'))
        novel_layout.addRow(
            _('Reserved for context (tokens)'), novel_context_tokens)
        self.disable_wheel_event(novel_context_tokens)

        novel_summary_tokens = QSpinBox()
        novel_summary_tokens.setRange(50, 5000)
        novel_summary_tokens.setSingleStep(50)
        novel_summary_tokens.setToolTip(_(
            'How long a chapter summary is expected to be. The value is '
            'reserved in the chunk budget, and it also bounds what gets '
            'stored: a summary longer than twice this size is truncated '
            'before being saved.\n\n'
            'A summary is re-read in the prompt of every chapter that '
            'follows, so a model that answers with the whole chapter '
            'instead of a few hundred words would otherwise fill the '
            'context budget for the rest of the book.'))
        novel_layout.addRow(
            _('Summary target size (tokens)'), novel_summary_tokens)
        self.disable_wheel_event(novel_summary_tokens)

        novel_glossary_max = QSpinBox()
        novel_glossary_max.setRange(10, 5000)
        novel_glossary_max.setSingleStep(10)
        novel_layout.addRow(
            _('Glossary max entries'), novel_glossary_max)
        self.disable_wheel_event(novel_glossary_max)

        novel_glossary_relevant = QCheckBox(_('Only what the chapter uses'))
        novel_glossary_relevant.setToolTip(_(
            'Send the model only the glossary entries whose name appears '
            'in the chapter being translated, instead of the whole '
            'glossary.\n\n'
            'On by default. A glossary keeps growing while a chapter '
            'keeps needing the same handful of names: the entries a '
            'chapter never mentions cannot be mistranslated in it, and '
            'listing them costs input tokens on every request. In the '
            'entity extraction call they cost more than that, since a '
            'model handed hundreds of names to skip has been observed '
            'copying the whole list back as new entries.'))
        novel_layout.addRow(
            _('Glossary sent to the model'), novel_glossary_relevant)

        novel_glossary_prompt_max = QSpinBox()
        novel_glossary_prompt_max.setRange(0, 5000)
        novel_glossary_prompt_max.setSingleStep(10)
        novel_glossary_prompt_max.setToolTip(_(
            'Never put more than this many glossary entries in a single '
            'prompt, keeping the most recently learned ones. Applied '
            'after the filter above. Set to 0 for no limit.'))
        novel_layout.addRow(
            _('Glossary entries per prompt'), novel_glossary_prompt_max)
        self.disable_wheel_event(novel_glossary_prompt_max)

        novel_context_max_tokens = QSpinBox()
        novel_context_max_tokens.setRange(0, 200000)
        novel_context_max_tokens.setSingleStep(500)
        novel_context_max_tokens.setToolTip(_(
            'Hard limit on the length of the summary and glossary '
            'replies. Both are short by nature, but nothing in the '
            'request says so, and a model that starts repeating itself '
            'stops only at its own output limit: one glossary call was '
            'measured writing 131072 tokens over eight minutes, '
            're-listing names it had been told to skip.\n\n'
            'Only engines with a max tokens setting are affected, and a '
            'tighter limit you set yourself is left alone. Set to 0 to '
            'send no limit at all.'))
        novel_layout.addRow(
            _('Summary/glossary reply cap (tokens)'), novel_context_max_tokens)
        self.disable_wheel_event(novel_context_max_tokens)

        novel_summary_input = QSpinBox()
        novel_summary_input.setRange(0, 1000000)
        novel_summary_input.setSingleStep(5000)
        novel_summary_input.setToolTip(_(
            'How much of the translated chapter the summary and glossary '
            'requests read, in characters, counted from the top. The '
            'opening of a chapter is where its characters and setting '
            'are introduced; sending the whole chapter costs input tokens '
            'the answer does not need. 0 sends it whole.'))
        novel_layout.addRow(
            _('Chapter text for the summary (chars)'), novel_summary_input)
        self.disable_wheel_event(novel_summary_input)

        novel_reply_max = QSpinBox()
        novel_reply_max.setRange(0, 1000000)
        novel_reply_max.setSingleStep(1024)
        novel_reply_max.setToolTip(_(
            'The most a translation request may ask the model to write, '
            'in tokens, on engines that otherwise leave the figure to the '
            'provider (OpenRouter with "max_tokens" at 0). Left out, many '
            'providers apply 4096 and a chunk comes back cut at a third. '
            'Each chunk asks for twice its own size plus room for the '
            'JSON, up to this cap and to what the model can write. 0 '
            'sends nothing.'))
        novel_layout.addRow(
            _('Reply room per chunk (tokens)'), novel_reply_max)
        self.disable_wheel_event(novel_reply_max)

        novel_rate_limit = QSpinBox()
        novel_rate_limit.setRange(0, 86400)
        novel_rate_limit.setSingleStep(60)
        novel_rate_limit.setToolTip(_(
            'How long a request refused for the moment (HTTP 429, a '
            'provider at capacity) may be waited out, in seconds, before '
            'it counts as a failed attempt. The wait follows what the '
            'provider asks and grows a little each time. 0 treats a rate '
            'limit like any other error.'))
        novel_layout.addRow(
            _('Wait out rate limits for (seconds)'), novel_rate_limit)
        self.disable_wheel_event(novel_rate_limit)

        novel_missing = QComboBox()
        novel_missing.addItem(_('Stop, so a resume asks again'), 'stop')
        novel_missing.addItem(_('Skip them and go on'), 'continue')
        novel_missing.setToolTip(_(
            'What to do with paragraphs the model never returns, once '
            'the retries inside a chunk and one more pass in smaller '
            'chunks have all been tried.\n\n'
            'Stop leaves the chapter unfinished, so its progress is not '
            'recorded and a resume asks for exactly those paragraphs '
            'again. Skip logs them and goes on to the next chapter: they '
            'keep their source text in the output, and nothing comes '
            'back for them later.'))
        novel_layout.addRow(
            _('Paragraphs the model never returns'), novel_missing)
        self.disable_wheel_event(novel_missing)

        novel_min_chars = QSpinBox()
        novel_min_chars.setRange(0, 100000)
        novel_min_chars.setSingleStep(50)
        novel_min_chars.setToolTip(_(
            'Chapters shorter than this many translated characters skip '
            'the summary and glossary LLM calls. Useful to avoid wasting '
            'time on Copyright, Table of Contents, About the Author, '
            'etc. Set to 0 to always run summary/glossary.'))
        novel_layout.addRow(
            _('Skip context under (chars)'), novel_min_chars)
        self.disable_wheel_event(novel_min_chars)

        novel_context_reasoning = QCheckBox(_('Allow'))
        novel_context_reasoning.setToolTip(_(
            'Let the model think before writing the chapter summary and '
            'the glossary. Off by default: neither is a reasoning task, '
            'and on a measured chapter the glossary call spent three '
            'quarters of its billed output deliberating before listing a '
            'handful of proper nouns.\n\n'
            'This only ever turns an engine\'s reasoning down for those '
            'two calls, never on, and never touches the translation '
            'itself: the engine keeps whatever the Fine-tuning section '
            'says.'))
        novel_layout.addRow(
            _('Reasoning on summary/glossary'), novel_context_reasoning)

        novel_combined_context = QCheckBox(_('One request'))
        novel_combined_context.setToolTip(_(
            'Ask for the chapter summary and the new glossary entries in '
            'a single request instead of two.\n\n'
            'On by default. Both tasks read the chapter that was just '
            'translated, so two calls send it twice: measured on a real '
            'book the summary call carried 7000 to 8000 tokens of '
            'chapter text that the glossary call was about to send '
            'again.\n\n'
            'A summary or glossary prompt of your own turns this off by '
            'itself, so that prompt is not silently ignored.'))
        novel_layout.addRow(
            _('Summary and glossary'), novel_combined_context)

        novel_narrative_only = QCheckBox(_('Story chapters only'))
        novel_narrative_only.setToolTip(_(
            'Keep the summary and the glossary only for chapters that '
            'belong to the story. The model that summarises a chapter '
            'is asked whether the chapter is part of the story at all; '
            'a copyright page, a list of the author\'s other books, a '
            'preface, an afterword or a note then gets no summary and '
            'no glossary entries.\n\n'
            'On by default: a summary of the copyright page was carried '
            'into every later prompt as if it were plot. Turn it off to '
            'keep a summary of everything.'))
        novel_layout.addRow(
            _('Summary and glossary of'), novel_narrative_only)

        novel_skip_last = QCheckBox(_('Skip'))
        novel_skip_last.setToolTip(_(
            'Do not ask for the summary and glossary of the last '
            'chapter. The context of a chapter is built for the '
            'chapters that follow it, and after the last one there are '
            'none: that call sends a full copy of the chapter for an '
            'answer nothing ever reads.\n\n'
            'Turn it off if you keep the glossary as a document in its '
            'own right.'))
        novel_layout.addRow(
            _('Context of the last chapter'), novel_skip_last)

        novel_author_style = QComboBox()
        novel_author_style.addItem(
            _('Search the web when the engine can'), 'auto')
        novel_author_style.addItem(_('Ask the model only'), 'model')
        novel_author_style.addItem(_('Do not ask'), 'off')
        novel_author_style.setToolTip(_(
            'Before the first chapter, ask once how this author writes: '
            'the register, the texture of the sentences, the use of '
            'dialect or period language. The answer is repeated in the '
            'prompt of every chapter so the translation keeps the '
            'author\'s manner instead of drifting into neutral prose.\n\n'
            'It costs one request per book. "Search the web" uses the '
            'engine\'s own search where it has one (OpenRouter, Claude, '
            'Gemini) and falls back to what the model already knows '
            'elsewhere; searching is billed per result by the gateway. '
            'An answer that admits it knows nothing about the author is '
            'discarded rather than used.\n\n'
            'The book needs an author in its calibre metadata. The brief '
            'is stored with the summaries, so it is paid for once and '
            'reused when you resume; "Reset context" in the translation '
            'window discards it.'))
        novel_layout.addRow(
            _('How the author writes'), novel_author_style)
        self.disable_wheel_event(novel_author_style)

        novel_dialogue = QComboBox()
        novel_dialogue.addItem(_('Follow the source'), 'auto')
        for key, convention in DIALOGUE_CONVENTIONS.items():
            novel_dialogue.addItem(convention['label'], key)
        novel_dialogue.addItem(_('Leave it to the model'), 'off')
        novel_dialogue.setToolTip(_(
            'How direct speech is punctuated. A chapter is translated by '
            'several independent requests and none of them can see what '
            'the others chose, so a model left to itself opens one '
            'chapter with guillemets and the next with quotation '
            'marks.\n\n'
            '"Follow the source" reads the marks off the book, chapter '
            'by chapter, and goes with what most chapters use -- it '
            'costs nothing, the text is already here -- and states the '
            'answer in every request. Pick a convention instead when the '
            'source is inconsistent or you want the translation to use '
            'another one. Fill the field below to prescribe a rule of '
            'your own in words.'))
        novel_layout.addRow(
            _('Dialogue punctuation'), novel_dialogue)
        self.disable_wheel_event(novel_dialogue)

        novel_dialogue_rules = QPlainTextEdit()
        novel_dialogue_rules.setFixedHeight(60)
        novel_dialogue_rules.setPlaceholderText(_(
            'Empty: read the convention off the source text.'))
        novel_dialogue_rules.setToolTip(_(
            'Your own rule for punctuating dialogue, in the language of '
            'the prompt, added to every request instead of the one read '
            'off the source. For example: Mark direct speech with '
            '« guillemets » and a quotation inside it with '
            'double quotation marks.'))
        novel_layout.addRow(
            _('Dialogue rule'), novel_dialogue_rules)

        novel_translation_prompt = QPlainTextEdit()
        novel_translation_prompt.setFixedHeight(320)
        novel_translation_prompt.setToolTip(_(
            'System prompt for Novel Mode. It replaces the engine prompt '
            'of the Fine-tuning section entirely; leave it empty to use '
            'the shipped one, shown greyed out.\n\n'
            'Write it in plain prose: no placeholder is required. The '
            'languages and the running summary and glossary are added on '
            'their own when you do not mention them. Should you want to '
            'place them yourself, <slang> and <tlang> are the languages '
            'and {context} is the summary and glossary block, which is '
            'best kept last so the rest of the prompt stays reusable '
            'from the provider cache chapter after chapter.'))
        novel_layout.addRow(
            _('Translation prompt'), novel_translation_prompt)

        layout.addWidget(novel_group)

        # Wire novel settings to config on change.
        def load_novel_settings():
            from .lib.novel import DEFAULT_NOVEL_TRANSLATION_PROMPT
            src = self.config.get(
                'novel_chapter_source', 'toc_level_1') or 'toc_level_1'
            idx = novel_chapter_source.findData(src)
            if idx >= 0:
                novel_chapter_source.setCurrentIndex(idx)
            novel_front_matter_min.setValue(int(self.config.get(
                'novel_front_matter_min_chars', 100) or 0))
            novel_chunk_tokens.setValue(int(self.config.get(
                'novel_chunk_tokens', 16000) or 16000))
            novel_max_paragraphs.setValue(int(self.config.get(
                'novel_max_paragraphs_per_chunk', 75) or 0))
            novel_overlap.setValue(int(self.config.get(
                'novel_overlap_paragraphs', 5) or 0))
            structured_mode = self.config.get(
                'novel_structured_output', 'auto') or 'auto'
            idx = novel_structured.findData(structured_mode)
            if idx >= 0:
                novel_structured.setCurrentIndex(idx)
            novel_output_aware.setChecked(bool(self.config.get(
                'novel_output_aware_chunking', True)))
            novel_reuse_paragraphs.setChecked(bool(self.config.get(
                'novel_reuse_translated_paragraphs', True)))
            novel_prompt_cache.setChecked(bool(self.config.get(
                'novel_prompt_cache', True)))
            novel_context_tokens.setValue(int(self.config.get(
                'novel_context_tokens', 4000) or 4000))
            novel_summary_tokens.setValue(int(self.config.get(
                'novel_summary_tokens', 600) or 600))
            novel_glossary_max.setValue(int(self.config.get(
                'novel_glossary_max_entries', 500) or 500))
            novel_glossary_relevant.setChecked(bool(self.config.get(
                'novel_glossary_relevant_only', True)))
            novel_glossary_prompt_max.setValue(int(self.config.get(
                'novel_glossary_prompt_max_entries', 150) or 0))
            novel_context_max_tokens.setValue(int(self.config.get(
                'novel_context_max_tokens', 4000) or 0))
            novel_min_chars.setValue(int(self.config.get(
                'novel_min_chars_for_context', 300) or 0))
            novel_summary_input.setValue(int(self.config.get(
                'novel_summary_input_max_chars', 40000) or 0))
            novel_reply_max.setValue(int(self.config.get(
                'novel_reply_max_tokens', 16384) or 0))
            novel_rate_limit.setValue(int(self.config.get(
                'novel_rate_limit_max_wait', 600) or 0))
            idx = novel_missing.findData(self.config.get(
                'novel_on_missing_paragraphs', 'stop') or 'stop')
            if idx >= 0:
                novel_missing.setCurrentIndex(idx)
            novel_context_reasoning.setChecked(bool(self.config.get(
                'novel_context_reasoning', False)))
            novel_combined_context.setChecked(bool(self.config.get(
                'novel_combined_context_call', True)))
            novel_narrative_only.setChecked(bool(self.config.get(
                'novel_context_narrative_only', True)))
            novel_skip_last.setChecked(bool(self.config.get(
                'novel_skip_context_last_chapter', True)))
            novel_translation_prompt.setPlaceholderText(
                DEFAULT_NOVEL_TRANSLATION_PROMPT)
            novel_translation_prompt.setPlainText(
                self.config.get('novel_translation_prompt') or '')
            author_style = self.config.get(
                'novel_author_style', 'model') or 'model'
            idx = novel_author_style.findData(author_style)
            if idx >= 0:
                novel_author_style.setCurrentIndex(idx)
            dialogue = self.config.get(
                'novel_dialogue_convention', 'auto') or 'auto'
            idx = novel_dialogue.findData(dialogue)
            if idx >= 0:
                novel_dialogue.setCurrentIndex(idx)
            novel_dialogue_rules.setPlainText(
                self.config.get('novel_dialogue_rules') or '')
        load_novel_settings()

        def _persist_novel(key, cast):
            def _handler(value):
                try:
                    self.config.update(**{key: cast(value)})
                except (ValueError, TypeError):
                    pass
            return _handler

        novel_chapter_source.currentIndexChanged.connect(
            lambda _idx: self.config.update(
                novel_chapter_source=novel_chapter_source.currentData()))
        novel_front_matter_min.valueChanged.connect(
            _persist_novel('novel_front_matter_min_chars', int))
        novel_chunk_tokens.valueChanged.connect(
            _persist_novel('novel_chunk_tokens', int))
        novel_max_paragraphs.valueChanged.connect(
            _persist_novel('novel_max_paragraphs_per_chunk', int))
        novel_overlap.valueChanged.connect(
            _persist_novel('novel_overlap_paragraphs', int))
        novel_structured.currentIndexChanged.connect(
            lambda _idx: self.config.update(
                novel_structured_output=novel_structured.currentData()))
        novel_output_aware.toggled.connect(
            lambda checked: self.config.update(
                novel_output_aware_chunking=bool(checked)))
        novel_reuse_paragraphs.toggled.connect(
            lambda checked: self.config.update(
                novel_reuse_translated_paragraphs=bool(checked)))
        novel_prompt_cache.toggled.connect(
            lambda checked: self.config.update(
                novel_prompt_cache=bool(checked)))
        novel_context_tokens.valueChanged.connect(
            _persist_novel('novel_context_tokens', int))
        novel_summary_tokens.valueChanged.connect(
            _persist_novel('novel_summary_tokens', int))
        novel_glossary_max.valueChanged.connect(
            _persist_novel('novel_glossary_max_entries', int))
        novel_glossary_relevant.toggled.connect(
            lambda checked: self.config.update(
                novel_glossary_relevant_only=bool(checked)))
        novel_glossary_prompt_max.valueChanged.connect(
            _persist_novel('novel_glossary_prompt_max_entries', int))
        novel_context_max_tokens.valueChanged.connect(
            _persist_novel('novel_context_max_tokens', int))
        novel_min_chars.valueChanged.connect(
            _persist_novel('novel_min_chars_for_context', int))
        novel_summary_input.valueChanged.connect(
            _persist_novel('novel_summary_input_max_chars', int))
        novel_missing.currentIndexChanged.connect(
            lambda _idx: self.config.update(
                novel_on_missing_paragraphs=novel_missing.currentData()))
        novel_rate_limit.valueChanged.connect(
            _persist_novel('novel_rate_limit_max_wait', int))
        novel_reply_max.valueChanged.connect(
            _persist_novel('novel_reply_max_tokens', int))
        novel_context_reasoning.toggled.connect(
            lambda checked: self.config.update(
                novel_context_reasoning=bool(checked)))
        novel_combined_context.toggled.connect(
            lambda checked: self.config.update(
                novel_combined_context_call=bool(checked)))
        novel_narrative_only.toggled.connect(
            lambda checked: self.config.update(
                novel_context_narrative_only=bool(checked)))
        novel_skip_last.toggled.connect(
            lambda checked: self.config.update(
                novel_skip_context_last_chapter=bool(checked)))
        novel_author_style.currentIndexChanged.connect(
            lambda _idx: self.config.update(
                novel_author_style=novel_author_style.currentData()))
        novel_dialogue.currentIndexChanged.connect(
            lambda _idx: self.config.update(
                novel_dialogue_convention=novel_dialogue.currentData()))

        def _persist_prompt(widget, key):
            def _handler():
                text = widget.toPlainText().strip()
                if text:
                    self.config.update(**{key: text})
                else:
                    self.config.delete(key)
            return _handler

        novel_translation_prompt.textChanged.connect(
            _persist_prompt(
                novel_translation_prompt, 'novel_translation_prompt'))
        novel_dialogue_rules.textChanged.connect(
            _persist_prompt(novel_dialogue_rules, 'novel_dialogue_rules'))

        # Setup genAI model
        def update_model_limits(model, changed=False):
            """Show what the provider says the chosen model can do, and
            remember its reply limit.

            The limit is written into the engine preferences so a
            translation can size its requests against a real number
            without asking the provider again. It is only ever written
            when a listing has actually been fetched. With no listing to
            speak from, what was stored stands while the model is the
            same and is forgotten when the model ``changed``: the old
            model's parameter list applied to a new model filtered the
            request wrongly, and with require_parameters on OpenRouter
            found no provider for it.
            """
            details = getattr(self.current_engine, 'model_details', None)
            if not details:
                if changed:
                    config = self.current_engine.config
                    config.pop('model_max_output_tokens', None)
                    config.pop('model_supported_parameters', None)
                genai_model_limits.setVisible(False)
                return
            limits = self.current_engine.get_model_limits(model)
            context = limits.get('context_length')
            output = limits.get('max_output_tokens')
            self.current_engine.config.update(
                model_max_output_tokens=int(output or 0),
                model_supported_parameters=list(
                    limits.get('supported_parameters') or []))
            if not limits:
                genai_model_limits.setText(_(
                    'The provider says nothing about this model.'))
                genai_model_limits.setVisible(True)
                return

            def thousands(value):
                if not value:
                    return _('not stated')
                return '{:,}'.format(int(value)).replace(',', ' ')
            genai_model_limits.setText(_(
                'Context: {context} tokens \u00b7 longest reply: {output} '
                'tokens \u00b7 JSON schema: {json}').format(
                    context=thousands(context), output=thousands(output),
                    json=_('yes') if limits.get('structured_output')
                    else _('no')))
            genai_model_limits.setVisible(True)

        def init_ai_models(model=None):
            if not issubclass(self.current_engine, GenAI):
                return
            try:
                genai_model_list.currentTextChanged.disconnect()
            except TypeError:
                pass
            config = self.current_engine.config
            models = self.current_engine.models
            genai_model_refresh.setVisible(len(models) < 1)
            # Clear the model list to refill data
            genai_model_list.clear()
            genai_model_list.setDisabled(False)
            genai_model_list.addItems(models)
            genai_model_list.addItem(_('Custom'))
            # Fill data according to the passed model or the default model
            previous = config.get('model', self.current_engine.model)
            if model is None:
                model = previous
            elif model != _('Custom'):
                config.update(model=model)
            if model in models:
                genai_model_list.setCurrentText(model)
                genai_model_input.setVisible(False)
            else:
                genai_model_list.setCurrentText(_('Custom'))
                genai_model_input.setVisible(True)
                genai_model_input.setText(model)
                if model in models or model == _('Custom'):
                    genai_model_input.clear()
            update_model_limits(model, changed=model != previous)
            genai_model_list.currentTextChanged.connect(init_ai_models)
        self.model_worker.finished.connect(init_ai_models)

        def set_custom_model(model):
            model = model.strip()
            # The field is cleared when "Custom" is picked; the previous
            # model stands until something is typed.
            if not model:
                return
            config = self.current_engine.config
            changed = model != config.get('model', self.current_engine.model)
            config.update(model=model)
            update_model_limits(model, changed=changed)
        genai_model_input.textChanged.connect(set_custom_model)

        def fetch_ai_models():
            try:
                genai_model_list.currentTextChanged.disconnect()
            except TypeError:
                pass
            genai_model_refresh.setVisible(False)
            genai_model_list.clear()
            genai_model_list.addItem(_('Fetching...'))
            genai_model_list.setDisabled(True)
            genai_model_input.setVisible(False)
            self.current_engine.set_config(self.get_engine_config())
            self.model_worker.start.emit(self.current_engine)

        def has_api_key():
            return self.api_keys.toPlainText().strip() != '' \
                or not self.engine_needs_api_key()

        def manual_fetch_ai_models():
            if has_api_key():
                fetch_ai_models()
            else:
                self.alert.pop(_('You need to provide an API key to proceed.'))
        genai_model_refresh.clicked.connect(manual_fetch_ai_models)

        def auto_fetch_ai_models():
            if issubclass(self.current_engine, GenAI) \
                    and self.tabs.currentIndex() != 0 \
                    and len(self.current_engine.models) < 1 \
                    and has_api_key():
                fetch_ai_models()
            else:
                init_ai_models()
        self.fetch_models.connect(auto_fetch_ai_models)

        def handle_worker_status(success, message=''):
            genai_model_refresh.setVisible(not success)
            if not success:
                error_dialog(self, _("Can't fetch model list"), _(
                    "Can't fetch model list, please check and try again."),
                    message, show=True)
        self.model_worker.success.connect(handle_worker_status)

        def show_genai_preferences(config):
            if not issubclass(self.current_engine, GenAI):
                return
            genai_group.setVisible(True)
            is_gemini = issubclass(self.current_engine, GeminiTranslate)
            top_p_label.setText('topP' if is_gemini else 'top_p')
            top_k.setText('topK' if is_gemini else 'top_k')
            temperature.setVisible(not is_gemini)
            top_p.setVisible(not is_gemini)
            # Temperature range
            is_chatgpt = issubclass(self.current_engine, ChatgptTranslate)
            temperature_value.setRange(0, 2 if is_chatgpt else 1)
            # Provider
            providers = getattr(self.current_engine, 'providers', None) or {}
            provider_label.setVisible(bool(providers))
            provider_list.setVisible(bool(providers))
            try:
                provider_list.currentIndexChanged.disconnect()
            except TypeError:
                pass
            provider_list.clear()
            preset = {}
            if providers:
                for key, item in providers.items():
                    provider_list.addItem(item['label'], key)
                current = config.get('provider') or self.current_engine.provider
                provider_list.setCurrentIndex(
                    max(0, provider_list.findData(current)))
                provider_list.currentIndexChanged.connect(
                    lambda index: change_provider(
                        config, provider_list.itemData(index)))
                preset = self.current_engine.preset_for(config)
            # Endpoint. Not for OpenRouter: it is one server, and
            # another one that speaks the same API is a provider of the
            # OpenAI-compatible engine.
            has_endpoint = self.engine_has_endpoint()
            endpoint_label.setVisible(has_endpoint)
            self.genai_endpoint.setVisible(has_endpoint)
            default_endpoint = preset.get(
                'endpoint') or self.current_engine.endpoint
            self.genai_endpoint.setPlaceholderText(default_endpoint)
            self.genai_endpoint.setText(
                config.get('endpoint') or default_endpoint)
            self.genai_endpoint.setCursorPosition(0)
            # Models
            auto_fetch_ai_models()
            # Sampling
            if not issubclass(self.current_engine, GeminiTranslate):
                sampling = config.get('sampling', self.current_engine.sampling)
                btn_id = self.current_engine.samplings.index(sampling)
                sampling_btn_group.button(btn_id).setChecked(True)
            temperature_value.setValue(config.get(
                'temperature',
                preset.get('temperature', self.current_engine.temperature)))
            temperature_value.valueChanged.connect(
                lambda value: config.update(temperature=round(value, 1)))
            top_p_value.setValue(
                config.get('top_p', self.current_engine.top_p))
            top_p_value.valueChanged.connect(
                lambda value: config.update(top_p=round(value, 1)))
            top_k.setVisible(False)
            top_k_value.setVisible(False)
            if not issubclass(self.current_engine, ChatgptTranslate):
                top_k.setVisible(True)
                top_k_value.setVisible(True)
                top_k_value.setValue(
                    config.get('top_k', self.current_engine.top_k))
                top_k_value.valueChanged.connect(
                    lambda value: config.update(top_k=value))
            # Stream
            stream_enabled.setChecked(
                config.get('stream', self.current_engine.stream))
            stream_enabled.toggled.connect(
                lambda checked: config.update(stream=checked))
            # Reasoning: OpenRouter speaks a richer dialect and gets its
            # own control in the section below, so hide the simple one.
            show_reasoning = is_chatgpt and not issubclass(
                self.current_engine, OpenRouterTranslate)
            reasoning_label.setVisible(show_reasoning)
            reasoning_effort_list.setVisible(show_reasoning)
            if show_reasoning:
                effort = config.get(
                    'reasoning_effort', self.current_engine.reasoning_effort)
                reasoning_effort_list.blockSignals(True)
                reasoning_effort_list.setCurrentIndex(
                    max(reasoning_effort_list.findData(effort), 0))
                reasoning_effort_list.blockSignals(False)
            genai_group.setVisible(True)

        # What a provider keeps for itself when another one is chosen.
        per_provider = (
            'api_keys', 'endpoint', 'model', 'model_max_output_tokens',
            'model_supported_parameters')

        def change_provider(config, provider):
            engine = self.current_engine
            previous = config.get('provider') or engine.provider
            if provider == previous:
                return
            stash = config.setdefault('providers', {})
            # The keys as typed, so they are not lost with the switch.
            if self.engine_needs_api_key():
                config['api_keys'] = [
                    k.strip() for k in
                    self.api_keys.toPlainText().split('\n') if k.strip()]
            stash[previous] = {
                key: config[key] for key in per_provider if key in config}
            for key in per_provider:
                config.pop(key, None)
            config.update(stash.get(provider, {}))
            config['provider'] = provider
            preset = engine.preset_for(config)
            if 'model' not in config and preset.get('model'):
                config['model'] = preset['model']
            if 'temperature' in config and 'temperature' in preset:
                config['temperature'] = preset['temperature']
            # The listing belongs to the provider that answered it.
            engine.models = []
            engine.model_details = {}
            self.reformat_api_keys()
            show_genai_preferences(config)

        def choose_default_engine(index):
            engine_name = engine_list.itemData(index)
            self.config.update(translate_engine=engine_name)
            self.current_engine = get_engine_class(engine_name)
            config = self.current_engine.config
            # Refresh preferred language
            source_lang = config.get('source_lang')
            self.source_lang.refresh.emit(
                self.current_engine.lang_codes.get('source'),
                source_lang, True)
            target_lang = config.get('target_lang')
            self.target_lang.refresh.emit(
                self.current_engine.lang_codes.get('target'),
                target_lang)
            # show use notice
            show_tip = self.current_engine.using_tip is not None
            self.tip_group.setVisible(show_tip)
            if show_tip:
                self.using_tip.setText(self.current_engine.using_tip)
            # show api key setting
            self.reformat_api_keys()
            # Request setting
            value = config.get('request_interval')
            if value is None:
                value = self.current_engine.request_interval
            request_interval.setValue(float(value))
            value = config.get('request_attempt')
            if value is None:
                value = self.current_engine.request_attempt
            request_attempt.setValue(value)
            value = config.get('request_timeout')
            if value is None:
                value = self.current_engine.request_timeout
            request_timeout.setValue(float(value))
            value = config.get('max_error_count')
            if value is None:
                value = self.current_engine.max_error_count
            max_error_count.setValue(value)
            request_interval.valueChanged.connect(
                lambda value: config.update(request_interval=round(value, 1)))
            request_attempt.valueChanged.connect(
                lambda value: config.update(request_attempt=value))
            request_timeout.valueChanged.connect(
                lambda value: config.update(request_timeout=round(value, 1)))
            max_error_count.valueChanged.connect(
                lambda value: config.update(max_error_count=value))
            # Show GenAI preferences
            genai_group.setVisible(False)
            novel_group.setVisible(False)
            openrouter_group.setVisible(False)
            if issubclass(self.current_engine, GenAI):
                genai_group.setVisible(True)
                novel_group.setVisible(True)
                show_genai_preferences(config)
            if issubclass(self.current_engine, OpenRouterTranslate):
                openrouter_group.setVisible(True)
                show_openrouter_preferences(config)
        choose_default_engine(engine_list.findData(self.current_engine.name))
        engine_list.currentIndexChanged.connect(choose_default_engine)

        def make_test_translator():
            # This gets the current settings from the UI, not the saved ones.
            self.current_engine.set_config(self.get_engine_config())
            translator = self.current_engine()
            translator.set_proxy(
                self.proxy_type.currentText(),
                self.proxy_host.text(),
                self.proxy_port.text())
            EngineTester(self, translator)
        engine_test.clicked.connect(make_test_translator)

        layout.addStretch(1)

        return widget

    def engine_has_endpoint(self):
        """Whether the engine's endpoint is the user's to set."""
        return not issubclass(
            self.current_engine, (GeminiTranslate, OpenRouterTranslate))

    def engine_needs_api_key(self):
        engine = self.current_engine
        if hasattr(engine, 'needs_api_key'):
            return engine.needs_api_key(engine.config)
        return engine.need_api_key

    def reformat_api_keys(self):
        need_api_key = self.engine_needs_api_key()
        self.keys_group.setVisible(need_api_key)
        if need_api_key:
            engine = self.current_engine
            hint = engine.key_hint(engine.config) \
                if hasattr(engine, 'key_hint') else engine.api_key_hint
            self.api_keys.setPlaceholderText(hint)
            api_keys = self.current_engine.config.get('api_keys', [])
            self.api_keys.clear()
            for api_key in api_keys:
                self.api_keys.appendPlainText(api_key)

    @layout_scroll_area('content')
    def layout_content(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Translation Position
        position_radios = QWidget()
        position_radios_layout = QVBoxLayout(position_radios)
        position_radios_layout.setContentsMargins(0, 0, 0, 0)
        below_original = QRadioButton(_('Below original'))
        above_original = QRadioButton(_('Above original'))
        right_to_original = QRadioButton(
            '%s (%s)' % (_('Right to original'), _('Beta')))
        left_to_original = QRadioButton(
            '%s (%s)' % (_('Left to original'), _('Beta')))
        delete_original = QRadioButton(_('With no original'))
        position_radios_layout.addWidget(below_original)
        position_radios_layout.addWidget(above_original)
        position_radios_layout.addWidget(right_to_original)
        position_radios_layout.addWidget(left_to_original)
        position_radios_layout.addWidget(delete_original)
        position_radios_layout.addStretch(1)

        position_samples = QWidget()
        position_samples_layout = QVBoxLayout(position_samples)
        position_samples_layout.setContentsMargins(0, 0, 0, 0)
        position_samples_layout.setSpacing(10)
        original_sample = QLabel(_('Original'))
        original_sample.setAlignment(Qt.AlignCenter)
        original_sample.setWordWrap(True)
        original_sample.setStyleSheet(
            'border:1px solid rgba(127,127,127,.3);'
            'background-color:rgba(127,127,127,.1);padding:10px;'
            'color:rgba(0,0,0,.3);font-size:28px;')
        translation_sample = QLabel(_('Translation'))
        translation_sample.setAlignment(Qt.AlignCenter)
        translation_sample.setWordWrap(True)
        translation_sample.setStyleSheet(
            'border:1px solid rgba(127,127,127,.3);'
            'background-color:rgba(127,127,127,.1);padding:10px;'
            'color:black;font-size:28px;')
        position_samples_layout.addWidget(original_sample, 1)
        position_samples_layout.addWidget(translation_sample, 1)

        position_setup = QWidget()
        position_setup.setVisible(False)
        position_setup_layout = QHBoxLayout(position_setup)
        position_setup_layout.setContentsMargins(0, 0, 0, 0)
        column_gap_type = QComboBox()
        column_gap_value = QSpinBox()
        column_gap_value.setRange(1, 100)
        position_setup_layout.addWidget(QLabel(_('Column Gap')))
        position_setup_layout.addWidget(column_gap_type)
        position_setup_layout.addWidget(column_gap_value)
        percentage_unit = QLabel('%')
        position_setup_layout.addWidget(percentage_unit)
        position_setup_layout.addStretch(1)

        self.disable_wheel_event(column_gap_type)
        self.disable_wheel_event(column_gap_value)

        column_gap_type.addItem(_('Percentage'), 'percentage')
        column_gap_type.addItem(_('Space count'), 'space_count')

        column_gap_config = self.config.get('column_gap') or {}
        column_gap_config = column_gap_config.copy()

        current_type = column_gap_config.get('_type')
        current_index = column_gap_type.findData(current_type)
        percentage_unit.setVisible(current_type == 'percentage')
        column_gap_type.setCurrentIndex(current_index)
        column_gap_value.setValue(column_gap_config.get(current_type))

        def change_column_gap_value(value):
            gap_type = column_gap_type.currentData()
            column_gap_config.update({gap_type: value})
            self.config.update(column_gap=column_gap_config)
        column_gap_value.valueChanged.connect(change_column_gap_value)

        def change_column_gap_type(index):
            gap_type = column_gap_type.itemData(index)
            percentage_unit.setVisible(gap_type == 'percentage')
            column_gap_value.setValue(column_gap_config.get(gap_type))
            column_gap_config.update(_type=gap_type)
            self.config.update(column_gap=column_gap_config)
        column_gap_type.currentIndexChanged.connect(change_column_gap_type)

        position_preview = QWidget()
        position_preview_layout = QVBoxLayout(position_preview)
        position_preview_layout.setSpacing(10)
        position_preview_layout.setContentsMargins(0, 0, 0, 0)
        position_preview_layout.addWidget(position_samples, 1)
        position_preview_layout.addWidget(position_setup)

        position_group = QGroupBox(_('Translation Position'))
        position_layout = QHBoxLayout(position_group)
        position_layout.addWidget(position_preview, 1)
        position_layout.addSpacing(10)
        position_layout.addWidget(position_radios)

        layout.addWidget(position_group)

        position_map = dict(enumerate(
            ['below', 'above', 'right', 'left', 'only']))
        position_rmap = dict((v, k) for k, v in position_map.items())
        # Add alias for compatibility with lower versions.
        position_rmap['after'] = 0
        position_rmap['before'] = 1
        position_btn_group = QButtonGroup(position_group)
        position_btn_group.addButton(below_original, 0)
        position_btn_group.addButton(above_original, 1)
        position_btn_group.addButton(right_to_original, 2)
        position_btn_group.addButton(left_to_original, 3)
        position_btn_group.addButton(delete_original, 4)

        map_key = self.config.get('translation_position') or 'only'
        if map_key not in position_rmap.keys():
            map_key = 'only'
        position_btn_group.button(position_rmap.get(map_key)).setChecked(True)

        names = ('TopToBottom', 'BottomToTop', 'LeftToRight', 'RightToLeft')
        directions = [getattr(QBoxLayout.Direction, name) for name in names]

        def choose_option(btn_id):
            original_sample.setVisible(btn_id != 4)
            position_samples.layout().setDirection(
                directions[btn_id] if btn_id != 4 else directions[0])
            position_setup.setVisible(btn_id in [2, 3])
            self.config.update(translation_position=position_map.get(btn_id))
        choose_option(position_btn_group.checkedId())
        position_btn_group.idClicked.connect(choose_option)

        # Color group
        color_group = QWidget()
        color_group_layout = QHBoxLayout(color_group)
        color_group_layout.setContentsMargins(0, 0, 0, 0)

        # Original text color
        original_color_group = QGroupBox(_('Original Text Color'))
        original_color_layout = QHBoxLayout(original_color_group)
        self.original_color = QLineEdit()
        self.original_color.setText(self.config.get('original_color'))
        self.original_color.setPlaceholderText(
            '%s %s' % (_('e.g.,'), '#0055ff'))
        original_color_show = QLabel()
        original_color_show.setObjectName('original_color_show')
        original_color_show.setFixedWidth(25)
        self.setStyleSheet(
            '#original_color{margin:1px 0;border:1 solid #eee'
            ';border-radius:2px;}')
        original_color_button = QPushButton(_('Choose'))
        original_color_layout.addWidget(original_color_show)
        original_color_layout.addWidget(self.original_color)
        original_color_layout.addWidget(original_color_button)
        color_group_layout.addWidget(original_color_group)

        # Translation Color
        translation_color_group = QGroupBox(_('Translation Text Color'))
        translation_color_layout = QHBoxLayout(translation_color_group)
        self.translation_color = QLineEdit()
        self.translation_color.setPlaceholderText(
            '%s %s' % (_('e.g.,'), '#0055ff'))
        self.translation_color.setText(self.config.get('translation_color'))
        translation_color_show = QLabel()
        translation_color_show.setObjectName('translation_color')
        translation_color_show.setFixedWidth(25)
        self.setStyleSheet(
            '#translation_color{margin:1px 0;border:1 solid #eee;'
            'border-radius:2px;}')
        translation_color_button = QPushButton(_('Choose'))
        translation_color_layout.addWidget(translation_color_show)
        translation_color_layout.addWidget(self.translation_color)
        translation_color_layout.addWidget(translation_color_button)
        color_group_layout.addWidget(translation_color_group)

        layout.addWidget(color_group)

        def show_color(color_show, color):
            valid = QColor(color).isValid()
            color_show.setStyleSheet(
                'background-color:{};border-color:{};'
                .format(valid and color or 'black',
                        valid and color or 'black'))
        show_color(original_color_show, self.original_color.text())
        show_color(translation_color_show, self.translation_color.text())

        self.original_color.textChanged.connect(
            lambda: show_color(
                original_color_show, self.original_color.text()))
        self.translation_color.textChanged.connect(
            lambda: show_color(
                translation_color_show, self.translation_color.text()))

        def create_color_picker(color_widget, color_show):
            color_picker = QColorDialog(self)
            color_picker.setOption(
                QColorDialog.ColorDialogOption.DontUseNativeDialog)
            color_picker.colorSelected.connect(
                lambda color: color_widget.setText(color.name()))
            color_picker.colorSelected.connect(
                lambda color: show_color(color_show, color.name()))
            return color_picker

        original_color_picker = create_color_picker(
            self.original_color, original_color_show)
        original_color_button.clicked.connect(original_color_picker.open)
        translation_color_picker = create_color_picker(
            self.translation_color, translation_color_show)
        translation_color_button.clicked.connect(translation_color_picker.open)

        # Priority element
        priority_group = QGroupBox(_('Priority Element'))
        priority_layout = QVBoxLayout(priority_group)
        self.priority_rules = QPlainTextEdit()
        self.priority_rules.setPlaceholderText(
            '%s %s' % (_('e.g.,'), 'section, #content, div.portion'))
        self.priority_rules.setMinimumHeight(100)
        self.priority_rules.insertPlainText(
            '\n'.join(self.config.get('priority_rules') or []))
        priority_layout.addWidget(QLabel(
            _('CSS selectors for priority elements. One rule per line:')))
        priority_layout.addWidget(self.priority_rules)
        priority_layout.addWidget(QLabel('%s%s' % (
            _('Tip: '),
            _('Stop further extraction once elements match these rules.'))))
        layout.addWidget(priority_group)

        # Ignore element
        element_group = QGroupBox(_('Ignore Element'))
        element_layout = QVBoxLayout(element_group)
        self.ignore_rules = QPlainTextEdit()
        self.ignore_rules.setPlaceholderText(
            '%s %s' % (_('e.g.,'), 'table, table#report, table.list'))
        self.ignore_rules.setMinimumHeight(100)
        element_rules = self.config.get('element_rules')
        if element_rules is not None:
            self.ignore_rules.insertPlainText(
                '\n'.join(self.config.get(
                    'ignore_rules', element_rules) or []))
        element_layout.addWidget(QLabel(
            _('CSS selectors to exclude elements. One rule per line:')))
        element_layout.addWidget(self.ignore_rules)
        element_layout.addWidget(QLabel('%s%s' % (
            _('Tip: '),
            _('Do not translate elements that matches these rules.'))))
        layout.addWidget(element_group)

        # Filter Content
        filter_group = QGroupBox(_('Ignore Paragraph'))
        filter_layout = QVBoxLayout(filter_group)

        scope_group = QWidget()
        scope_layout = QHBoxLayout(scope_group)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        scope_layout.addWidget(QLabel(_('Scope')))
        scope_text = QRadioButton(_('Text only'))
        scope_text.setChecked(True)
        scope_element = QRadioButton(_('HTML element'))
        scope_layout.addWidget(scope_text)
        scope_layout.addWidget(scope_element, 1)

        mode_group = QWidget()
        mode_layout = QHBoxLayout(mode_group)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.addWidget(QLabel(_('Mode')))
        normal_mode = QRadioButton(_('Normal'))
        normal_mode.setChecked(True)
        inormal_mode = QRadioButton(_('Normal (case-sensitive)'))
        regex_mode = QRadioButton(_('Regular Expression'))
        mode_layout.addWidget(normal_mode)
        mode_layout.addWidget(inormal_mode)
        mode_layout.addWidget(regex_mode)
        mode_layout.addStretch(1)

        tip = QLabel()
        self.filter_rules = QPlainTextEdit()
        self.filter_rules.setMinimumHeight(100)
        self.filter_rules.insertPlainText(
            '\n'.join(self.config.get('filter_rules') or []))

        filter_layout.addWidget(scope_group)
        filter_layout.addWidget(mode_group)
        filter_layout.addWidget(self._divider())
        filter_layout.addWidget(tip)
        filter_layout.addWidget(self.filter_rules)
        filter_layout.addWidget(QLabel('%s%s' % (
            _('Tip: '), _('Do not translate extracted elements that contain '
            'these rules.'))))
        layout.addWidget(filter_group)

        scope_map = dict(enumerate(['text', 'html']))
        scope_rmap = dict((v, k) for k, v in scope_map.items())
        scope_btn_group = QButtonGroup(scope_group)
        scope_btn_group.addButton(scope_text, 0)
        scope_btn_group.addButton(scope_element, 1)

        filter_scope = self.config.get('filter_scope')
        if filter_scope is not None:
            scope_btn_group.button(
                scope_rmap.get(filter_scope)).setChecked(True)

        scope_btn_group.idClicked.connect(
            lambda btn_id: self.config.update(
                filter_scope=scope_map.get(btn_id)))

        mode_map = dict(enumerate(['normal', 'case', 'regex']))
        mode_rmap = dict((v, k) for k, v in mode_map.items())
        mode_btn_group = QButtonGroup(mode_group)
        mode_btn_group.addButton(normal_mode, 0)
        mode_btn_group.addButton(inormal_mode, 1)
        mode_btn_group.addButton(regex_mode, 2)

        tips = (
            _('Exclude paragraph by keyword. One keyword per line:'),
            _('Exclude paragraph by case-sensitive keyword.'
              ' One keyword per line:'),
            _('Exclude paragraph by regular expression pattern.'
              ' One pattern per line:'))

        def choose_filter_mode(btn_id):
            tip.setText(tips[btn_id])
            self.config.update(rule_mode=mode_map.get(btn_id))

        rule_mode = self.config.get('rule_mode')
        if rule_mode is not None:
            mode_btn_group.button(mode_rmap.get(rule_mode)).setChecked(True)
            tip.setText(tips[mode_btn_group.checkedId()])

        mode_btn_group.idClicked.connect(choose_filter_mode)

        # Reserve element
        reserve_group = QGroupBox(_('Reserve Element'))
        reserve_layout = QVBoxLayout(reserve_group)
        self.reserve_rules = QPlainTextEdit()
        self.reserve_rules.setPlaceholderText(
            '%s %s' % (_('e.g.,'), 'span.footnote, a#footnote'))
        self.reserve_rules.setMinimumHeight(100)
        self.reserve_rules.insertPlainText(
            '\n'.join(self.config.get('reserve_rules') or []))
        reserve_layout.addWidget(QLabel(
            _('CSS selectors to reserve elements. One rule per line:')))
        reserve_layout.addWidget(self.reserve_rules)
        reserve_layout.addWidget(QLabel('%s%s' % (
            _('Tip: '),
            _('Keep elements that match these rules for extraction.'))))
        layout.addWidget(reserve_group)

        # Ebook Metadata
        metadata_group = QGroupBox(_('Ebook Metadata'))
        metadata_layout = QFormLayout(metadata_group)
        self.apply_form_layout_policy(metadata_layout)
        self.metadata_translation = QCheckBox(
            _('Translate all of the metadata information'))
        self.metadata_lang_mark = QCheckBox(
            _('Append target language to title metadata'))
        self.metadata_lang_code = QCheckBox(
            _('Set target language code to language metadata'))
        self.metadata_subject = QPlainTextEdit()
        self.metadata_subject.setPlaceholderText(
            _('Subjects of ebook (one subject per line)'))
        metadata_layout.addRow(
            _('Metadata Translation'), self.metadata_translation)
        metadata_layout.addRow(_('Language Mark'), self.metadata_lang_mark)
        metadata_layout.addRow(_('Language Code'), self.metadata_lang_code)
        metadata_layout.addRow(_('Append Subjects'), self.metadata_subject)
        layout.addWidget(metadata_group)

        self.metadata_translation.setChecked(
            self.config.get('ebook_metadata.metadata_translation', False))
        self.metadata_lang_mark.setChecked(
            self.config.get('ebook_metadata.lang_mark', False))
        self.metadata_lang_code.setChecked(self.config.get(
            'ebook_metadata.lang_code',
            self.config.get('ebook_metadata.language', False)))  # old key
        self.metadata_subject.setPlainText(
            '\n'.join(self.config.get('ebook_metadata.subjects') or []))

        layout.addStretch(1)

        return widget

    def test_proxy_connection(self):
        proxy_type = self.proxy_type.currentText()
        host = self.proxy_host.text()
        port = self.proxy_port.text()
        if not (self.is_valid_data(self.host_validator, host) and port):
            return self.alert.pop(
                _('Proxy host or port is incorrect.'), level='warning')
        if proxy_type == 'http':
            if is_proxy_available(host, port):
                return self.alert.pop(_('The proxy is available.'))
            return self.alert.pop(_('The proxy is not available.'), 'error')
        elif proxy_type == 'socks5':
            try:
                with socks_proxy(host, port) as socket:
                    # Test connection to a known external host
                    test_socket = socket.socket(
                        socket.AF_INET, socket.SOCK_STREAM)
                    test_socket.settimeout(10)
                    test_socket.connect(("www.google.com", 80))
                    test_socket.close()
                    self.alert.pop(_('The proxy is available.'))
            except Exception as e:
                self.alert.pop(
                    _('The proxy is not available.') + f'\nError: {e}',
                    'error')

    def is_valid_data(self, validator, value):
        state = validator.validate(value, 0)[0]
        return state.value == 2

    def update_general_config(self):
        # Output path
        if not self.config.get('to_library'):
            output_path = self.output_path_entry.text()
            if not os.path.exists(output_path):
                self.alert.pop(
                    _('The specified path does not exist.'), 'warning')
                return False
            self.config.update(output_path=output_path.strip())

        # Proxy setting
        proxy_setting = self.config.get('proxy_setting') or {}
        proxy_type = self.proxy_type.currentText()
        host = self.proxy_host.text()
        port = self.proxy_port.text()
        if self.config.get('proxy_enabled') or (host or port):
            if not (self.is_valid_data(self.host_validator, host) and port):
                self.alert.pop(
                    _('Proxy host or port is incorrect.'), level='warning')
                return False
            self.config.update(proxy_type=proxy_type)
            # Compatible with old proxy settings stored as a list.
            if isinstance(proxy_setting, list):
                proxy_setting = {} if len(proxy_setting) < 1 else \
                    {'http': proxy_setting}
            proxy_setting[proxy_type] = [host, int(port)]
            self.config.update(proxy_setting=proxy_setting)
        if len(proxy_setting) < 1:
            self.config.delete('proxy_setting')

        return True

    def get_engine_config(self) -> dict:
        config = self.current_engine.config
        # API key
        if self.engine_needs_api_key():
            api_keys = []
            api_key_validator = QRegularExpressionValidator(
                QRegularExpression(self.current_engine.api_key_pattern))
            key_str = re.sub('\n+', '\n', self.api_keys.toPlainText()).strip()
            for key in [k.strip() for k in key_str.split('\n')]:
                if self.is_valid_data(api_key_validator, key):
                    api_keys.append(key)
            config.update(api_keys=api_keys)
            self.reformat_api_keys()

        # GenAI preference
        if issubclass(self.current_engine, GenAI):
            if 'endpoint' in config:
                del config['endpoint']
            if self.engine_has_endpoint():
                endpoint = self.genai_endpoint.text().strip()
                if endpoint and endpoint != self.current_engine.endpoint:
                    config.update(endpoint=endpoint)
        # Preferred Language
        source_lang = self.source_lang.currentText()
        if 'source_lang' in config:
            del config['source_lang']
        if source_lang != _('Auto detect'):
            config.update(source_lang=source_lang)
        config.update(target_lang=self.target_lang.currentText())

        return config

    def update_engine_config(self):
        config = self.get_engine_config()
        # Do not update directly as you may get default preferences!
        engine_config = self.config.get('engine_preferences') or {}
        engine_config = engine_config.copy()
        engine_config.update({self.current_engine.name: config})
        # Cleanup unused engine preferences
        engine_names = [engine.name for engine in builtin_engines]
        for name in engine_config.copy():
            if name not in engine_names:
                engine_config.pop(name)
        # Update modified engine preferences
        self.config.update(engine_preferences=engine_config)
        return True

    def update_content_config(self):
        # Original text color
        original_color = self.original_color.text()
        if original_color and not QColor(original_color).isValid():
            self.alert.pop(_('Invalid color value.'), 'warning')
            return False
        self.config.update(original_color=original_color or None)

        # Translation color
        translation_color = self.translation_color.text()
        if translation_color and not QColor(translation_color).isValid():
            self.alert.pop(_('Invalid color value.'), 'warning')
            return False
        self.config.update(translation_color=translation_color or None)

        # Priority rules
        rule_content = self.priority_rules.toPlainText()
        priority_rules = [r for r in rule_content.split('\n') if r.strip()]
        for rule in priority_rules:
            if css(rule) is None:
                self.alert.pop(
                    _('{} is not a valid CSS selector.')
                    .format(rule), 'warning')
                return False
        self.config.delete('priority_rules')
        if priority_rules:
            self.config.update(priority_rules=priority_rules)

        # Filter rules
        rule_content = self.filter_rules.toPlainText()
        filter_rules = [r for r in rule_content.split('\n') if r.strip()]
        if self.config.get('rule_mode') == 'regex':
            for rule in filter_rules:
                if not self.is_valid_regex(rule):
                    self.alert.pop(
                        _('{} is not a valid regular expression.')
                        .format(rule), 'warning')
                    return False
        self.config.delete('filter_rules')
        if filter_rules:
            self.config.update(filter_rules=filter_rules)

        # Element rules
        rule_content = self.ignore_rules.toPlainText()
        ignore_rules = [r for r in rule_content.split('\n') if r.strip()]
        for rule in ignore_rules:
            if css(rule) is None:
                self.alert.pop(
                    _('{} is not a valid CSS selector.')
                    .format(rule), 'warning')
                return False
        self.config.delete('element_rules')
        self.config.delete('ignore_rules')
        if ignore_rules:
            self.config.update(ignore_rules=ignore_rules)

        # Reserve rules
        rule_content = self.reserve_rules.toPlainText()
        reserve_rules = [r for r in rule_content.split('\n') if r.strip()]
        for rule in reserve_rules:
            if css(rule) is None:
                self.alert.pop(
                    _('{} is not a valid CSS selector.')
                    .format(rule), 'warning')
                return False
        self.config.delete('reserve_rules')
        if reserve_rules:
            self.config.update(reserve_rules=reserve_rules)

        # Ebook metadata
        ebook_metadata = self.config.get('ebook_metadata') or {}
        ebook_metadata = ebook_metadata.copy()
        ebook_metadata.clear()
        ebook_metadata.update(
            metadata_translation=self.metadata_translation.isChecked())
        ebook_metadata.update(lang_mark=self.metadata_lang_mark.isChecked())
        ebook_metadata.update(lang_code=self.metadata_lang_code.isChecked())
        subject_content = self.metadata_subject.toPlainText().strip()
        if subject_content:
            subjects = [s.strip() for s in subject_content.split('\n')]
            ebook_metadata.update(subjects=subjects)
        self.config.update(ebook_metadata=ebook_metadata)
        return True

    def is_valid_regex(self, rule):
        try:
            re.compile(rule)
        except Exception:
            return False
        return True

    def disable_wheel_event(self, widget):
        widget.wheelEvent = lambda event: None

    def apply_form_layout_policy(self, layout):
        layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.setLabelAlignment(Qt.AlignRight)

    def done(self, result):
        self.model_thread.quit()
        self.model_thread.wait()
        QDialog.done(self, result)
