# 架构决策记录

## ADR-001：Python桌面优先

日期：2026-11-02。状态：采用。

选择PySide6和PyQtGraph。现有分析为Python，用户重视本地自定义研究及键鼠交互，直接复用计算与数据逻辑更适合当前范围。

代价：K线、交易时间轴和股票操作习惯需要组合实现；还需要真实目标平台打包验收。

备选：Tauri＋Web金融图表＋Python侧进程。若浏览器访问或多人共享成为主要目标，再评估此方案。领域和应用层保持无Qt依赖，降低更换呈现技术的成本。

依据：[PyQtGraph区域选择](https://pyqtgraph.readthedocs.io/en/latest/api_reference/graphicsItems/linearregionitem.html)、[联动与坐标映射](https://pyqtgraph.readthedocs.io/en/latest/api_reference/graphicsItems/viewbox.html)、[Qt部署](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html)。

## ADR-002：模块化单体与显式端口

状态：采用。

领域、应用、基础设施和UI分层，在bootstrap注入依赖。端口只为已经存在的替换需求建立，当前为快照、分析引擎与工作区；不提前设计所有潜在插件框架。

代价：需要维护领域契约和转换代码；收益是UI、数据源和计算不相互侵入，便于逐步替换旧模块。

## ADR-003：交互任务只保留最新待执行项

状态：采用。

加载与区间分析使用1个活动任务＋1个待执行任务，带版本校验。减少反复拖动产生的排队和迟到结果覆盖。

边界：此规则不能用于逐笔采集。当前不提供运行中计算的强制取消；长任务执行器必须另行设计。

## ADR-004：旧原型通过适配器迁移

状态：采用。

保留原CLI与回归测试；旧数据类型只存在于infrastructure适配层。新领域把真实OHLC和采样价明确区分。后续可替换AnalysisEngine而保持窗口代码和研究记录契约稳定。

代价：过渡期存在新旧模型转换。暂时的转换成本比同时重写采集、分析、报告和界面更容易验证。

## ADR-005：SQLite工作区与版本化快照

状态：部分实施。

当前工作区使用SQLite事务、WAL和schema版本；行情使用已保存文件的内容哈希标识。历史行情仓储、逐笔Parquet分区及DuckDB查询尚未实现，按实际数据量推进。

依据：[Python SQLite接口](https://docs.python.org/3.10/library/sqlite3.html)。

## ADR-006：公开行情轮询与可终止网络进程

日期：2026-11-02。状态：0.2采用。

首先交付沪深A股按需搜索、日K／分钟采样分时与每15秒轮询，不将非官方公开接口描述为有延迟保证的逐笔实时服务。
报价时间、图表覆盖和取得时间分开保存；缺失成交额不推算，分钟采样价不转换成真实分钟OHLC。

网络任务使用spawn进程而不是原分析线程池，避免HTTP/DNS挂起时在Qt线程池析构阶段拖住窗口退出。
每项任务只有一个活动进程，新请求替换旧请求，父进程负责超时和终止；应用／领域不依赖Qt或供应商。
代价是每次请求有进程启动开销；当前两次小HTTP读取和15秒轮询的负载下可接受。

协议依据来自实际只读端点验证，并参考数据适配项目
[Ashare的腾讯K线适配](https://github.com/mpquant/Ashare/blob/master/Ashare.py)。
未复制或引入该项目代码和依赖。来源接口可能变化，后续在MarketProvider边界加入更多数据源。
