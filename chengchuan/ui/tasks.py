import logging

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from chengchuan.application.services import RequestGate

log = logging.getLogger(__name__)


class _Signals(QObject):
    done = Signal(object, object, str)


class _Work(QRunnable):
    def __init__(self, token, function):
        super().__init__()
        self.token, self.function = token, function
        self.signals = _Signals()

    def run(self):
        try:
            result = self.function()
        except Exception as exc:
            log.exception("后台任务失败")
            self.signals.done.emit(self.token, None, str(exc))
        else:
            self.signals.done.emit(self.token, result, "")


class LatestTaskRunner(QObject):
    """最多一个运行任务和一个待执行任务；新请求覆盖待执行项。"""

    ready = Signal(object)
    failed = Signal(str)
    busy = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.gate = RequestGate()
        self.active = None
        self.pending = None
        self.closed = False

    def submit(self, revision, selection, function):
        if self.closed:
            return
        token = self.gate.begin(revision, selection)
        self.busy.emit(True)
        task = (token, function)
        if self.active is None:
            self._start(task)
        else:
            self.pending = task

    def _start(self, task):
        work = _Work(*task)
        self.active = work
        work.signals.done.connect(self._finished)
        self.pool.start(work)

    @Slot(object, object, str)
    def _finished(self, token, result, error):
        self.active = None
        next_task, self.pending = self.pending, None
        if not self.closed and self.gate.is_current(token):
            if error:
                self.failed.emit(error)
            else:
                self.ready.emit(result)
        # A ready callback may synchronously submit another task.
        if self.active is None and next_task is not None and not self.closed:
            self._start(next_task)
        if self.active is None:
            self.busy.emit(False)

    def cancel(self):
        self.gate.invalidate()
        self.pending = None

    def shutdown(self):
        self.closed = True
        self.cancel()
        self.pool.waitForDone(3000)
