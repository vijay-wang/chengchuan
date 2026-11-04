# 澄川开发约定

## Git工作流

本仓库当前仅在本地管理。`main`保存可运行切片；后续功能使用`feat/…`、修复使用`fix/…`分支，完成相关验证后合入。提交保持单一目的，说明功能和验证结果。避免将运行缓存、日志、数据库、虚拟环境或密钥写入Git。

已有研究资料与原型报告已在初始基线保存。新增大规模行情进入本地数据目录，不继续向Git追加完整逐笔历史。仓库不自动关联或推送到远程。

## 安装与验证

```bash
python -m venv .venv
.venv/bin/python -m pip install -c requirements/desktop-tested.txt -e '.[desktop,dev]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check chengchuan tests/test_chengchuan.py tests/test_desktop.py tests/test_market.py tests/test_market_desktop.py tests/test_forecast*.py tests/test_meoz*.py tests/test_chart*.py tests/test_settings*.py scripts
.venv/bin/ruff format --check chengchuan tests/test_chengchuan.py tests/test_desktop.py tests/test_market.py tests/test_market_desktop.py tests/test_forecast*.py tests/test_meoz*.py tests/test_chart*.py tests/test_settings*.py scripts
```

Windows将`.venv/bin/python`替换为`.venv\Scripts\python`。

只开发领域和应用层可不装desktop依赖；桌面测试将跳过。Qt离屏测试不代替Windows/Linux实机、字体、高DPI与安装包验收。

自动测试不得读取真实API Key或发出付费数据请求。猫爪实机脚本默认离线，显式`--allow-network`才允许有本地额度保护的HTTP请求；原始行情、接口文档下载和账号配置不进入Git。

## 扩展要求

- 先阅读PRODUCT、ARCHITECTURE和ROADMAP，确认所属工作流与依赖边界。
- 新数据源不得把缺失值变成零、把采样价变成真实OHLC；时间、单位、字段覆盖必须明确。
- 核心计算不在UI中实现，窗口不直接调用供应商接口或SQL。
- 新参数和算法改变需更新版本与缓存键；保存研究记录时保留数据和算法口径。
- 任务、内存、缓存、重试均有边界。慢任务需要取消、错误恢复和关闭行为设计。
- 对影响正确性、稳定性和边界的行为做必要验证；不为简单样式变化编写镜像实现的测试。
- 变更后更新CHANGELOG与对应功能状态，区分已实现、已验证和计划中的能力。
