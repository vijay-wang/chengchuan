"""Versioned application preferences; credentials never enter the settings record."""

import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AppSettings:
    provider: str = "auto"
    auto_refresh: bool = True
    refresh_seconds: int = 15
    show_crosshair: bool = True
    show_tooltip: bool = True
    tooltip_opacity: int = 63

    def __post_init__(self):
        if self.provider not in ("auto", "meoz", "tencent"):
            raise ValueError("请选择有效的数据源")
        if any(
            type(v) is not bool for v in (self.auto_refresh, self.show_crosshair, self.show_tooltip)
        ):
            raise ValueError("开关设置无效")
        if type(self.refresh_seconds) is not int or not 15 <= self.refresh_seconds <= 3600:
            raise ValueError("更新检查间隔应在15至3600秒之间")
        if type(self.tooltip_opacity) is not int or not 30 <= self.tooltip_opacity <= 90:
            raise ValueError("浮窗不透明度应在30%至90%之间")


@dataclass(frozen=True)
class SettingsInfo:
    credential_status: str = "当前环境未提供数据源配置"
    environment_override: bool = False
    paths: tuple[tuple[str, str], ...] = ()
    usage: tuple[tuple[str, str], ...] = ()


class SettingsSaveError(ValueError):
    def __init__(self, message, key_saved=False):
        super().__init__(message)
        self.key_saved = key_saved


class SettingsService:
    def __init__(self, workspace, sources=None):
        self.workspace, self.sources = workspace, sources

    def load(self, legacy=None):
        obj = self.workspace.load("preferences")
        if obj == {}:
            legacy = legacy or {}
            return AppSettings(
                provider=legacy.get("provider") or "auto",
                auto_refresh=bool(legacy.get("auto_refresh", True)),
            )
        if not isinstance(obj, dict):
            raise ValueError("本机设置内容无效，请检查设置存储")
        if obj.get("version") != 1:
            raise ValueError("设置由其他版本创建，请升级软件后打开")
        try:
            return AppSettings(**{key: obj[key] for key in asdict(AppSettings()) if key in obj})
        except (TypeError, ValueError):
            raise ValueError("本机设置内容无效，请检查设置存储") from None

    def save(self, settings, api_key=""):
        # Validate all input before writing either storage. General settings and
        # credentials use distinct stores; report a partial save explicitly.
        if not isinstance(settings, AppSettings):
            raise ValueError("设置类型无效")
        value = api_key.strip()
        if value and not re.fullmatch(r"nc_[A-Za-z0-9_-]{32}", value):
            raise SettingsSaveError("API Key格式无效，请检查后重试")
        key_saved = False
        if value:
            if self.sources is None:
                raise SettingsSaveError("当前环境未提供数据源配置")
            try:
                self.sources.configure_key(value)
                key_saved = True
            except Exception:
                raise SettingsSaveError("API Key未能保存，请检查本机配置文件权限") from None
        try:
            self.workspace.save("preferences", dict(version=1, **asdict(settings)))
        except Exception:
            message = (
                "API Key已保存；其他设置未能保存，请重试"
                if key_saved
                else "设置未能保存，请检查本地工作区权限后重试"
            )
            raise SettingsSaveError(message, key_saved) from None

    def info(self):
        if self.sources is None:
            return SettingsInfo()
        try:
            return self.sources.settings_info()
        except Exception:
            return SettingsInfo(credential_status="配置状态暂不可读取，请检查本机配置文件")
