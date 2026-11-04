"""Unified settings UI. Persistence and credential access are injected use cases."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from chengchuan import __version__
from chengchuan.application.settings import AppSettings, SettingsSaveError


def caption(text, name="muted"):
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(True)
    return label


def card(title):
    frame = QFrame()
    frame.setObjectName("settingsCard")
    box = QVBoxLayout(frame)
    box.setContentsMargins(18, 16, 18, 18)
    box.setSpacing(12)
    box.addWidget(caption(title, "sectionTitle"))
    return frame, box


class SettingsDialog(QDialog):
    page_codes = ("data", "general", "storage")

    def __init__(self, settings, info, save_handler, info_handler, can_configure=True, parent=None):
        super().__init__(parent)
        self.save_handler, self.info_handler = save_handler, info_handler
        self._source_chosen = False
        self.setWindowTitle("设置 · 澄川")
        self.resize(900, 700)
        self.setMinimumSize(760, 590)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QHBoxLayout()
        body.setSpacing(0)
        side = QWidget()
        side.setObjectName("settingsSidebar")
        side.setFixedWidth(170)
        left = QVBoxLayout(side)
        left.setContentsMargins(18, 24, 12, 18)
        left.addWidget(caption("设置", "stockTitle"))
        left.addWidget(caption("让工作台适合你的习惯", "eyebrow"))
        self.navigation = QListWidget()
        self.navigation.addItems(["数据连接", "常规与图表", "本地存储"])
        left.addWidget(self.navigation, 1)
        left.addWidget(caption(f"澄川 Chengchuan\nv{__version__}", "eyebrow"))
        body.addWidget(side)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)
        outer.addLayout(body, 1)
        self._data_page(settings, info, can_configure)
        self._general_page(settings)
        self._storage_page(info)
        footer = QWidget()
        footer.setObjectName("settingsFooter")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(18, 14, 20, 14)
        self.error = caption("")
        foot.addWidget(self.error, 1)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("保存设置")
        self.save_button.setObjectName("primary")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        foot.addWidget(self.buttons)
        outer.addWidget(footer)
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        self.finished.connect(self._clear_secret)

    def _page(self, title, subtitle):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        box = QVBoxLayout(content)
        box.setContentsMargins(28, 24, 28, 24)
        box.setSpacing(16)
        box.addWidget(caption(title, "sectionTitle"))
        box.addWidget(caption(subtitle))
        scroll.setWidget(content)
        self.pages.addWidget(scroll)
        return box

    def _data_page(self, settings, info, can_configure):
        box = self._page("数据连接", "选择行情来源，为这台电脑配置访问凭据。")
        frame, group = card("行情数据源")
        self.provider = QComboBox()
        for code, title in (
            ("auto", "自动选择"),
            ("meoz", "猫爪 · 12周期"),
            ("tencent", "腾讯 · 基础行情"),
        ):
            self.provider.addItem(title, code)
        self.provider.setCurrentIndex(self.provider.findData(settings.provider))
        self.provider.setEnabled(can_configure)
        self.provider.activated.connect(self._provider_chosen)
        group.addWidget(self.provider)
        group.addWidget(caption("猫爪支持全部12周期和历史日期；腾讯提供日K与采样分时。", "eyebrow"))
        box.addWidget(frame)
        frame, group = card("猫爪 API Key")
        self.credential_status = caption(info.credential_status)
        group.addWidget(self.credential_status)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("输入新API Key；留空保留已有配置")
        self.api_key.setMaxLength(128)
        self.api_key.setEnabled(can_configure)
        self.api_key.textEdited.connect(self._key_edited)
        group.addWidget(self.api_key)
        self.reveal_key = QCheckBox("显示本次输入")
        self.reveal_key.setEnabled(can_configure)
        self.reveal_key.toggled.connect(
            lambda checked: self.api_key.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        group.addWidget(self.reveal_key)
        group.addWidget(
            caption(
                "已有密钥不回显。输入新密钥时默认选择猫爪，可在上方改选；保存到本机，不随仓库同步。",
                "eyebrow",
            )
        )
        group.addWidget(
            caption(
                "MEOZ_API_KEY环境变量优先于本机配置文件。此处保存文件，不修改环境变量。", "eyebrow"
            )
        )
        box.addWidget(frame)
        box.addStretch()

    def _provider_chosen(self):
        self._source_chosen = True

    def _key_edited(self, text):
        if text.strip() and not self._source_chosen:
            self.provider.setCurrentIndex(self.provider.findData("meoz"))

    def _general_page(self, settings):
        box = self._page("常规与图表", "保存后立即生效，下次打开软件继续使用。")
        frame, group = card("行情更新")
        self.auto_refresh = QCheckBox("自动检查行情更新")
        self.auto_refresh.setChecked(settings.auto_refresh)
        group.addWidget(self.auto_refresh)
        self.refresh_seconds = QSpinBox()
        self.refresh_seconds.setRange(15, 3600)
        self.refresh_seconds.setSingleStep(15)
        self.refresh_seconds.setSuffix(" 秒")
        self.refresh_seconds.setValue(settings.refresh_seconds)
        form = QFormLayout()
        form.addRow("检查间隔", self.refresh_seconds)
        group.addLayout(form)
        group.addWidget(
            caption(
                "检查优先读取缓存；猫爪付费行情仍至少复用180秒，历史浏览暂停自动更新。", "eyebrow"
            )
        )
        box.addWidget(frame)
        frame, group = card("图表悬停")
        self.show_crosshair = QCheckBox("显示十字虚线")
        self.show_crosshair.setChecked(settings.show_crosshair)
        self.show_tooltip = QCheckBox("显示跟随鼠标的行情浮窗")
        self.show_tooltip.setChecked(settings.show_tooltip)
        group.addWidget(self.show_crosshair)
        group.addWidget(self.show_tooltip)
        line = QHBoxLayout()
        line.addWidget(caption("浮窗不透明度"))
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(30, 90)
        self.opacity.setValue(settings.tooltip_opacity)
        self.opacity_value = QLabel(f"{settings.tooltip_opacity}%")
        self.opacity_value.setFixedWidth(40)
        self.opacity.valueChanged.connect(lambda value: self.opacity_value.setText(f"{value}%"))
        line.addWidget(self.opacity, 1)
        line.addWidget(self.opacity_value)
        group.addLayout(line)
        box.addWidget(frame)
        box.addStretch()

    def _storage_page(self, info):
        box = self._page("本地存储", "查看数据保存位置和本机请求计数。")
        frame, group = card("存储位置")
        for title, path in info.paths:
            group.addWidget(caption(title, "eyebrow"))
            field = QLineEdit(path)
            field.setReadOnly(True)
            group.addWidget(field)
        if not info.paths:
            group.addWidget(caption("当前运行环境未提供存储位置。"))
        box.addWidget(frame)
        frame, group = card("请求与缓存")
        form = QFormLayout()
        for title, value in info.usage:
            form.addRow(title, QLabel(value))
        group.addLayout(form)
        group.addWidget(
            caption(
                "这里显示打开设置时的本机计数，不包括其他设备。缓存命中不消耗调用，界面检查间隔不会绕过额度保护。",
                "eyebrow",
            )
        )
        box.addWidget(frame)
        box.addStretch()

    def select_page(self, code):
        self.navigation.setCurrentRow(self.page_codes.index(code))

    def _save(self):
        try:
            settings = AppSettings(
                provider=self.provider.currentData(),
                auto_refresh=self.auto_refresh.isChecked(),
                refresh_seconds=self.refresh_seconds.value(),
                show_crosshair=self.show_crosshair.isChecked(),
                show_tooltip=self.show_tooltip.isChecked(),
                tooltip_opacity=self.opacity.value(),
            )
            self.save_handler(settings, self.api_key.text())
        except ValueError as exc:
            if isinstance(exc, SettingsSaveError) and exc.key_saved:
                self._clear_secret()
                self.credential_status.setText(self.info_handler().credential_status)
            self.error.setText(str(exc))
            self.error.setStyleSheet("color: #f07b86;")
            return
        self.accept()

    def _clear_secret(self):
        self.api_key.setText("")
        self.reveal_key.setChecked(False)
