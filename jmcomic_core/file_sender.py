"""JMComic 文件发送器。

支持两种传输模式（由 service 在初始化时解析好后传入，运行期不再重新判定）：

- ``local``: AstrBot 与协议端（NapCat 等）在同一文件系统，直接把绝对路径交给
  OneBot 的 ``upload_group_file`` / ``upload_private_file``
- ``docker``: 二者文件系统隔离，通过内置 HTTP 文件服务器把文件暴露成 URL
  再交给 OneBot

大文件上传会在 QQ 的 Highway 通道上随机中断（与文件大小无关），失败点无规律，
因此上传失败后自动重试。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File


class FileSender:
    """文件发送器，按已解析的传输模式发送文件给用户。

    统一走 OneBot 的文件上传 API，绕开 AstrBot File 组件在 aiocqhttp 下的已知问题；
    上传失败自动重试，重试耗尽后回退 File 组件，非 aiocqhttp 平台直接用 File 组件。
    """

    def __init__(
        self,
        mode: str = "local",
        file_url_base: str = "",
        debug_callback: Callable[[str], None] | None = None,
        upload_retry: int = 3,
    ) -> None:
        """
        Args:
            mode: 已解析的实际传输模式，``"local"`` 或 ``"docker"``（其他值按 local 处理）
            file_url_base: docker 模式下文件 URL 的完整前缀，已包含文件服务器地址与
                带 token 的路径前缀（如 ``http://172.17.0.1:18790/files/<token>``），
                由 service 在装配时拼好
            debug_callback: 调试日志回调
            upload_retry: 上传总尝试次数（含首次），最小 1
        """
        self.mode = "docker" if mode == "docker" else "local"
        self.file_url_base = file_url_base.rstrip("/") if file_url_base else ""
        self._debug = debug_callback or (lambda msg: None)
        self.upload_retry = max(1, upload_retry)

    def _build_file_ref(self, file_path: Path) -> str:
        """构造交给 OneBot 的 file 参数。

        - docker 模式：HTTP URL（文件名做百分号编码，保证中日文文件名在请求行中合法）
        - local 模式：本地绝对路径
        """
        if self.mode == "docker" and self.file_url_base:
            return f"{self.file_url_base}/{quote(file_path.name, safe='')}"
        if not file_path.is_absolute():
            file_path = file_path.resolve()
        return str(file_path)

    async def send(
        self,
        event: AstrMessageEvent,
        file_path: Path,
        file_name: str,
        album_id: str,
    ) -> None:
        """按传输模式发送文件。

        Args:
            event: AstrBot 消息事件
            file_path: 文件在磁盘上的绝对路径
            file_name: 发送时显示的文件名
            album_id: 本子 ID（用于失败提示）
        """
        # docker 模式必须有可用的文件服务器地址，否则拼出来的 URL 会退化成
        # 相对路径，协议端只能按本地路径解析并报错。此时回退本地路径更可能成功。
        if self.mode == "docker" and not self.file_url_base:
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

        # 上传重试：Highway 通道会在随机位置中断，重试是唯一有效手段。
        for attempt in range(1, self.upload_retry + 1):
            try:
                await self._upload_via_onebot(event, file_ref, file_name)
                if attempt > 1:
                    logger.info(f"第 {attempt}/{self.upload_retry} 次尝试上传成功: {file_name}")
                return
            except Exception as e:
                if attempt < self.upload_retry:
                    delay = min(3 * 2 ** (attempt - 1), 15)
                    logger.warning(
                        f"OneBot 上传失败（第 {attempt}/{self.upload_retry} 次），"
                        f"{delay}s 后重试: {e}"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"OneBot 上传连续 {self.upload_retry} 次失败: {e}", exc_info=True)

        # 回退 1：改用 AstrBot File 组件（部分协议端更接受消息段形式）
        try:
            await self._send_via_component(event, file_path, file_name)
            logger.info(f"已通过 File 组件回退发送文件: {file_name}")
            return
        except Exception as e:
            logger.error(f"File 组件回退发送同样失败: {e}", exc_info=True)

        # 回退 2：告知用户（不暴露内部服务 URL，避免信息泄露）
        await event.send(
            event.plain_result(
                f"⚠️ 文件发送失败，请稍后重试或联系管理员。\n"
                f"（文件: {file_name}，本子ID: {album_id}）"
            )
        )

    async def _upload_via_onebot(
        self,
        event: AstrMessageEvent,
        file_ref: str,
        file_name: str,
    ) -> None:
        """直接调用 OneBot 文件上传 API，绕过 AstrBot File 组件的已知问题。"""
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )

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
    ) -> None:
        """使用 AstrBot File 组件发送（跨平台通用路径）。"""
        await event.send(event.chain_result([File(file=str(file_path), name=file_name)]))
        logger.info(f"已通过 File 组件发送文件: {file_path}")
