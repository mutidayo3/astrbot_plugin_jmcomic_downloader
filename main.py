from astrbot.api.event import filter as astr_filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, File
from pathlib import Path
import asyncio
import os
import gc
import sys
import shutil
import time
import socket
from typing import Optional, Dict, Set
from aiohttp import web

try:
    import jmcomic
    from PIL import Image
except ImportError as e:
    logger.error(f"缺少依赖库: {e}")
    logger.error("请运行: pip install jmcomic Pillow aiohttp")


class FileServer:
    """轻量级 HTTP 文件服务器，用于跨容器传输文件"""

    def __init__(self, root_dir: Path, host: str = "0.0.0.0", port: int = 18790):
        self.root_dir = root_dir.resolve()
        self.host = host
        self.port = port
        self.app = web.Application()
        self.app.router.add_get("/files/{path:.*}", self._handle)
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        logger.info(f"HTTP 文件服务器已启动: http://{self.host}:{self.port}")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            logger.info("HTTP 文件服务器已停止")

    async def _handle(self, request: web.Request) -> web.Response:
        """提供文件下载，带目录遍历防护"""
        rel_path = request.match_info["path"]
        target = (self.root_dir / rel_path).resolve()

        # 安全检查：确保请求路径不超出 root_dir
        try:
            target.relative_to(self.root_dir)
        except ValueError:
            return web.Response(status=403, text="Forbidden")

        if not target.exists() or not target.is_file():
            return web.Response(status=404, text="Not Found")

        return web.FileResponse(target, headers={
            "Content-Disposition": f'attachment; filename="{target.name}"'
        })


@register("jmcomic_downloader", "mutidayo3", "JMComic 本子下载器", "0.4.0")
class JMComicPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.download_dir: Optional[Path] = None

        # 下载配置
        self.max_workers = max(1, min(self.config.get('max_workers', 4), 8))
        self.image_format = self.config.get('image_format', 'webp')
        self.download_timeout = self.config.get('download_timeout', 300)
        self.auto_cleanup = self.config.get('auto_cleanup', True)
        self.pdf_resolution = self.config.get('pdf_resolution', 150.0)
        self.max_pdf_size_mb = self.config.get('max_pdf_size_mb', 100)

        # 缓存配置
        self.max_cache_count = max(0, self.config.get('max_cache_count', 20))

        # 文件传输模式: auto/local/docker
        self.transfer_mode = self.config.get('transfer_mode', 'auto')

        # 文件服务器配置（Docker 跨容器必需）
        self.file_server_port = self.config.get('file_server_port', 18790)
        self.file_server_base_url = self.config.get('file_server_base_url', "").rstrip("/")
        self.file_server: Optional[FileServer] = None

        # 并发控制
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._served_files: Set[Path] = set()

    @staticmethod
    def _is_running_in_docker() -> bool:
        """检测当前是否在 Docker 容器中运行"""
        if os.path.exists('/.dockerenv'):
            return True
        try:
            with open('/proc/1/cgroup', 'r') as f:
                return 'docker' in f.read()
        except Exception:
            pass
        return False

    async def initialize(self):
        """插件初始化"""
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        data_path = get_astrbot_data_path()
        self.download_dir = Path(data_path) / "plugin_data" / self.name / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # 解析实际传输模式
        actual_mode = self.transfer_mode
        if actual_mode == "auto":
            actual_mode = "docker" if self._is_running_in_docker() else "local"
            logger.info(f"自动检测到运行环境: {actual_mode}")

        # Docker 模式下启动 HTTP 文件服务器
        if actual_mode == "docker":
            self.file_server = FileServer(self.download_dir, port=self.file_server_port)
            await self.file_server.start()

            if not self.file_server_base_url:
                host_ip = self._get_docker_host_ip()
                self.file_server_base_url = f"http://{host_ip}:{self.file_server_port}"
                logger.warning(
                    f"未配置 file_server_base_url，自动推断为: {self.file_server_base_url}\n"
                    f"如果 NapCat 无法访问，请在插件配置中手动设置为宿主机可访问的 IP"
                )

            logger.info(f"文件服务: {self.file_server_base_url}")
        else:
            logger.info("本地模式运行，未启动 HTTP 文件服务器")

        # 显示当前缓存状态
        cached = self._list_cached_pdfs()
        logger.info(f"JMComic 插件初始化完成")
        logger.info(f"下载目录: {self.download_dir}")
        logger.info(f"传输模式: {actual_mode}")
        logger.info(f"PDF 缓存: {len(cached)}/{self.max_cache_count} 本")

    def _get_docker_host_ip(self) -> str:
        """获取宿主机在 Docker 网桥中的 IP"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("172.17.0.1", 1))
            ip = s.getsockname()[0]
            s.close()
            if ip.startswith("172.") or ip.startswith("192.168."):
                return "172.17.0.1"
        except Exception:
            pass
        return "172.17.0.1"

    def _get_album_lock(self, album_id: str) -> asyncio.Lock:
        """获取指定本子的下载锁"""
        if album_id not in self._locks:
            self._locks[album_id] = asyncio.Lock()
        return self._locks[album_id]

    def _list_cached_pdfs(self) -> list[Path]:
        """列出当前缓存的所有 PDF，按访问时间从新到旧排序"""
        if not self.download_dir or not self.download_dir.exists():
            return []
        pdfs = [f for f in self.download_dir.iterdir() if f.is_file() and f.suffix.lower() == '.pdf']
        # 按 atime（访问时间）降序，最新的在前面
        return sorted(pdfs, key=lambda p: p.stat().st_atime, reverse=True)

    def _check_cache(self, album_id: str) -> Optional[Path]:
        """检查本地缓存中是否存在指定本子的 PDF"""
        if self.max_cache_count <= 0:
            return None
        pdf_path = self.download_dir / f"{album_id}.pdf"
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            # 更新访问时间，用于 LRU 淘汰
            os.utime(pdf_path, None)
            logger.info(f"命中本地缓存: {pdf_path.name}")
            return pdf_path
        return None

    def _cleanup_cache(self, keep_path: Optional[Path] = None):
        """按 LRU 策略淘汰旧缓存，保留 keep_path"""
        if self.max_cache_count <= 0:
            return

        cached = self._list_cached_pdfs()
        # 过滤掉需要保留的文件
        to_check = [p for p in cached if keep_path is None or p.resolve() != keep_path.resolve()]

        while len(to_check) >= self.max_cache_count:
            oldest = to_check.pop()  # 列表按 atime 降序，最后一个是最旧的
            try:
                oldest.unlink()
                logger.info(f"缓存淘汰: 删除旧 PDF {oldest.name}")
            except Exception as e:
                logger.warning(f"缓存淘汰失败 {oldest.name}: {e}")

    @astr_filter.command("jmcomic")
    async def download_jmcomic(self, event: AstrMessageEvent, album_id: str = None):
        """下载 JMComic 本子并转换为 PDF

        用法: jmcomic <本子ID>
        例如: jmcomic 422866
        """
        if album_id is None:
            yield event.plain_result(
                "❌ 请提供本子 ID\n用法: jmcomic <本子ID>\n例如: jmcomic 422866"
            )
            return

        album_id = str(album_id).strip()
        if not album_id.isdigit():
            yield event.plain_result("❌ 本子 ID 必须是数字")
            return

        lock = self._get_album_lock(album_id)
        if lock.locked():
            yield event.plain_result(f"⏳ 本子 {album_id} 正在下载中，请稍候...")
            return

        async with lock:
            album_dir: Optional[Path] = None
            pdf_path: Optional[Path] = None
            start_time = time.time()
            from_cache = False

            try:
                # 1. 检查本地缓存
                cached_pdf = self._check_cache(album_id)
                if cached_pdf:
                    pdf_path = cached_pdf
                    from_cache = True
                    file_size_mb = pdf_path.stat().st_size / 1024 / 1024
                    logger.info(f"PDF 缓存命中: {pdf_path.name} ({file_size_mb:.2f} MB)")
                    yield event.plain_result(f"📦 命中本地缓存，直接发送本子 {album_id}...")
                else:
                    # 2. 下载
                    yield event.plain_result(f"📥 开始下载本子 {album_id}...")

                    album_dir = self.download_dir / album_id
                    album_dir.mkdir(parents=True, exist_ok=True)

                    await self._download_album(album_id, album_dir)

                    # 3. 检查图片
                    image_files = self._collect_image_files(album_dir)
                    if not image_files:
                        yield event.plain_result("❌ 下载完成后未找到图片文件")
                        return

                    yield event.plain_result(
                        f"✅ 下载完成 ({len(image_files)} 页)，正在转换为 PDF..."
                    )

                    # 4. 转 PDF
                    pdf_path = await self._convert_to_pdf(image_files, album_id)

                    if not pdf_path or not pdf_path.exists():
                        yield event.plain_result("❌ PDF 转换失败")
                        return

                    # 5. 检查大小
                    file_size = pdf_path.stat().st_size
                    file_size_mb = file_size / 1024 / 1024
                    logger.info(f"PDF 生成成功: {pdf_path} ({file_size_mb:.2f} MB)")

                    if file_size_mb > self.max_pdf_size_mb:
                        yield event.plain_result(
                            f"⚠️ PDF 文件过大 ({file_size_mb:.1f} MB)，可能发送较慢或失败"
                        )

                # 6. 发送文件（根据配置选择传输方式）
                if from_cache:
                    yield event.plain_result(f"📚 正在发送缓存的本子 {album_id}...")
                else:
                    yield event.plain_result(f"📚 本子 {album_id} 处理完成，正在发送文件...")

                await self._send_file(event, pdf_path, f"{album_id}.pdf", album_id)

                elapsed = time.time() - start_time
                logger.info(f"本子 {album_id} 总耗时: {elapsed:.1f}s (缓存: {from_cache})")

            except asyncio.TimeoutError:
                logger.error(f"下载本子 {album_id} 超时")
                yield event.plain_result("❌ 下载超时，请稍后重试")
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

                # 缓存淘汰（保留当前这本）
                if pdf_path:
                    self._cleanup_cache(keep_path=pdf_path)

                async with self._global_lock:
                    if album_id in self._locks and not self._locks[album_id].locked():
                        del self._locks[album_id]

                self._force_gc()

    async def _send_file(self, event: AstrMessageEvent, file_path: Path, file_name: str, album_id: str):
        """根据配置选择文件传输方式"""
        mode = self.transfer_mode
        if mode == "auto":
            mode = "docker" if self._is_running_in_docker() else "local"

        if mode == "local":
            # 本地模式：直接使用 File 组件，AstrBot 底层会处理本地文件路径
            await event.send(
                event.chain_result([
                    Plain(f"📚 本子 {album_id} 处理完成，正在发送..."),
                    File(file=str(file_path), name=file_name)
                ])
            )
            logger.info(f"已通过本地路径发送文件: {file_path}")
        else:
            # Docker 模式：使用 HTTP 文件服务器 + OneBot API
            await self._send_file_via_onebot(event, file_path, file_name)

    async def _send_file_via_onebot(self, event: AstrMessageEvent, file_path: Path, file_name: str):
        """直接调用 OneBot API 发送文件，绕过 AstrBot File 组件的 bug"""
        if event.get_platform_name() != "aiocqhttp":
            # 非 aiocqhttp 平台回退到普通方式
            await event.send(event.chain_result([File(file=str(file_path), name=file_name)]))
            return

        # 获取 aiocqhttp 的 bot 客户端
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        assert isinstance(event, AiocqhttpMessageEvent)
        bot = event.bot

        # 判断是群聊还是私聊
        message_obj = event.message_obj
        is_group = hasattr(message_obj, 'group_id') and message_obj.group_id

        # 构建文件 URL（NapCat 会通过 HTTP 下载）
        file_url = f"{self.file_server_base_url}/files/{file_path.name}"

        try:
            if is_group:
                group_id = int(message_obj.group_id)
                await bot.call_action(
                    "upload_group_file",
                    group_id=group_id,
                    file=file_url,
                    name=file_name
                )
                logger.info(f"已通过 upload_group_file 发送文件到群 {group_id}")
            else:
                user_id = int(message_obj.sender.user_id)
                await bot.call_action(
                    "upload_private_file",
                    user_id=user_id,
                    file=file_url,
                    name=file_name
                )
                logger.info(f"已通过 upload_private_file 发送文件给用户 {user_id}")
        except Exception as e:
            logger.error(f"OneBot API 发送文件失败: {e}", exc_info=True)
            # 降级：发送文件下载链接
            await event.send(event.plain_result(f"文件下载链接: {file_url}"))

    def _collect_image_files(self, image_dir: Path) -> list[Path]:
        """收集并排序图片文件"""
        if not image_dir or not image_dir.exists():
            return []

        image_files = [
            f for f in image_dir.rglob('*')
            if f.is_file() and f.suffix.lower() in {'.webp', '.jpg', '.jpeg', '.png', '.gif'}
        ]

        # 按文件名数字排序（修复 filter 命名冲突）
        def sort_key(p: Path):
            stem = p.stem
            digits = ''.join([c for c in stem if c.isdigit()])
            return (int(digits) if digits else 0, stem)

        return sorted(image_files, key=sort_key)

    async def _download_album(self, album_id: str, download_dir: Path):
        """异步下载本子"""
        loop = asyncio.get_event_loop()

        def _download():
            option = jmcomic.JmOption.default()

            suffix_map = {
                'webp': '.webp', 'jpg': '.jpg',
                'jpeg': '.jpg', 'png': '.png'
            }
            option.download.image.suffix = suffix_map.get(self.image_format, '.webp')
            option.dir_rule.rule = 'Aid'
            option.dir_rule.base_dir = str(download_dir)
            option.download.threading.image = self.max_workers

            logger.info(f"开始下载 {album_id} | 线程: {self.max_workers}")
            jmcomic.download_album(album_id, option)

            file_count = sum(1 for _ in download_dir.rglob('*') if _.is_file())
            logger.info(f"下载完成，共 {file_count} 个文件")

        await asyncio.wait_for(
            loop.run_in_executor(None, _download),
            timeout=self.download_timeout
        )

    async def _convert_to_pdf(self, image_files: list[Path], album_id: str) -> Path:
        """将图片转换为 PDF"""
        pdf_path = self.download_dir / f"{album_id}.pdf"

        logger.info(f"开始生成 PDF: {len(image_files)} 页 -> {pdf_path}")

        loop = asyncio.get_event_loop()

        def _build_pdf():
            images: list[Image.Image] = []

            try:
                for img_path in image_files:
                    try:
                        img = Image.open(img_path)
                        if img.mode in ('RGBA', 'P', 'LA', 'L'):
                            img = img.convert('RGB')
                        elif img.mode != 'RGB':
                            img = img.convert('RGB')
                        images.append(img)
                    except Exception as e:
                        logger.warning(f"无法打开 {img_path.name}: {e}")
                        continue

                if not images:
                    raise ValueError("没有有效的图片可以转换")

                first_image = images[0]
                other_images = images[1:] if len(images) > 1 else []

                first_image.save(
                    str(pdf_path),
                    "PDF",
                    resolution=self.pdf_resolution,
                    save_all=True,
                    append_images=other_images
                )

                logger.info(f"PDF 生成成功: {pdf_path.name}")

            finally:
                for img in images:
                    try:
                        img.close()
                    except Exception:
                        pass
                images.clear()

        await loop.run_in_executor(None, _build_pdf)
        self._force_gc()

        return pdf_path

    async def _cleanup_files(self, album_dir: Optional[Path], pdf_path: Optional[Path]):
        """清理临时文件（保留 PDF 缓存）"""
        if album_dir and album_dir.exists():
            try:
                shutil.rmtree(album_dir, ignore_errors=True)
                logger.info(f"已清理目录: {album_dir}")
            except Exception as e:
                logger.warning(f"清理目录失败: {e}")

    def _force_gc(self):
        gc.collect()
        logger.debug("已执行垃圾回收")

    async def terminate(self):
        """插件卸载"""
        if self.file_server:
            await self.file_server.stop()
        self._force_gc()
        logger.info("JMComic 插件已卸载")