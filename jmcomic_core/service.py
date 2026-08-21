"""下载编排服务。

将原 ``main.py`` 中冗长的下载-转换-发送-缓存流程抽离为独立服务类，
使 :class:`Star` 插件入口保持精简。插件 Handler 仅做参数转发，
所有副作用与状态都封装在此处。

流程概览::

    process(event, album_id)
      ├── 限频检查（同一聊天同一本子在窗口内只允许一次）
      ├── 图片数量预检（防止超大本子耗尽资源）
      ├── 专辑锁（同一本子不重复处理）
      └── FIFO 信号量（按请求顺序放行）
            ├── 1. 缓存命中 -> 直接发送
            ├── 2. 下载（子进程隔离）
            ├── 3. 收集图片
            ├── 4. 转 PDF
            ├── 5. 统一命名 + 更新缓存映射
            ├── 6. ZIP 压缩（可选）
            ├── 7. 发送文件 + 撤回状态消息
            └── finally: 清理临时目录 + LRU 淘汰
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
import shutil
import time
from collections.abc import AsyncGenerator
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult

from jmcomic_core.cache import CacheManager
from jmcomic_core.config import PluginConfig
from jmcomic_core.converter import compress_to_zip, convert_to_pdf
from jmcomic_core.downloader import Downloader
from jmcomic_core.env import (
    collect_image_files,
    container_detection_reason,
    get_host_ip,
    is_running_in_docker,
)
from jmcomic_core.fifo_semaphore import FIFOSemaphore
from jmcomic_core.file_sender import FileSender
from jmcomic_core.file_server import FileServer
from jmcomic_core.message_manager import MessageManager
from jmcomic_core.names import build_pdf_name, sanitize_title


class DownloadService:
    """下载编排服务：装配各子模块，编排完整的下载-发送流程。"""

    def __init__(
        self,
        config: PluginConfig,
        download_dir: Path,
        debug_callback,
    ) -> None:
        self.config = config
        self.download_dir = download_dir
        self._debug = debug_callback

        # 活动下载子进程集合（用于插件重载时统一清理）
        self._active_processes: set[multiprocessing.Process] = set()

        # 装配子模块
        self._downloader = Downloader(
            max_workers=config.max_workers,
            image_format=config.image_format,
            download_timeout=config.download_timeout,
            debug_callback=self._debug,
            active_processes=self._active_processes,
        )
        self._cache = CacheManager(
            download_dir=download_dir,
            max_cache_count=config.max_cache_count,
            debug_callback=self._debug,
        )
        self._fifo = FIFOSemaphore(config.max_concurrent)
        self._msg = MessageManager(auto_recall=config.auto_recall)
        self._sender: FileSender | None = None
        self._file_server: FileServer | None = None

        # 专辑级锁字典（key=album_id）与其引用计数
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_refs: dict[str, int] = {}
        # 限频字典（key=chat_id, value={album_id: last_request_time}）
        self._rate_limits: dict[str, dict[str, float]] = {}
        self._rate_limit_lock = asyncio.Lock()

        # 实际生效的传输模式，由 setup() 解析一次后固定
        self.actual_mode = "local"

    # ================================================================
    #  生命周期
    # ================================================================

    async def setup(self) -> None:
        """初始化：装配缓存、启动文件服务器、解析传输模式。"""
        self._cache.setup()
        self._cache.sync_disk_to_memory()
        self._cleanup_dirty_files()

        # 解析传输模式（只在此处解析一次，避免宿主机容器状态变化导致不一致）
        configured_mode = self.config.transfer_mode
        actual_mode = configured_mode
        if actual_mode == "auto":
            in_container = is_running_in_docker()
            actual_mode = "docker" if in_container else "local"
            logger.info(
                f"自动检测到运行环境: {actual_mode} "
                f"(容器内={in_container}, 依据: {container_detection_reason()})"
            )
        self._debug(f"配置传输模式: {configured_mode} -> 实际模式: {actual_mode}")

        # Docker 模式下启动 HTTP 文件服务器
        if actual_mode == "docker":
            self._file_server = FileServer(
                self.download_dir,
                port=self.config.file_server_port,
                debug=self.config.debug_log,
            )
            try:
                await self._file_server.start()
            except OSError:
                # 端口被占用等原因启动失败：降级为本地模式
                self._file_server = None
                actual_mode = "local"
                logger.error(
                    f"HTTP 文件服务器启动失败，已降级为 local 模式。"
                    f"若 AstrBot 与协议端不在同一文件系统，请释放端口 "
                    f"{self.config.file_server_port} 或改用其他端口后重载插件。"
                )

        base_url = self.config.file_server_base_url
        file_url_base = ""
        if actual_mode == "docker":
            if not base_url:
                host_ip = get_host_ip(self.config.file_server_port)
                base_url = f"http://{host_ip}:{self.config.file_server_port}"
                logger.warning(
                    f"未配置 file_server_base_url，自动推断为: {base_url}\n"
                    f"【重要】如果 NapCat 无法下载文件，请在插件配置中手动设置该地址为宿主机 IP"
                )
            # 文件 URL 带上服务器本次启动生成的随机 token
            file_url_base = f"{base_url}{self._file_server.path_prefix}"
            logger.info(f"文件服务: {base_url}")
        else:
            logger.info("本地模式运行，未启动 HTTP 文件服务器")

        self.actual_mode = actual_mode
        self._sender = FileSender(
            mode=actual_mode,
            file_url_base=file_url_base,
            debug_callback=self._debug,
            upload_retry=self.config.upload_retry,
        )

        self._debug(f"当前配置摘要: {self.config.summary}")
        self._debug(f"缓存映射表: {self._cache.cache_map}")
        logger.info("JMComic 插件初始化完成")
        logger.info(f"下载目录: {self.download_dir}")
        logger.info(f"传输模式: {actual_mode}")
        logger.info(f"PDF 缓存: {self._cache.cached_count()}/{self.config.max_cache_count} 本")

    async def shutdown(self) -> None:
        """卸载：终止所有下载子进程、停止文件服务器。"""
        for p in list(self._active_processes):
            if p.is_alive():
                logger.info(f"正在终止残留下载子进程: PID={p.pid}")
                p.terminate()
                p.join(timeout=3)
                if p.is_alive():
                    logger.warning(f"子进程 {p.pid} 未响应 SIGTERM，强制杀死")
                    p.kill()
                    p.join(timeout=2)
        if self._active_processes:
            logger.info(f"已清理 {len(self._active_processes)} 个下载子进程")
            self._active_processes.clear()

        await self._msg.terminate()

        if self._file_server:
            await self._file_server.stop()
        logger.info("JMComic 插件已卸载")

    # ================================================================
    #  缓存管理（对外命令使用）
    # ================================================================

    def cache_overview(self, limit: int = 15) -> tuple[list[tuple[str, str]], int]:
        """一次扫描返回 ``(缓存摘要列表, 缓存总数)``。"""
        return self._cache.overview(limit=limit)

    def cached_count(self) -> int:
        """当前缓存 PDF 数量。"""
        return self._cache.cached_count()

    async def clear_cache(self) -> int:
        """清空全部缓存，返回删除的文件数。"""
        return await self._cache.clear_all()

    # ================================================================
    #  下载编排
    # ================================================================

    async def process(
        self,
        event: AstrMessageEvent,
        album_id: str | None,
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理一次下载请求（异步生成器，产出需要回复给用户的消息）。"""
        # ---- 参数校验 ----
        if album_id is None:
            yield event.plain_result(
                "❌ 请提供本子 ID\n用法: jmcomic <本子ID>\n例如: jmcomic 422866"
            )
            return

        album_id = str(album_id).strip()
        logger.info(f"[TRACE] process 接收到 album_id={album_id!r}")
        if not album_id.isdigit():
            yield event.plain_result("❌ 本子 ID 必须是数字")
            return

        # ---- 限频检查 ----
        chat_id = getattr(event, "session_id", None) or str(
            getattr(event, "unified_msg_origin", "unknown")
        )
        if self.config.rate_limit_window > 0:
            reject_msg = await self._check_rate_limit(chat_id, album_id)
            if reject_msg is not None:
                yield event.plain_result(reject_msg)
                return

        # ---- 图片数量预检 ----
        if self.config.max_image_count > 0:
            too_many = await self._check_image_count(album_id)
            if too_many is not None:
                # 请求被挡在实际工作之前，退还刚占用的限频名额
                await self._clear_rate_limit(chat_id, album_id)
                yield event.plain_result(too_many)
                return

        self._debug(f"获取本子锁: {album_id} (当前锁数量: {len(self._locks)})")

        async with self._album_lock(album_id):
            tracked_ids: list[str] = []

            # ---- 队列位置提示 ----
            queued_count = self._fifo.queued
            if queued_count > 0:
                queue_msg = (
                    f"⏳ 本子 {album_id} 已加入队列，前面还有 {queued_count} 个任务，请耐心等待..."
                )
                if (
                    await self._msg.send_text(
                        event,
                        queue_msg,
                        track=True,
                        tracked_ids=tracked_ids,
                    )
                    is None
                ):
                    yield event.plain_result(queue_msg)

            async with self._fifo:
                async for result in self._run_pipeline(event, album_id, chat_id, tracked_ids):
                    yield result

        self._gc_state()

    async def _run_pipeline(
        self,
        event: AstrMessageEvent,
        album_id: str,
        chat_id: str,
        tracked_ids: list[str],
    ) -> AsyncGenerator[MessageEventResult, None]:
        """FIFO 槽位获取后的核心流水线。"""
        self._debug(f"FIFO 已获取，队列深度: {self._fifo.queued}")
        album_dir: Path | None = None
        pdf_path: Path | None = None
        start_time = time.time()
        from_cache = False
        skipped_cache = False
        album_title = album_id

        try:
            # ---- 1. 检查缓存 ----
            cached_pdf = await self._cache.check(album_id)
            if cached_pdf:
                pdf_path = cached_pdf
                from_cache = True
                file_size_mb = pdf_path.stat().st_size / 1024 / 1024
                logger.info(f"PDF 缓存命中: {pdf_path.name} ({file_size_mb:.2f} MB)")
                # 尝试获取标题以正确命名（文件可能被改名，必须接住新路径）
                album_title, pdf_path = await self._refresh_cached_title(album_id, pdf_path)
            else:
                # ---- 2. 下载 ----
                if (
                    await self._msg.send_text(
                        event,
                        f"📥 正在下载本子 {album_id}，完成后自动转换为 PDF...",
                        track=True,
                        tracked_ids=tracked_ids,
                    )
                    is None
                ):
                    yield event.plain_result(f"📥 正在下载本子 {album_id}，完成后自动转换为 PDF...")
                album_dir = self.download_dir / album_id
                if album_dir.exists():
                    logger.warning(f"检测到残留目录，正在清理: {album_dir}")
                    shutil.rmtree(album_dir, ignore_errors=True)
                album_dir.mkdir(parents=True, exist_ok=True)

                t0 = time.time()
                raw_title = await self._downloader.download(album_id, album_dir)
                album_title = sanitize_title(raw_title)
                self._debug(f"下载耗时: {time.time() - t0:.1f}s, 标题: {album_title}")

                # ---- 3. 收集图片 ----
                image_files = collect_image_files(album_dir)
                self._debug(f"找到图片: {len(image_files)} 张")
                if self.config.debug_log and image_files:
                    total_size = sum(f.stat().st_size for f in image_files) / 1024 / 1024
                    self._debug(
                        f"图片总大小: {total_size:.2f} MB, "
                        f"首张: {image_files[0].name}, 末张: {image_files[-1].name}"
                    )
                if not image_files:
                    yield event.plain_result("❌ 下载完成后未找到图片文件")
                    return

                # 精确兜底：预检依赖网络、可能失败或被跳过，而转 PDF 会把所有图片
                # 字节留在内存里，超大本子正是 OOM 的来源。这里用实际图片数再挡一次。
                if 0 < self.config.max_image_count < len(image_files):
                    logger.warning(
                        f"本子 {album_id} 实际 {len(image_files)} 张图片，"
                        f"超过上限 {self.config.max_image_count}，放弃转换"
                    )
                    yield event.plain_result(
                        f"🚫 本子 {album_id} 实际共 {len(image_files)} 页，"
                        f"超过上限 {self.config.max_image_count} 页，已取消"
                    )
                    return

                # ---- 4. 转 PDF ----
                t0 = time.time()
                logger.info(f"[TRACE] 转PDF: album_id={album_id!r}")
                pdf_path = await convert_to_pdf(
                    image_files,
                    self.download_dir / f"{album_id}.pdf",
                    self.config.pdf_resolution,
                )
                self._debug(f"PDF 转换耗时: {time.time() - t0:.1f}s")
                if not pdf_path or not pdf_path.exists():
                    yield event.plain_result("❌ PDF 转换失败")
                    return
                file_size_mb = pdf_path.stat().st_size / 1024 / 1024
                logger.info(f"PDF 生成成功: {pdf_path.name} ({file_size_mb:.2f} MB)")

            # ---- 5. 统一命名（JM{id}-{title}.pdf）----
            expected_name = build_pdf_name(album_id, album_title)
            final_pdf_path = self.download_dir / expected_name
            if pdf_path.resolve() != final_pdf_path.resolve():
                try:
                    if final_pdf_path.exists():
                        final_pdf_path.unlink()
                    pdf_path.rename(final_pdf_path)
                    pdf_path = final_pdf_path
                except OSError as e:
                    logger.warning(f"PDF 最终重命名失败: {e}，使用当前文件名")
                    expected_name = pdf_path.name

            # 更新缓存映射（超大文件跳过缓存）
            pdf_size_mb = pdf_path.stat().st_size / 1024 / 1024
            if self.config.max_cache_size_mb > 0 and pdf_size_mb > self.config.max_cache_size_mb:
                logger.info(
                    f"PDF 过大 ({pdf_size_mb:.1f} MB > {self.config.max_cache_size_mb} MB)，"
                    f"跳过缓存: {expected_name}"
                )
                skipped_cache = True
            else:
                async with self._cache.cache_lock:
                    self._cache.cache_map[album_id] = expected_name
                    await self._cache.save_index()

            # ---- 6. ZIP 压缩（可选）----
            final_file_path = pdf_path
            final_file_name = expected_name
            if self.config.enable_zip:
                t0 = time.time()
                zip_path = await compress_to_zip(
                    pdf_path,
                    self.download_dir / f"JM{album_id}-{album_title}.zip",
                    self.config.zip_password,
                )
                self._debug(f"ZIP 压缩耗时: {time.time() - t0:.1f}s")
                if zip_path:
                    final_file_path = zip_path
                    final_file_name = f"JM{album_id}-{album_title}.zip"
                    self._debug(f"ZIP 文件大小: {zip_path.stat().st_size / 1024 / 1024:.2f} MB")
                else:
                    logger.warning(f"ZIP 压缩失败，回退发送原始 PDF: {pdf_path.name}")

            # ---- 7. 发送文件 ----
            self._debug(f"准备发送文件: {final_file_name} ({final_file_path})")
            await self._sender.send(event, final_file_path, final_file_name, album_id)
            self._debug("文件发送完成")

            # 文件发送成功后撤回所有状态消息
            await self._msg.recall_all(event, tracked_ids)

            # 清理 ZIP 临时文件
            if self.config.enable_zip and final_file_path.suffix.lower() == ".zip":
                try:
                    final_file_path.unlink()
                    logger.info(f"已清理临时 ZIP: {final_file_path.name}")
                except OSError as e:
                    logger.warning(f"清理 ZIP 失败: {e}")

            if self.config.enable_zip and self.config.zip_password:
                yield event.plain_result(f"🔐 压缩包密码: {self.config.zip_password}")

            elapsed = time.time() - start_time
            logger.info(f"本子 {album_id} 总耗时: {elapsed:.1f}s (缓存: {from_cache})")
            self._debug(
                f"完成明细: 缓存={from_cache}, 文件={final_file_name}, 锁数量={len(self._locks)}"
            )

            # 记录限频时间戳（更新为完成时间，重置窗口起点）
            if self.config.rate_limit_window > 0:
                self._rate_limits.setdefault(chat_id, {})[album_id] = time.time()

        except asyncio.TimeoutError:
            logger.error(f"下载本子 {album_id} 超时（子进程已终止）")
            yield event.plain_result(
                f"❌ 下载超时（{self.config.download_timeout}s），请稍后重试。"
            )
        except Exception as e:
            logger.error(f"下载本子 {album_id} 时出错: {e}", exc_info=True)
            yield event.plain_result(f"❌ 下载失败: {e}")
        finally:
            # 清理临时图片目录
            if self.config.auto_cleanup and album_dir and album_dir.exists():
                try:
                    shutil.rmtree(album_dir, ignore_errors=True)
                    logger.info(f"已清理临时目录: {album_dir}")
                except Exception as e:
                    logger.warning(f"清理目录失败: {e}")

            # LRU 缓存淘汰
            if pdf_path:
                await self._cache.cleanup(keep_path=pdf_path)

            # 超大文件：发送后删除，不占磁盘
            if skipped_cache and pdf_path and pdf_path.exists():
                try:
                    pdf_path.unlink()
                    logger.info(f"已删除超大缓存文件: {pdf_path.name}")
                except OSError as e:
                    logger.warning(f"删除超大缓存文件失败: {e}")

    # ================================================================
    #  辅助方法
    # ================================================================

    @contextlib.asynccontextmanager
    async def _album_lock(self, album_id: str) -> AsyncGenerator[asyncio.Lock, None]:
        """按本子串行化处理，并在最后一个使用者离开后回收锁对象。

        用引用计数而不是 ``lock.locked()`` 判断能否回收：``asyncio.Lock`` 在
        ``release()`` 与下一个等待者恢复之间会短暂处于「未锁定」状态，此刻若把锁
        从字典里删掉，后到的请求就会新建一把锁，同一本子于是被两个协程并发处理——
        两者都往 ``download_dir/{album_id}`` 写图片，后者的 rmtree 会清掉前者的下载。
        """
        lock = self._locks.setdefault(album_id, asyncio.Lock())
        self._lock_refs[album_id] = self._lock_refs.get(album_id, 0) + 1
        try:
            async with lock:
                yield lock
        finally:
            remaining = self._lock_refs.get(album_id, 1) - 1
            if remaining <= 0:
                self._lock_refs.pop(album_id, None)
                self._locks.pop(album_id, None)
            else:
                self._lock_refs[album_id] = remaining

    async def _check_rate_limit(self, chat_id: str, album_id: str) -> str | None:
        """限频检查：命中则返回拒绝消息，否则记录时间戳并返回 None。"""
        now = time.time()
        async with self._rate_limit_lock:
            chat_limits = self._rate_limits.get(chat_id, {})
            last_time = chat_limits.get(album_id, 0)
            elapsed_since = now - last_time
            if elapsed_since < self.config.rate_limit_window:
                remaining = int(self.config.rate_limit_window - elapsed_since)
                return (
                    f"⏰ 本子 {album_id} 在 {int(elapsed_since)} 秒前刚被获取过，"
                    f"请 {remaining} 秒后再试"
                )
            # 立即记录时间戳，防止排队期间被重复请求
            self._rate_limits.setdefault(chat_id, {})[album_id] = now
        return None

    async def _clear_rate_limit(self, chat_id: str, album_id: str) -> None:
        """撤销限频记录，用于请求在真正开工前就被拒绝的场景。"""
        async with self._rate_limit_lock:
            albums = self._rate_limits.get(chat_id)
            if not albums:
                return
            albums.pop(album_id, None)
            if not albums:
                self._rate_limits.pop(chat_id, None)

    @staticmethod
    def _fetch_album_detail(album_id: str):
        """同步获取本子详情。

        jmcomic 用的是阻塞式 HTTP 客户端（curl_cffi），必须放到线程里跑，否则一次
        查询就会把整个 AstrBot 事件循环卡住（网络差时可达数十秒，期间 bot 完全无响应）。
        """
        import jmcomic

        client = jmcomic.JmOption.default().new_jm_client()
        return client.get_album_detail(album_id)

    @staticmethod
    def _count_album_images(album_id: str, limit: int) -> int:
        """统计本子的总图片数，累计一旦超过 ``limit`` 就提前返回。

        ``len(JmAlbumDetail)`` 是**章节数**而不是图片数，早先直接拿它跟图片上限比，
        这道资源保护基本等于没开——多章本子的章节数通常只有个位数，永远撞不到 500。
        ``album.page_count`` 同样不能用：默认的 api 客户端把它硬编码成 0。
        真正的图片数只存在于章节详情里（且从 album 迭代出来的章节是浅对象，
        ``page_arr`` 为 None，必须重新拉取），所以逐章累加并及早停手——
        绝大多数本子只有一章，代价就是多一次请求。

        同样是阻塞调用，需在线程中执行。
        """
        import jmcomic

        client = jmcomic.JmOption.default().new_jm_client()
        album = client.get_album_detail(album_id)
        if not album:
            return 0

        # 每章至少一张图：章节数已经超限，无需再逐章查
        chapter_count = len(album)
        if chapter_count > limit:
            return chapter_count

        total = 0
        for photo in album:
            total += len(client.get_photo_detail(photo.photo_id))
            if total > limit:
                break
        return total

    async def _check_image_count(self, album_id: str) -> str | None:
        """图片数量预检：超过上限返回拒绝消息，失败/正常返回 None。"""
        limit = self.config.max_image_count
        try:
            page_count = await asyncio.to_thread(self._count_album_images, album_id, limit)
        except Exception as e:
            # 预检失败不阻塞下载：转 PDF 前还有一道基于实际图片数的精确检查兜底
            logger.warning(f"查询本子 {album_id} 页数失败: {e}，跳过图片数量预检")
            return None

        if page_count > limit:
            return f"🚫 本子 {album_id} 共 {page_count} 页，超过上限 {limit} 页，拒绝下载"
        return None

    async def _refresh_cached_title(self, album_id: str, pdf_path: Path) -> tuple[str, Path]:
        """缓存命中时刷新标题，并把缓存文件同步改成规范文件名。

        Returns:
            ``(标题, 实际路径)``。**调用方必须使用返回的路径**——文件可能已被改名，
            继续用旧路径会导致后续 rename 删掉刚改好的缓存文件。

        网络查询失败时不再退化成 ``JM{id}-{id}.pdf`` 这种无意义名字，
        而是从现有文件名反推标题、原地保留。
        """
        fallback_title = self._title_from_filename(album_id, pdf_path)
        try:
            album = await asyncio.to_thread(self._fetch_album_detail, album_id)
        except Exception as e:
            logger.warning(f"获取缓存本子标题失败，沿用现有文件名: {e}")
            return fallback_title, pdf_path

        if not album:
            return fallback_title, pdf_path

        album_title = sanitize_title(album.title)
        expected_name = build_pdf_name(album_id, album_title)
        if pdf_path.name == expected_name:
            return album_title, pdf_path

        new_pdf_path = pdf_path.parent / expected_name
        try:
            if new_pdf_path.exists():
                new_pdf_path.unlink()
            pdf_path.rename(new_pdf_path)
        except OSError as e:
            logger.warning(f"缓存文件重命名失败: {e}，保留原文件名")
            return fallback_title, pdf_path

        logger.info(f"缓存文件已重命名为: {new_pdf_path.name}")
        return album_title, new_pdf_path

    @staticmethod
    def _title_from_filename(album_id: str, pdf_path: Path) -> str:
        """从 ``JM{id}-{title}.pdf`` 反推标题，取不到则退回 album_id。"""
        prefix = f"JM{album_id}-"
        stem = pdf_path.stem
        if stem.startswith(prefix) and len(stem) > len(prefix):
            return stem[len(prefix) :]
        return album_id

    def _gc_state(self) -> None:
        """清理 ``_rate_limits`` 字典，防止无界增长。

        ``_locks`` 由 :meth:`_album_lock` 的引用计数精确回收，无需在此兜底。
        """
        if len(self._rate_limits) > 100:
            cutoff = time.time() - max(self.config.rate_limit_window * 2, 3600)
            stale_chats = [
                cid
                for cid, albums in self._rate_limits.items()
                if not albums or all(t < cutoff for t in albums.values())
            ]
            for cid in stale_chats:
                self._rate_limits.pop(cid, None)
            if stale_chats:
                self._debug(
                    f"限频字典清理: 移除 {len(stale_chats)} 个过期条目, "
                    f"剩余 {len(self._rate_limits)}"
                )

    def _cleanup_dirty_files(self) -> None:
        """清理上次异常重载遗留的脏文件。

        处理三类脏文件：
        1. 带编号的下载临时目录（如 448016/），内有不完整图片
        2. 中间 PDF 文件（{album_id}.pdf），已重命名为 JM 前缀后即为废稿
        3. JM 前缀 PDF 未在 cache_map 中注册的孤本
        """
        if not self.download_dir or not self.download_dir.exists():
            return

        cache_ids = set(self._cache.cache_map.keys())
        cleaned_dirs = 0
        cleaned_pdfs = 0

        for entry in self.download_dir.iterdir():
            # 1. 清理数字命名的临时下载目录
            if entry.is_dir() and entry.name.isdigit():
                try:
                    shutil.rmtree(entry, ignore_errors=True)
                    cleaned_dirs += 1
                    logger.info(f"已清理残留下载目录: {entry.name}")
                except Exception as e:
                    logger.warning(f"清理残留目录失败: {entry.name}: {e}")

            # 2. 清理中间 PDF（{album_id}.pdf 且不在 cache_map 中）
            elif (
                entry.is_file()
                and entry.suffix.lower() == ".pdf"
                and entry.stem.isdigit()
                and entry.stem not in cache_ids
            ):
                try:
                    entry.unlink()
                    cleaned_pdfs += 1
                    logger.info(f"已清理中间 PDF: {entry.name}")
                except OSError as e:
                    logger.warning(f"清理中间 PDF 失败: {entry.name}: {e}")

            # 3. 清理 JM 前缀但在 cache_map 中无记录的孤本 PDF
            elif (
                entry.is_file()
                and entry.suffix.lower() == ".pdf"
                and entry.stem.startswith("JM")
                and "-" in entry.stem
            ):
                album_id = entry.stem[2:].split("-", 1)[0]
                if album_id.isdigit() and album_id not in cache_ids:
                    try:
                        entry.unlink()
                        cleaned_pdfs += 1
                        logger.info(f"已清理孤本 PDF: {entry.name}")
                    except OSError as e:
                        logger.warning(f"清理孤本 PDF 失败: {entry.name}: {e}")

        if cleaned_dirs or cleaned_pdfs:
            logger.info(f"脏文件清理完成: {cleaned_dirs} 个目录, {cleaned_pdfs} 个 PDF")
