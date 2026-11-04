# 0.5.1：十字线边缘重绘与周期入口

日期：2026-11-04。用户反馈来自另一台Ubuntu 22.04：鼠标经过图表底部残留密集十字虚线，周期只能选分时和日K。

## 修正

价格和成交量两个视口采用完整重绘，十字线移动和隐藏后都异步请求重画，避免依赖Qt／PyQtGraph边缘图元的最小脏矩形。浮窗更新位置与内容时保持显示，移出或换面板才隐藏；沿用四条线和两个标签，不累加图元。Qt合并绘制请求，继续使用100Hz悬停限制及蜡烛图绘图缓存，不随鼠标获取行情、重算指标或分析。

只显示两周期的代码路径来自腾讯基础数据源；其他电脑未配置猫爪密钥时，原auto模式会采用该源，旧界面隐藏其他周期且没有切换入口。0.5.1增加数据源选择、能力说明和密码方式配置猫爪。12周期始终列出，腾讯不支持的置灰并提示原因；配置成功切换猫爪，源选择可恢复。密钥仅存在目标电脑私有配置，Git和工作区不保存密钥。

切源时取消旧行情／搜索任务、清理旧搜索结果、使用各源独立缓存。旧源不支持的周期回到日K；源支持时保留所选历史日期。离线模式切换腾讯仍只读已有缓存。源装配在基础设施层，GUI和CLI共用原子配置函数。

## 验证

- 全套106项自动测试通过，Ruff静态／格式检查和0.5.1 wheel构建通过。新增回归覆盖底部连续移动及移出后的重绘、数据源能力和隔离、GUI切源后五日／周／年／15与60分钟K、配置取消／失败／成功及不回显、私有原子配置及保留原字段、离线腾讯禁止HTTP。
- Ubuntu 22.04.5 LTS、Python 3.10.12、PySide6／Shiboken 6.11.3、PyQtGraph 0.14.0原生xcb窗口的6项交互测试通过，包括日历、浮窗、边缘、切源和配置。远程X11中仅移动QCursor可能不产生鼠标事件，测试通过Qt viewport实际事件路径注入，不依赖游标传送。
- 独立、已激活的原生窗口执行720次连续移动，覆盖主副图上边缘、下边缘和时间轴区域。6个阶段分别对比当前屏幕像素与强制重绘后的像素，差异均为0；移出后的最终像素与初始画面差异也为0。使用`QScreen.grabWindow`读取实际画面，避免`QWidget.grab()`强制重绘掩盖残影。
- 久立002318真实缓存依次加载全部12周期，均正确呈现和分析；新增源选择和12周期菜单已截图并目视检查。该回放完全离线，API预留次数保持10不变（daily 1、minute 6、tradecal 3）。自动测试使用模拟数据／HTTP，不读取真实密钥。

截图：[数据源和分钟K](../design/chengchuan-source-controls.png)、[全部12周期](../design/chengchuan-source-periods.png)。详细像素与GUI记录位于本机忽略目录，不提交行情库或私有配置。

```bash
.venv/bin/python -m unittest discover -s tests -v
QT_QPA_PLATFORM=xcb .venv/bin/python -m unittest discover -s tests -p 'test_chart_navigation_desktop.py' -v
QT_QPA_PLATFORM=xcb .venv/bin/python -m scripts.check_chart_repaint \
  --output-dir .chengchuan/cursor-repaint/check
QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.check_meoz_workbench \
  --output-dir .chengchuan/meoz/source-fix-smoke
```

本机原生Qt原缺少`libxcb-cursor0`，本轮将Ubuntu软件包解包到忽略的本地运行目录，仅在验证进程使用相应`LD_LIBRARY_PATH`，没有更改系统或加入Git。目标电脑若能正常打开当前GUI，无需重复这一步。

本机没有稳定复现用户原先的密集残影，修复针对局部重绘与浮窗生命周期的脆弱路径，并增加实际像素检查。不同显卡、驱动、Wayland／缩放组合仍需在目标电脑确认，不能用本机xcb结果代替所有桌面环境。

## 另一台电脑使用

同步代码并重启，确认左下角为0.5.1。周期栏上方选“猫爪 · 12周期”；若提示本机未配置，点“配置猫爪”通过隐藏输入保存，再打开股票。密钥不会随仓库同步；环境变量`MEOZ_API_KEY`仍优先于配置文件。当前完整行情能力依然取决于实际套餐权限及缓存／接口覆盖。
