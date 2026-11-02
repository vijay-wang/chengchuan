# 澄川工程基础版：验证记录

日期：2026-11-02。平台：Ubuntu 22.04 x86_64，Python 3.10.12，PyQtGraph 0.14.0。
最初使用PySide6 Essentials 6.12.0；鼠标崩溃修复后改为PySide6 Essentials／Shiboken 6.11.3。

## 已完成

- 桌面依赖按`requirements/desktop-tested.txt`约束安装，项目editable安装成功，`chengchuan --help`入口可用。
- 桌面环境中37项测试全部通过：原引擎18项、领域／应用／存储和架构13项、真实Qt交互及任务4项，
  另加Qt引用计数和持续鼠标交互2项独立进程回归。
- 不带Qt依赖的原Python环境中，核心测试可运行，桌面测试自动跳过；核心导入不要求Qt。
- Ruff静态检查和格式检查通过；Git差异空白检查通过。
- 使用真实久立日线与分钟采样快照启动Qt窗口、后台分析、离屏抓图，并目视检查结果。分时纵轴已按可见价格范围适配。
- 实际鼠标事件验证Ctrl拖选会改变分析记录范围，Esc清除；任务测试验证旧结果不覆盖新请求、队列合并及失败后继续执行。
- 工作区事务、关闭连接、损坏数据报错且保留、版本化缓存及旧CLI指标对照已验证。

真实运行截图：[日K工作台](../design/chengchuan-desktop.png)、[分时工作台](../design/chengchuan-intraday.png)。

## 鼠标交互崩溃排查与修复

原来的35项短测试和截图验证不足以证明持续交互稳定。用户报告点击K线后出现
`Fatal Python error: none_dealloc: deallocating None`，堆栈位于PyQtGraph的
`pixelVectors → QTransform.setMatrix`。该位置是引用计数耗尽时正在执行的调用，不是根因。

在不导入PyQtGraph、不创建窗口的最小程序中，反复执行`QObject.setObjectName`，
直接比较调用前后的`sys.getrefcount(None)`，得到：

| Qt版本 | 调用次数 | None引用计数变化 | 进程结果 |
|---|---:|---:|---|
| 6.12.0 | 10／100／1000 | -10／-100／-1000 | 退出清理时同样abort |
| 6.11.3 | 10／100／1000／100000 | 全部为0 | 正常退出 |

原6.12.0环境向实际行情图注入持续鼠标移动，也在约1750次后复现相同致命错误，进程返回-6。
因此本机故障定位为PySide6／Shiboken 6.12.0的底层引用计数问题，Python 3.10无需更换。
Python 3.12起`None`为不朽对象，可能掩盖此类引用计数问题，不能据此判断旧版本Python本身有错。
相关上游资料：[Qt PYSIDE-3424讨论](https://wiki.qt.io/Qt_for_Python_Development_Notes)、
[Python 3.12 None对象说明](https://docs.python.org/3.12/c-api/none.html)。

`pyproject.toml`已排除6.12.0，`requirements/desktop-tested.txt`已固定6.11.3；
当前`.venv`也已重新安装。新增独立子进程测试会检查退出码，避免底层abort直接中断整个测试集，
并验证正常解释器清理，而非通过强制退出绕过错误。

持续交互脚本通过Qt视口发送鼠标事件，并检查行情悬停信号和选区计算结果。测试时关闭
PyQtGraph的鼠标限频，确保注入事件实际进入悬停处理；不会修改正常程序的限频配置。
本机X11复验完成8000次鼠标移动、8000次行情悬停更新、8次日K／分时加载，以及点击、
Ctrl拖选、Esc清除和窗口调整，进程正常退出。该检查不等同于所有交互和长时间运行验收。
复验命令：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
QT_QPA_PLATFORM=xcb .venv/bin/python -X faulthandler \
  -m scripts.check_desktop_stability --cycles 4 --moves 1000
```

## 小样本性能基线

执行：

```bash
.venv/bin/python -m scripts.benchmark_core --repeat 50 --output benchmarks/foundation.json
```

| 场景 | 加载记录 | 所选记录 | 未缓存分析p50 | 未缓存分析p95 |
|---|---:|---:|---:|---:|
| 久立多日日线 | 69 | 11 | 0.7415毫秒 | 0.7987毫秒 |
| 久立全天分钟 | 1210 | 242 | 9.2270毫秒 | 16.3129毫秒 |

本次测试包含兼容适配中的新旧模型转换，不等于之前单独旧内核的计时口径。缓存命中只返回已有结果，不计为重新计算的吞吐能力。

原始结果与环境范围见[性能数据](../benchmarks/foundation.json)。这是当前机器、小规模内存数据的基线，不包含联网、图表帧时、大规模逐笔和长时间运行，不能用于宣称全市场负载已达标。

## 尚未完成的验收

- Windows原生窗口、安装包、高DPI、多屏、真实键鼠及不同字体环境。
- 1万根及更大行情图表的帧时、长时间操作的内存变化。
- 实时源失败、断网、缓存过期、重新连接及正在加载时退出。
- 长计算的协作取消、工作进程终止和有界退出。
- 复权、真实分钟OHLC、多周期及Level-2字段一致性。
- 在线CI矩阵：工作流文件已配置，本地仓库尚未推送，不能声称远程CI已通过。
