"""JMComic 下载子进程工作模块。

独立于 main.py 以避免 multiprocessing.spawn 重新导入时触发
AstrBot 插件注册 (@register) 等全局副作用。
子进程仅导入此模块和 jmcomic，不加载任何插件框架代码。
"""

import os
import signal
import sys


def download_album_worker(album_id, download_dir_str, image_format, max_workers, conn):
    """在子进程中下载本子并通过 Pipe 返回标题。

    Args:
        album_id: 本子 ID
        download_dir_str: 下载目录字符串路径
        image_format: 图片格式 (webp/jpg/png)
        max_workers: 下载线程数
        conn: multiprocessing.Connection (子进程端)
    """
    _sent = False  # 标记是否已成功发送结果

    # 注册 SIGTERM 处理：超时被 terminate 时尽量发送诊断信息
    def _sigterm_handler(signum, frame):
        if not _sent:
            try:
                conn.send(('error', f'子进程收到 SIGTERM (PID={os.getpid()})，下载被终止'))
            except Exception:
                pass
        sys.exit(1)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    try:
        import jmcomic
        option = jmcomic.JmOption.default()
        suffix_map = {'webp': '.webp', 'jpg': '.jpg', 'jpeg': '.jpg', 'png': '.png'}
        option.download.image.suffix = suffix_map.get(image_format, '.webp')
        option.dir_rule.rule = 'Aid'
        option.dir_rule.base_dir = download_dir_str
        option.download.threading.image = max_workers

        result = jmcomic.download_album(album_id, option)

        if isinstance(result, tuple):
            album = result[0]
        else:
            album = result

        title = album_id
        if album:
            title = album.title
            invalid_chars = '<>:"/\\|?*'
            for char in invalid_chars:
                title = title.replace(char, '')
            title = title.strip()

        conn.send(('ok', title))
        _sent = True
    except SystemExit:
        # SIGTERM handler 已发送诊断信息，直接退出
        raise
    except Exception as e:
        conn.send(('error', str(e)))
        _sent = True
    finally:
        conn.close()
