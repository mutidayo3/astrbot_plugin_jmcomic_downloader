"""JMComic 文件发送器。

支持三种传输模式：
- local: 直接使用本地文件路径发送
- docker: 通过 OneBot API (upload_group_file / upload_private_file) 发送 HTTP 文件
- auto: 自动检测运行环境选择模式
"""

from pathlib import Path
from typing import Optional, Callable
from urllib.parse import quote

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File


class FileSender:
    """文件发送器，根据配置选择传输模式发送文件给用户。

    自动处理 Docker 跨容器场景，对非 ASCII 文件名进行 RFC 5987 编码。
    """

    def __init__(
        self,
        transfer_mode: str = "auto",
        file_server_base_url: str = "",
        debug_callback: Optional[Callable[[str], None]] = None,
        file_server: object = None,
        is_docker_checker: Optional[Callable[[], bool]] = None,
    ):
        self.transfer_mode = transfer_mode
        self.file_server_base_url = file_server_base_url.rstrip("/") if file_server_base_url else ""
        self._debug = debug_callback or (lambda msg: None)
        self._file_server = file_server
        self._is_docker_checker = is_docker_checker or (lambda: False)

    def _resolve_mode(self) -> str:
        """解析实际传输模式（auto → local/docker）"""
        mode = self.transfer_mode
        if mode == "auto":
            mode = "docker" if self._is_docker_checker() else "local"
        return mode

    async def send(
        self,
        event: AstrMessageEvent,
        file_path: Path,
        file_name: str,
        album_id: str,
    ):
        """根据配置选择文件传输方式发送文件。

        Args:
            event: AstrBot 消息事件
            file_path: 文件在磁盘上的绝对路径
            file_name: 发送时显示的文件名
            album_id: 本子 ID（仅用于调试日志）
        """
        mode = self._resolve_mode()
        self._debug(
            f"发送模式解析: 配置={self.transfer_mode} -> 实际={mode}, "
            f"文件={file_name}, 路径={file_path}"
        )

        if mode == "local":
            # 本地模式：直接使用 File 组件
            await event.send(
                event.chain_result([
                    File(file=str(file_path), name=file_name)
                ])
            )
            logger.info(f"已通过本地路径发送文件: {file_path}")
        else:
            # Docker 模式：使用 HTTP 文件服务器 + OneBot API
            await self._send_via_onebot(event, file_path, file_name, album_id)

    async def _send_via_onebot(
        self,
        event: AstrMessageEvent,
        file_path: Path,
        file_name: str,
        album_id: str,
    ):
        """直接调用 OneBot API 发送文件，绕过 AstrBot File 组件的已知问题。

        仅在 aiocqhttp 平台下工作，其他平台回退到普通方式。
        """
        if event.get_platform_name() != "aiocqhttp":
            # 非 aiocqhttp 平台回退到普通方式
            await event.send(event.chain_result([File(file=str(file_path), name=file_name)]))
            return

        # 获取 aiocqhttp 的 bot 客户端
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        if not isinstance(event, AiocqhttpMessageEvent):
            raise TypeError(f"Expected AiocqhttpMessageEvent, got {type(event).__name__}")
        bot = event.bot

        # 判断是群聊还是私聊
        message_obj = event.message_obj
        is_group = hasattr(message_obj, 'group_id') and message_obj.group_id

        # 构建文件 URL（NapCat 会通过 HTTP 下载）
        # RFC 3986: 对文件名进行百分号编码，确保中文等非 ASCII 字符在 HTTP 请求行中合法
        # aiohttp 路由的 {path:.*} 参数会自动进行 URL-decode，因此服务端能正确匹配
        file_url = f"{self.file_server_base_url}/files/{quote(file_path.name, safe='')}"
        self._debug(f"OneBot 发送参数: is_group={is_group}, file_url={file_url}, name={file_name}")
        if is_group:
            self._debug(f"目标群: {message_obj.group_id}")
        else:
            self._debug(f"目标用户: {message_obj.sender.user_id}")

        try:
            if is_group:
                group_id = int(message_obj.group_id)
                await bot.call_action(
                    "upload_group_file",
                    group_id=group_id,
                    file=file_url,
                    name=file_name,
                )
                logger.info(f"已通过 upload_group_file 发送文件到群 {group_id}")
            else:
                user_id = int(message_obj.sender.user_id)
                await bot.call_action(
                    "upload_private_file",
                    user_id=user_id,
                    file=file_url,
                    name=file_name,
                )
                logger.info(f"已通过 upload_private_file 发送文件给用户 {user_id}")
        except Exception as e:
            logger.error(f"OneBot API 发送文件失败: {e}", exc_info=True)
            # 降级：提示用户发送失败（不暴露内部服务 URL，避免信息泄露）
            await event.send(event.plain_result(
                f"⚠️ 文件发送失败，请稍后重试或联系管理员。\n"
                f"（文件: {file_name}，本子ID: {album_id}）"
            ))
