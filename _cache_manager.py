"""PDF 缓存管理器。

提供内存映射表 + 磁盘持久化索引 + LRU 淘汰策略的完整缓存方案。
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Dict, Optional, Callable


class CacheManager:
    """PDF 缓存管理器：内存映射 + 磁盘持久化索引 + LRU 淘汰。

    缓存索引持久化到 cache_index.json，重启后依然精准命中。
    文件命名采用 JM{album_id}-{title}.pdf 格式，同时维护 album_id 到文件名的映射。
    """

    def __init__(
        self,
        download_dir: Path,
        max_cache_count: int,
        debug_callback: Optional[Callable[[str], None]] = None,
    ):
        self.download_dir = download_dir
        self.max_cache_count = max_cache_count
        self._debug = debug_callback or (lambda msg: None)
        self._cache_lock = asyncio.Lock()
        self._cache_map: Dict[str, str] = {}  # album_id -> pdf_filename
        self._index_file: Optional[Path] = None

    # ---- 公共属性 ----

    @property
    def cache_map(self) -> Dict[str, str]:
        """album_id -> pdf_filename 的内存映射表"""
        return self._cache_map

    @property
    def cache_lock(self) -> asyncio.Lock:
        """保护 _cache_map 和磁盘索引的全局异步锁"""
        return self._cache_lock

    # ---- 初始化 ----

    def setup(self):
        """初始化索引文件路径并从磁盘加载缓存。

        应在 download_dir 确定后调用。
        """
        self._index_file = self.download_dir / "cache_index.json"
        self._load_index()

    def sync_disk_to_memory(self):
        """将磁盘上的 PDF 文件同步到内存映射表。

        用于插件启动时将磁盘缓存恢复到内存中，应对上次运行中映射表未持久化的情况。
        识别两种命名格式：{album_id}.pdf 和 JM{album_id}-{title}.pdf。
        """
        for pdf in self._list_cached():
            album_id = self._extract_album_id(pdf.stem)
            if album_id is not None and album_id not in self._cache_map:
                self._cache_map[album_id] = pdf.name

    # ---- 持久化 ----

    def _load_index(self):
        """从磁盘加载缓存索引到内存映射表。"""
        if self._index_file and self._index_file.exists():
            try:
                with open(self._index_file, 'r', encoding='utf-8') as f:
                    self._cache_map = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                self._cache_map = {}

    async def save_index(self):
        """将内存映射表持久化到磁盘（原子写入）。

        使用临时文件 + replace 策略，防止进程崩溃时损坏索引文件。
        调用方需持有 _cache_lock。
        """
        if self._index_file:
            tmp_path = self._index_file.with_suffix('.json.tmp')
            try:
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    json.dump(self._cache_map, f, ensure_ascii=False, indent=2)
                tmp_path.replace(self._index_file)
            except OSError:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    # ---- 查询 ----

    async def check(self, album_id: str) -> Optional[Path]:
        """检查缓存中是否存在指定本子的 PDF。

        查找顺序：内存映射表 → 磁盘标准 ID 命名。
        命中时更新文件 atime 用于 LRU 排序。
        线程/协程安全（内部持有 _cache_lock）。
        """
        if self.max_cache_count <= 0:
            self._debug("缓存已禁用 (max_cache_count=0)")
            return None

        async with self._cache_lock:
            # 1. 优先查内存映射表
            if album_id in self._cache_map:
                filename = self._cache_map[album_id]
                pdf_path = self.download_dir / filename
                if pdf_path.exists():
                    os.utime(pdf_path, None)  # 更新 atime 用于 LRU
                    self._debug(f"缓存命中 (内存映射): {album_id} -> {filename}")
                    return pdf_path
                else:
                    self._debug(f"内存映射命中但文件不存在: {album_id} -> {filename}，已清理")
                    del self._cache_map[album_id]

            # 2. 尝试标准 ID 命名
            pdf_path = self.download_dir / f"{album_id}.pdf"
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                self._cache_map[album_id] = pdf_path.name
                os.utime(pdf_path, None)
                self._debug(f"缓存命中 (磁盘扫描): {album_id} -> {pdf_path.name}")
                return pdf_path

            self._debug(f"缓存未命中: {album_id}")
            return None

    # ---- 淘汰 ----

    async def cleanup(self, keep_path: Optional[Path] = None):
        """按 LRU 策略淘汰旧缓存，保留 keep_path 不被删除。

        按 atime 降序排列，删除末尾最旧的 PDF。
        同步清理 _cache_map 中的对应条目，避免悬空引用。
        线程/协程安全（内部持有 _cache_lock）。
        """
        if self.max_cache_count <= 0:
            return

        async with self._cache_lock:
            cached = self._list_cached()
            self._debug(f"缓存淘汰检查: 当前 {len(cached)} 个 PDF, 上限 {self.max_cache_count}")
            # 过滤掉需要保留的文件
            to_check = [p for p in cached if keep_path is None or p.resolve() != keep_path.resolve()]
            evicted = 0

            while len(to_check) >= self.max_cache_count:
                oldest = to_check.pop()  # 列表按 atime 降序，最后一个是最旧的
                try:
                    # 同步清理内存映射表中的对应条目
                    album_id = self._extract_album_id(oldest.stem)
                    if album_id and album_id in self._cache_map:
                        del self._cache_map[album_id]
                    oldest.unlink()
                    evicted += 1
                except OSError:
                    pass

            if evicted:
                await self.save_index()
                self._debug(f"缓存淘汰完成: 删除 {evicted} 个, 剩余 {len(cached) - evicted} 个")

    def _extract_album_id(self, stem: str) -> Optional[str]:
        """从文件名 stem 提取 album_id。支持两种命名格式。

        - 格式1: 纯数字 ID（如 422866）
        - 格式2: JM{id}-{title}（如 JM422866-SomeTitle）
        """
        if stem.isdigit():
            return stem
        if stem.startswith('JM') and '-' in stem:
            album_id = stem[2:].split('-', 1)[0]
            if album_id.isdigit():
                return album_id
        return None

    def _list_cached(self) -> list[Path]:
        """列出所有缓存 PDF，按 atime 降序排列（最新的在前）。"""
        if not self.download_dir or not self.download_dir.exists():
            return []
        pdfs = [f for f in self.download_dir.iterdir() if f.is_file() and f.suffix.lower() == '.pdf']
        return sorted(pdfs, key=lambda p: p.stat().st_atime, reverse=True)
