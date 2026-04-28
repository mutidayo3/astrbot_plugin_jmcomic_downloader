# JMComic 本子下载器

AstrBot 插件，支持通过本子 ID 从 JMComic 下载并自动转换为 PDF 发送。

## 功能特性

- 📥 **自动下载**：通过本子 ID 自动下载全部章节图片
- 📄 **PDF 转换**：自动将图片合并为单份 PDF 文件
- 🚀 **本地缓存**：支持 PDF 缓存，重复请求秒发（LRU 淘汰策略）
- 🐳 **Docker 兼容**：内置 HTTP 文件服务器，完美解决跨容器文件传输问题
- ⚙️ **三种传输模式**：`auto`（自动检测）/ `local`（本地路径）/ `docker`（HTTP 服务）
- 🔒 **并发安全**：同一本子同时只允许一个下载任务
- 🧹 **自动清理**：可选自动删除临时图片目录，节省磁盘空间

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
| `file_server_port` | 整数 | `18790` | HTTP 文件服务器端口（Docker 模式需映射到宿主机） |
| `file_server_base_url` | 字符串 | `""` | Docker 模式下文件服务器的外网访问地址，如 `http://172.17.0.1:18790` |

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
📥 开始下载本子 422866...
✅ 下载完成 (50 页)，正在转换为 PDF...
📚 本子 422866 处理完成，正在发送文件...
```

**缓存命中时**：
```
📦 命中本地缓存，直接发送本子 422866...
📚 正在发送缓存的本子 422866...
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

- **缓存位置**：`AstrBot/data/plugin_data/jmcomic_downloader/downloads/{album_id}.pdf`
- **淘汰策略**：LRU（最近最少使用），按文件访问时间排序
- **命中效果**：跳过下载和 PDF 转换，直接发送，秒级响应
- **数量控制**：通过 `max_cache_count` 控制，超过上限自动删除最旧的缓存
- **临时文件**：下载的原始图片目录仍受 `auto_cleanup` 控制，发送后自动删除

## 常见问题

### Q1: 发送文件失败，提示"文件消息缺少参数"
**A**：这是 AstrBot `File` 组件在 aiocqhttp 适配器下的已知问题。请确保 `transfer_mode` 设置为 `auto` 或 `docker`，插件会自动使用 OneBot API 绕过此问题。

### Q2: NapCat 提示下载文件超时
**A**：`file_server_base_url` 配置错误，NapCat 无法访问到 AstrBot 的文件服务器。请按上文【Docker 部署特别说明】排查网络。

### Q3: 如何清空缓存？
**A**：直接删除 `AstrBot/data/plugin_data/jmcomic_downloader/downloads/` 目录下的 `.pdf` 文件，或把 `max_cache_count` 设为 `0` 后重启插件。

### Q4: 本地运行不想用 HTTP 服务
**A**：将 `transfer_mode` 设为 `local`，插件不会启动 HTTP 服务器，直接使用本地文件路径发送。

## 版本历史

| 版本 | 更新内容 |
|------|----------|
| 0.0.15 | 正式发版 |
