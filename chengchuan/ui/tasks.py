import logging
import multiprocessing
import time
import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

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


def _process_loop(connection):
    """Reuse imports between local tasks. No result/function is retained while idle."""
    try:
        while True:
            try:
                token, function = connection.recv()
            except (EOFError, OSError):
                break
            try:
                result = function()
            except Exception as exc:
                connection.send((token, False, str(exc), traceback.format_exc()))
            else:
                connection.send((token, True, result, ""))
                del result
            finally:
                del function
    finally:
        connection.close()


class ProcessTaskRunner(QObject):
    """网络任务使用可终止进程；新请求直接替换旧请求，关闭不等待网络超时。"""

    ready = Signal(object)
    failed = Signal(str)
    busy = Signal(bool)

    def __init__(self, parent=None, timeout_ms=18000, task_label="行情", idle_ms=10000):
        super().__init__(parent)
        self.context = multiprocessing.get_context("spawn")
        self.active = None
        self._worker = None
        self._generation = self._jobs = 0
        self.connection = None
        self.closed = False
        self.timeout_ms = timeout_ms
        self.task_label = task_label
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._poll)
        self.idle_timer = QTimer(self)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.setInterval(idle_ms)
        self.idle_timer.timeout.connect(self._retire)

    def submit(self, revision, selection, function):
        if self.closed:
            return
        self.cancel()
        self.idle_timer.stop()
        try:
            if self._worker is None or not self._worker.is_alive() or self._jobs >= 32:
                self._retire()
                receive, send = self.context.Pipe(duplex=True)
                process = self.context.Process(target=_process_loop, args=(send,), daemon=True)
                try:
                    process.start()
                except Exception:
                    receive.close()
                    raise
                finally:
                    send.close()
                self._worker, self.connection, self._jobs = process, receive, 0
            self._generation += 1
            self.connection.send((self._generation, function))
        except Exception as exc:
            self._retire()
            self.failed.emit(f"无法启动{self.task_label}任务：" + str(exc))
            return
        self.active = self._worker
        self._jobs += 1
        self.started = time.monotonic()
        self.busy.emit(True)
        self.timer.start()

    def _poll(self):
        if self.active is None:
            return
        if self.connection.poll():
            token = self._generation
            try:
                token, ok, result, details = self.connection.recv()
                if token != self._generation:
                    return
            except (EOFError, OSError):
                ok, result, details = False, f"{self.task_label}任务意外退出，请重试", ""
            self.timer.stop()
            self.active = None
            self.idle_timer.start()
            self.busy.emit(False)
            if token != self._generation:
                return
            if not ok:
                log.error("%s任务失败：%s\n%s", self.task_label, result, details)
            (self.ready if ok else self.failed).emit(result)
        elif not self.active.is_alive():
            self.cancel()
            self.failed.emit(f"{self.task_label}任务意外退出，请重试")
        elif (time.monotonic() - self.started) * 1000 >= self.timeout_ms:
            self.cancel()
            hint = "请检查网络后刷新" if self.task_label == "行情" else "请缩小范围或稍后重试"
            self.failed.emit(f"{self.task_label}请求超时，{hint}")

    def cancel(self):
        self.timer.stop()
        self.idle_timer.stop()
        self._generation += 1
        if self.active is not None:
            self._retire()
        elif self._worker is not None:
            self.idle_timer.start()
        self.busy.emit(False)

    def _retire(self):
        self.idle_timer.stop()
        self.timer.stop()
        process, self._worker, self.active = self._worker, None, None
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(0.15)
            if process.is_alive():
                process.kill()
                process.join(0.15)
            if not process.is_alive():
                process.close()
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def shutdown(self):
        self.closed = True
        self._retire()
        self.busy.emit(False)
