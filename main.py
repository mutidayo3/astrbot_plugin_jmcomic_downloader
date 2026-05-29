"""JMComic 本子下载器 — AstrBot 插件主入口（编排层）。

将下载、转换、压缩、发送、缓存等子任务委派给独立模块：
- _fifo_semaphore: FIFO 有序信号量
- _utils: Docker 检测 / IP 获取 / 图片收集
- _file_server: HTTP 文件服务器
- _cache_manager: PDF 缓存管理
- _downloader: 子进程隔离下载
- _converter: 图片→PDF / PDF→ZIP
- _file_sender: 文件发送（local / OneBot）
"""

import asyncio
import multiprocessing
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Set

# 确保插件目录在 sys.path 中，支持绝对导入同目录模块
_plugin_dir = str(Path(__file__).resolve().parent)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from astrbot.api.event import filter as astr_filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

from _fifo_semaphore import _FIFOSemaphore
from _utils import is_running_in_docker, get_host_ip, collect_image_files
from _file_server import FileServer
from _cache_manager import CacheManager
from _downloader import Downloader
from _converter import convert_to_pdf, compress_to_zip
from _file_sender import FileSender
from _message_manager import MessageManager

# ---- 依赖检测 ----
DEPENDENCIES_MET = True
try:
    import jmcomic   # noqa: F401
    import img2pdf   # noqa: F401
    import pyzipper  # noqa: F401
    from PIL import Image  # noqa: F401
except ImportError as e:
    logger.error(f"缺少依赖库: {e}")
    logger.error("请运行: pip install jmcomic Pillow img2pdf pyzipper aiohttp")
    DEPENDENCIES_MET = False


@register("jmcomic_downloader", "mutidayo3", "JMComic 本子下载器", "0.0.28")
class JMComicPlugin(Star):
    """JMComic 本子下载器插件 — 编排层。

    负责配置加载、模块装配、请求编排和生命周期管理。
    """

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.download_dir: Optional[Path] = None
        self.auto_cleanup = self.config.get('auto_cleanup', True)
        self.debug_log = self.config.get('debug_log', False)
        if self.debug_log:
            logger.info("JMComic 插件调试日志已启用")

        # 传输配置
        self.transfer_mode = self.config.get('transfer_mode', 'auto')
        self.file_server_port = self.config.get('file_server_port', 18790)
        self.file_server_base_url = self.config.get('file_server_base_url', "").rstrip("/")
        self.file_server: Optional[FileServer] = None

        # 压缩配置
        self.enable_zip = self.config.get('enable_zip', False)
        self.zip_password = self.config.get('zip_password', "")

        # 解析配置参数
        max_workers = max(1, min(self.config.get('max_workers', 4), 8))
        image_format = self.config.get('image_format', 'webp')
        download_timeout = self.config.get('download_timeout', 300)
        max_cache_count = max(0, self.config.get('max_cache_count', 20))
        pdf_resolution = self.config.get('pdf_resolution', 150.0)
        self.max_pdf_size_mb = self.config.get('max_pdf_size_mb', 100)
        self.max_cache_size_mb = self.config.get('max_cache_size_mb', 200)
        max_concurrent = max(1, self.config.get('max_concurrent', 1))
        self.rate_limit_window = max(0, self.config.get('rate_limit_window', 300))
        self.max_image_count = max(0, self.config.get('max_image_count', 500))
        self.auto_recall = self.config.get('auto_recall', True)

        # 消息管理器（发送 + 撤回）
        self._msg = MessageManager(auto_recall=self.auto_recall)

        # 活动下载子进程集合（用于插件重载时统一清理）
        self._active_processes: Set[multiprocessing.Process] = set()

        # 装配子模块
        self._downloader = Downloader(
            max_workers=max_workers,
            image_format=image_format,
            download_timeout=download_timeout,
            debug_callback=self._debug,
            active_processes=self._active_processes,
        )
        self._cache = CacheManager(
            max_cache_count=max_cache_count,
            download_dir=None,  # 延迟设置：initialize() 中确定
            debug_callback=self._debug,
        )
        self._fifo = _FIFOSemaphore(max_concurrent)
        self._sender: Optional[FileSender] = None

        # PDF 转换参数
        self._pdf_resolution = pdf_resolution

        # 专辑级锁字典（key=album_id, value=asyncio.Lock）
        self._locks: Dict[str, asyncio.Lock] = {}

        # 限频字典（key=chat_id, value={album_id: last_request_time}）
        self._rate_limits: Dict[str, Dict[str, float]] = {}
        # 限频检查锁，防止竞态导致重复请求漏过
        self._rate_limit_lock = asyncio.Lock()

        # 保存供日志输出的摘要
        self._config_summary = (
            f"workers={max_workers}, format={image_format}, timeout={download_timeout}s, "
            f"cleanup={self.auto_cleanup}, dpi={pdf_resolution}, "
            f"max_pdf_mb={self.max_pdf_size_mb}, cache_max={max_cache_count}, "
            f"cache_size_mb={self.max_cache_size_mb}, zip={self.enable_zip}, "
            f"fifo={max_concurrent}, img_max={self.max_image_count}, debug={self.debug_log}"
        )

    def _debug(self, msg: str):
        """输出调试日志（仅在 debug_log 启用时生效）"""
        if self.debug_log:
            logger.debug(f"[JMComic] {msg}")

    # ================================================================
    #  生命周期
    # ================================================================

    async def initialize(self):
        """插件初始化：创建目录、装配缓存、启动文件服务器。"""
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        data_path = get_astrbot_data_path()
        self.download_dir = Path(data_path) / "plugin_data" / self.name / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # 完成缓存管理器装配
        self._cache.download_dir = self.download_dir
        self._cache.setup()

        # 同步磁盘文件到内存映射
        self._cache.sync_disk_to_memory()

        # 清理上次异常重载遗留的脏文件（下载临时目录 + 中间 PDF）
        self._cleanup_dirty_files()

        # 解析传输模式
        actual_mode = self.transfer_mode
        if actual_mode == "auto":
            actual_mode = "docker" if is_running_in_docker() else "local"
            logger.info(f"自动检测到运行环境: {actual_mode}")
        self._debug(f"配置传输模式: {self.transfer_mode} -> 实际模式: {actual_mode}")

        # Docker 模式下启动 HTTP 文件服务器
        if actual_mode == "docker":
            self.file_server = FileServer(
                self.download_dir,
                port=self.file_server_port,
                debug=self.debug_log,
            )
            await self.file_server.start()

            if not self.file_server_base_url:
                host_ip = get_host_ip(self.file_server_port)
                self.file_server_base_url = f"http://{host_ip}:{self.file_server_port}"
                logger.warning(
                    f"未配置 file_server_base_url，自动推断为: {self.file_server_base_url}\n"
                    f"【重要】如果 NapCat 无法下载文件，请在插件配置中手动设置该地址为宿主机 IP"
                )
            logger.info(f"文件服务: {self.file_server_base_url}")
        else:
            logger.info("本地模式运行，未启动 HTTP 文件服务器")

        # 装配文件发送器
        self._sender = FileSender(
            transfer_mode=self.transfer_mode,
            file_server_base_url=self.file_server_base_url,
            debug_callback=self._debug,
            file_server=self.file_server,
            is_docker_checker=is_running_in_docker,
        )

        cached_count = len(self._cache._list_cached())
        self._debug(f"当前配置摘要: {self._config_summary}")
        self._debug(f"缓存映射表: {self._cache.cache_map}")

        logger.info("JMComic 插件初始化完成")
        logger.info(f"下载目录: {self.download_dir}")
        logger.info(f"传输模式: {actual_mode}")
        logger.info(f"PDF 缓存: {cached_count}/{self._cache.max_cache_count} 本")

    async def terminate(self):
        """插件卸载：终止所有下载子进程、停止文件服务器。"""
        # 终止所有活动下载子进程，防止重载后残留进程继续下载
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

        # 取消所有未完成的撤回任务
        await self._msg.terminate()

        if self.file_server:
            await self.file_server.stop()
        logger.info("JMComic 插件已卸载")

    # ================================================================
    #  专辑锁管理
    # ================================================================

    def _get_album_lock(self, album_id: str) -> asyncio.Lock:
        """获取指定本子的下载锁，保证同一本子不重复处理。"""
        return self._locks.setdefault(album_id, asyncio.Lock())

    # ================================================================
    #  命令处理
    # ================================================================

    @astr_filter.command("jmcomic")
    async def download_jmcomic(self, event: AstrMessageEvent, album_id: str = None):
        """下载 JMComic 本子并转换为 PDF 发送。

        用法: /jmcomic <本子ID>
        """
        if not DEPENDENCIES_MET:
            yield event.plain_result(
                "❌ 插件依赖缺失！\n请在 AstrBot 终端或容器内执行:\n"
                "pip install jmcomic Pillow img2pdf pyzipper aiohttp"
            )
            return

        if album_id is None:
            yield event.plain_result(
                "❌ 请提供本子 ID\n用法: jmcomic <本子ID>\n例如: jmcomic 422866"
            )
            return

        album_id = str(album_id).strip()
        logger.info(f"[TRACE] download_jmcomic 接收到 album_id={album_id!r}")
        if not album_id.isdigit():
            yield event.plain_result("❌ 本子 ID 必须是数字")
            return

        # ---- 限频检查：同一聊天中同一本子在窗口期内只允许获取一次 ----
        now = time.time()
        chat_id = getattr(event, 'session_id', None) or str(getattr(event, 'unified_msg_origin', 'unknown'))
        if self.rate_limit_window > 0:
            should_reject = False
            reject_msg = ""
            async with self._rate_limit_lock:
                chat_limits = self._rate_limits.get(chat_id, {})
                last_time = chat_limits.get(album_id, 0)
                elapsed_since = now - last_time
                if elapsed_since < self.rate_limit_window:
                    remaining = int(self.rate_limit_window - elapsed_since)
                    should_reject = True
                    reject_msg = (
                        f"⏰ 本子 {album_id} 在 {int(elapsed_since)} 秒前刚被获取过，"
                        f"请 {remaining} 秒后再试"
                    )
                else:
                    # 立即记录时间戳，防止排队期间被重复请求
                    self._rate_limits.setdefault(chat_id, {})[album_id] = now
            if should_reject:
                if await self._msg.send_text(event, reject_msg, 30) is None:
                    yield event.plain_result(reject_msg)
                return

        # ---- 图片数量检查：防止超大本子耗尽资源 ----
        if self.max_image_count > 0:
            if await self._msg.send_text(event, f"🔍 正在查询本子 {album_id} 信息...", 30) is None:
                yield event.plain_result(f"🔍 正在查询本子 {album_id} 信息...")
            try:
                client = jmcomic.JmOption.default().new_jm_client()
                album = client.get_album_detail(album_id)
                if album:
                    page_count = len(album) if hasattr(album, '__len__') else 0
                    if page_count > self.max_image_count:
                        if await self._msg.send_text(event, f"🚫 本子 {album_id} 共 {page_count} 页，超过上限 {self.max_image_count} 页，拒绝下载", 30) is None:
                            yield event.plain_result(
                                f"🚫 本子 {album_id} 共 {page_count} 页，超过上限 {self.max_image_count} 页，拒绝下载"
                            )
                        return
            except Exception as e:
                logger.warning(f"查询本子 {album_id} 页数失败: {e}，跳过图片数量检查")

        lock = self._get_album_lock(album_id)
        self._debug(f"获取本子锁: {album_id} (当前锁数量: {len(self._locks)})")

        async with lock:
            # ---- 队列位置提示 ----
            queued_count = self._fifo.queued
            if queued_count > 0:
                if await self._msg.send_text(event, f"⏳ 本子 {album_id} 的请求已加入处理队列，前面还有 {queued_count} 个任务，请耐心等待...", 30) is None:
                    yield event.plain_result(
                        f"⏳ 本子 {album_id} 的请求已加入处理队列，"
                        f"前面还有 {queued_count} 个任务，请耐心等待..."
                    )
            else:
                if await self._msg.send_text(event, f"✅ 已接收本子 {album_id} 的请求，正在处理...", 30) is None:
                    yield event.plain_result(
                        f"✅ 已接收本子 {album_id} 的请求，正在处理..."
                    )

            async with self._fifo:
                self._debug(f"FIFO 已获取，队列深度: {self._fifo.queued}")
                album_dir: Optional[Path] = None
                pdf_path: Optional[Path] = None
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
                        if await self._msg.send_text(event, "📦 命中本地缓存，正在获取本子信息...", 30) is None:
                            yield event.plain_result("📦 命中本地缓存，正在获取本子信息...")

                        # 尝试获取标题以正确命名
                        try:
                            client = jmcomic.JmOption.default().new_jm_client()
                            album = client.get_album_detail(album_id)
                            if album:
                                album_title = self._sanitize_title(album.title)
                                expected_name = f"JM{album_id}-{album_title}.pdf"
                                if pdf_path.name != expected_name:
                                    new_pdf_path = pdf_path.parent / expected_name
                                    try:
                                        if new_pdf_path.exists():
                                            new_pdf_path.unlink()
                                        pdf_path.rename(new_pdf_path)
                                        pdf_path = new_pdf_path
                                        logger.info(f"缓存文件已重命名为: {pdf_path.name}")
                                    except OSError as e:
                                        logger.warning(f"缓存文件重命名失败: {e}，保留原文件名")
                        except Exception as e:
                            logger.warning(f"获取缓存本子标题失败，将使用 ID 作为文件名: {e}")
                    else:
                        # ---- 2. 下载 ----
                        if await self._msg.send_text(event, f"📥 开始下载本子 {album_id}...", 30) is None:
                            yield event.plain_result(f"📥 开始下载本子 {album_id}...")
                        album_dir = self.download_dir / album_id

                        if album_dir.exists():
                            logger.warning(f"检测到残留目录，正在清理: {album_dir}")
                            shutil.rmtree(album_dir, ignore_errors=True)
                        album_dir.mkdir(parents=True, exist_ok=True)

                        t0 = time.time()
                        album_title = await self._downloader.download(album_id, album_dir)
                        self._debug(f"下载耗时: {time.time() - t0:.1f}s, 标题: {album_title}")

                        # ---- 3. 收集图片 ----
                        image_files = collect_image_files(album_dir)
                        self._debug(f"找到图片: {len(image_files)} 张")
                        if self.debug_log and image_files:
                            total_size = sum(f.stat().st_size for f in image_files) / 1024 / 1024
                            self._debug(
                                f"图片总大小: {total_size:.2f} MB, "
                                f"首张: {image_files[0].name}, 末张: {image_files[-1].name}"
                            )
                        if not image_files:
                            yield event.plain_result("❌ 下载完成后未找到图片文件")
                            return

                        if await self._msg.send_text(event, f"✅ 下载完成 ({len(image_files)} 页)，正在转换为 PDF...", 30) is None:
                            yield event.plain_result(
                                f"✅ 下载完成 ({len(image_files)} 页)，正在转换为 PDF..."
                            )

                        # ---- 4. 转 PDF ----
                        t0 = time.time()
                        logger.info(f"[TRACE] 转PDF: album_id={album_id!r}")
                        pdf_path = await convert_to_pdf(
                            image_files,
                            self.download_dir / f"{album_id}.pdf",
                            self._pdf_resolution,
                        )
                        self._debug(f"PDF 转换耗时: {time.time() - t0:.1f}s")

                        if not pdf_path or not pdf_path.exists():
                            yield event.plain_result("❌ PDF 转换失败")
                            return

                        file_size_mb = pdf_path.stat().st_size / 1024 / 1024
                        logger.info(f"PDF 生成成功: {pdf_path.name} ({file_size_mb:.2f} MB)")

                        if file_size_mb > self.max_pdf_size_mb:
                            if await self._msg.send_text(event, f"⚠️ PDF 文件过大 ({file_size_mb:.1f} MB)，可能发送较慢或失败", 30) is None:
                                yield event.plain_result(
                                    f"⚠️ PDF 文件过大 ({file_size_mb:.1f} MB)，可能发送较慢或失败"
                                )

                    # ---- 5. 统一命名（JM{id}-{title}.pdf） ----
                    expected_name = f"JM{album_id}-{album_title}.pdf"
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
                    if self.max_cache_size_mb > 0 and pdf_size_mb > self.max_cache_size_mb:
                        logger.info(
                            f"PDF 过大 ({pdf_size_mb:.1f} MB > {self.max_cache_size_mb} MB)，"
                            f"跳过缓存: {expected_name}"
                        )
                        skipped_cache = True
                    else:
                        async with self._cache.cache_lock:
                            self._cache.cache_map[album_id] = expected_name
                            await self._cache.save_index()

                    # ---- 6. ZIP 压缩（可选） ----
                    final_file_path = pdf_path
                    final_file_name = expected_name

                    if self.enable_zip:
                        t0 = time.time()
                        zip_path = await compress_to_zip(
                            pdf_path,
                            self.download_dir / f"JM{album_id}-{album_title}.zip",
                            self.zip_password,
                        )
                        self._debug(f"ZIP 压缩耗时: {time.time() - t0:.1f}s")
                        if zip_path:
                            final_file_path = zip_path
                            final_file_name = f"JM{album_id}-{album_title}.zip"
                            self._debug(
                                f"ZIP 文件大小: {zip_path.stat().st_size / 1024 / 1024:.2f} MB"
                            )
                        else:
                            logger.warning(f"ZIP 压缩失败，回退发送原始 PDF: {pdf_path.name}")

                    # ---- 7. 发送文件 ----
                    if from_cache:
                        if await self._msg.send_text(event, f"📚 正在发送缓存的本子 {album_id}...", 30) is None:
                            yield event.plain_result(f"📚 正在发送缓存的本子 {album_id}...")
                    else:
                        if await self._msg.send_text(event, f"📚 本子 {album_id} 处理完成，正在发送文件...", 30) is None:
                            yield event.plain_result(f"📚 本子 {album_id} 处理完成，正在发送文件...")

                    self._debug(f"准备发送文件: {final_file_name} ({final_file_path})")
                    await self._sender.send(event, final_file_path, final_file_name, album_id)
                    self._debug("文件发送完成")

                    # 清理 ZIP 临时文件
                    if self.enable_zip and final_file_path.suffix.lower() == '.zip':
                        try:
                            final_file_path.unlink()
                            logger.info(f"已清理临时 ZIP: {final_file_path.name}")
                        except OSError as e:
                            logger.warning(f"清理 ZIP 失败: {e}")

                    if self.enable_zip and self.zip_password:
                        yield event.plain_result(f"🔐 压缩包密码: {self.zip_password}")

                    elapsed = time.time() - start_time
                    logger.info(f"本子 {album_id} 总耗时: {elapsed:.1f}s (缓存: {from_cache})")
                    self._debug(
                        f"完成明细: 缓存={from_cache}, 文件={final_file_name}, "
                        f"锁数量={len(self._locks)}"
                    )

                    # ---- 记录限频时间戳（更新为完成时间，重置窗口起点） ----
                    if self.rate_limit_window > 0:
                        self._rate_limits.setdefault(chat_id, {})[album_id] = time.time()

                except asyncio.TimeoutError:
                    logger.error(f"下载本子 {album_id} 超时（子进程已终止）")
                    yield event.plain_result(
                        f"❌ 下载超时（{self._downloader.download_timeout}s），请稍后重试。"
                    )
                except Exception as e:
                    logger.error(f"下载本子 {album_id} 时出错: {e}", exc_info=True)
                    yield event.plain_result(f"❌ 下载失败: {str(e)}")
                finally:
                    # 清理临时图片目录
                    if self.auto_cleanup and album_dir and album_dir.exists():
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

        # 释放不再使用的锁对象
        if not lock.locked():
            self._locks.pop(album_id, None)
        # 定期清理 _locks 字典防止无界增长：超过 200 个锁时强制 GC
        if len(self._locks) > 200:
            stale = [aid for aid, lk in self._locks.items() if not lk.locked()]
            for aid in stale:
                self._locks.pop(aid, None)
            self._debug(f"锁字典清理: 移除 {len(stale)} 个过期锁, 剩余 {len(self._locks)}")
        # 定期清理限频字典：清除超过窗口期 2 倍的过期条目
        if len(self._rate_limits) > 100:
            cutoff = time.time() - max(self.rate_limit_window * 2, 3600)
            stale_chats = [
                cid for cid, albums in self._rate_limits.items()
                if not albums or all(t < cutoff for t in albums.values())
            ]
            for cid in stale_chats:
                self._rate_limits.pop(cid, None)
            if stale_chats:
                self._debug(f"限频字典清理: 移除 {len(stale_chats)} 个过期条目, 剩余 {len(self._rate_limits)}")

    # ================================================================
    #  工具方法
    # ================================================================

    def _cleanup_dirty_files(self):
        """清理上次异常重载遗留的脏文件。

        处理两类脏文件：
        1. 带编号的下载临时目录（如 448016/），内有不完整图片
        2. 中间 PDF 文件（{album_id}.pdf），已重命名为 JM 前缀后该文件即为废稿
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
            elif entry.is_file() and entry.suffix.lower() == '.pdf' and entry.stem.isdigit():
                if entry.stem not in cache_ids:
                    try:
                        entry.unlink()
                        cleaned_pdfs += 1
                        logger.info(f"已清理中间 PDF: {entry.name}")
                    except OSError as e:
                        logger.warning(f"清理中间 PDF 失败: {entry.name}: {e}")

            # 3. 清理 JM 前缀但在 cache_map 中无记录的孤本 PDF
            elif (
                entry.is_file()
                and entry.suffix.lower() == '.pdf'
                and entry.stem.startswith('JM')
                and '-' in entry.stem
            ):
                album_id = entry.stem[2:].split('-', 1)[0]
                if album_id.isdigit() and album_id not in cache_ids:
                    try:
                        entry.unlink()
                        cleaned_pdfs += 1
                        logger.info(f"已清理孤本 PDF: {entry.name}")
                    except OSError as e:
                        logger.warning(f"清理孤本 PDF 失败: {entry.name}: {e}")

        if cleaned_dirs or cleaned_pdfs:
            logger.info(
                f"脏文件清理完成: {cleaned_dirs} 个目录, {cleaned_pdfs} 个 PDF"
            )

    @staticmethod
    def _sanitize_title(title: str) -> str:
        """清理本子标题中的非法文件名字符。

        移除：
        - Windows 非法字符: <>:"/\\|?*
        - 控制字符 (0x00-0x1F, 0x7F)
        - 首尾空格和点号
        - 连续空格合并为单个空格
        """
        # 移除 Windows 非法文件名字符
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            title = title.replace(char, '')
        # 移除控制字符（保留换行/制表符用于后续替换为空格）
        title = ''.join(
            ' ' if ord(c) in (0x09, 0x0A, 0x0D) else
            '' if ord(c) < 0x20 or ord(c) == 0x7F else c
            for c in title
        )
        # 合并连续空格
        title = re.sub(r' {2,}', ' ', title)
        # 移除首尾空格和点号（Windows 不允许文件名以点结尾）
        title = title.strip(' .')
        # 空标题降级为 "untitled"
        return title if title else "untitled"
