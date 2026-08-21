"""轻量级 HTTP 文件服务器。

用于 Docker 跨容器文件传输场景，将本地下载目录通过 HTTP 暴露给
NapCat 等外部容器访问。

服务器必须绑定 ``0.0.0.0`` 才能被其它容器访问，因此下载目录对整个局域网可见。
为此做了两层收敛：URL 中带一段每次启动随机生成的 token，且只放行成品文件后缀
（``.pdf`` / ``.zip``），避免 ``cache_index.json`` 这类内部文件被顺手拖走。
"""

from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import quote

from aiohttp import web
from astrbot.api import logger

# 只对外提供成品文件，避免泄露 cache_index.json（内含全部本子 ID 与标题）
_ALLOWED_SUFFIXES = frozenset({".pdf", ".zip"})


class FileServer:
    """轻量级 HTTP 文件服务器，用于跨容器传输文件。

    特性：
    - 目录遍历防护（path traversal protection）
    - 随机 token 前缀 + 后缀白名单，收敛 ``0.0.0.0`` 暴露面
    - RFC 5987 非 ASCII 文件名编码
    - 调试日志可开关
    """

    def __init__(
        self,
        root_dir: Path,
        host: str = "0.0.0.0",
        port: int = 18790,
        debug: bool = False,
    ) -> None:
        self.root_dir = root_dir.resolve()
        self.host = host
        self.port = port
        self.debug = debug
        # 每次启动重新生成：插件重载后旧 URL 自动失效
        self.token = secrets.token_urlsafe(16)
        self.app = web.Application()
        self.app.router.add_get("/files/{token}/{path:.*}", self._handle)
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    @property
    def path_prefix(self) -> str:
        """文件 URL 的路径前缀（含随机 token），供发送器拼接完整地址。"""
        return f"/files/{self.token}"

    async def start(self) -> None:
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

    async def stop(self) -> None:
        """停止 HTTP 服务器。"""
        if self.runner:
            await self.runner.cleanup()
            logger.info("HTTP 文件服务器已停止")

    async def _handle(self, request: web.Request) -> web.Response:
        """提供文件下载，带 token 校验、后缀白名单与目录遍历防护。"""
        rel_path = request.match_info["path"]

        # token 不匹配时一律返回 404：不确认该路径下是否真有文件
        if not secrets.compare_digest(request.match_info.get("token", ""), self.token):
            if self.debug:
                logger.debug(f"[FileServer] token 校验失败: {rel_path}")
            return web.Response(status=404, text="Not Found")

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

        if target.suffix.lower() not in _ALLOWED_SUFFIXES:
            if self.debug:
                logger.debug(f"[FileServer] 后缀不在白名单: {target.name}")
            return web.Response(status=403, text="Forbidden")

        if not target.exists() or not target.is_file():
            if self.debug:
                logger.debug(f"[FileServer] 文件不存在: {target}")
            return web.Response(status=404, text="Not Found")

        if self.debug:
            size_mb = target.stat().st_size / 1024 / 1024
            logger.debug(f"[FileServer] 发送文件: {target.name} ({size_mb:.2f} MB)")

        # RFC 5987/6266: 统一使用 filename*=UTF-8'' 编码发送所有文件名
        # 优势：避免 ASCII 文件名含双引号导致的头注入；天然支持所有 Unicode
        # 字符（中文/日文），Linux 文件名含中文时直接放 ASCII header 会导致
        # NapCat 下载失败。
        encoded_name = quote(target.name, safe="")
        content_disp = f"attachment; filename*=UTF-8''{encoded_name}"

        return web.FileResponse(target, headers={"Content-Disposition": content_disp})
