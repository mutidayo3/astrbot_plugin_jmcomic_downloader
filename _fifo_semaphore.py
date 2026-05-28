"""FIFO 有序信号量。

严格按 acquire 调用顺序放行，避免标准 asyncio.Semaphore 的无序唤醒。
内部维护一个 Future 等待队列，release 时总是唤醒队首的 Future，
确保先请求的协程先获得执行权。
"""

import asyncio


class _FIFOSemaphore:
    """FIFO 有序信号量：严格按 acquire 调用顺序放行。

    适用于需要按请求先后顺序处理或发送的场景（如多用户并发请求时
    保证先提交的下载任务先得到处理）。
    """

    def __init__(self, value: int = 1):
        if value < 1:
            raise ValueError(f"Semaphore value must be >= 1, got {value}")
        self._value = value
        self._acquired = False  # 实例级追踪，避免跨实例 ContextVar 污染
        self._waiters: list[asyncio.Future] = []
        self._queue_lock = asyncio.Lock()  # 保护 _waiters 列表的并发访问

    @property
    def queued(self) -> int:
        """当前排队等待的协程数量"""
        return len(self._waiters)

    async def acquire(self):
        self._acquired = False
        async with self._queue_lock:
            if self._value > 0:
                self._value -= 1
                self._acquired = True
                return
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._waiters.append(fut)
        try:
            await fut
            self._acquired = True
        except asyncio.CancelledError:
            async with self._queue_lock:
                if fut in self._waiters:
                    # 仍在队列中，release() 尚未处理此 Future，安全移除
                    self._waiters.remove(fut)
                elif fut.done() and not fut.cancelled():
                    # release() 已弹出此 Future 并通过 call_soon 调度了 set_result，
                    # 但任务在回调执行前被取消，导致槽位未被消费。
                    # 重新触发 release 将槽位传递给下一个等待者，避免泄漏。
                    self.release()
            raise

    def release(self):
        """释放一个槽位，按 FIFO 顺序唤醒下一个等待者。

        注意：此方法是同步的，依赖 asyncio 单线程模型保证原子性。
        两个 await 点之间的同步代码不会被其他协程打断，因此无需持有 _queue_lock。
        如果将来将此方法改为 async，必须加上 async with self._queue_lock 保护 _waiters。
        """
        loop = asyncio.get_running_loop()
        # 循环唤醒：跳过已取消的 Future，找到第一个有效的等待者
        while True:
            with_fut = None
            # 快速弹出队首（此处无并发修改风险，release 在事件循环线程同步执行）
            if self._waiters:
                with_fut = self._waiters.pop(0)
            if with_fut is None:
                self._value += 1
                return
            if not with_fut.done():
                loop.call_soon(with_fut.set_result, None)
                return
            # Future 已取消，继续找下一个

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # 使用实例变量追踪获取状态，避免跨实例 ContextVar 污染
        # （asyncio 单线程模型下无需加锁）
        if self._acquired:
            self.release()
            self._acquired = False
        return False

    def __repr__(self):
        return (
            f"<_FIFOSemaphore value={self._value} "
            f"waiters={len(self._waiters)}>"
        )
