from astrbot.api.event import filter as astr_filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, File
from pathlib import Path
import asyncio
import os
import gc
import shutil
import time
import socket
import zipfile
import json
from typing import Optional, Dict, Set
from aiohttp import web

# 依赖检测标志
DEPENDENCIES_MET = True
try:
    import jmcomic
    import img2pdf
    import pyzipper
    from PIL import Image
except ImportError as e:
    logger.error(f"缺少依赖库: {e}")
    logger.error("请运行: pip install jmcomic Pillow img2pdf pyzipper aiohttp")
    DEPENDENCIES_MET = False


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

        # 压缩与加密配置
        self.enable_zip = self.config.get('enable_zip', False)
        self.zip_password = self.config.get('zip_password', "")

        # 并发控制
        self._locks: Dict[str, asyncio.Lock] = {}
        self._served_files: Set[Path] = set()
        self._dirty_downloads: Set[str] = set()  # 记录因超时而处于“脏”状态的下载任务 ID
        self._cache_map: Dict[str, str] = {}  # 内存映射: album_id -> pdf_filename
        self._index_file: Path = None  # 持久化索引文件路径

    @staticmethod
    def _is_running_in_docker() -> bool:
        """增强版 Docker 环境检测"""
        # 1. 检查 /.dockerenv 文件 (最可靠)
        if os.path.exists('/.dockerenv'):
            return True
        # 2. 检查 cgroup 信息
        try:
            with open('/proc/1/cgroup', 'r') as f:
                content = f.read()
                if 'docker' in content or 'kubepods' in content:
                    return True
        except Exception:
            pass
        # 3. 检查环境变量
        if os.environ.get('container') == 'docker':
            return True
        return False

    async def initialize(self):
        """插件初始化"""
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        data_path = get_astrbot_data_path()
        self.download_dir = Path(data_path) / "plugin_data" / self.name / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._index_file = self.download_dir / "cache_index.json"

        # 加载持久化的缓存索引
        self._load_cache_index()

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
                host_ip = self._get_host_ip()
                self.file_server_base_url = f"http://{host_ip}:{self.file_server_port}"
                logger.warning(
                    f"未配置 file_server_base_url，自动推断为: {self.file_server_base_url}\n"
                    f"【重要】如果 NapCat 无法下载文件，请在插件配置中手动设置该地址为宿主机 IP"
                )

            logger.info(f"文件服务: {self.file_server_base_url}")
        else:
            logger.info("本地模式运行，未启动 HTTP 文件服务器")

        # 显示当前缓存状态并同步磁盘文件到内存映射
        cached = self._list_cached_pdfs()
        for pdf in cached:
            stem = pdf.stem
            # 如果内存中没有，但磁盘上有，且文件名是 ID.pdf，则录入
            if stem.isdigit() and stem not in self._cache_map:
                self._cache_map[stem] = pdf.name
        
        logger.info(f"JMComic 插件初始化完成")
        logger.info(f"下载目录: {self.download_dir}")
        logger.info(f"传输模式: {actual_mode}")
        logger.info(f"PDF 缓存: {len(cached)}/{self.max_cache_count} 本")

    def _get_host_ip(self) -> str:
        """智能获取宿主机可访问 IP"""
        # 优先尝试获取默认网关对应的本地 IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            # 尝试连接一个公网地址以触发路由表选择本地出口 IP
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            # 如果是内网地址，通常 NapCat 可以通过该 IP 访问宿主机
            if local_ip.startswith("192.168.") or local_ip.startswith("10.") or local_ip.startswith("172."):
                logger.info(f"检测到本地出口 IP: {local_ip}")
                return local_ip
        except Exception:
            pass

        # 备选方案：尝试常见的 Docker 网关
        for gateway in ["172.17.0.1", "172.18.0.1", "192.168.65.1"]:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                result = s.connect_ex((gateway, self.file_server_port))
                s.close()
                if result == 0:
                    logger.info(f"通过端口探测发现可用网关: {gateway}")
                    return gateway
            except Exception:
                continue
        
        logger.warning("无法自动确定宿主机 IP，请手动配置 file_server_base_url")
        return "127.0.0.1"

    def _get_album_lock(self, album_id: str) -> asyncio.Lock:
        """获取指定本子的下载锁（线程/协程安全）"""
        # 使用 setdefault 确保在并发调用时也能正确返回同一个锁实例
        return self._locks.setdefault(album_id, asyncio.Lock())

    def _list_cached_pdfs(self) -> list[Path]:
        if not self.download_dir or not self.download_dir.exists():
            return []
        pdfs = [f for f in self.download_dir.iterdir() if f.is_file() and f.suffix.lower() == '.pdf']
        # 按 atime（访问时间）降序，最新的在前面
        return sorted(pdfs, key=lambda p: p.stat().st_atime, reverse=True)

    def _load_cache_index(self):
        """从磁盘加载缓存索引"""
        if self._index_file and self._index_file.exists():
            try:
                with open(self._index_file, 'r', encoding='utf-8') as f:
                    self._cache_map = json.load(f)
                logger.info(f"已加载缓存索引: {len(self._cache_map)} 条记录")
            except Exception as e:
                logger.warning(f"加载缓存索引失败: {e}")
                self._cache_map = {}

    def _save_cache_index(self):
        """将缓存索引保存到磁盘"""
        if self._index_file:
            try:
                with open(self._index_file, 'w', encoding='utf-8') as f:
                    json.dump(self._cache_map, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"保存缓存索引失败: {e}")

    def _check_cache(self, album_id: str) -> Optional[Path]:
        """检查本地缓存中是否存在指定本子的 PDF"""
        if self.max_cache_count <= 0:
            return None
        
        # 1. 优先查内存映射表
        if album_id in self._cache_map:
            filename = self._cache_map[album_id]
            pdf_path = self.download_dir / filename
            if pdf_path.exists():
                os.utime(pdf_path, None)
                return pdf_path

        # 2. 尝试标准 ID 命名
        pdf_path = self.download_dir / f"{album_id}.pdf"
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            self._cache_map[album_id] = pdf_path.name
            os.utime(pdf_path, None)
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
        if not DEPENDENCIES_MET:
            yield event.plain_result(
                "❌ 插件依赖缺失！\n请在 AstrBot 终端或容器内执行:\npip install jmcomic Pillow img2pdf aiohttp"
            )
            return

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
        
        # 优化：直接尝试获取锁，避免 locked() 检查与 acquire 之间的竞态条件
        # 如果锁已被占用，协程会在此处自动挂起等待
        async with lock:
            album_dir: Optional[Path] = None
            pdf_path: Optional[Path] = None
            start_time = time.time()
            from_cache = False

            try:
                # 1. 检查本地缓存
                cached_pdf = self._check_cache(album_id)
                album_title = album_id  # 默认使用 ID

                if cached_pdf:
                    pdf_path = cached_pdf
                    from_cache = True
                    file_size_mb = pdf_path.stat().st_size / 1024 / 1024
                    logger.info(f"PDF 缓存命中: {pdf_path.name} ({file_size_mb:.2f} MB)")
                    yield event.plain_result(f"📦 命中本地缓存，正在获取本子信息...")
                    
                    # 缓存命中时，我们也需要获取标题以正确命名文件
                    # 这里我们通过 jmcomic API 快速获取元数据，而不下载图片
                    try:
                        client = jmcomic.JmOption.default().new_jm_client()
                        album = client.get_album_detail(album_id)
                        if album:
                            album_title = album.title
                            invalid_chars = '<>:"/\\|?*'
                            for char in invalid_chars:
                                album_title = album_title.replace(char, '')
                            album_title = album_title.strip()
                            
                            # 如果缓存的文件名不是标题格式，则重命名
                            expected_name = f"{album_title}.pdf"
                            if pdf_path.name != expected_name:
                                new_pdf_path = pdf_path.parent / expected_name
                                pdf_path.rename(new_pdf_path)
                                pdf_path = new_pdf_path
                                logger.info(f"缓存文件已重命名为: {pdf_path.name}")
                    except Exception as e:
                        logger.warning(f"获取缓存本子标题失败，将使用 ID 作为文件名: {e}")
                else:
                    # 2. 下载
                    yield event.plain_result(f"📥 开始下载本子 {album_id}...")
                    
                    album_dir = self.download_dir / album_id
                                    
                    # 检查是否存在因上次超时而留下的“脏”目录
                    if album_id in self._dirty_downloads and album_dir.exists():
                        logger.warning(f"检测到未完成的脏目录，正在清理: {album_dir}")
                        try:
                            shutil.rmtree(album_dir, ignore_errors=True)
                        except Exception:
                            pass
                        self._dirty_downloads.discard(album_id)
                    
                    album_dir.mkdir(parents=True, exist_ok=True)

                    # 获取本子标题
                    album_title = await self._download_album(album_id, album_dir)

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

                    # 5. 检查大小并重命名 PDF 为标题格式
                    file_size = pdf_path.stat().st_size
                    file_size_mb = file_size / 1024 / 1024
                    
                    # 确保 PDF 文件名与标题一致
                    expected_pdf_name = f"{album_title}.pdf"
                    if pdf_path.name != expected_pdf_name:
                        new_pdf_path = pdf_path.parent / expected_pdf_name
                        pdf_path.rename(new_pdf_path)
                        pdf_path = new_pdf_path

                    logger.info(f"PDF 生成成功: {pdf_path.name} ({file_size_mb:.2f} MB)")

                    if file_size_mb > self.max_pdf_size_mb:
                        yield event.plain_result(
                            f"⚠️ PDF 文件过大 ({file_size_mb:.1f} MB)，可能发送较慢或失败"
                        )

                # 6. 处理压缩与命名（确保磁盘只有一份文件）
                # 此时 pdf_path 可能是 ID 命名的（来自缓存），也可能是新生成的
                # 我们需要将其重命名为标题格式（如果可能）
                expected_name = f"{album_title}.pdf"
                final_pdf_path = self.download_dir / expected_name

                if pdf_path.resolve() != final_pdf_path.resolve():
                    # 如果文件名不一致，执行重命名
                    if final_pdf_path.exists():
                        final_pdf_path.unlink()
                    pdf_path.rename(final_pdf_path)
                    # 更新内存映射并持久化
                    self._cache_map[album_id] = expected_name
                    self._save_cache_index()
                    pdf_path = final_pdf_path
                else:
                    self._cache_map[album_id] = pdf_path.name
                    # 即使没改名，也确保索引是最新的（以防手动改过文件名）
                    self._save_cache_index()

                final_file_path = pdf_path
                final_file_name = expected_name
                
                if self.enable_zip:
                    zip_path = await self._compress_to_zip(pdf_path, album_title, self.zip_password)
                    if zip_path:
                        final_file_path = zip_path
                        final_file_name = f"{album_title}.zip"

                # 7. 发送文件（根据配置选择传输方式）
                # 注意：文件发送涉及底层 API 调用，此处使用 event.send 确保及时性
                # 但在此之前，所有的状态提示均通过 yield 返回
                
                if from_cache:
                    yield event.plain_result(f"📚 正在发送缓存的本子 {album_id}...")
                else:
                    yield event.plain_result(f"📚 本子 {album_id} 处理完成，正在发送文件...")

                # 执行文件发送
                await self._send_file(event, final_file_path, final_file_name, album_id)

                # 如果设置了密码，通过 yield 返回密码提示，确保在文件发送后显示
                if self.enable_zip and self.zip_password:
                    yield event.plain_result(f"🔐 压缩包密码: {self.zip_password}")

                elapsed = time.time() - start_time
                logger.info(f"本子 {album_id} 总耗时: {elapsed:.1f}s (缓存: {from_cache})")

            except asyncio.TimeoutError:
                logger.error(f"下载本子 {album_id} 超时")
                # 标记为脏状态，防止后续重试时发生目录竞争
                self._dirty_downloads.add(album_id)
                yield event.plain_result("❌ 下载超时，请稍后重试。后台下载已终止，但临时文件可能需要稍后清理。")
                return  # 直接返回，不执行 finally 中的清理逻辑，留给下次请求处理
            except Exception as e:
                logger.error(f"下载本子 {album_id} 时出错: {e}", exc_info=True)
                yield event.plain_result(f"❌ 下载失败: {str(e)}")
            finally:
                # 清理临时图片目录
                # 只有在非超时且非脏状态下才立即清理
                if self.auto_cleanup and album_dir and album_dir.exists() and album_id not in self._dirty_downloads:
                    try:
                        shutil.rmtree(album_dir, ignore_errors=True)
                        logger.info(f"已清理临时目录: {album_dir}")
                    except Exception as e:
                        logger.warning(f"清理目录失败: {e}")

                # 缓存淘汰（保留当前这本）
                if pdf_path:
                    self._cleanup_cache(keep_path=pdf_path)

                # 内存优化说明：
                # 不再手动清理 _locks 中的条目。对于离散的本子 ID，保留 Lock 对象
                # 可以避免复杂的竞态条件检查，且内存开销极小（每个 Lock 约几十字节）。
                # 同时移除了手动 gc.collect()，依赖 Python 自动引用计数回收内存，避免阻塞事件循环。

    async def _send_file(self, event: AstrMessageEvent, file_path: Path, file_name: str, album_id: str):
        """根据配置选择文件传输方式（内部使用 event.send 绕过框架限制）"""
        mode = self.transfer_mode
        if mode == "auto":
            mode = "docker" if self._is_running_in_docker() else "local"

        if mode == "local":
            # 本地模式：直接使用 File 组件
            await event.send(
                event.chain_result([
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

    async def _download_album(self, album_id: str, download_dir: Path) -> str:
        """异步下载本子并返回标题"""
        loop = asyncio.get_event_loop()
        album_title = album_id  # 默认使用 ID 作为标题

        def _download():
            nonlocal album_title
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
            
            # 执行下载并获取本子对象
            result = jmcomic.download_album(album_id, option)
            
            # 兼容不同版本的 jmcomic 返回值：可能是 JmAlbumDetail 对象，也可能是 (JmAlbumDetail, ...) 元组
            if isinstance(result, tuple):
                album = result[0]
            else:
                album = result

            if album:
                album_title = album.title
                # 清理标题中的非法字符，防止文件系统报错
                invalid_chars = '<>:"/\\|?*'
                for char in invalid_chars:
                    album_title = album_title.replace(char, '')
                album_title = album_title.strip()

            file_count = sum(1 for _ in download_dir.rglob('*') if _.is_file())
            logger.info(f"下载完成: {album_title} (共 {file_count} 个文件)")

        await asyncio.wait_for(
            loop.run_in_executor(None, _download),
            timeout=self.download_timeout
        )
        return album_title

    async def _compress_to_zip(self, pdf_path: Path, album_title: str, password: str = "") -> Optional[Path]:
        """将 PDF 压缩为 ZIP 文件（支持 AES-256 加密，仅存储模式以节省 CPU）"""
        zip_path = self.download_dir / f"{album_title}.zip"
        logger.info(f"正在打包 PDF (Store 模式): {pdf_path.name} -> {zip_path.name}")
        
        loop = asyncio.get_event_loop()
        def _do_compress():
            try:
                # 使用 pyzipper 实现真正的加密写入
                # compression=pyzipper.ZIP_STORED: 仅存储，不进行算法压缩，极大降低 CPU 开销
                with pyzipper.AESZipFile(
                    zip_path, 'w', 
                    compression=pyzipper.ZIP_STORED,
                    encryption=pyzipper.WZ_AES if password else None
                ) as zf:
                    if password:
                        zf.setpassword(password.encode('utf-8'))
                        # 设置 AES 加密强度 (128 or 256)
                        zf.setencryption(pyzipper.WZ_AES, nbits=256)
                    zf.write(pdf_path, arcname=pdf_path.name)
                return zip_path
            except Exception as e:
                logger.error(f"ZIP 打包失败: {e}")
                return None

        return await loop.run_in_executor(None, _do_compress)

    async def _convert_to_pdf(self, image_files: list[Path], album_id: str) -> Path:
        """使用 img2pdf 将图片转换为 PDF（流式处理，低内存占用）"""
        pdf_path = self.download_dir / f"{album_id}.pdf"
        logger.info(f"开始生成 PDF (img2pdf): {len(image_files)} 页 -> {pdf_path}")

        loop = asyncio.get_event_loop()

        def _build_pdf_stream():
            # img2pdf 支持直接传入文件路径列表，它会在内部以流式方式读取和转换
            # 这种方式比手动 open/close 更简洁且能有效管理资源
            try:
                with open(pdf_path, "wb") as f:
                    # dpi 设置：虽然 img2pdf 主要是封装，但可以通过 convert 选项处理一些兼容性
                    # fit_into 可以用于缩放，这里我们保持原图尺寸以确保清晰度
                    f.write(img2pdf.convert(
                        [str(p) for p in image_files],
                        layout_fun=img2pdf.get_layout_fun(None, None, None, None, None)
                    ))
                logger.info(f"PDF 生成成功: {pdf_path.name}")
            except Exception as e:
                logger.error(f"img2pdf 转换失败: {e}")
                raise

        await loop.run_in_executor(None, _build_pdf_stream)
        return pdf_path

    async def _cleanup_files(self, album_dir: Optional[Path], pdf_path: Optional[Path]):
        """清理临时文件（保留 PDF 缓存）"""
        if album_dir and album_dir.exists():
            try:
                shutil.rmtree(album_dir, ignore_errors=True)
                logger.info(f"已清理目录: {album_dir}")
            except Exception as e:
                logger.warning(f"清理目录失败: {e}")


    async def terminate(self):
        """插件卸载"""
        if self.file_server:
            await self.file_server.stop()
        logger.info("JMComic 插件已卸载")