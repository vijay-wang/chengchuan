"""One bounded latest-value stream buffer; network stays outside the Qt thread."""

import threading

from PySide6.QtCore import QObject, QTimer, Signal

from chengchuan.application.errors import MarketError
from chengchuan.domain.microstructure import BookSnapshot


class BookStreamRunner(QObject):
    ready = Signal(object)
    status = Signal(str)
    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stop_event = None
        self.closed = False
        self.generation = 0
        self.latest = None
        self.message = None
        self.ended = None
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._flush)

    def start(self, function):
        self.stop()
        self.closed = False
        self.generation += 1
        generation = self.generation
        stop = threading.Event()
        self.stop_event = stop
        self.ended = None

        def run():
            try:
                for item in function(stop):
                    if stop.is_set() or self.closed or self.generation != generation:
                        break
                    if isinstance(item, BookSnapshot):
                        self.latest = (generation, item)
                    else:
                        self.message = (generation, str(item))
            except Exception as error:
                if not stop.is_set() and not self.closed and self.generation == generation:
                    self.message = (
                        generation,
                        str(error)
                        if isinstance(error, MarketError)
                        else "订阅未完成或已中断；可手动重连",
                    )
            finally:
                if self.generation == generation:
                    self.ended = generation

        threading.Thread(target=run, daemon=True, name="chengchuan-book-stream").start()
        self.timer.start()

    def _flush(self):
        latest, self.latest = self.latest, None
        message, self.message = self.message, None
        if latest and latest[0] == self.generation:
            self.ready.emit(latest[1])
        if message and message[0] == self.generation:
            self.status.emit(message[1])
        if self.ended == self.generation:
            self.timer.stop()
            self.finished.emit()

    def stop(self):
        self.generation += 1
        if self.stop_event:
            self.stop_event.set()
        self.timer.stop()
        self.latest = self.message = None
        self.ended = None

    def shutdown(self):
        self.closed = True
        self.stop()
