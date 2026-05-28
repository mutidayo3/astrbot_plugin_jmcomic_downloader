"""JMComic 插件工具函数。

提供 Docker 运行环境检测、宿主机 IP 获取、图片文件收集等纯函数。
"""

import os
import socket
from pathlib import Path


def is_running_in_docker() -> bool:
    """增强版 Docker 环境检测（兼容 cgroup v1/v2）。

    通过多层检查确保在各种 Docker 运行时环境下都能准确识别：
    - /.dockerenv 文件（最可靠）
    - cgroup v1 信息（/proc/1/cgroup）
    - cgroup v2 信息（/proc/1/mountinfo, /proc/self/cgroup）
    - 环境变量 container=docker
    """
    # 1. 检查 /.dockerenv 文件 (最可靠)
    if os.path.exists('/.dockerenv'):
        return True
    # 2. 检查 cgroup v1 信息
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            if 'docker' in content or 'kubepods' in content:
                return True
    except Exception:
        pass
    # 3. 检查 cgroup v2 信息（现代 Docker + systemd）
    try:
        with open('/proc/1/mountinfo', 'r') as f:
            content = f.read()
            if 'docker' in content or 'kubepods' in content:
                return True
    except Exception:
        pass
    # 4. 检查 PID 1 的 cgroup 控制器路径（cgroup v2 格式）
    try:
        with open('/proc/self/cgroup', 'r') as f:
            content = f.read()
            if 'docker' in content or 'kubepods' in content:
                return True
    except Exception:
        pass
    # 5. 检查环境变量
    if os.environ.get('container') == 'docker':
        return True
    return False


def get_host_ip(file_server_port: int = 18790) -> str:
    """智能获取宿主机可访问 IP。

    优先级：
    1. 通过 UDP 连接公网地址探测本地出口 IP（最可靠）
    2. 遍历常见 Docker 网关地址，通过 TCP 端口探测验证可达性
    3. 降级返回 127.0.0.1
    """
    # 优先尝试获取默认网关对应的本地 IP
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            # 尝试连接一个公网地址以触发路由表选择本地出口 IP
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        # 如果是内网地址，通常 NapCat 可以通过该 IP 访问宿主机
        if local_ip.startswith("192.168.") or local_ip.startswith("10.") or local_ip.startswith("172."):
            return local_ip
    except Exception:
        pass

    # 备选方案：尝试常见的 Docker 网关
    for gateway in ["172.17.0.1", "172.18.0.1", "192.168.65.1"]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex((gateway, file_server_port))
            if result == 0:
                return gateway
        except Exception:
            continue

    return "127.0.0.1"


def collect_image_files(image_dir: Path) -> list[Path]:
    """收集并排序图片文件。

    递归扫描目录下所有图片，按文件名中的数字排序。
    修复了 filter 命名冲突导致的排序问题。
    """
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
