# JMComic 本子下载器

AstrBot 插件，支持通过本子 ID 从 JMComic 下载并自动转换为 PDF 发送。

## 功能特性

- 📥 **自动下载**：通过本子 ID 自动下载全部章节图片
- 📄 **PDF 转换**：使用 `img2pdf` 流式处理，低内存占用且高效
- 🗜️ **ZIP 压缩**：支持将 PDF 打包为 ZIP 发送，并提供 AES-256 强加密保护
- 🚀 **本地缓存**：智能标题命名与持久化索引，重复请求秒发（LRU 淘汰策略）
- 🐳 **Docker 兼容**：内置 HTTP 文件服务器，完美解决跨容器文件传输问题
- ⚙️ **三种传输模式**：`auto`（自动检测）/ `local`（本地路径）/ `docker`（HTTP 服务）
- 🔒 **并发安全**：针对每个本子 ID 独立加锁，防止重复下载冲突
- 🧹 **自动清理**：可选自动删除临时图片目录，节省磁盘空间
- 💬 **消息自动撤回**：状态提示在文件发送完成后集中撤回，群聊中最终只留下文件

## 配置说明

在 AstrBot WebUI → 插件配置 → **jmcomic_downloader** 中进行设置：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `transfer_mode` | 下拉选择 | `auto` | 文件传输模式：`auto` 自动检测、`local` 本地路径、`docker` HTTP 服务器 |
| `max_cache_count` | 整数 | `20` | PDF 本地缓存数量上限（0 表示不缓存），按 LRU 淘汰 |
| `max_workers` | 整数 | `4` | 下载线程数（1~8） |
| `image_format` | 下拉选择 | `webp` | 下载图片格式：`webp` / `jpg` / `png` |
| `download_timeout` | 整数 | `300` | 单本下载超时时间（秒） |
| `auto_cleanup` | 布尔 | `true` | 发送后是否自动清理临时图片目录 |
| `pdf_resolution` | 浮点数 | `150.0` | PDF 分辨率 DPI |
| `max_pdf_size_mb` | 整数 | `100` | PDF 大小警告阈值（MB），超过会提示 |
| `max_cache_size_mb` | 整数 | `200` | 缓存文件大小上限（MB），超过不缓存（0 不限制） |
| `file_server_port` | 整数 | `18790` | HTTP 文件服务器端口（Docker 模式需映射到宿主机） |
| `file_server_base_url` | 字符串 | `""` | Docker 模式下文件服务器的外网访问地址，如 `http://172.17.0.1:18790` |
| `enable_zip` | 布尔 | `false` | 是否将 PDF 压缩为 ZIP 格式发送（可减小体积并支持加密） |
| `zip_password` | 字符串 | `""` | ZIP 压缩包密码，留空则不加密（建议使用强密码） |
| `rate_limit_window` | 整数 | `300` | 重复请求限频窗口（秒），同一聊天同一本子只允许获取一次（0 不限制） |
| `max_image_count` | 整数 | `500` | 图片数量上限，超过时拒绝下载（0 不限制） |
| `max_concurrent` | 整数 | `1` | 同时下载的最大并发数（1~4），设为 1 时严格按请求顺序处理 |
| `debug_log` | 布尔 | `false` | 启用调试日志，输出详尽运行信息 |
| `auto_recall` | 布尔 | `true` | 自动撤回状态消息，文件发送后集中撤回，群聊中最终只留下文件 |

### 传输模式详解

#### `auto`（推荐）
- 插件自动检测运行环境
- 检测到 Docker 容器 → 自动使用 `docker` 模式
- 非 Docker 环境 → 自动使用 `local` 模式

#### `local`
- 直接使用本地文件路径发送 PDF
- **适用场景**：AstrBot 与 NapCat/QQBot 在同一台机器、同一用户下运行，可直接访问文件系统
- **优点**：无需配置端口、无需 HTTP 服务，最简单
- **缺点**：Docker 环境下会因文件系统隔离导致发送失败

#### `docker`
- 启动内置 HTTP 文件服务器，通过 OneBot API (`upload_group_file` / `upload_private_file`) 发送文件
- **适用场景**：AstrBot 与 NapCat 分别运行在不同 Docker 容器中，无共享文件夹
- **优点**：完美解决跨容器文件传输问题，支持大文件
- **缺点**：需要正确配置网络，让 NapCat 能访问到 AstrBot 的文件服务器

## 使用方式

在群聊或私聊中发送：

```
/jmcomic <本子ID>
```

**示例**：
```
/jmcomic 422866
```

**回复示例**：
```
📥 正在下载本子 422866，完成后自动转换为 PDF...
📥 ... (文件直接发送)
🔐 解压密码: your_password
```

**缓存命中时**：
```
⏳ ...（无状态消息，直接发送文件）
```

## Docker 部署特别说明

### 1. 端口映射

AstrBot 的 `docker-compose.yml` 或 `docker run` 必须暴露文件服务器端口：

```yaml
services:
  astrbot:
    image: soulter/astrbot:latest
    ports:
      - "6185:6185"
      - "18790:18790"   # 必须：HTTP 文件服务器端口
    volumes:
      - ./data:/AstrBot/data
```

### 2. 配置 `file_server_base_url`

这是最容易出错的环节。`file_server_base_url` 必须是 **NapCat 容器能访问到的宿主机地址**。

**获取方法**：

```bash
# 在宿主机执行，查看 Docker 网桥 IP
ip addr show docker0 | grep inet
# 通常输出：inet 172.17.0.1/16 scope global docker0
```

然后填入插件配置：
```json
{
  "file_server_base_url": "http://172.17.0.1:18790"
}
```

**验证 NapCat 能否访问**：
```bash
docker exec -it <napcat容器名> curl -I http://172.17.0.1:18790/files/
# 应返回 HTTP/1.1 404 Not Found（服务正常，只是目录下没文件）
```

### 3. iptables 防火墙问题

如果 NapCat 无法访问，可能是宿主机防火墙拦截了 Docker 容器到宿主机的流量：

```bash
# 放行 Docker 网段访问宿主机 18790 端口
sudo iptables -A INPUT -i docker0 -p tcp --dport 18790 -j ACCEPT

# 保存规则（Debian/Ubuntu）
sudo apt-get install -y iptables-persistent
sudo netfilter-persistent save
```

### 4. 使用宿主机局域网 IP（备选）

如果 `172.17.0.1` 不通，可直接使用宿主机的局域网 IP：

```bash
# 获取局域网 IP
hostname -I
# 输出示例：192.168.1.100 172.17.0.1
```

配置改为：
```json
{
  "file_server_base_url": "http://192.168.1.100:18790"
}
```

## 缓存机制

- **缓存位置**：`AstrBot/data/plugin_data/jmcomic_downloader/downloads/`
- **命名规则**：优先使用 `JM{本子ID}-{本子标题}.pdf`，确保文件名包含 ID 便于检索
- **持久化索引**：内置 `cache_index.json`，重启后依然能精准命中缓存
- **淘汰策略**：LRU（最近最少使用），按文件访问时间排序
- **命中效果**：跳过下载和 PDF 转换，直接发送，秒级响应
- **空间优化**：每本漫画仅保留一份文件，通过内存映射实现 ID 与标题的快速查找

## 常见问题

### Q1: 发送文件失败，提示"文件消息缺少参数"
**A**：这是 AstrBot `File` 组件在 aiocqhttp 适配器下的已知问题。请确保 `transfer_mode` 设置为 `auto` 或 `docker`，插件会自动使用 OneBot API 绕过此问题。

### Q2: NapCat 提示下载文件超时
**A**：`file_server_base_url` 配置错误，NapCat 无法访问到 AstrBot 的文件服务器。请按上文【Docker 部署特别说明】排查网络。

### Q3: 如何清空缓存？
**A**：直接删除 `AstrBot/data/plugin_data/jmcomic_downloader/downloads/` 目录下的文件，或把 `max_cache_count` 设为 `0` 后重启插件。插件会自动同步更新缓存索引。

### Q4: 为什么生成的 ZIP 无法用普通方式打开？
**A**：如果您设置了 `zip_password`，插件会使用 AES-256 算法进行加密。请使用支持 AES 加密的解压软件（如 7-Zip, WinRAR, Bandizip）并输入正确密码解压。

### Q5: 本地运行不想用 HTTP 服务
**A**：将 `transfer_mode` 设为 `local`，插件不会启动 HTTP 服务器，直接使用本地文件路径发送。

## 版本历史

| 版本 | 更新内容 |
|------|----------|
| 0.0.31 | **撤回机制优化**：状态消息改为任务完成后集中撤回，避免下载耗时过长时状态消息提前消失
| 0.0.27 | **多并发资源优化与请求顺序保证**：<br>- 新增 `_FIFOSemaphore` 类，实现严格按请求先后顺序放行的有序信号量，避免标准 `Semaphore` 的无序唤醒问题<br>- 新增 `max_concurrent` 配置项（默认 1），限制全局并发下载数，防止多请求同时启动子进程导致资源耗尽<br>- FIFO 信号量包裹整个下载→转换→发送流程，确保文件按请求顺序发送 |
| 0.0.26 | **文件命名规范优化**：<br>- PDF/ZIP 文件名统一为 `JM{本子ID}-{本子标题}.{pdf,zip}` 格式，便于识别和检索 |
| 0.0.25 | **PDF 转换性能与稳定性修复**：<br>- 彻底移除灾难性慢速的 DPI 重编码循环（WebP lossless 重编码 10–30s/张，260张 = 43–130 分钟）<br>- 改用 `img2pdf.get_layout_fun()` 在 PDF 层面控制页面尺寸，零额外 I/O<br>- 直接读取图片二进制内容传给 img2pdf，彻底解决路径类型兼容导致的 `TypeError`<br>- 添加 10 分钟超时保护，防止 PDF 转换无限挂起<br>- 添加进度日志，用户可观察转换状态 |
| 0.0.24 | **边界容错与诊断增强**：<br>- 子进程增加 `SIGTERM` 信号处理，超时时尽力发送诊断信息而非静默退出<br>- 父进程根据 `exitcode` 提供精准诊断：区分信号杀死（含 OOM Killer 提示）、正常退出未返回、异常崩溃<br>- 移除 `img2pdf.convert` 冗余的 `layout_fun` 参数（与默认行为等价） |
| 0.0.23 | **架构与健壮性深度修复**：<br>- 将下载 worker 拆分到独立模块 `_download_worker.py`，解决 `multiprocessing.spawn` 重新导入主模块导致插件重复注册的隐患<br>- 修复原地修改图片的副作用：处理 `format=None`、GIF/WebP 质量损失、磁盘写入中断损坏原图等问题（原子写入 + 格式感知质量参数）<br>- 用 `Pipe` 替代 `Queue` 进行进程间通信，消除 `join_thread()` 阻塞事件循环的风险<br>- 缓存锁字典不再只增不减，`finally` 中释放不再使用的锁对象<br>- 收窄裸 `except Exception` 为 `OSError`（文件 IO 操作），避免吞掉 `KeyboardInterrupt`/`SystemExit`<br>- `asyncio.get_event_loop()` 替换为 `asyncio.get_running_loop()`（Python 3.10+ 弃用警告）<br>- `assert` 运行时类型检查替换为显式 `isinstance + raise`<br>- `img2pdf.convert` 使用 `outputstream` 参数直写文件，避免中间 bytes 对象 |
| 0.0.22 | 新增 `debug_log` 配置项，开启后输出详尽的调试日志（缓存命中、下载参数、文件操作、耗时统计等），方便排查问题 |
| 0.0.21 | **代码质量与健壮性优化**：<br>- 修复 `@register` 版本号与 `metadata.yaml` 不一致的问题<br>- 使用 `AstrBotConfig` 替代 `dict` 类型声明，支持 `save_config()` 等完整功能<br>- 让 `pdf_resolution` 配置真正生效：通过 PIL 为图片添加 DPI 元数据，使 img2pdf 正确识别分辨率<br>- 修复缓存命中时文件重命名的竞态风险（`FileExistsError` 防护）<br>- 自动清理发送后的 ZIP 临时文件，避免磁盘泄漏（PDF 保留为缓存）<br>- 移除未使用的导入 (`gc`, `zipfile`, `PIL.Image`)、成员变量 (`_served_files`) 和方法 (`_cleanup_files`) |
| 0.0.20 | **性能与稳定性深度优化**：<br>- 引入 `pyzipper` 实现真正的 AES-256 ZIP 加密，修复标准库无法加密写入的安全漏洞<br>- 优化缓存机制：采用持久化索引 (`cache_index.json`) 和标题命名，重启后依然精准命中且磁盘零冗余<br>- 增强依赖检测：插件启动时自动校验核心库，缺失时提供友好的安装指引而非崩溃报错<br>- 移除手动 GC 调用与不安全的锁清理逻辑，显著提升异步环境下的运行效率与稳定性<br>- 配置文件规范化：使用 `options` 替代 `enum`，并拆分 `description` 与 `hint` 提升 WebUI 体验 |
| 0.0.19 | 增加 ZIP 压缩发送功能，支持密码保护；优化 PDF 生成逻辑，使用 `img2pdf` 进行流式处理以降低内存占用 |
| 0.0.18 | 优化并发锁实现，修复竞态条件；完善 Docker 网络检测，支持自动推断宿主机 IP |
| 0.0.17 | 增加本子标题作为 PDF 文件名；优化消息发送逻辑，统一使用 `yield` 返回进度提示 |
| 0.0.16 | 增加本地 PDF 缓存功能，支持 LRU 淘汰策略；优化超时处理，防止后台线程竞争 |
| 0.0.15 | 正式发版，支持基础的 JMComic 下载与 PDF 转换功能 |
