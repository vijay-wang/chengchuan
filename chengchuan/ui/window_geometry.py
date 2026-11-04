"""Resizable tool windows, including a fallback for desktops without a WM."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog


class MaximizableDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self.setSizeGripEnabled(True)
        self._normal_rect = None
        self._fallback_max = False
        self._maximize_timer = QTimer(self)
        self._maximize_timer.setSingleShot(True)
        self._maximize_timer.timeout.connect(self._ensure_maximized_geometry)

    def showMaximized(self):
        if not self.isMaximized():
            self._normal_rect = self.geometry()
            self._fallback_max = False
        super().showMaximized()
        self._maximize_timer.start(80)

    def _ensure_maximized_geometry(self):
        if not self.isMaximized() or self.screen() is None:
            return
        available = self.screen().availableGeometry()
        if self.width() < available.width() * 0.9 or self.height() < available.height() * 0.9:
            self._fallback_max = True
            self.setGeometry(available)
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def showNormal(self):
        self._maximize_timer.stop()
        fallback = self._fallback_max
        super().showNormal()
        if fallback and self._normal_rect is not None:
            self.setGeometry(self._normal_rect)
        self._fallback_max = False
