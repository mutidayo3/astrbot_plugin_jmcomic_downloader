"""消息发送与撤回管理器。

通过 OneBot API 直接发送文字消息并追踪 message_id，
支持任务完成后集中撤回所有追踪的消息，多条撤回间隔不小于 6 秒。

用法:
    mgr = MessageManager(auto_recall=True)
    tracked = []
    msg_id = await mgr.send_text(event, "排队中...", track=True, tracked_ids=tracked)
    if msg_id is None:
        yield event.plain_result("排队中...")  # fallback
    # ... 任务完成后 ...
    await mgr.recall_all(tracked)
"""

import asyncio
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

# 多条撤回最小间隔（秒）
RECALL_SPACING = 6.0


class MessageManager:
    """通过 OneBot API 发送文字消息并支持任务完成后集中撤回。

    发送时直接调用 event.bot 的 send_group_msg / send_private_msg，
    捕获返回的 message_id。任务完成后调用 recall_all() 统一撤回。
    """

    def __init__(
        self,
        auto_recall: bool = True,
        recall_spacing: float = RECALL_SPACING,
    ):
        self.auto_recall = auto_recall
        self.recall_spacing = recall_spacing
        # 撤回间隔信号量：同一时间只允许一个撤回在进行
        self._spacing_sem = asyncio.Semaphore(1)
        # 追踪活跃撤回任务（用于 terminate 时取消）
        self._tasks: set[asyncio.Task] = set()
        # 缓存最近一次 send_text 使用的 bot 实例，供 recall_all 使用
        self._cached_bot = None

    # ---- 公开 API ----

    async def send_text(
        self,
        event: AstrMessageEvent,
        text: str,
        track: bool = False,
        tracked_ids: Optional[list[str]] = None,
    ) -> Optional[str]:
        """发送文字消息并可选追踪 message_id 用于后续集中撤回。

        Args:
            event: AstrBot 消息事件，从中提取 bot 实例和会话信息
            text: 要发送的文字内容
            track: 是否将 message_id 追加到 tracked_ids 列表（用于任务完成后统一撤回）
            tracked_ids: 外部维护的追踪列表，仅在 track=True 时写入

        Returns:
            消息的 message_id（字符串），失败时返回 None。
            调用方应在返回 None 时 fallback 到 yield event.plain_result()。
        """
        bot = getattr(event, 'bot', None)
        if bot is None:
            return None
        self._cached_bot = bot

        # 判断群聊/私聊
        group_id = event.get_group_id()
        is_group = bool(group_id)
        session_id = group_id if is_group else event.get_session_id()

        if not session_id or not session_id.isdigit():
            return None

        message = [{"type": "text", "data": {"text": text}}]

        try:
            if is_group:
                ret = await bot.send_group_msg(
                    group_id=int(session_id), message=message
                )
            else:
                ret = await bot.send_private_msg(
                    user_id=int(session_id), message=message
                )
        except Exception as e:
            logger.warning(f"[MessageManager] 发送失败: {e}")
            return None

        message_id = str(ret.get('message_id', '')) if isinstance(ret, dict) else None

        if self.auto_recall and track and message_id and tracked_ids is not None:
            tracked_ids.append(message_id)

        return message_id

    async def recall_all(self, message_ids: list[str]):
        """任务完成后集中撤回所有追踪的消息（按 spacing 间隔顺序撤回）。

        Args:
            message_ids: 待撤回的 message_id 列表
        """
        if not self.auto_recall or not message_ids:
            return

        task = asyncio.create_task(self._recall_batch(message_ids))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- 生命周期 ----

    async def terminate(self):
        """取消所有未完成的撤回任务。"""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.debug("[MessageManager] 已取消所有撤回任务")

    # ---- 内部实现 ----

    async def _recall_batch(self, message_ids: list[str]):
        """顺序撤回消息列表，通过信号量保证撤回间隔 ≥ recall_spacing 秒。"""
        bot = self._cached_bot
        if bot is None:
            logger.debug("[MessageManager] 无可用 bot，跳过撤回")
            return
        try:
            for mid in message_ids:
                async with self._spacing_sem:
                    try:
                        await bot.call_action('delete_msg', message_id=mid)
                        logger.debug(f"[MessageManager] 已撤回: {mid}")
                    except Exception as e:
                        logger.debug(f"[MessageManager] 撤回失败 ({mid}): {e}")
                    await asyncio.sleep(self.recall_spacing)
        except asyncio.CancelledError:
            pass
