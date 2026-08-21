"""JMComic 下载子进程工作模块。

独立于主插件，避免 ``multiprocessing.spawn`` 重新导入时触发 AstrBot 插件
注册 (``@register``) 等全局副作用。子进程仅导入本模块和 jmcomic，
不加载任何插件框架代码。

本模块故意只依赖标准库与 jmcomic，以确保 spawn 子进程能轻量启动。
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from typing import Any


def download_album_worker(
    album_id: str,
    download_dir_str: str,
    image_format: str,
    max_workers: int,
    conn: Any,
) -> None:
    """在子进程中下载本子并通过 Pipe 返回标题。

    Args:
        album_id: 本子 ID
        download_dir_str: 下载目录字符串路径
        image_format: 图片格式 (jpg/webp/png)
        max_workers: 下载线程数
        conn: multiprocessing.Connection（子进程端）
    """
    sent = False  # 标记是否已成功发送结果

    # 注册 SIGTERM 处理：超时被 terminate 时尽量发送诊断信息
    def _sigterm_handler(signum: int, frame: Any) -> None:
        nonlocal sent
        if not sent:
            with contextlib.suppress(Exception):
                conn.send(("error", f"子进程收到 SIGTERM (PID={os.getpid()})，下载被终止"))
        sys.exit(1)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    try:
        import jmcomic

        option = jmcomic.JmOption.default()
        suffix_map = {"jpg": ".jpg", "jpeg": ".jpg", "webp": ".webp", "png": ".png"}
        option.download.image.suffix = suffix_map.get(image_format, ".jpg")
        option.dir_rule.rule = "Aid"
        option.dir_rule.base_dir = download_dir_str
        option.download.threading.image = max_workers

        result = jmcomic.download_album(album_id, option)

        album = result[0] if isinstance(result, tuple) else result

        # 返回原始标题，文件名清理交由主进程统一处理
        title = album.title if album else album_id
        conn.send(("ok", title))
        sent = True
    except SystemExit:
        # SIGTERM handler 已发送诊断信息，直接退出
        raise
    except Exception as e:
        conn.send(("error", str(e)))
        sent = True
    finally:
        conn.close()
