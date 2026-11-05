"""股票搜索与自选；数据获取由注入用例执行。"""

from functools import partial

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from chengchuan.application.errors import StorageError
from chengchuan.application.memory_cache import MemoryCache
from chengchuan.domain.market import Instrument, instrument_symbol


class MarketBrowser(QWidget):
    open_requested = Signal(str)

    def __init__(self, market, workspace, runner_factory, parent=None):
        super().__init__(parent)
        self.market, self.workspace = market, workspace
        self.cache = MemoryCache(capacity=64, max_weight=2000)
        self._search_query = None
        self.runner = runner_factory(self)
        self.runner.ready.connect(self._searched)
        self.runner.failed.connect(self._failed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("名称 / 代码 / 拼音，回车打开")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.hint = QLabel("输入六位代码可直接打开沪深A股")
        self.hint.setObjectName("eyebrow")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.results = QListWidget()
        self.results.setMaximumHeight(150)
        self.results.hide()
        self.results.itemClicked.connect(self._open_item)
        layout.addWidget(self.results)
        layout.addWidget(QLabel("自选证券"))
        self.watchlist = QListWidget()
        self.watchlist.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.watchlist.setMinimumHeight(90)
        self.watchlist.itemClicked.connect(self._open_item)
        layout.addWidget(self.watchlist, 1)
        remove = QPushButton("移出自选")
        remove.clicked.connect(self.remove_selected)
        layout.addWidget(remove)
        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.search_now)
        self.search.textEdited.connect(self._edited)
        self.search.returnPressed.connect(self._enter)
        saved = self.workspace.load("watchlist").get("items", [])
        for row in saved[:100]:
            try:
                self._add_item(self.watchlist, Instrument(**row))
            except (TypeError, ValueError):
                continue
        self.watchlist.model().rowsMoved.connect(self._save)

    def _add_item(self, target, instrument):
        item = QListWidgetItem(f"{instrument.name}  {instrument.symbol[-6:]}")
        item.setData(Qt.ItemDataRole.UserRole, instrument)
        target.addItem(item)

    def set_market(self, market):
        self.timer.stop()
        self.runner.cancel()
        self.cache.clear()
        self._search_query = None
        self.market = market
        self.results.clear()
        self.results.hide()
        self.hint.setText("已切换数据源，输入名称／代码重新搜索")

    def _open_item(self, item):
        self.open_requested.emit(item.data(Qt.ItemDataRole.UserRole).symbol)

    def _edited(self, text):
        self.timer.stop()
        self.runner.cancel()
        self.results.clear()
        self.results.hide()
        if text.strip():
            self.timer.start()
        else:
            self.hint.setText("输入六位代码可直接打开沪深A股")

    def _enter(self):
        self.timer.stop()
        query = self.search.text().strip()
        try:
            symbol = instrument_symbol(query)
        except ValueError:
            self.search_now()
        else:
            self.runner.cancel()
            self.open_requested.emit(symbol)

    def search_now(self):
        query = self.search.text().strip()
        if not query:
            return
        self._search_query = query
        cached = self.cache.get(query.casefold(), max_age=60)
        if cached is not None:
            self.runner.cancel()
            self._searched(cached, remember=False)
            return
        self.hint.setText("正在搜索沪深A股…")
        self.results.clear()
        self.runner.submit(query, None, partial(self.market.search, query))

    def _searched(self, result, remember=True):
        if remember and self._search_query:
            self.cache.put(self._search_query.casefold(), result, max(1, len(result.items)))
        self.results.clear()
        for instrument in result.items:
            self._add_item(self.results, instrument)
        self.results.setVisible(bool(result.items))
        prefix = "离线目录 · " if result.cached else ""
        self.hint.setText(
            prefix
            + (
                f"找到{len(result.items)}项，点击打开"
                if result.items
                else "没有匹配的沪深A股，可输入六位代码重试"
            )
            + ("\n" + result.notice if result.notice else "")
        )

    def _failed(self, error):
        self.hint.setText("搜索未完成：" + error)

    def add_stock(self, instrument):
        for i in range(self.watchlist.count()):
            if self.watchlist.item(i).data(Qt.ItemDataRole.UserRole).symbol == instrument.symbol:
                self.watchlist.setCurrentRow(i)
                return
        if self.watchlist.count() >= 100:
            self.hint.setText("自选最多100只，请先移除不需要的股票")
            return
        self._add_item(self.watchlist, instrument)
        self._save()

    def remove_selected(self):
        row = self.watchlist.currentRow()
        if row >= 0:
            self.watchlist.takeItem(row)
            self._save()

    def _save(self, *args):
        items = [
            self.watchlist.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.watchlist.count())
        ]
        try:
            self.workspace.save(
                "watchlist",
                {
                    "items": [
                        {"symbol": i.symbol, "name": i.name, "initials": i.initials} for i in items
                    ]
                },
            )
        except (StorageError, ValueError, OSError) as exc:
            self.hint.setText("自选保存失败：" + str(exc))

    def shutdown(self):
        self.timer.stop()
        self.runner.shutdown()
