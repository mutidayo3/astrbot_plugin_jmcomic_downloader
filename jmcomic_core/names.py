"""文件名安全化与标题处理。"""

from __future__ import annotations

import re

# Windows / 通用文件系统非法字符
_INVALID_FILENAME_CHARS = '<>:"/\\|?*'


def sanitize_title(title: str) -> str:
    """清理本子标题，使其可安全用作文件名。

    处理内容：
    - 移除 Windows 非法字符 ``<>:"/\\|?*``
    - 将制表符 / 换行符替换为空格，移除其它控制字符
    - 合并连续空格
    - 去除首尾空格与点号（Windows 不允许文件名以点结尾）
    - 空标题降级为 ``untitled``
    """
    if not title:
        return "untitled"

    for char in _INVALID_FILENAME_CHARS:
        title = title.replace(char, "")

    title = "".join(
        " " if ord(ch) in (0x09, 0x0A, 0x0D) else "" if ord(ch) < 0x20 or ord(ch) == 0x7F else ch
        for ch in title
    )
    title = re.sub(r" {2,}", " ", title)
    title = title.strip(" .")
    return title or "untitled"


def build_pdf_name(album_id: str, title: str) -> str:
    """构造统一的 PDF 文件名：``JM{album_id}-{title}.pdf``。"""
    return f"JM{album_id}-{title}.pdf"
