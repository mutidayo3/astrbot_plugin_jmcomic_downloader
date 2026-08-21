"""JMComic 本子下载器 — AstrBot 插件主入口。

本文件仅负责 AstrBot 生命周期与指令注册，所有业务逻辑都委派给
``jmcomic_core.service.DownloadService``，遵循 AstrBot 规范中
“文件行数过长时将服务写在外部”的建议。

子包 ``jmcomic_core`` 结构：
- config: 强类型配置封装
- names: 文件名安全化
- env: Docker 检测 / IP 获取 / 图片收集
- fifo_semaphore: FIFO 有序信号量
- file_server: HTTP 文件服务器
- cache: PDF 缓存管理
- downloader + worker: 子进程隔离下载
- converter: 图片 -> PDF / PDF -> ZIP
- file_sender: 文件发送（local / OneBot）
- message_manager: 消息发送与撤回
- service: 下载编排服务
"""

import sys
from pathlib import Path

# 确保插件目录在 sys.path 中，支持导入 jmcomic_core 子包。
# 多文件 AstrBot 插件的必要步骤；multiprocessing.spawn 也会将此路径
# 传递给子进程，使其能定位 jmcomic_core.worker。
_plugin_dir = str(Path(__file__).resolve().parent)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from jmcomic_core.config import PluginConfig
from jmcomic_core.service import DownloadService

# ---- 依赖检测 ----
DEPENDENCIES_MET = True
try:
    import img2pdf  # noqa: F401
    import jmcomic  # noqa: F401
    import pyzipper  # noqa: F401
    from PIL import Image  # noqa: F401
except ImportError as e:
    logger.error(f"缺少依赖库: {e}")
    logger.error("请运行: pip install jmcomic Pillow img2pdf pyzipper aiohttp")
    DEPENDENCIES_MET = False

# 服务未就绪时的统一回复
_NOT_READY = "❌ 插件尚未初始化完成，请稍后重试。"
# /jmcomic_cache list 单次展示的条数上限
_CACHE_LIST_LIMIT = 15


@register(
    "jmcomic_downloader",
    "mutidayo3",
    "JMComic 本子下载器",
    "0.1.0",
    "https://github.com/mutidayo3/astrbot_plugin_jmcomic_downloader",
)
class JMComicPlugin(Star):
    """JMComic 本子下载器插件入口。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = PluginConfig.from_dict(config)
        if self.config.debug_log:
            logger.info("JMComic 插件调试日志已启用")
        self._service: DownloadService | None = None
        # setup() 完整跑完才算就绪；中途失败时 _service 已挂上（便于 terminate 回收
        # 已启动的资源），但内部子模块可能还是 None，此时不能接指令。
        self._ready = False

    def _debug(self, msg: str) -> None:
        """输出调试日志（仅在 debug_log 启用时生效）。"""
        if self.config.debug_log:
            logger.debug(f"[JMComic] {msg}")

    @property
    def _service_ready(self) -> DownloadService | None:
        """已就绪的服务实例，未就绪返回 None。"""
        return self._service if self._ready else None

    # ================================================================
    #  生命周期
    # ================================================================

    async def initialize(self):
        """插件初始化：创建数据目录并装配下载服务。"""
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        data_path = get_astrbot_data_path()
        download_dir = Path(data_path) / "plugin_data" / self.name / "downloads"
        download_dir.mkdir(parents=True, exist_ok=True)

        # 先挂上再 setup：即使 setup 中途抛错，terminate() 也能回收已启动的文件服务器
        self._service = DownloadService(self.config, download_dir, self._debug)
        await self._service.setup()
        self._ready = True

    async def terminate(self):
        """插件卸载：终止子进程、停止文件服务器。"""
        self._ready = False
        if self._service is not None:
            await self._service.shutdown()
            self._service = None

    # ================================================================
    #  指令
    # ================================================================

    @filter.command("jmcomic")
    async def download_jmcomic(self, event: AstrMessageEvent, album_id: str | None = None):
        """下载 JMComic 本子并转换为 PDF 发送。用法: /jmcomic <本子ID>"""
        if not DEPENDENCIES_MET:
            yield event.plain_result(
                "❌ 插件依赖缺失！\n请在 AstrBot 终端或容器内执行:\n"
                "pip install jmcomic Pillow img2pdf pyzipper aiohttp"
            )
            return

        service = self._service_ready
        if service is None:
            yield event.plain_result(_NOT_READY)
            return

        async for result in service.process(event, album_id):
            yield result

    @filter.command_group("jmcomic_cache")
    def jmcomic_cache(self):
        """JMComic 缓存管理指令组。"""

    @jmcomic_cache.command("list")
    async def cache_list(self, event: AstrMessageEvent):
        """查看当前 PDF 缓存列表。用法: /jmcomic_cache list"""
        service = self._service_ready
        if service is None:
            yield event.plain_result(_NOT_READY)
            return

        items, total = service.cache_overview(limit=_CACHE_LIST_LIMIT)
        if total == 0:
            yield event.plain_result("📭 当前没有缓存的本子。")
            return

        header = f"📦 PDF 缓存（共 {total} 本"
        header += f"，展示最近 {len(items)} 本）：" if total > len(items) else "）："
        lines = [header]
        for idx, (album_id, filename) in enumerate(items, 1):
            lines.append(f"{idx}. [{album_id}] {filename}")
        yield event.plain_result("\n".join(lines))

    @jmcomic_cache.command("clear")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cache_clear(self, event: AstrMessageEvent):
        """清空全部 PDF 缓存（仅管理员）。用法: /jmcomic_cache clear"""
        service = self._service_ready
        if service is None:
            yield event.plain_result(_NOT_READY)
            return

        removed = await service.clear_cache()
        yield event.plain_result(f"🧹 已清空缓存，共删除 {removed} 个 PDF 文件。")

    @filter.command("jmhelp")
    async def jmhelp(self, event: AstrMessageEvent):
        """查看 JMComic 下载器使用帮助。用法: /jmhelp"""
        yield event.plain_result(
            "📖 JMComic 本子下载器 使用帮助\n"
            "────────────────\n"
            "/jmcomic <本子ID>  下载本子并转换为 PDF 发送\n"
            "  示例: /jmcomic 422866\n"
            "/jmcomic_cache list  查看当前缓存\n"
            "/jmcomic_cache clear  清空全部缓存（管理员）\n"
            "/jmhelp  显示本帮助\n"
            "────────────────\n"
            "提示：建议将 image_format 设为 jpg，体积更小、转换更快。"
        )
