"""JMComic 文件发送器。

支持两种传输模式（由 main.py 在初始化时解析好后传入，运行期不再重新判定）：
- local: AstrBot 与协议端（NapCat 等）在同一文件系统，直接把绝对路径交给
  OneBot 的 upload_group_file / upload_private_file
- docker: 二者文件系统隔离，通过内置 HTTP 文件服务器把文件暴露成 URL 再交给 OneBot
"""

from pathlib import Path
from typing import Optional, Callable
from urllib.parse import quote

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File


class FileSender:
    """文件发送器，按已解析的传输模式发送文件给用户。

    统一走 OneBot 的文件上传 API，绕开 AstrBot File 组件在 aiocqhttp 下的已知问题；
    非 aiocqhttp 平台或上传 API 失败时回退到 File 组件。
    """

    def __init__(
        self,
        mode: str = "local",
        file_server_base_url: str = "",
        debug_callback: Optional[Callable[[str], None]] = None,
    ):
        """
        Args:
            mode: 已解析的实际传输模式，"local" 或 "docker"（其他值按 local 处理）
            file_server_base_url: docker 模式下文件服务器的外部访问地址
                （内置 HTTP 服务器或用户自建的反向代理均可）
            debug_callback: 调试日志回调
        """
        self.mode = "docker" if mode == "docker" else "local"
        self.file_server_base_url = file_server_base_url.rstrip("/") if file_server_base_url else ""
        self._debug = debug_callback or (lambda msg: None)

    def _build_file_ref(self, file_path: Path) -> str:
        """构造交给 OneBot 的 file 参数。

        - docker 模式：HTTP URL（文件名做百分号编码，保证中日文文件名在请求行中合法；
          aiohttp 路由的 {path:.*} 会自动 URL-decode，服务端能正确匹配）
        - local 模式：本地绝对路径（OneBot v11 规定 upload_*_file 的 file 为本地文件路径）
        """
        if self.mode == "docker" and self.file_server_base_url:
            return f"{self.file_server_base_url}/files/{quote(file_path.name, safe='')}"
        if not file_path.is_absolute():
            file_path = file_path.resolve()
        return str(file_path)

    async def send(
        self,
        event: AstrMessageEvent,
        file_path: Path,
        file_name: str,
        album_id: str,
    ):
        """按传输模式发送文件。

        Args:
            event: AstrBot 消息事件
            file_path: 文件在磁盘上的绝对路径
            file_name: 发送时显示的文件名
            album_id: 本子 ID（用于失败提示）
        """
        # docker 模式必须有可用的文件服务器地址，否则拼出来的 URL 会退化成
        # "/files/xxx.pdf" 这种相对路径，协议端只能按本地路径解析并报
        # "未知文件类型或路径不存在"。此时回退到本地路径发送更可能成功。
        if self.mode == "docker" and not self.file_server_base_url:
            logger.warning(
                "docker 传输模式缺少 file_server_base_url，已回退为本地路径发送。"
                "若 AstrBot 与协议端确实不在同一文件系统，请在插件配置中填写该地址。"
            )
            self.mode = "local"

        file_ref = self._build_file_ref(file_path)
        self._debug(
            f"发送模式={self.mode}, 文件={file_name}, 路径={file_path}, file 参数={file_ref}"
        )

        if event.get_platform_name() != "aiocqhttp":
            # 非 aiocqhttp 平台交给 AstrBot 自行处理
            await self._send_via_component(event, file_path, file_name)
            return

        try:
            await self._upload_via_onebot(event, file_ref, file_name)
            return
        except Exception as e:
            logger.error(f"OneBot API 发送文件失败: {e}", exc_info=True)

        # 回退 1：改用 AstrBot File 组件（部分协议端更接受消息段形式）
        try:
            await self._send_via_component(event, file_path, file_name)
            logger.info(f"已通过 File 组件回退发送文件: {file_name}")
            return
        except Exception as e:
            logger.error(f"File 组件回退发送同样失败: {e}", exc_info=True)

        # 回退 2：告知用户（不暴露内部服务 URL，避免信息泄露）
        await event.send(event.plain_result(
            f"⚠️ 文件发送失败，请稍后重试或联系管理员。\n"
            f"（文件: {file_name}，本子ID: {album_id}）"
        ))

    async def _upload_via_onebot(
        self,
        event: AstrMessageEvent,
        file_ref: str,
        file_name: str,
    ):
        """直接调用 OneBot 文件上传 API，绕过 AstrBot File 组件的已知问题。"""
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        if not isinstance(event, AiocqhttpMessageEvent):
            raise TypeError(f"Expected AiocqhttpMessageEvent, got {type(event).__name__}")
        bot = event.bot

        group_id = event.get_group_id()
        if group_id:
            self._debug(f"OneBot 上传: 目标群={group_id}, name={file_name}")
            await bot.call_action(
                "upload_group_file",
                group_id=int(group_id),
                file=file_ref,
                name=file_name,
            )
            logger.info(f"已通过 upload_group_file 发送文件到群 {group_id}")
        else:
            user_id = event.get_sender_id()
            self._debug(f"OneBot 上传: 目标用户={user_id}, name={file_name}")
            await bot.call_action(
                "upload_private_file",
                user_id=int(user_id),
                file=file_ref,
                name=file_name,
            )
            logger.info(f"已通过 upload_private_file 发送文件给用户 {user_id}")

    async def _send_via_component(
        self,
        event: AstrMessageEvent,
        file_path: Path,
        file_name: str,
    ):
        """使用 AstrBot File 组件发送（跨平台通用路径）。"""
        await event.send(event.chain_result([
            File(file=str(file_path), name=file_name)
        ]))
        logger.info(f"已通过 File 组件发送文件: {file_path}")
