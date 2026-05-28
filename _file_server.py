"""轻量级 HTTP 文件服务器。

用于 Docker 跨容器文件传输场景，将本地下載目录通过 HTTP 暴露给 NapCat 等外部容器访问。
"""

from pathlib import Path
from typing import Optional
from urllib.parse import quote

from aiohttp import web

from astrbot.api import logger


class FileServer:
    """轻量级 HTTP 文件服务器，用于跨容器传输文件。

    特性：
    - 目录遍历防护（path traversal protection）
    - RFC 5987 非 ASCII 文件名编码
    - 调试日志可开关
    """

    def __init__(
        self,
        root_dir: Path,
        host: str = "0.0.0.0",
        port: int = 18790,
        debug: bool = False,
    ):
        self.root_dir = root_dir.resolve()
        self.host = host
        self.port = port
        self.debug = debug
        self.app = web.Application()
        self.app.router.add_get("/files/{path:.*}", self._handle)
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

    async def start(self):
        """启动 HTTP 服务器。"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        try:
            self.site = web.TCPSite(self.runner, self.host, self.port)
            await self.site.start()
            logger.info(f"HTTP 文件服务器已启动: http://{self.host}:{self.port}")
        except OSError as e:
            logger.error(f"HTTP 文件服务器启动失败 (端口 {self.port}): {e}")
            await self.runner.cleanup()
            self.runner = None
            raise

    async def stop(self):
        """停止 HTTP 服务器。"""
        if self.runner:
            await self.runner.cleanup()
            logger.info("HTTP 文件服务器已停止")

    async def _handle(self, request: web.Request) -> web.Response:
        """提供文件下载，带目录遍历防护。"""
        rel_path = request.match_info["path"]
        target = (self.root_dir / rel_path).resolve()

        if self.debug:
            logger.debug(f"[FileServer] 收到请求: {rel_path} -> {target}")

        # 安全检查：确保请求路径不超出 root_dir
        try:
            target.relative_to(self.root_dir)
        except ValueError:
            if self.debug:
                logger.debug(f"[FileServer] 路径遍历防护拦截: {rel_path}")
            return web.Response(status=403, text="Forbidden")

        if not target.exists() or not target.is_file():
            if self.debug:
                logger.debug(f"[FileServer] 文件不存在: {target}")
            return web.Response(status=404, text="Not Found")

        if self.debug:
            size_mb = target.stat().st_size / 1024 / 1024
            logger.debug(f"[FileServer] 发送文件: {target.name} ({size_mb:.2f} MB)")

        # RFC 5987/6266: 统一使用 filename*=UTF-8'' 编码发送所有文件名
        # 优势：
        # - 避免 ASCII 文件名中含双引号导致的 HTTP 头注入漏洞
        # - 天然支持所有 Unicode 字符（中文、日文等）
        # - Linux 下文件名经常包含中文，直接放在 ASCII header 中会导致 NapCat 下载失败
        encoded_name = quote(target.name, safe='')
        content_disp = f"attachment; filename*=UTF-8''{encoded_name}"

        return web.FileResponse(target, headers={
            "Content-Disposition": content_disp,
        })
