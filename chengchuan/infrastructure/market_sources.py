"""Local source selection and credential setup, independent of Qt."""

from pathlib import Path

from chengchuan.application.market import MarketService
from chengchuan.infrastructure.market_cache import SqliteMarketCache
from chengchuan.infrastructure.market_data import TencentMarketProvider
from chengchuan.infrastructure.meoz_client import MeozClient, configured, save_private_key
from chengchuan.infrastructure.meoz_market import MeozMarketProvider


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
            notice = (
                "12周期 · 历史日期 · 复用本机缓存"
                if configured()
                else "本机未配置猫爪API Key，请点击“配置猫爪”；密钥不会随Git同步到其他电脑"
            )
            return MarketService(
                MeozMarketProvider(MeozClient(offline=self.offline)),
                SqliteMarketCache(self.state_dir / "meoz-market.sqlite"),
                ttl_seconds=180,
                source_id=source,
                source_label="猫爪／猫头鹰",
                connection_notice=notice,
            )
        return MarketService(
            TencentMarketProvider(),
            SqliteMarketCache(self.state_dir / "market.sqlite"),
            source_id=source,
            source_label="腾讯",
            connection_notice="仅分时／日K；完整12周期请切换猫爪并配置本机API Key",
            offline=self.offline,
        )

    def configure_key(self, value):
        save_private_key(value)
