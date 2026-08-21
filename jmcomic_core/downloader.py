"""JMComic 下载器（子进程隔离）。

使用 ``multiprocessing.spawn`` + Pipe 通信将下载任务隔离到独立子进程，
支持超时终止、SIGTERM 诊断、exitcode 分析等功能。
"""

from __future__ import annotations

import asyncio
import multiprocessing
import shutil
import signal
from collections.abc import Callable
from pathlib import Path

from astrbot.api import logger

from jmcomic_core.worker import download_album_worker


class Downloader:
    """JMComic 本子下载器，在独立子进程中执行下载任务。

    特性：
    - 使用 spawn 上下文避免污染插件框架
    - Pipe 通信替代 Queue，消除 ``join_thread()`` 阻塞
    - 超时后两级终止（SIGTERM -> SIGKILL）
    - 详细的 exitcode 诊断（OOM Killer、崩溃等）
    - 支持外部 active_processes 集合追踪，便于统一清理
    """

    def __init__(
        self,
        max_workers: int = 4,
        image_format: str = "jpg",
        download_timeout: int = 300,
        debug_callback: Callable[[str], None] | None = None,
        active_processes: set[multiprocessing.Process] | None = None,
    ) -> None:
        self.max_workers = max_workers
        self.image_format = image_format
        self.download_timeout = download_timeout
        self._debug = debug_callback or (lambda msg: None)
        self._active_processes: set[multiprocessing.Process] = active_processes or set()

    async def download(self, album_id: str, download_dir: Path) -> str:
        """异步下载本子并返回原始标题。

        Args:
            album_id: 本子 ID
            download_dir: 下载目标目录（必须已存在）

        Returns:
            本子原始标题（未经文件名清理）

        Raises:
            RuntimeError: 下载失败或子进程异常
            asyncio.TimeoutError: 下载超时
        """
        album_title = album_id
        self._debug(
            f"下载参数: id={album_id}, dir={download_dir}, format={self.image_format}, "
            f"workers={self.max_workers}, timeout={self.download_timeout}s"
        )

        loop = asyncio.get_running_loop()
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=False)

        process = ctx.Process(
            target=download_album_worker,
            args=(album_id, str(download_dir), self.image_format, self.max_workers, child_conn),
            daemon=True,
        )
        process.start()
        self._active_processes.add(process)
        child_conn.close()  # 父进程不使用子端连接
        self._debug(f"下载子进程已启动: PID={process.pid}")

        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, process.join),
                timeout=self.download_timeout,
            )

            # 从 Pipe 读取结果（进程已结束，数据在缓冲区中，非阻塞）
            if parent_conn.poll():
                status, value = parent_conn.recv()
                if status == "ok":
                    album_title = value
                else:
                    raise RuntimeError(f"下载失败: {value}")
            else:
                # 子进程未发送任何数据即退出，根据 exitcode 提供诊断信息
                raise RuntimeError(self._diagnose_exitcode(process.exitcode))

        except asyncio.TimeoutError:
            logger.error(f"下载子进程超时，正在终止: PID={process.pid}")
            process.terminate()
            try:
                process.join(timeout=5)
                if process.is_alive():
                    logger.warning(f"子进程 {process.pid} 未响应 SIGTERM，强制杀死")
                    process.kill()
                    process.join(timeout=3)
            except OSError:
                pass

            if download_dir.exists():
                shutil.rmtree(download_dir, ignore_errors=True)
                logger.info(f"已清理超时的不完整下载: {download_dir}")
            raise

        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
            parent_conn.close()
            self._active_processes.discard(process)
            self._debug(f"下载子进程已结束: PID={process.pid}, exitcode={process.exitcode}")

        file_count = sum(1 for _ in download_dir.rglob("*") if _.is_file())
        logger.info(f"下载完成: {album_title} (共 {file_count} 个文件)")
        return album_title

    @staticmethod
    def _diagnose_exitcode(exitcode: int | None) -> str:
        """根据子进程 exitcode 生成人类可读的诊断信息。"""
        if exitcode is not None and exitcode < 0:
            sig_name = (
                signal.Signals(-exitcode).name
                if hasattr(signal, "Signals")
                else f"signal {-exitcode}"
            )
            detail = f"子进程被信号杀死 ({sig_name}, exitcode={exitcode})"
            if -exitcode == getattr(signal, "SIGKILL", 9):
                detail += "，可能是 OOM Killer 介入，请检查系统内存"
            return detail
        if exitcode == 0:
            return f"子进程正常退出但未返回结果 (exitcode={exitcode})，可能是 jmcomic 库内部异常"
        return f"子进程异常退出 (exitcode={exitcode})，可能是未捕获的异常或崩溃"
