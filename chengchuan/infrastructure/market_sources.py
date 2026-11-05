"""Local source selection and credential setup, independent of Qt."""

import os
from pathlib import Path

from chengchuan.application.company import CompanyService
from chengchuan.application.market import MarketService
from chengchuan.application.research import SpecialtyResearchService
from chengchuan.application.settings import SettingsInfo
from chengchuan.infrastructure.market_cache import SqliteMarketCache
from chengchuan.infrastructure.market_data import TencentMarketProvider
from chengchuan.infrastructure.meoz_client import (
    MeozClient,
    MeozStore,
    configured,
    save_private_key,
)
from chengchuan.infrastructure.meoz_company import MeozCompanyProvider
from chengchuan.infrastructure.meoz_market import MeozMarketProvider
from chengchuan.infrastructure.meoz_research import MeozResearchProvider
from chengchuan.infrastructure.microstructure import JsonOrderTapeRepository
from chengchuan.infrastructure.research_archive import SqliteResearchArchive


class ConfiguredMarketSources:
    choices = (("meoz", "猫爪 · 12周期"), ("tencent", "腾讯 · 基础行情"))

    def __init__(self, state_dir, offline=False):
        self.state_dir = Path(state_dir)
        self.offline = offline

    def connect(self, source):
        if source == "auto":
            source = "meoz" if configured() else "tencent"
        if source not in ("meoz", "tencent"):
            raise ValueError("不支持的行情数据源")
        if source == "meoz":
            client = MeozClient(offline=self.offline)
            notice = (
                "12周期 · 历史日期 · 复用本机缓存"
                if configured()
                else "本机未配置猫爪API Key，请点击“配置猫爪”；密钥不会随Git同步到其他电脑"
            )
            return MarketService(
                MeozMarketProvider(client),
                SqliteMarketCache(self.state_dir / "meoz-market.sqlite"),
                ttl_seconds=180,
                source_id=source,
                source_label="猫爪／猫头鹰",
                connection_notice=notice,
                tape_repository=JsonOrderTapeRepository(),
                research_service=SpecialtyResearchService(
                    MeozResearchProvider(
                        client, SqliteResearchArchive(self.state_dir / "research.sqlite")
                    )
                ),
                company_service=CompanyService(
                    MeozCompanyProvider(
                        client, SqliteResearchArchive(self.state_dir / "company.sqlite")
                    )
                ),
            )
        return MarketService(
            TencentMarketProvider(),
            SqliteMarketCache(self.state_dir / "market.sqlite"),
            source_id=source,
            source_label="腾讯",
            connection_notice="仅分时／日K；完整12周期请切换猫爪并配置本机API Key",
            offline=self.offline,
            tape_repository=JsonOrderTapeRepository(),
        )

    def configure_key(self, value):
        save_private_key(value)

    def settings_info(self):
        environment = bool(os.environ.get("MEOZ_API_KEY", "").strip())
        status = "已配置（未联网验证）" if configured() else "尚未配置有效API Key"
        if environment:
            status += " · 环境变量优先于配置文件"
        usage = MeozStore().usage()
        return SettingsInfo(
            credential_status=status,
            environment_override=environment,
            paths=(
                ("工作区", str(self.state_dir)),
                ("猫爪私有配置", str(Path.home() / ".meoz/config.json")),
                ("原始数据缓存", str(Path.home() / ".meoz/chengchuan.sqlite")),
                ("公司资料档案", str(self.state_dir / "company.sqlite")),
            ),
            usage=(
                ("本机今日请求", str(usage["total"])),
                *((api, f"{n}/95") for api, n in usage["interfaces"].items()),
            ),
        )
