"""JMComic 插件工具函数。

提供 Docker 运行环境检测、宿主机 IP 获取、图片文件收集等纯函数。
"""

import functools
import os
import re
import socket
from pathlib import Path

# cgroup 路径中必须出现容器 ID 才算容器内。
# 匹配 /docker/<id>、/system.slice/docker-<id>.scope、/kubepods/...、/lxc/<name>，
# 但不匹配宿主机上名字里带 docker 的 systemd 单元（如 docker.service）。
_CONTAINER_CGROUP_RE = re.compile(
    r'/(?:docker|containerd|crio|libpod|podman)[/-][0-9a-f]{12,}'
    r'|/kubepods'
    r'|/lxc/'
)


@functools.lru_cache(maxsize=1)
def _detect_container() -> tuple[bool, str]:
    """检测当前进程是否运行在容器内，返回 (结果, 判定依据)。

    只依据【当前进程自身】的特征判断，绝不检查宿主机的全局状态：
    - /.dockerenv、/run/.containerenv 标记文件（最可靠）
    - 自身 / PID 1 的 cgroup 路径中的容器 ID（cgroup v1、kubepods）
    - 根文件系统为 overlay（无标记文件的精简镜像兜底）
    - 环境变量 container

    【历史坑】旧版本会扫描 /proc/1/mountinfo 中是否含 "docker" 字样，
    但宿主机只要跑过任意容器，该文件就会出现 /var/lib/docker/overlay2/...
    挂载记录，于是宿主机被误判为容器，插件转而走 HTTP 传输模式，
    最终因缺少 file_server_base_url 而发送失败。切勿恢复该检查。
    """
    # 1. 容器运行时标记文件（Docker / Podman，最可靠）
    for marker in ('/.dockerenv', '/run/.containerenv'):
        if os.path.exists(marker):
            return True, f"存在标记文件 {marker}"

    # 2. 自身与 PID 1 的 cgroup 路径中带容器 ID（cgroup v1 / Kubernetes）
    for cgroup_file in ('/proc/self/cgroup', '/proc/1/cgroup'):
        try:
            with open(cgroup_file, 'r') as f:
                for line in f:
                    # 格式: hierarchy-ID:controller-list:cgroup-path
                    path = line.rstrip('\n').split(':', 2)[-1]
                    if _CONTAINER_CGROUP_RE.search(path):
                        return True, f"{cgroup_file} 含容器 cgroup 路径: {path}"
        except Exception:
            continue

    # 3. 根文件系统为 overlay（cgroup v2 容器常见，宿主机通常是 ext4/xfs/btrfs）
    try:
        with open('/proc/self/mountinfo', 'r') as f:
            for line in f:
                # 格式: ... mount-point options [optional fields] - fstype source superopts
                left, sep, right = line.partition(' - ')
                if not sep:
                    continue
                fields = left.split()
                if len(fields) > 4 and fields[4] == '/' and right.split()[0] == 'overlay':
                    return True, "根文件系统为 overlay"
    except Exception:
        pass

    # 4. 环境变量（systemd-nspawn / Podman / LXC 会设置）
    env_container = os.environ.get('container', '')
    if env_container in ('docker', 'podman', 'oci', 'lxc', 'containerd'):
        return True, f"环境变量 container={env_container}"

    return False, "未发现容器特征"


def is_running_in_docker() -> bool:
    """当前进程是否运行在容器内（结果全程缓存，避免前后判定不一致）。"""
    return _detect_container()[0]


def container_detection_reason() -> str:
    """容器检测的判定依据，仅用于日志排查。"""
    return _detect_container()[1]


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
