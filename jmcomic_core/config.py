"""插件配置的强类型封装。

将 ``AstrBotConfig``（dict-like）一次性解析为 :class:`PluginConfig`，
集中处理默认值、边界裁剪与归一化，避免业务代码里散落 ``self.config.get(...)``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def _as_int(value: Any, default: int) -> int:
    """尽力把配置值转成整数，失败时回退默认值。

    WebUI 理论上按 ``_conf_schema.json`` 的类型校验，但手改配置文件、
    旧版本遗留的脏值都可能塞进字符串。此处兜底，避免插件因一个坏配置直接加载失败。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    """尽力把配置值转成浮点数，失败时回退默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: Any, low: int, high: int, default: int | None = None) -> int:
    """将配置值转成整数并限定在 ``[low, high]`` 区间内。"""
    return max(low, min(_as_int(value, default if default is not None else low), high))


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    """将配置值转成浮点数并限定在 ``[low, high]`` 区间内。"""
    return max(low, min(_as_float(value, default), high))


@dataclass
class PluginConfig:
    """插件运行配置（全部字段均有默认值，可由 WebUI 覆盖）。"""

    # ---- 传输 ----
    transfer_mode: str = "auto"
    file_server_port: int = 18790
    file_server_base_url: str = ""

    # ---- 下载 ----
    max_workers: int = 4
    image_format: str = "jpg"
    download_timeout: int = 300
    max_concurrent: int = 1
    max_image_count: int = 500

    # ---- 转换 / 压缩 ----
    pdf_resolution: float = 150.0
    enable_zip: bool = False
    zip_password: str = ""

    # ---- 缓存 ----
    max_cache_count: int = 20
    max_cache_size_mb: int = 200
    auto_cleanup: bool = True

    # ---- 体验 ----
    rate_limit_window: int = 300
    upload_retry: int = 3
    auto_recall: bool = True
    debug_log: bool = False

    @classmethod
    def from_dict(cls, config: Mapping[str, Any] | None) -> PluginConfig:
        """从 AstrBotConfig 字典构造并校验配置。"""
        c = config or {}

        transfer_mode = str(c.get("transfer_mode", "auto")).strip().lower()
        if transfer_mode not in ("auto", "local", "docker"):
            transfer_mode = "auto"

        image_format = str(c.get("image_format", "jpg")).strip().lower()
        if image_format not in ("jpg", "jpeg", "webp", "png"):
            image_format = "jpg"
        # 统一 jpeg -> jpg
        if image_format == "jpeg":
            image_format = "jpg"

        return cls(
            transfer_mode=transfer_mode,
            # 端口必须落在合法范围内，否则 bind 直接抛 OSError
            file_server_port=_clamp(c.get("file_server_port", 18790), 1, 65535, 18790),
            file_server_base_url=str(c.get("file_server_base_url", "")).strip().rstrip("/"),
            max_workers=_clamp(c.get("max_workers", 4), 1, 8, 4),
            image_format=image_format,
            download_timeout=_clamp(c.get("download_timeout", 300), 1, 86400, 300),
            max_concurrent=_clamp(c.get("max_concurrent", 1), 1, 4, 1),
            max_image_count=_clamp(c.get("max_image_count", 500), 0, 100000, 500),
            # 下限 1：converter 用 DPI 做除数，0 会直接 ZeroDivisionError
            pdf_resolution=_clamp_float(c.get("pdf_resolution", 150.0), 1.0, 1200.0, 150.0),
            enable_zip=bool(c.get("enable_zip", False)),
            zip_password=str(c.get("zip_password", "")),
            max_cache_count=_clamp(c.get("max_cache_count", 20), 0, 10000, 20),
            max_cache_size_mb=_clamp(c.get("max_cache_size_mb", 200), 0, 1000000, 200),
            auto_cleanup=bool(c.get("auto_cleanup", True)),
            rate_limit_window=_clamp(c.get("rate_limit_window", 300), 0, 86400, 300),
            upload_retry=_clamp(c.get("upload_retry", 3), 1, 10, 3),
            auto_recall=bool(c.get("auto_recall", True)),
            debug_log=bool(c.get("debug_log", False)),
        )

    @property
    def summary(self) -> str:
        """供日志输出的人类可读摘要。"""
        return (
            f"workers={self.max_workers}, format={self.image_format}, "
            f"timeout={self.download_timeout}s, cleanup={self.auto_cleanup}, "
            f"dpi={self.pdf_resolution}, cache_max={self.max_cache_count}, "
            f"cache_size_mb={self.max_cache_size_mb}, zip={self.enable_zip}, "
            f"fifo={self.max_concurrent}, img_max={self.max_image_count}, "
            f"retry={self.upload_retry}, debug={self.debug_log}"
        )
