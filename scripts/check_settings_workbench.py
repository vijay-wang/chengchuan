"""Preview the three settings pages against cached real data without HTTP or key changes."""

import argparse
import json
import time
from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from chengchuan.application.services import AnalysisService
from chengchuan.infrastructure.forecast_input import ForecastSnapshotRepository
from chengchuan.infrastructure.legacy import LegacyAnalysisEngine
from chengchuan.infrastructure.market_sources import ConfiguredMarketSources
from chengchuan.infrastructure.meoz_client import MeozStore
from chengchuan.infrastructure.workspace import SqliteWorkspaceRepository
from chengchuan.ui.window import MainWindow


def check(folder):
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Noto Sans CJK SC", 10))
    sources = ConfiguredMarketSources(folder, offline=True)
    usage_before = MeozStore().usage()
    workspace = SqliteWorkspaceRepository(folder / "workspace.sqlite")
    window = MainWindow(
        ForecastSnapshotRepository(),
        AnalysisService(LegacyAnalysisEngine()),
        workspace,
        market=sources.connect("meoz"),
        market_sources=sources,
    )
    window.resize(1600, 1000)
    window.show()
    if app.platformName() == "xcb":
        window.raise_()
        window.activateWindow()
        if not QTest.qWaitForWindowActive(window, 2000):
            raise RuntimeError("原生窗口未激活")
    window.auto_refresh.setChecked(False)
    window.open_stock("002318", "min15")
    try:
        deadline = time.monotonic() + 30
        while not (
            window.frame
            and window.snapshot
            and window.market_runner.active is None
            and window.runner.active is None
        ):
            app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                raise RuntimeError("本地缓存行情加载超时：" + window.market_status.text())
        window.open_settings()
        dialog = window.settings_dialog
        if dialog.api_key.text():
            raise RuntimeError("设置页回显了私有密钥")
        for code in dialog.page_codes:
            dialog.select_page(code)
            QTest.qWait(80)
            if not dialog.grab().save(str(folder / ("settings-" + code + ".png"))):
                raise RuntimeError("无法保存设置页截图")
        dialog.refresh_seconds.setValue(60)
        dialog.opacity.setValue(45)
        dialog.save_button.click()
        app.processEvents()
        if window.settings_dialog is not None or window.refresh_timer.interval() != 60000:
            raise RuntimeError("设置未生效")
        window.open_settings("general")
        if window.settings_dialog.opacity.value() != 45:
            raise RuntimeError("重新打开设置丢失偏好")
        window.settings_dialog.reject()
        after = MeozStore().usage()
        if usage_before != after:
            raise RuntimeError("离线设置验收意外产生付费调用")
        stored = workspace.load("preferences")
        if "api_key" in stored:
            raise RuntimeError("密钥误写入普通设置")
        result = dict(
            platform=app.platformName(),
            pages=list(dialog.page_codes),
            preferences=stored,
            usage_before=usage_before,
            usage_after=after,
        )
        (folder / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result
    finally:
        window.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    print(json.dumps(check(args.output_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
