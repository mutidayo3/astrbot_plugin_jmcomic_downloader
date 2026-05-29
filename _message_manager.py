"""消息发送与撤回管理器。

通过 OneBot API 直接发送文字消息并追踪 message_id，
支持按延迟自动撤回，多条撤回间隔不小于 6 秒。

用法:
    mgr = MessageManager(auto_recall=True)
    msg_id = await mgr.send_text(event, "排队中...", recall_delay=30)
    if msg_id is None:
        yield event.plain_result("排队中...")  # fallback
"""

import asyncio
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

# 状态消息默认撤回延迟（秒）
DEFAULT_RECALL_DELAY = 30.0
# 多条撤回最小间隔（秒）
RECALL_SPACING = 6.0


class MessageManager:
    """通过 OneBot API 发送文字消息并支持自动撤回。

    发送时直接调用 event.bot 的 send_group_msg / send_private_msg，
    捕获返回的 message_id，并在后台任务中按延迟+间隔撤回。
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

    # ---- 公开 API ----

    async def send_text(
        self,
        event: AstrMessageEvent,
        text: str,
        recall_delay: float = 0.0,
    ) -> Optional[str]:
        """发送文字消息并可选安排撤回。

        Args:
            event: AstrBot 消息事件，从中提取 bot 实例和会话信息
            text: 要发送的文字内容
            recall_delay: 撤回延迟秒数。0 或 auto_recall=False 时不撤回

        Returns:
            消息的 message_id（字符串），失败时返回 None。
            调用方应在返回 None 时 fallback 到 yield event.plain_result()。
        """
        bot = getattr(event, 'bot', None)
        if bot is None:
            return None

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

        if self.auto_recall and recall_delay > 0 and message_id:
            task = asyncio.create_task(
                self._delayed_recall(message_id, bot, recall_delay)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return message_id

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

    async def _delayed_recall(self, message_id: str, bot, delay: float):
        """延迟撤回单条消息，通过信号量保证撤回间隔 ≥ recall_spacing 秒。

        设计说明：
        - 每条消息独立创建一个 Task，各自 sleep(delay) 后排队抢信号量
        - 信号量确保同一时刻只有一个撤回操作在进行
        - 撤回完成后在信号量内 sleep(spacing)，阻止下一个立即抢到锁
        """
        try:
            await asyncio.sleep(delay)
            async with self._spacing_sem:
                try:
                    await bot.call_action('delete_msg', message_id=message_id)
                    logger.debug(f"[MessageManager] 已撤回: {message_id}")
                except Exception as e:
                    logger.debug(f"[MessageManager] 撤回失败 ({message_id}): {e}")
                # 在信号量内 sleep，确保下一条撤回至少间隔 recall_spacing 秒
                await asyncio.sleep(self.recall_spacing)
        except asyncio.CancelledError:
            pass
