# 澄川 Chengchuan

本地股票研究工作台：看行情、选区间、计算特征、追溯证据。

![澄川行情工作台](design/chengchuan-market-daily.png)

当前为**0.6统一设置版**：左侧“设置”或Ctrl＋逗号打开配置窗口，可输入API Key、选择数据源、设置更新检查和图表悬停。保留12周期、历史日期跳转、半透明浮窗、持久缓存与额度保护，预测增强仍按P2后置。

**2026-11-04需求调整：** 后续只保留盘后、北京时间14:35两个时点预测下一交易日，优先级P2后置，排在行情、稳定性、基础指标和选区体验之后；相关Level-2／GPU增强同样后置。本次只更新文档，既有0.3 H1／H2代码未改动。详见[最新预测需求](docs/NEXT_DAY_FORECAST.md)及[排期](docs/ROADMAP.md)。

## 启动桌面

需要Python 3.10或更高版本。在仓库目录执行：

```bash
python -m venv .venv
.venv/bin/python -m pip install -c requirements/desktop-tested.txt -e '.[desktop,dev]'
.venv/bin/python -m chengchuan
```

Windows使用`.venv\Scripts\python`替换`.venv/bin/python`。安装包尚未构建；不同平台需要分别验收。

依赖已排除PySide6 6.12.0：该版本在Python 3.10/3.11下反复调用Qt方法会损坏`None`引用计数，
导致鼠标交互时出现`none_dealloc: deallocating None`并退出。已安装旧环境时，重新执行上面的
`pip install`命令即可切换到已验证的6.11.3，无须更换Python。

默认联网打开上次查看的股票，首次为久立特材002318。左侧输入六位代码回车可直接打开，输入名称或拼音后点击搜索结果；“加入自选”保存当前股票，自选支持拖动排序和移除。图表上方可选择周期、F5刷新、开启或暂停自动更新；检查间隔默认15秒，可在设置中调整。

已配置猫爪密钥时，启动默认使用猫爪／猫头鹰数据，周期下拉框提供全部12种图表。未配置时保留腾讯日K／采样分时。数据源分别缓存，不会混淆来源；当前全部价格不复权。

**另一台电脑只看到分时／日K：** 这是腾讯基础源的能力。0.5.1在周期上方增加**数据源**，可选择“猫爪 · 12周期”；若本机尚未配置，点击**配置猫爪**，通过隐藏输入保存API Key。配置成功自动切换猫爪，之后可选全部12周期，并记住数据源选择。密钥仅保存在那台电脑，不随Git同步；腾讯下其他周期会置灰并说明原因，不再直接消失。`--provider auto|meoz|tencent`可覆盖保存的选择。

0.6中“配置猫爪”直接进入统一设置的**数据连接**页。也可点击左侧**设置**或按**Ctrl＋逗号**，配置：

- **数据连接**：自动／猫爪／腾讯来源、API Key输入和配置状态。已有密钥不回显，留空保留；默认隐藏输入，可显示本次输入。输入新密钥时默认选择猫爪，也可手动改选。
- **常规与图表**：自动检查开关、15—3600秒检查间隔、十字线／浮窗开关及30%—90%浮窗不透明度。
- **本地存储**：查看工作区、私有配置、原始缓存位置和本机请求计数。

点击“保存设置”立即应用并持久保存；取消不保存修改。普通偏好与API Key分开存储，密钥仍只在本机私有文件中。`MEOZ_API_KEY`环境变量继续优先，配置页面会提示；配置状态未作联网权限验证。修改检查间隔不绕过付费接口180秒缓存或调用额度。

```bash
# 本机已有配置时可直接运行；其他PC先通过隐藏输入配置密钥
.venv/bin/python -m chengchuan.meoz configure-key
.venv/bin/python -m chengchuan --provider meoz --symbol 002318 --interval min15
# 查看本机今日调用计数、缓存路径和配置状态（不会打印密钥）
.venv/bin/python -m chengchuan.meoz status
# 同一已缓存日期的全部周期可离线查看
.venv/bin/python -m chengchuan --provider meoz --offline --symbol 002318 --interval five_day
```

密钥读取`MEOZ_API_KEY`或`~/.meoz/config.json`，不随仓库分发。接口响应、完整日线及调用账本位于`~/.meoz/chengchuan.sqlite`，窗口缓存仍在应用工作区。日线首次查询2000年起实际可得数据，后续仅更新末段；分钟K默认最近5个交易日，这是当前加载策略，并非套餐历史权限上限。

体验版官方上限为每日10万次、每高频接口100次／日、3次／秒。本机保守按每接口95次、总计90000次保护，所有请求先持久预留，失败和中断也计数；两站共用限流，缓存命中不计。付费数据盘中最短180秒复用，F5不会绕过此保护；盘后确认后复用当日数据。每15秒仅检查更新，并不每次重新请求付费接口。其他软件／设备用量无法由本机推知，账号总额仍以服务端为准。详见[数据接入说明](docs/MEOZ_DATA.md)。

报价时间、图表覆盖时间和取得时间分别显示，统一为北京时间。当前采用查询和缓存，可能延迟，尚未持续订阅WebSocket。收盘后价格保持源提供的最后有效报价；缺少报价时间时不伪造实时报价。刷新同股时保留缩放和选区，切股时清空上一只股票的数据与分析。

Ctrl左键拖动选择时间段，松开后计算，拖动边界调整，Esc清除，Ctrl＋O导入旧版标准JSON快照。本地快照模式不会自动联网更新。

图表下方**日期跳转**弹出日历，点击某一天后加载对应日期：分时查看该交易日，五日／分钟K查看截至所选日的最近5个市场交易日，日／周／月／季／年K截断到所选日。非交易日显示上一可用交易日并提示实际日期。历史浏览暂停自动更新，切换周期和F5保留所选日期；点击**返回最新**恢复。翻月不请求行情，重复查看优先读取本地缓存；未缓存的历史分钟需要补取，仍受额度保护。腾讯源及本地快照只定位已有覆盖范围。

鼠标在价格／成交量图内移动时，十字虚线联动，半透明浮窗随鼠标移动并避开边缘，不拦截拖选或缩放。显示记录时间（年月日时分秒）、实际价格及涨跌幅，另列光标所在价位；秒字段遵循源记录，分钟数据不代表秒级成交。日内／日K优先使用源昨收，周月季年使用前根K线收盘，缺基准显示“—”。

```bash
.venv/bin/python -m chengchuan --symbol 600519 --interval day
# 仅在启动时打开本地示例，不发起行情请求
.venv/bin/python -m chengchuan --offline
# 打开指定历史快照
.venv/bin/python -m chengchuan \
  --dataset reports/jiuli_20261102_full_day.dataset.json
```

工作区、自选、行情缓存和轮转日志默认保存到操作系统的应用数据目录，可用`--state-dir PATH`指定。行情缓存按股票和周期隔离，联网失败时保留原数据时间，绝不标为新行情。当前单个导入快照上限32MB。

## 使用量价预测

打开任意有足够历史的沪深A股日K，在右侧切换到**量价预测**：

1. 选择已完成的基准日D，点击“计算方向”。显示较昨／前2／5／20日均量、此前20／60日位置、H1／H2方向、相似日数量及不预测原因。
2. 可勾选“基准日跟随Ctrl选区末日”，从图表选择历史D；计算使用截至D的固定历史窗口。
3. 点击“逐日验证”，按时间重新训练并对照真实结果。目标日期严格小于“回测目标早于”的日期，分别报告价格A、量价B、阶段融合C与同样本基线。
4. 点击“导出报告与完整对照表”，生成Markdown、JSON及CSV。后台任务可取消；行情刷新不会自动重新训练。

H1预测次交易日收盘相对D收盘的方向；H2预测两个交易日后的累计方向。至少需要61条连续有效日线提取特征，再积累40条成熟训练样本才给研究方向。平票、数据不足或日历不支持会明确不预测；相似日分布不作为未来胜率。

命令行不需要Qt或API密钥：

```bash
# 在线计算，并保存输入供以后离线复算
.venv/bin/python -m chengchuan.forecast predict --symbol 002318 \
  --save-source .chengchuan/forecast/inputs/sz002318.json \
  --output .chengchuan/forecast/jiuli_prediction

# 历史逐日验证：11月3日及以后的目标结果全部排除
.venv/bin/python -m chengchuan.forecast backtest \
  --dataset .chengchuan/forecast/inputs/sz002318.json \
  --before 2026-11-03 --output .chengchuan/forecast/jiuli_backtest

# 指定历史D回放；也支持原来的.dataset.json日线文件
.venv/bin/python -m chengchuan.forecast predict \
  --dataset .chengchuan/forecast/inputs/sz002318.json \
  --as-of 2026-10-29 --output .chengchuan/forecast/replay
```

CLI研究账本默认保存在`.chengchuan/forecast/research.sqlite`，GUI保存到工作区的`forecast.sqlite`。预测先事务归档，评价另行追加；重复运行不重复计数，冲突不会覆盖原记录。当前仅核对2025／2026沪深日历，单次最多2000条日线。除权除息、跨年日历、多年多股独立验证、概率校准及真实前瞻自动评价仍需后续补齐。

已运行三只股票的固定参数诊断，结果见[回放记录](docs/VALIDATION_FORECAST.md)及[完整汇总](reports/forecast_20261103/summary.json)。这些结果不能当成已验证的未来准确率。

## 规划与架构

- [产品规划与当前功能](docs/PRODUCT.md)
- [分阶段路线与验收清单](docs/ROADMAP.md)
- [猫爪数据接入、缓存与调用额度](docs/MEOZ_DATA.md)
- [0.4多周期真实数据验证](docs/VALIDATION_MEOZ.md)
- [0.5日期跳转与半透明浮窗验证](docs/VALIDATION_CHART_NAVIGATION.md)
- [0.5.1边缘重绘、数据源及跨电脑配置验证](docs/VALIDATION_CHART_FIXES.md)
- [0.6设置页与保存／恢复验证](docs/VALIDATION_SETTINGS.md)
- [最新预测需求：盘后／14:35预测下一交易日，P2后置](docs/NEXT_DAY_FORECAST.md)
- [量价联合预测原设计与0.3实施背景](docs/SHORT_HORIZON_FORECAST.md)
- [历史逐日预测与准确率验证方案](docs/FORECAST_VALIDATION.md)（已实现诊断回放，完整验证仍待完成）
- [架构、性能与稳定性约束](docs/ARCHITECTURE.md)
- [UI设计和操作规范](docs/UI_DESIGN.md)
- [架构决策](docs/DECISIONS.md)
- [开发和Git约定](CONTRIBUTING.md)
- [变更记录](CHANGELOG.md)

新架构采用PySide6＋PyQtGraph，领域和应用层独立于GUI、供应商与数据库。旧分析引擎位于兼容边界，之后可以逐步替换。

## 桌面验证

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check chengchuan tests/test_chengchuan.py tests/test_desktop.py tests/test_market*.py tests/test_forecast*.py tests/test_meoz*.py tests/test_chart*.py tests/test_settings*.py scripts
QT_QPA_PLATFORM=offscreen .venv/bin/python -m chengchuan \
  --offline --state-dir /tmp/chengchuan-preview --screenshot /tmp/chengchuan-preview.png
# Linux桌面交互复验：悬停、点击、Ctrl拖选、Esc和日K/分时切换
QT_QPA_PLATFORM=xcb .venv/bin/python -m scripts.check_desktop_stability
# 联网实机验收（额外等待一次15秒定时刷新）
QT_QPA_PLATFORM=xcb .venv/bin/python -m scripts.check_market_workbench --wait-refresh
# 已保存日线的预测、回测、导出与界面验收
QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.check_forecast_workbench \
  --dataset .chengchuan/forecast/inputs/sz002318.json --before 2026-11-03
# 猫爪12周期验收：默认只读已有本地数据，不消耗API额度
QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.check_meoz_workbench
# 日期跳转与浮窗：只读缓存，需先有对应日期数据
QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.check_chart_navigation \
  --date 2026-10-16 --output-dir .chengchuan/meoz/date-navigation
```

离屏检查不能替代真实目标系统和显示器验收。详见[行情工作台验证](docs/VALIDATION_MARKET.md)和[原Qt崩溃排查记录](docs/VALIDATION.md)。

## 公开数据原型说明（兼容保留）

以下说明适用于保留的`flowradar`命令行工具；新GUI当前读取其`.dataset.json`快照。

支持选择数小时、数天或数周的区间，计算量价、相对强弱、订单金额分类和多种阶段解释，并生成本地可打开的交互式 HTML、Markdown、JSON 报告。

当前版本是 **公开数据研究原型**。它不连接证券交易账户，也没有经过真实机构账户标签校准。阶段规则分数不是概率；“吸筹、洗盘、派发”是候选解释。

## 查看久立示例

- [2026-11-02全天报告](reports/jiuli_20261102_full_day.html)
- [2026-11-02午后两小时报告](reports/jiuli_20261102_afternoon.html)
- [2026-10-19至11-02多日报告](reports/jiuli_20261019_1102.html)
- [本次验证记录](docs/验证记录_2026-11-02.md)

## 快速运行

需要 Python 3.10 或更高版本，无第三方运行依赖。在本目录执行：

```bash
# 小时区间：以北京时间输入起止时间
python -m flowradar analyze --symbol 002318 --interval minute \
  --start 2026-11-02T13:00 --end 2026-11-02T15:00 \
  --refresh --output reports/jiuli_20261102_afternoon

# 多日／多周区间
python -m flowradar analyze --symbol 002318 --interval day \
  --start 2026-10-19 --end 2026-11-02 \
  --refresh --output reports/jiuli_20261019_1102

# 已保存数据离线复算，无须重新请求接口
python -m flowradar analyze \
  --dataset reports/jiuli_20261102_afternoon.dataset.json \
  --start 2026-11-02T09:30 --end 2026-11-02T15:00 \
  --output reports/jiuli_20261102_full_day

# 滚动观察：检查规则候选与后续5个交易日价格表现的关系
python -m flowradar evaluate --symbol 002318 --interval day \
  --start 2026-09-01 --end 2026-11-02 \
  --lookback 20 --horizon 5 --output reports/jiuli_outcomes
```

打开输出的 `.html` 即可查看报告；图表滑块仅缩放价格图，不改变报告分析区间。重新分析请再次运行命令。

代码不自动选择或调用付费 AI 模型。软件的数字由可复算程序计算，当前也不需要 OpenAI API 密钥。

## 已实现

- 公开分钟行情、日线行情、基准行情、平台订单金额分类的采集与本地缓存。
- 小时／多日／多周区间分析，以及 CSV、标准 JSON 数据导入。
- 成交额和成交量单位统一；分钟累计数据先转换为增量，再切割分析区间。
- “承接／疑似吸筹、拉升／趋势上行、卖压／疑似派发、回撤承接／洗盘候选”规则证据。
- 同时展示其他解释、数据覆盖和缺失项。
- 原始响应缓存、来源链接、获取时间、离线复算与交互式报告。
- 只使用分析时点之前数据的滚动价格结果观察工具。

## 明确的能力边界

1. **机构净买入金额目前输出无法识别。** 平台大小单净额不等于真实机构持仓变化。没有经验证的估计方法时，软件不会编造金额区间。
2. **未运行母单识别或 HMM。** TWAP、VWAP、POV 和限价执行目前列为候选模型；分钟数据和金额分档不构成某个参与者的执行轨迹。
3. 免费分钟接口通常只覆盖近期几个交易日。多周小时级历史研究需要补充分钟或逐笔数据；日线可用于多周形态分析。
4. 公共成交明细可能存在3秒时间格点。这个周期不能直接用于识别3秒拆单算法。
5. 日线当前未复权；跨除权除息日需另行处理公司行为。
6. 主日线源失败时回退腾讯日线。回退源不含成交额，成交额和 VWAP 将明确缺失，不用收盘价乘成交量冒充。
7. 历史价格表现只能验证信号与后续走势的关系，不能证明“主力识别准确率”。窗口重叠，也不能把样本当成相互独立。

## CSV 输入

```csv
timestamp,open,high,low,close,volume_shares,amount_yuan
2026-11-02T13:00:00,16.28,16.28,16.28,16.28,0,0
2026-11-02T13:01:00,16.28,16.30,16.27,16.29,10000,162850
```

成交量单位是**股**，成交额单位是**元**。上述两条仅展示格式，不是历史成交记录。没有成交额时留空，不能填0冒充真实值。

```bash
python -m flowradar analyze --csv your_bars.csv --symbol 002318 \
  --interval minute --start 2026-11-02T13:00 --end 2026-11-02T14:00 \
  --output reports/imported
```

## 验证

```bash
python -m unittest discover -s tests -v
```

开发目标、数据升级、模型训练、历史案例和验收标准见 [开发任务书](docs/资金行为研究系统开发任务书.md)。
