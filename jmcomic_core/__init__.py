"""JMComic 下载器核心子包。

intentionally keeps no imports here so that ``multiprocessing.spawn``
子进程在导入 :mod:`jmcomic_core.worker` 时不会触发 AstrBot 框架或其它
重型模块的副作用。
"""
