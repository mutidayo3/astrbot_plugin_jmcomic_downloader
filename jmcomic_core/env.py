"""运行环境检测与文件系统工具。

提供容器检测、宿主机 IP 推断、图片收集等纯函数，全部无副作用、可独立测试。
"""

from __future__ import annotations

import functools
import os
import re
import socket
from pathlib import Path

# cgroup 路径中必须出现容器 ID 才算容器内。
# 匹配 /docker/<id>、/system.slice/docker-<id>.scope、/kubepods/...、/lxc/<name>，
# 但不匹配宿主机上名字里带 docker 的 systemd 单元（如 docker.service）。
_CONTAINER_CGROUP_RE = re.compile(
    r"/(?:docker|containerd|crio|libpod|podman)[/-][0-9a-f]{12,}"
    r"|/kubepods"
    r"|/lxc/"
)


@functools.lru_cache(maxsize=1)
def _detect_container() -> tuple[bool, str]:
    """检测当前进程是否运行在容器内，返回 ``(结果, 判定依据)``。

    只依据**当前进程自身**的特征判断，绝不检查宿主机的全局状态。

    【历史坑】旧版本会扫描 /proc/1/mountinfo 中是否含 "docker" 字样，
    但宿主机只要跑过任意容器，该文件就会出现 /var/lib/docker/overlay2/...
    挂载记录，于是宿主机被误判为容器。切勿恢复该检查。
    """
    # 1. 容器运行时标记文件（Docker / Podman，最可靠）
    for marker in ("/.dockerenv", "/run/.containerenv"):
        if os.path.exists(marker):
            return True, f"存在标记文件 {marker}"

    # 2. 自身与 PID 1 的 cgroup 路径中带容器 ID（cgroup v1 / Kubernetes）
    for cgroup_file in ("/proc/self/cgroup", "/proc/1/cgroup"):
        try:
            with open(cgroup_file, encoding="utf-8") as f:
                for line in f:
                    path = line.rstrip("\n").split(":", 2)[-1]
                    if _CONTAINER_CGROUP_RE.search(path):
                        return True, f"{cgroup_file} 含容器 cgroup 路径: {path}"
        except OSError:
            continue

    # 3. 根文件系统为 overlay（cgroup v2 容器常见，宿主机通常是 ext4/xfs/btrfs）
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            for line in f:
                left, sep, right = line.partition(" - ")
                if not sep:
                    continue
                fields = left.split()
                if len(fields) > 4 and fields[4] == "/" and right.split()[0] == "overlay":
                    return True, "根文件系统为 overlay"
    except OSError:
        pass

    # 4. 环境变量（systemd-nspawn / Podman / LXC 会设置，变量名本就是小写）
    env_container = os.environ.get("container", "")  # noqa: SIM112
    if env_container in ("docker", "podman", "oci", "lxc", "containerd"):
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
            # 连接公网地址以触发路由表选择本地出口 IP（不会真正发包）
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        if local_ip.startswith(("192.168.", "10.", "172.")):
            return local_ip
    except OSError:
        pass

    # 备选方案：尝试常见的 Docker 网关
    for gateway in ("172.17.0.1", "172.18.0.1", "192.168.65.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                if s.connect_ex((gateway, file_server_port)) == 0:
                    return gateway
        except OSError:
            continue

    return "127.0.0.1"


def collect_image_files(image_dir: Path) -> list[Path]:
    """递归收集并按文件名中的数字排序图片文件。"""
    if not image_dir or not image_dir.exists():
        return []

    image_files = [
        f
        for f in image_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in {".webp", ".jpg", ".jpeg", ".png", ".gif"}
    ]

    def sort_key(p: Path) -> tuple[int, str]:
        digits = "".join(ch for ch in p.stem if ch.isdigit())
        return (int(digits) if digits else 0, p.stem)

    return sorted(image_files, key=sort_key)
