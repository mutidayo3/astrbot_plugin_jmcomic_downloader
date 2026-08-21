"""FIFO 有序信号量。

严格按 ``acquire`` 调用顺序放行，避免标准 ``asyncio.Semaphore`` 的无序唤醒。
内部维护一个 Future 等待队列，``release`` 时总是唤醒队首的 Future，
确保先请求的协程先获得执行权。
"""

from __future__ import annotations

import asyncio
import contextvars


class FIFOSemaphore:
    """FIFO 有序信号量：严格按 acquire 调用顺序放行。

    适用于需要按请求先后顺序处理或发送的场景（如多用户并发请求时
    保证先提交的下载任务先得到处理）。

    注意：``_acquired_var`` 是类级 ContextVar，每个 asyncio Task 有独立副本，
    确保不同协程的获取状态互不干扰。
    """

    _acquired_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
        "fifo_acquired", default=False
    )

    def __init__(self, value: int = 1):
        if value < 1:
            raise ValueError(f"Semaphore value must be >= 1, got {value}")
        self._value = value
        self._waiters: list[asyncio.Future] = []
        self._queue_lock = asyncio.Lock()  # 保护 _waiters 列表的并发访问

    @property
    def queued(self) -> int:
        """当前排队等待的协程数量。"""
        return len(self._waiters)

    async def acquire(self) -> None:
        FIFOSemaphore._acquired_var.set(False)
        async with self._queue_lock:
            if self._value > 0:
                self._value -= 1
                FIFOSemaphore._acquired_var.set(True)
                return
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._waiters.append(fut)
        try:
            await fut
            FIFOSemaphore._acquired_var.set(True)
        except asyncio.CancelledError:
            async with self._queue_lock:
                if fut in self._waiters:
                    # 仍在队列中，release() 尚未处理此 Future，安全移除
                    self._waiters.remove(fut)
                elif fut.done():
                    # release() 已弹出此 Future 并通过 call_soon 调度了 set_result，
                    # 但任务在回调执行前被取消，导致槽位已被消费但未被使用。
                    # 重新触发 release 将槽位传递给下一个等待者，避免泄漏。
                    self.release()
            raise

    def release(self) -> None:
        """释放一个槽位，按 FIFO 顺序唤醒下一个等待者。

        注意：此方法是同步的，依赖 asyncio 单线程模型保证原子性。
        两个 await 点之间的同步代码不会被其他协程打断，因此无需持有
        ``_queue_lock``。若将来改为 async，必须加上 ``async with`` 保护。
        """
        loop = asyncio.get_running_loop()
        # 循环唤醒：跳过已取消的 Future，找到第一个有效的等待者
        while True:
            with_fut = None
            if self._waiters:
                with_fut = self._waiters.pop(0)
            if with_fut is None:
                self._value += 1
                return
            if not with_fut.done():
                loop.call_soon(self._wake, with_fut)
                return
            # Future 已取消，继续找下一个

    @staticmethod
    def _wake(fut: asyncio.Future) -> None:
        """在事件循环回调里唤醒等待者。

        ``release`` 与本回调之间存在一个事件循环时隙，等待者可能在此期间被取消；
        直接 ``set_result`` 会抛 ``InvalidStateError`` 并污染日志。此时槽位由
        ``acquire`` 的取消分支负责转交给下一个等待者，这里安静跳过即可。
        """
        if not fut.done():
            fut.set_result(None)

    async def __aenter__(self) -> FIFOSemaphore:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        # ContextVar 保证每个协程独立读取自己的获取状态
        if FIFOSemaphore._acquired_var.get():
            self.release()
        return False

    def __repr__(self) -> str:
        return f"<FIFOSemaphore value={self._value} waiters={len(self._waiters)}>"
