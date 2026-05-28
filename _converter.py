"""文件转换工具：图片→PDF 转换 + PDF→ZIP 压缩。

PDF 转换使用 img2pdf 流式处理，支持 DPI 控制及超时保护。
ZIP 压缩使用 pyzipper 实现 AES-256 强加密。
"""

import asyncio
from pathlib import Path
from typing import Optional

from astrbot.api import logger


async def convert_to_pdf(
    image_files: list[Path],
    pdf_path: Path,
    pdf_resolution: float = 150.0,
    pdf_timeout: int = 600,
) -> Path:
    """使用 img2pdf 将图片列表转换为单个 PDF 文件。

    特性：
    - 直接读取图片二进制内容传给 img2pdf，规避路径类型兼容问题
    - 使用 layout_fun 在 PDF 层面控制页面尺寸，基于 DPI 换算
    - 超时保护，防止转换无限挂起

    Args:
        image_files: 已排序的图片文件路径列表
        pdf_path: 输出 PDF 路径
        pdf_resolution: 目标 DPI（影响清晰度与文件大小）
        pdf_timeout: 超时时间（秒）

    Returns:
        生成的 PDF 文件路径

    Raises:
        RuntimeError: 转换超时或失败
    """
    logger.info(f"开始生成 PDF (img2pdf): {len(image_files)} 页 -> {pdf_path}")

    loop = asyncio.get_running_loop()
    page_count = len(image_files)

    def _do_convert():
        import img2pdf

        # 直接读取图片二进制内容，彻底避免 img2pdf 路径兼容问题
        # （部分版本对 bytes/str 路径处理不一致，导致 TypeError）
        logger.info(f"正在读取 {page_count} 张图片...")
        image_data = []
        for i, p in enumerate(image_files):
            image_data.append(p.read_bytes())
            if (i + 1) % 50 == 0 or (i + 1) == page_count:
                logger.info(f"已读取 {i + 1}/{page_count} 张图片")

        # 使用 layout_fun 在 PDF 层面控制页面尺寸（基于 DPI 换算）
        # img2pdf 原生机制，无需对图片做任何重编码
        dpi = pdf_resolution
        a4_width_pt, a4_height_pt = 595.276, 841.890  # A4 纸张点数
        layout_fun = img2pdf.get_layout_fun(
            (a4_width_pt / dpi * 72, a4_height_pt / dpi * 72),
            None, None, None, None,
        )

        logger.info("正在生成 PDF 文档...")
        with open(pdf_path, "wb") as f:
            img2pdf.convert(image_data, outputstream=f, layout_fun=layout_fun)

        size_mb = pdf_path.stat().st_size / (1024 * 1024)
        logger.info(f"PDF 生成成功: {pdf_path.name} ({size_mb:.1f} MB, {page_count} 页)")

    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, _do_convert),
            timeout=pdf_timeout,
        )
    except asyncio.TimeoutError:
        logger.error(f"PDF 转换超时 ({pdf_timeout}s)，图片数量: {page_count}")
        if pdf_path.exists():
            pdf_path.unlink()
        raise RuntimeError(f"PDF 转换超时（{pdf_timeout}秒），请减少图片数量或稍后重试")

    return pdf_path


async def compress_to_zip(
    pdf_path: Path,
    zip_path: Path,
    password: str = "",
) -> Optional[Path]:
    """将 PDF 压缩为 ZIP 文件（支持 AES-256 加密，仅存储模式以节省 CPU）。

    使用 pyzipper 实现真正的加密写入。ZIP_STORED 模式不做二次压缩，
    因为 PDF 本身已是压缩格式。

    Args:
        pdf_path: 源 PDF 文件路径
        zip_path: 输出 ZIP 文件路径
        password: 加密密码（空字符串表示不加密）

    Returns:
        生成的 ZIP 文件路径，失败返回 None
    """
    import pyzipper

    logger.info(f"正在打包 PDF (Store 模式): {pdf_path.name} -> {zip_path.name}")

    loop = asyncio.get_running_loop()

    def _do_compress():
        try:
            # 使用 pyzipper 实现真正的加密写入
            # compression=pyzipper.ZIP_STORED: 仅存储，不进行算法压缩，极大降低 CPU 开销
            with pyzipper.AESZipFile(
                zip_path, 'w',
                compression=pyzipper.ZIP_STORED,
                encryption=pyzipper.WZ_AES if password else None,
            ) as zf:
                if password:
                    zf.setpassword(password.encode('utf-8'))
                    # 设置 AES 加密强度 (256 bit)
                    zf.setencryption(pyzipper.WZ_AES, nbits=256)
                zf.write(pdf_path, arcname=pdf_path.name)
            return zip_path
        except Exception as e:
            logger.error(f"ZIP 打包失败: {e}")
            return None

    return await loop.run_in_executor(None, _do_compress)
