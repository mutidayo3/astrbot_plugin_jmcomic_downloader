# JMComic 本子下载器

AstrBot 插件，支持通过本子 ID 从 JMComic 下载并自动转换为 PDF 发送。

## 功能特性

- 📥 **自动下载**：通过本子 ID 自动下载全部章节图片
- 📄 **PDF 转换**：使用 `img2pdf` 直传原始字节，不做有损重编码
- 🗜️ **ZIP 压缩**：支持将 PDF 打包为 ZIP 发送，并提供 AES-256 强加密保护
- 🚀 **本地缓存**：智能标题命名与持久化索引，重复请求秒发（LRU 淘汰策略）
- 🐳 **Docker 兼容**：内置 HTTP 文件服务器，完美解决跨容器文件传输问题
- ⚙️ **三种传输模式**：`auto`（自动检测）/ `local`（本地路径）/ `docker`（HTTP 服务）
- 🔒 **并发安全**：针对每个本子 ID 独立加锁（引用计数回收），防止重复下载冲突
- 🧹 **自动清理**：可选自动删除临时图片目录，节省磁盘空间
- 💬 **消息自动撤回**：状态提示在文件发送完成后集中撤回，群聊中最终只留下文件
- 🛡️ **资源保护**：图片数量双重校验 + 上传失败自动重试 + 子进程隔离下载

## 项目结构

业务逻辑拆分到 `jmcomic_core` 子包，`main.py` 只保留 AstrBot 生命周期与指令注册：

```
astrbot_plugin_jmcomic_downloader/
├── main.py                     # 插件入口：生命周期 + 指令注册
├── jmcomic_core/
│   ├── config.py               # 强类型配置封装（含边界裁剪）
│   ├── names.py                # 文件名安全化
│   ├── env.py                  # 容器检测 / 宿主机 IP / 图片收集
│   ├── fifo_semaphore.py       # FIFO 有序信号量
│   ├── file_server.py          # HTTP 文件服务器（token + 后缀白名单）
│   ├── cache.py                # PDF 缓存管理（LRU + 持久化索引）
│   ├── downloader.py           # 子进程隔离下载
│   ├── worker.py               # 下载子进程入口（只依赖标准库 + jmcomic）
│   ├── converter.py            # 图片 → PDF / PDF → ZIP
│   ├── file_sender.py          # 文件发送（local / OneBot）
│   ├── message_manager.py      # 消息发送与撤回
│   └── service.py              # 下载编排服务
├── _conf_schema.json
├── metadata.yaml
└── requirements.txt
```

`worker.py` 刻意只依赖标准库与 `jmcomic`，保证 `multiprocessing.spawn` 子进程能轻量启动，
不会重新导入 AstrBot 框架并触发插件重复注册。

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
🔐 压缩包密码: your_password
```

**缓存命中时**：
```
⏳ ...（无状态消息，直接发送文件）
```

### 指令一览

| 指令 | 权限 | 说明 |
|------|------|------|
| `/jmcomic <本子ID>` | 所有人 | 下载本子并转换为 PDF 发送 |
| `/jmcomic_cache list` | 所有人 | 查看当前 PDF 缓存（按最近使用排序，最多 15 条） |
| `/jmcomic_cache clear` | **管理员** | 清空全部 PDF 缓存 |
| `/jmhelp` | 所有人 | 显示使用帮助 |

## 配置说明

在 AstrBot WebUI → 插件配置 → **jmcomic_downloader** 中进行设置：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `transfer_mode` | 下拉选择 | `auto` | 文件传输模式：`auto` 自动检测、`local` 本地路径、`docker` HTTP 服务器 |
| `max_cache_count` | 整数 | `20` | PDF 本地缓存数量上限（0 表示不缓存），按 LRU 淘汰 |
| `max_workers` | 整数 | `4` | 下载线程数（1~8） |
| `image_format` | 下拉选择 | `jpg` | 下载图片格式：`jpg` / `webp` / `png`。**保持 `jpg`**，体积可小数倍（见 Q8） |
| `download_timeout` | 整数 | `300` | 单本下载超时时间（秒） |
| `auto_cleanup` | 布尔 | `true` | 发送后是否自动清理临时图片目录 |
| `pdf_resolution` | 浮点数 | `150.0` | PDF 分辨率 DPI（1~1200），只影响页面尺寸、不影响体积 |
| `max_cache_size_mb` | 整数 | `200` | 缓存文件大小上限（MB），超过不缓存（0 不限制） |
| `file_server_port` | 整数 | `18790` | HTTP 文件服务器端口（Docker 模式需映射到宿主机） |
| `file_server_base_url` | 字符串 | `""` | Docker 模式下文件服务器的外网地址，如 `http://172.17.0.1:18790`（只填到端口） |
| `enable_zip` | 布尔 | `false` | 是否将 PDF 压缩为 ZIP 格式发送（可防在线扫描并支持加密） |
| `zip_password` | 字符串 | `""` | ZIP 压缩包密码，留空则不加密（建议使用强密码） |
| `rate_limit_window` | 整数 | `300` | 重复请求限频窗口（秒），同一聊天同一本子只允许获取一次（0 不限制） |
| `max_image_count` | 整数 | `500` | 图片数量上限，超过时拒绝下载（0 不限制） |
| `max_concurrent` | 整数 | `1` | 同时下载的最大并发数（1~4），设为 1 时严格按请求顺序处理 |
| `debug_log` | 布尔 | `false` | 启用调试日志，输出详尽运行信息 |
| `upload_retry` | 整数 | `3` | 文件上传总尝试次数（1~10），失败按 3s/6s/12s 退避重试（见 Q9） |
| `auto_recall` | 布尔 | `true` | 自动撤回状态消息，文件发送后集中撤回，群聊中最终只留下文件 |

所有数值配置在读取时都会做类型转换与边界裁剪，填了非法值只会回退默认值，不会导致插件加载失败。

### 传输模式详解

#### `auto`（推荐）
- 插件自动检测运行环境
- 检测到自身运行在容器内 → 自动使用 `docker` 模式
- 运行在宿主机 → 自动使用 `local` 模式
- 检测只依据当前进程自身的特征（`/.dockerenv`、自身 cgroup、根文件系统类型），
  宿主机上装了 Docker 或跑着别的容器都不会误判

#### `local`
- 把 PDF 的**本地绝对路径**交给 OneBot API (`upload_group_file` / `upload_private_file`) 发送
- **适用场景**：AstrBot 与 NapCat/QQBot 在同一台机器上运行，可直接访问同一文件系统
- **优点**：无需配置端口、无需 HTTP 服务，大文件不经过网络传输，最快最简单
- **缺点**：AstrBot 与协议端文件系统隔离（分处不同容器）时无法使用

#### `docker`
- 启动内置 HTTP 文件服务器，把文件 URL 交给 OneBot API (`upload_group_file` / `upload_private_file`) 发送
- **适用场景**：AstrBot 与 NapCat 分别运行在不同 Docker 容器中，无共享文件夹
- **优点**：完美解决跨容器文件传输问题，支持大文件
- **缺点**：需要正确配置网络，让 NapCat 能访问到 AstrBot 的文件服务器

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

这是最容易出错的环节。`file_server_base_url` 必须是 **NapCat 容器能访问到的宿主机地址**，
只填到端口即可，插件会自动补上带 token 的路径前缀。

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
# 应返回 HTTP/1.1 404 Not Found（服务在监听，只是 token 不对）
```
只要不是 `Connection refused` / 超时，就说明网络是通的。

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

### 5. 文件服务器的访问控制

文件服务器必须绑定 `0.0.0.0` 才能被别的容器访问，也就意味着下载目录对整个局域网可见。
为此做了两层收敛：

- **随机 token 前缀**：文件 URL 形如 `/files/<token>/JM123-标题.pdf`，token 每次插件启动重新生成，
  插件重载后旧链接立即失效；token 不匹配一律返回 404
- **后缀白名单**：只放行 `.pdf` / `.zip`，`cache_index.json` 这类内部文件返回 403

如果你把文件服务器暴露到公网，请自行在前面加反向代理与鉴权。

## 缓存机制

- **缓存位置**：`AstrBot/data/plugin_data/jmcomic_downloader/downloads/`
- **命名规则**：优先使用 `JM{本子ID}-{本子标题}.pdf`，确保文件名包含 ID 便于检索
- **持久化索引**：内置 `cache_index.json`，重启后依然能精准命中缓存
- **淘汰策略**：LRU（最近最少使用），按文件访问时间排序，淘汰后总数恰好为 `max_cache_count`
- **命中效果**：跳过下载和 PDF 转换，直接发送，秒级响应
- **空间优化**：每本漫画仅保留一份文件，通过内存映射实现 ID 与标题的快速查找
- **启动自检**：启动时清理残留的下载临时目录、中间 PDF 与未登记的孤本 PDF

## 常见问题

### Q1: 发送文件失败，提示"文件消息缺少参数"
**A**：这是 AstrBot `File` 组件在 aiocqhttp 适配器下的已知问题。插件在 `local` / `docker` 两种模式下都会直接调用 OneBot 的 `upload_group_file` / `upload_private_file` 绕过此问题，任意 `transfer_mode` 均可。

### Q2: NapCat 提示下载文件超时
**A**：`file_server_base_url` 配置错误，NapCat 无法访问到 AstrBot 的文件服务器。请按上文【Docker 部署特别说明】排查网络。

### Q3: 如何清空缓存？
**A**：管理员发送 `/jmcomic_cache clear` 即可，也可以用 `/jmcomic_cache list` 先看看缓存了哪些。
或者把 `max_cache_count` 设为 `0` 后重启插件；直接删除 `downloads/` 目录下的文件同样可行，插件会自动同步索引。

### Q4: 为什么生成的 ZIP 无法用普通方式打开？
**A**：如果您设置了 `zip_password`，插件会使用 AES-256 算法进行加密。请使用支持 AES 加密的解压软件（如 7-Zip, WinRAR, Bandizip）并输入正确密码解压。

### Q5: 本地运行不想用 HTTP 服务
**A**：将 `transfer_mode` 设为 `local`，插件不会启动 HTTP 服务器，直接把本地绝对路径交给 OneBot 上传接口。

### Q6: 报错 `未知文件类型或路径不存在: /files/xxx.pdf`
**A**：v0.0.32 之前的版本在宿主机部署时可能出现，成因是环境检测误判成容器、走了 HTTP 模式，但没有可用的 `file_server_base_url`，导致拼出 `/files/xxx.pdf` 这种相对路径被协议端当本地路径解析。升级到 v0.0.32 即可；旧版可临时把 `transfer_mode` 手动设为 `local` 规避。

### Q7: 日志显示自动检测的模式不对
**A**：启动日志会打印判定依据，例如 `自动检测到运行环境: local (容器内=False, 依据: 未发现容器特征)`。如与实际不符，把 `transfer_mode` 显式设为 `local` 或 `docker` 即可，插件不会再自行推断。

### Q8: 生成的 PDF 体积异常大（几百 MB）
**A**：把 `image_format` 改成 `jpg`（v0.1.0 起已是默认值）。

插件用 img2pdf 生成 PDF，而 img2pdf 的原则是绝不做有损重编码：JPEG 输入会被**原字节直接嵌入**，而 WebP/PNG 因为 PDF 格式不支持，必须先解码成位图再用 Flate 无损压缩塞进去，体积会膨胀数倍。

实测同一本 220 页的本子：

| `image_format` | PDF 体积 | 每页 | 转换耗时 |
|---|---|---|---|
| `webp` | 649 MB | 2.95 MB | 数秒 |
| `jpg` | **136 MB** | 0.62 MB | **0.5 秒** |

注意 `pdf_resolution` 帮不上忙——它只控制 PDF 页面尺寸，不重新编码图片，因此不影响文件大小。

### Q9: 发送大文件时报 `[Highway] httpUpload Error ... code 102902`
**A**：这是 QQ 的 Highway 传输通道在随机位置中断，**与文件大小无关**。实测同一个 136 MB 文件三次尝试分别断在 101 MB、51 MB，第三次成功——失败点毫无规律，也不是超时（速率一直有 3~6 MB/s）。

v0.0.34 起上传失败会自动重试（`upload_retry`，默认 3 次，按 3s/6s/12s 退避），绝大多数情况下能自动传成。如果你的网络到腾讯 CDN 特别不稳，可以把 `upload_retry` 调到 5~10。

另外建议先按 Q8 把体积降下来：单次传输的字节越少，中断概率越低。

### Q10: 转 PDF 时进程被 OOM Killer 杀掉
**A**：img2pdf 会把所有图片字节留在内存里直到 PDF 写出，因此峰值内存约等于图片总体积。
v0.1.0 起不再额外自留一份字节（峰值内存直接减半），并且 `max_image_count` 会在下载前和转换前各拦一次。
内存紧张的小机器建议把 `max_image_count` 调到 300 以下，并确保 `image_format` 为 `jpg`。

## 版本历史

| 版本 | 更新内容 |
|------|----------|
| 0.1.0 | **架构重构 + 一批缺陷修复**：<br>**重构**：业务逻辑从单文件 `main.py` 拆分为 `jmcomic_core` 子包（config / names / env / cache / downloader / worker / converter / file_sender / file_server / message_manager / service），`main.py` 只保留生命周期与指令注册；配置改为强类型 `PluginConfig` 并统一做边界裁剪；新增 `/jmcomic_cache list`、`/jmcomic_cache clear`（管理员）、`/jmhelp` 指令<br>**修复**：缓存命中且文件名需要更新时，改名后的新路径未回传给主流程，导致后续「统一命名」步骤把刚改好的缓存文件 `unlink` 掉、再 `rename` 失败，最终用户收到「下载失败」且缓存被删除<br>**修复**：`max_image_count` 此前比较的是 `len(JmAlbumDetail)`（**章节数**）而非图片数，且默认 api 客户端的 `album.page_count` 恒为 0，这道资源保护实际从未生效；改为逐章累加真实图片数并提前短路，另在转 PDF 前用实际图片数精确复核<br>**修复**：`jmcomic` 的阻塞式 HTTP 查询（页数预检、缓存标题刷新）直接跑在事件循环上，网络差时会让整个 bot 卡死数十秒，改为 `asyncio.to_thread`<br>**修复**：转 PDF 前会把全部图片字节自行读入 list，而 img2pdf 内部本来就要留一份，峰值内存翻倍（实测 141 MB 图片多占 143 MB）；改为直接传 `Path` 交由 img2pdf 逐张读取<br>**修复**：本子锁在 `release()` 与下一个等待者恢复之间会被误判为空闲而从字典移除，导致同一本子被两个协程并发处理、互相 `rmtree` 掉对方的下载目录；改用引用计数回收<br>**修复**：`pdf_resolution` 填 0 会在换算页面尺寸时 `ZeroDivisionError`，`file_server_port` 越界会直接 bind 失败；所有数值配置补齐范围裁剪与非法值回退<br>**修复**：撤回消息复用「最近一次发送」的 bot 实例，多账号在线时会撤到错误的账号上；改为按事件取 bot<br>**修复**：因页数超限被拒的请求仍会占掉限频名额，用户要白等一个窗口才能重试<br>**修复**：FIFO 信号量唤醒等待者时若该等待者刚被取消，会抛 `InvalidStateError` 污染日志<br>**修复**：`keep_path` 为 `None` 时 LRU 会多淘汰一个缓存<br>**安全**：HTTP 文件服务器绑定 `0.0.0.0`，此前局域网内任何人都能拉走 `cache_index.json`（含全部本子 ID 与标题）；改为随机 token 路径前缀 + `.pdf`/`.zip` 后缀白名单 |
| 0.0.34 | **上传失败自动重试 + 澄清体积相关配置**：<br>- QQ 的 Highway 通道会在随机位置中断大文件上传（实测同一个 136 MB 文件三次尝试分别断在 101 MB、51 MB 与成功，与文件大小无关、也非超时），新增 `upload_retry` 配置项（默认 3 次，按 3s/6s/12s 退避），重试耗尽后才回退 File 组件<br>- 修正 `pdf_resolution` 的错误说明：它只通过 `img2pdf.get_layout_fun()` 控制页面尺寸，不重新编码图片，**因此不影响文件大小**<br>- 补充 `image_format` 说明：img2pdf 会把 JPEG 原字节直接嵌入，而 webp/png 必须解码成无损位图，实测同一本子 webp 生成 649 MB、jpg 仅 136 MB 且转换快上百倍。新增 FAQ Q8/Q9 |
| 0.0.33 | **移除失效的 `max_pdf_size_mb` 配置项**：该项的唯一用途——PDF 超过阈值时向用户发送"文件过大"提示——已在 v0.0.30 的消息合并改动中被删除，配置项自此空转两个版本（只被读取和打印进调试摘要，从不参与任何判断）。现一并清理 `_conf_schema.json`、`main.py` 与文档；WebUI 中该项会消失，已保存的旧值自动失效，无需手动处理 |
| 0.0.32 | **修复宿主机部署时发送文件失败（`未知文件类型或路径不存在: /files/xxx.pdf`）**：<br>- 修复容器检测误判：旧逻辑扫描 `/proc/1/mountinfo` 是否含 `docker` 字样，而宿主机只要跑过任意容器该文件就会出现 `/var/lib/docker/overlay2/...` 挂载记录，导致宿主机被判为容器<br>- 检测改为只看当前进程自身特征（标记文件、自身 cgroup 中的容器 ID、根文件系统是否为 overlay），并缓存结果，避免运行期容器状态变化导致前后判定不一致<br>- 传输模式只在初始化时解析一次并传给发送器，消除"初始化判为 local、发送时判为 docker"造成的空 `file_server_base_url`<br>- `docker` 模式缺少 `file_server_base_url` 时不再拼出 `/files/xxx.pdf` 相对路径，自动回退为本地路径发送并告警<br>- `local` 模式改用 OneBot `upload_group_file` / `upload_private_file` 直传本地绝对路径，同机部署不再经过 HTTP，大文件更快<br>- HTTP 文件服务器启动失败（如端口占用）时降级为 `local` 模式，不再让插件初始化中断<br>- 上传接口失败时回退 File 组件，群号/QQ 号改用 `get_group_id()` / `get_sender_id()` 获取 |
| 0.0.31 | **撤回机制优化**：状态消息改为任务完成后集中撤回，避免下载耗时过长时状态消息提前消失 |
| 0.0.27 | **多并发资源优化与请求顺序保证**：<br>- 新增 FIFO 有序信号量，实现严格按请求先后顺序放行，避免标准 `Semaphore` 的无序唤醒问题<br>- 新增 `max_concurrent` 配置项（默认 1），限制全局并发下载数，防止多请求同时启动子进程导致资源耗尽<br>- FIFO 信号量包裹整个下载→转换→发送流程，确保文件按请求顺序发送 |
| 0.0.26 | **文件命名规范优化**：PDF/ZIP 文件名统一为 `JM{本子ID}-{本子标题}.{pdf,zip}` 格式，便于识别和检索 |
| 0.0.25 | **PDF 转换性能与稳定性修复**：<br>- 彻底移除灾难性慢速的 DPI 重编码循环（WebP lossless 重编码 10–30s/张，260张 = 43–130 分钟）<br>- 改用 `img2pdf.get_layout_fun()` 在 PDF 层面控制页面尺寸，零额外 I/O<br>- 添加 10 分钟超时保护，防止 PDF 转换无限挂起<br>- 添加进度日志，用户可观察转换状态 |
| 0.0.24 | **边界容错与诊断增强**：<br>- 子进程增加 `SIGTERM` 信号处理，超时时尽力发送诊断信息而非静默退出<br>- 父进程根据 `exitcode` 提供精准诊断：区分信号杀死（含 OOM Killer 提示）、正常退出未返回、异常崩溃 |
| 0.0.23 | **架构与健壮性深度修复**：<br>- 将下载 worker 拆分到独立模块，解决 `multiprocessing.spawn` 重新导入主模块导致插件重复注册的隐患<br>- 用 `Pipe` 替代 `Queue` 进行进程间通信，消除 `join_thread()` 阻塞事件循环的风险<br>- 缓存锁字典不再只增不减，`finally` 中释放不再使用的锁对象<br>- 收窄裸 `except Exception` 为 `OSError`（文件 IO 操作），避免吞掉 `KeyboardInterrupt`/`SystemExit`<br>- `asyncio.get_event_loop()` 替换为 `asyncio.get_running_loop()`<br>- `img2pdf.convert` 使用 `outputstream` 参数直写文件，避免中间 bytes 对象 |
| 0.0.22 | 新增 `debug_log` 配置项，开启后输出详尽的调试日志（缓存命中、下载参数、文件操作、耗时统计等），方便排查问题 |
| 0.0.21 | **代码质量与健壮性优化**：<br>- 修复 `@register` 版本号与 `metadata.yaml` 不一致的问题<br>- 使用 `AstrBotConfig` 替代 `dict` 类型声明，支持 `save_config()` 等完整功能<br>- 修复缓存命中时文件重命名的竞态风险（`FileExistsError` 防护）<br>- 自动清理发送后的 ZIP 临时文件，避免磁盘泄漏（PDF 保留为缓存） |
| 0.0.20 | **性能与稳定性深度优化**：<br>- 引入 `pyzipper` 实现真正的 AES-256 ZIP 加密，修复标准库无法加密写入的安全漏洞<br>- 优化缓存机制：采用持久化索引 (`cache_index.json`) 和标题命名，重启后依然精准命中且磁盘零冗余<br>- 增强依赖检测：插件启动时自动校验核心库，缺失时提供友好的安装指引而非崩溃报错<br>- 配置文件规范化：使用 `options` 替代 `enum`，并拆分 `description` 与 `hint` 提升 WebUI 体验 |
| 0.0.19 | 增加 ZIP 压缩发送功能，支持密码保护；优化 PDF 生成逻辑，使用 `img2pdf` 降低内存占用 |
| 0.0.18 | 优化并发锁实现，修复竞态条件；完善 Docker 网络检测，支持自动推断宿主机 IP |
| 0.0.17 | 增加本子标题作为 PDF 文件名；优化消息发送逻辑，统一使用 `yield` 返回进度提示 |
| 0.0.16 | 增加本地 PDF 缓存功能，支持 LRU 淘汰策略；优化超时处理，防止后台线程竞争 |
| 0.0.15 | 正式发版，支持基础的 JMComic 下载与 PDF 转换功能 |

## 依赖

```
jmcomic>=2.5.0
Pillow>=9.0.0
img2pdf>=0.5.0
pyzipper>=0.3.6
aiohttp>=3.8.0
```

AstrBot 会在安装插件时自动装好 `requirements.txt`；如需手动安装：

```bash
pip install jmcomic Pillow img2pdf pyzipper aiohttp
```

## 许可证

本项目基于 [GPL-3.0](LICENSE) 许可证开源。
