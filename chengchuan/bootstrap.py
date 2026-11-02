"""依赖装配与进程入口；业务模块不反向依赖这里。"""

import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from chengchuan import APP_ID, APP_NAME


def main(argv=None):
    parser = argparse.ArgumentParser(description="澄川 Chengchuan · 股票研究工作台")
    parser.add_argument("--dataset", type=Path, help="打开已保存的flowradar数据快照")
    parser.add_argument("--state-dir", type=Path, help="本地工作区和日志目录")
    parser.add_argument("--screenshot", type=Path, help="离屏验收：保存窗口截图后退出")
    args = parser.parse_args(argv)
    try:
        from PySide6.QtCore import QStandardPaths, QTimer
        from PySide6.QtGui import QFont, QIcon
        from PySide6.QtWidgets import QApplication
    except ImportError:
        parser.exit(2, "缺少桌面依赖，请执行：python -m pip install -e '.[desktop]'\n")
    try:
        from chengchuan.ui.window import MainWindow
    except ImportError:
        parser.exit(2, "缺少图表依赖，请执行：python -m pip install -e '.[desktop]'\n")
    from chengchuan.application.errors import StorageError
    from chengchuan.application.services import AnalysisService
    from chengchuan.infrastructure.legacy import LegacyAnalysisEngine, LegacyJsonRepository
    from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository

    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "ui/assets/chengchuan.svg")))
    font = QFont()
    font.setFamilies(["Noto Sans CJK SC", "Microsoft YaHei UI", "PingFang SC", "sans-serif"])
    font.setPointSize(10)
    app.setFont(font)
    state_dir = args.state_dir or Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        state_dir / "chengchuan.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger("chengchuan").addHandler(handler)
    logging.getLogger("chengchuan").setLevel(logging.INFO)
    root = Path.cwd()
    daily = root / "reports/jiuli_20261019_1102.dataset.json"
    minute = root / "reports/jiuli_20261102_full_day.dataset.json"
    catalog = [
        (title, path)
        for title, path in (
            ("久立特材  002318\n日K · 已保存快照", daily),
            ("久立特材  002318\n分时 · 分钟采样快照", minute),
        )
        if path.exists()
    ]
    try:
        workspace = SqliteWorkspaceRepository(state_dir / "workspace.sqlite")
        window = MainWindow(
            LegacyJsonRepository(), AnalysisService(LegacyAnalysisEngine()), workspace, catalog
        )
    except (OSError, ValueError, StorageError) as exc:
        parser.exit(2, "无法打开本地工作区：" + str(exc) + "\n")
    window.show()
    initial = args.dataset or (daily if daily.exists() else None)
    if initial:
        window.open_snapshot(initial)
    if args.screenshot:
        attempts = [0]

        def capture():
            attempts[0] += 1
            if window.runner.active is not None and attempts[0] < 100:
                QTimer.singleShot(100, capture)
                return
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            saved = window.grab().save(str(args.screenshot))
            app.setProperty("capture_ok", saved)
            window.close()
            app.quit()

        QTimer.singleShot(500, capture)
    code = app.exec()
    handler.close()
    logging.getLogger("chengchuan").removeHandler(handler)
    if args.screenshot and not app.property("capture_ok"):
        return 2
    return code
