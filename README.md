# TG 频道转发（tg-forwarder）

[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-xiaoyu96%2Ftg--forwarder-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/xiaoyu96/tg-forwarder)
[![Docker Pulls](https://img.shields.io/docker/pulls/xiaoyu96/tg-forwarder)](https://hub.docker.com/r/xiaoyu96/tg-forwarder)
[![Image Size](https://img.shields.io/docker/image-size/xiaoyu96/tg-forwarder/latest)](https://hub.docker.com/r/xiaoyu96/tg-forwarder/tags)
[![Platform](https://img.shields.io/badge/platform-amd64%20%7C%20arm64-2496ED)](https://hub.docker.com/r/xiaoyu96/tg-forwarder/tags)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

把某个 Telegram 频道的**新消息**，按**关键词正则**过滤后，自动转发到另一个频道。
**事件驱动**：消息一到就转（秒级），不做定时轮询；**带一个中文 Web 面板**，手机上也能改配置。

```
来源频道  ──(白名单/黑名单正则过滤)──►  目标频道
   新消息进来立刻判断，相册（多图/多视频+一条文字）整组原样转发，不会被拆成散条
```

> 面板：`http://<主机IP>:9020` ，初始密码 `tgforward`（进去后在「④ 面板设置」里随时改）

---

## 功能

- **多规则**：每条规则 = 来源频道 → 目标频道 + 白名单/黑名单正则，可随时增删、单独启用/停用；
- **相册整组转发**：一条消息含多张图/多个视频时，整组一起转，保持原来的聚合格式；
- **两种转发方式**：转发原消息（保留媒体/预览）或只发文本+链接（最轻量）；
- **实时记录**：③ 最近记录是服务端推送（SSE），有转发/跳过/失败立刻出现，不用刷新；
- **记录自动清理**：可设「最多保留 N 条」（200/500/1000/3000/不限），超了自动清理，也能一键清空；
- **面板设置**：改密码（立即生效）、恢复码（忘记密码自助重置）、退出面板 / 退出 Telegram 登录；
- **只读预览**：「预览会转什么」列出最近消息会不会被转发，不发送任何东西。

---

## 快速开始（Docker，复制粘贴即可）

**镜像地址（点这里直达 Docker Hub）**：<https://hub.docker.com/r/xiaoyu96/tg-forwarder>

| | |
|---|---|
| 镜像 | `xiaoyu96/tg-forwarder:latest` |
| 可用 tag | `latest`（跟主分支最新）/ `1.0.2`（当前发布版）/ `1.0.1` / `1.0.0`；都由同一份源码构建 |
| 架构 | `linux/amd64`、`linux/arm64`（x86 服务器 / 群晖、树莓派等 arm 设备都能跑） |
| 面板端口 | `9020` |
| 手动拉取 | `docker pull xiaoyu96/tg-forwarder:latest` |

### 1️⃣ 建目录，把下面这段存成 `docker-compose.yml`

```bash
mkdir -p tg-forwarder && cd tg-forwarder
```

```yaml
services:
  tg-forwarder:
    image: xiaoyu96/tg-forwarder:latest
    container_name: tg-forwarder
    restart: unless-stopped
    ports:
      - "9020:9020"                    # 想换端口就改左边，如 "19020:9020"
    environment:
      - TZ=Asia/Shanghai
      - WEB_PASSWORD=tgforward         # 面板初始密码（首次启动生效）
      - SECRET_KEY=please-change-me    # 改成一串随机字符，容器重启后不用重新登录面板
      # 可选：把 Telegram API 凭据用环境变量给（首次启动生效；也可留空，之后在面板/配置文件里填）
      # - TG_API_ID=1234567
      # - TG_API_HASH=0123456789abcdef0123456789abcdef
    volumes:
      - ./data:/data                   # 配置 / TG 会话 / 转发记录都在这里，别删
```

### 2️⃣ 启动

```bash
docker compose up -d
docker compose logs -f --tail=50       # 看到「消息监听已注册」就是好了；Ctrl+C 只退出看日志，不影响运行
```

### 3️⃣ 打开面板

`http://<主机IP>:9020` ，初始密码 = 上面填的 `WEB_PASSWORD`（默认 `tgforward`）。
然后按「第 0 步」准备 Telegram API 凭据 →「第 1 步」登录你自己的账号。

**更新到最新版**：

```bash
docker compose pull && docker compose up -d      # data/ 不动，登录状态和规则都保留
```

> 不用 compose 的话，等价的单条命令：
>
> ```bash
> docker run -d --name tg-forwarder --restart unless-stopped \
>   -p 9020:9020 -e TZ=Asia/Shanghai \
>   -e WEB_PASSWORD=tgforward -e SECRET_KEY=please-change-me \
>   -v "$PWD/data:/data" \
>   xiaoyu96/tg-forwarder:latest
> ```
>
> 不想装 Python、不想构建 —— 全部东西都在镜像里。

### 第 0 步（必须）：准备 Telegram API ID / Hash

本工具用你自己的 Telegram 账号（MTProto 用户会话）读写频道，所以需要一对 `api_id` / `api_hash`：

1. 浏览器打开 <https://my.telegram.org> → 用你的手机号登录 → **API development tools**；
2. 随便填个应用名，创建后会给你 `api_id`（数字）和 `api_hash`（32 位字符串）；
3. 二选一填进本工具：

```bash
# 方式 A（推荐）：在 compose 同目录建 .env，写进去（首次启动时生效）
cat > .env <<'EOF'
TG_API_ID=1234567
TG_API_HASH=0123456789abcdef0123456789abcdef
WEB_PASSWORD=你的初始面板密码
SECRET_KEY=随便一串随机字符
EOF
docker compose up -d
```

```jsonc
// 方式 B：直接编辑 ./data/config.json 的 tg 段（改完点一下面板里的「发送验证码」即可，不用重启）
{ "tg": { "api_id": 1234567, "api_hash": "0123456789abcdef0123456789abcdef", "phone": "" } }
```

### 第 1 步：登录你自己的 Telegram 账号（只需一次）

面板第①块：填手机号（国际格式，如 `+8613800000000`）→ 点「发送验证码」（验证码发到你**现在的 Telegram** 里）→ 填验证码 → 「完成登录」。
账号开了两步验证时，会提示再填一次密码。**会话保存在本机 `./data/`，重启容器不用重登。**

### 第 2 步：加转发规则

面板第②块：填 名称 / 来源频道 ID / 目标频道 ID → 需要过滤就在「高级」里写白名单、黑名单正则 → 「💾 保存并生效」。

> **登录的账号必须已经在来源频道里，并且在目标频道有发言权限**，否则日志里会看到「无法解析」或转发失败。

### 第 3 步（可选）：先「预览会转什么」

每张卡片有「预览会转什么」（只读，不发送任何东西）和「测试这条」（真的转一条）。配好正则后建议先预览。

---

## 面板说明

### ② 转发规则

| 字段 | 说明 |
|---|---|
| 名称 | 备注，随便起 |
| 启用 | 关掉后这条规则不工作（配置保留） |
| 来源频道 ID | 要监听哪个频道，如 `-1001234567890`（频道 ID 一律 `-100` 开头，私密频道也是） |
| 目标频道 ID | 转发到哪里，如 `-1001234567890` |
| 转发方式 | 「转发原消息」保留媒体/预览（推荐）；「只发文本+链接」最轻量 |
| 白名单正则（高级） | 每行一条，**留空＝不过滤、全部转发**；命中任意一条才转发 |
| 黑名单正则（高级） | 每行一条，命中任意一条就跳过（优先级高于白名单） |
| 媒体重传（高级） | 目标频道禁止转发时，勾上会**下载再重新上传**媒体（大文件慢，受 `media_max_mb` 限制） |

- 匹配范围：消息**正文 + 文件名**；相册取**整组**文字/文件名；
- **默认忽略大小写**，要严格区分就写 `(?-i)FC2`；
- 标准 Python 正则，写错了保存时会直接告诉你哪一条有问题；
- 保存是**按 id 合并**的：只影响你改的那张卡片，**不会因为浏览器里的卡片少而把别处的规则冲掉**；删除必须点卡片里的「删除」。

### ③ 最近记录（实时推送 + 自动清理）

标题右边显示 `实时 ● · 132 条 / 上限 500`：

- **`实时 ●`** 绿色＝正在推送；**`连接断开，正在重连…`** 红色＝断线了，浏览器会自己重连（这时才出现「刷新」按钮）；
- **清空记录**：一键清空列表（只清记录，不影响转发和规则；其它打开着的页面也会同时变空）；
- **记录最多保留**：`200 / 500（默认）/ 1000 / 3000 / 不限`，选完立即生效，超过上限自动只保留最新的这些条。

### ④ 面板设置（密码 / 恢复码 / 退出）

| 功能 | 说明 |
|---|---|
| **修改面板密码** | 填当前密码 + 新密码两遍 → 立即生效（不用重启）；其它设备/浏览器上的登录会失效，需要用新密码重登；同时会**生成一个新的恢复码** |
| **恢复码** | 忘记密码时用它自助重置。**只显示一次，请抄下来存好**；改密码或「重新生成恢复码」后旧的立即作废 |
| **退出面板登录** | 只是退出这个网页面板，**不影响 Telegram 登录和转发规则** |

**忘记密码了怎么办**：登录页点「忘记密码？」→

1. 有恢复码：填恢复码 + 新密码 → 立刻重置（并显示新的恢复码）；
2. 没有恢复码：在**运行容器的宿主机**上跑兜底脚本（不依赖面板、不依赖恢复码）：

```bash
bash ./reset_panel_password.sh            # 随机生成新密码并打印
bash ./reset_panel_password.sh 我的新密码   # 指定新密码
# 数据目录不在脚本旁边时：TG_CONFIG=/你的路径/data/config.json bash ./reset_panel_password.sh
```

它会打印新密码 + 新恢复码，并在数据目录留一份 `config.json.bak-时间戳` 备份，立即生效、不用重启容器。

---

## 配置（`./data/config.json`）

容器首次启动会自动生成；改完**立即生效**（规则/密码类不用重启，`tg` 段改完点一次「发送验证码」即可）：

| 键 | 默认 | 说明 |
|---|---|---|
| `web.password` | `tgforward` | 面板密码（请在面板里改，别直接编辑） |
| `web.recovery_hash` / `recovery_salt` | — | 恢复码的加盐哈希（不可反查，忘了就用兜底脚本） |
| `web.pw_ver` | `1` | 改密码计数：自增后其它设备的旧会话立即失效 |
| `tg.api_id` / `tg.api_hash` | 环境变量 `TG_API_ID` / `TG_API_HASH` | Telegram API 凭据 |
| `tg.phone` | — | 上次登录用的手机号（面板里自动回填） |
| `rules` | `[]` | 转发规则数组（面板里改） |
| `media_max_mb` | `1800` | 媒体重传的大小上限（MB） |
| `log_max_lines` | `500` | 最近记录保留条数（0=不限） |
| `debug` | 关闭 | 打开后容器日志会打印每条收到的消息（排查用）：`docker compose logs -f` |

---

## 从源码构建 / 本地开发

```bash
git clone https://github.com/yq487900/tg-forwarder.git && cd tg-forwarder
bash build_local.sh                                            # 一键构建并起个测试实例
# 等价于：
docker compose -f docker-compose.build.yml up -d --build       # 本地镜像 tg-forwarder:local
```

构建出来的实例**刻意和正式部署错开**，可以同时跑：容器 `tg-forwarder-local`、端口 `9025`、数据目录 `./data-local`（绝不碰正式部署的 `./data`）。

> 国内网络慢：在同目录建 `.env` 写 `PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`。

**改了代码怎么发布**（本项目的标准流程）：

```bash
git commit -am "说明改了什么" && git push          # 推 main → GitHub Actions 自动构建多架构镜像
```

CI 跑完（约 2~4 分钟）镜像就绪，发布流程到此为止。部署机想升级时再执行
`docker compose pull && docker compose up -d`（见「运维」，`data/` 不动，登录状态和规则都保留），
这一步属于日常运维、不是发布流程里必须的验证环节。

CI 只在代码有变化时才构建（纯文档改动不触发）；发大版本时打个 tag：`git tag v1.0.1 && git push origin v1.0.1`
—— 推 tag 会构建**对应版本号**的镜像，并**自动创建一个同名的 GitHub Release**（正文按 commit 自动生成）。

目录结构：

```
app/main.py             # Flask 面板 + Telethon 后台线程（事件驱动转发、相册聚合、SSE 日志）
app/templates/          # index.html（面板）/ login.html（登录 + 忘记密码）
Dockerfile              # python:3.13-slim + telethon/flask（版本固定）
docker-compose.yml      # 用户侧：直接用现成镜像
docker-compose.build.yml# 开发侧：从源码构建（容器/端口/数据目录与正式部署错开）
build_local.sh          # 一键本地构建 + 起测试实例
reset_panel_password.sh # 忘了密码、又没有恢复码时的兜底脚本（宿主机执行）
```

## 运维

```bash
docker compose logs -f --tail=100     # 看日志
docker compose restart                # 重启
docker compose down                   # 停掉（./data 保留，随时 up -d 起回来）
docker compose pull && docker compose up -d   # 更新到 Docker Hub 上的最新镜像
docker compose up -d --build          # 改过源码后重建（配合 -f docker-compose.build.yml）
```

- 数据（`config.json`、TG 会话 `tg.session`、转发记录 `forward.log`）都在 `./data/`，建议 600 权限、只在本机；
- **彻底删除**：`docker compose down` 后删掉整个目录（会一起删掉登录会话）；
- **备份/回档**：直接备份 `./data/` 目录即可。

## 安全说明（请按实情评估）

这是一个**单用户、自用**的局域网小工具，设计上假设它只在内网/可信网络里跑：

- 面板走 **HTTP**，没有 HTTPS —— 别直接暴露到公网；要外网用请套一层反向代理（HTTPS）或 VPN；
- 面板密码以**明文**存在 `./data/config.json`（文件权限 600，只在本机）；恢复码只存加盐哈希，不可反查；
- 已做的防护：登录/恢复码连续失败会逐步延迟（防爆破）、改密码后其它设备的会话立即失效、Cookie `HttpOnly` + `SameSite=Lax`（顺带挡掉跨站 POST）；
- 没有做 CSRF token、没有多用户/角色概念 —— 有人能进面板就等于能用你的 TG 会话。

## 常见问题

| 现象 | 处理 |
|---|---|
| 面板一直显示「未登录」，点发送验证码报错 | 先确认第 0 步的 `api_id` / `api_hash` 填了 |
| 记录里写「无法解析 xxx」 | 登录的账号没加入该频道（来源必须已加入；目标必须有发言权限） |
| 「转发原消息失败…改用文本」 | 来源频道禁止转发（内容保护）。要带媒体就勾「媒体重传」 |
| 「该消息没有文本内容」 | 纯媒体且没有说明，而转发方式是「只发文本」→ 改用「转发原消息」 |
| 「跳过：未命中白名单」 | 白名单正则没匹配上（记录里会写命中了哪条） |
| 相册被拆成散条 | 1.0.1 起，转发前会按 `grouped_id` 回服务端把整组补全，分片晚到也不会拆；历史已散着发出去的消息只能手工整理 |
| 整组相册漏转（源里有、目标没转） | 1.0.2 起加了**补偿扫描**：按水位线 `min_id` 回扫源频道，把事件推送漏掉的组补转。默认每 5 分钟一次，可在配置里改 `reconcile_seconds`（0=关闭）。手动干预见下方「水位线运维」 |
| 收不到验证码 | 确认手机号带国家码（`+86…`）；验证码发到你**已登录的 Telegram** 会话里；连续点「发送验证码」会让旧验证码失效 |
| 容器重启后要重新登录面板 | `.env` 里没设 `SECRET_KEY`（会话签名密钥），设一串随机字符即可 |

### 水位线运维（1.0.2）

补偿扫描靠 `data/state.json` 记录「每个源频道已处理到的最大消息 id」。正常情况下自动维护，
不需要手工碰；只有当水位线被异常抬高、漏掉的组再也补不回来时，才需要人工回退：

```bash
# 查看当前水位线
docker exec tg-forwarder python /app/state_tool.py show

# 强制把某源的水位线设回某个 id（可回退，绕过「只增」逻辑）
docker exec tg-forwarder python /app/state_tool.py set -1003436294431 1640

# 删除该源的水位线（下次扫描重新初始化，不回补历史）
docker exec tg-forwarder python /app/state_tool.py reset -1003436294431
```

> `set` 之后，下次补偿扫描（或重启容器）会把水位线之上的所有消息重新对账一遍，
> 命中规则的组会补转。属于运维手段，请谨慎使用。

## 许可

MIT License，见 [LICENSE](LICENSE)。仅供个人自用与学习，请遵守 Telegram 的服务条款与当地法律法规。
