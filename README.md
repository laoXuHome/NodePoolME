# NodePoolME

[![build](https://github.com/laoXuHome/NodePoolME/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/laoXuHome/NodePoolME/actions/workflows/docker-publish.yml)
[![image](https://img.shields.io/badge/ghcr.io-nodepoolme-2496ED?logo=docker&logoColor=white)](https://github.com/laoXuHome/NodePoolME/pkgs/container/nodepoolme)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

多机场节点管理、检测与统一订阅系统。一个自包含的 Web 服务：管理多个机场订阅，本地缓存节点池，按可配置的检测组验证节点，最后输出唯一一个 `/sub/all` 统一订阅。

> 不依赖 Subs-Check-Pro、Sub-Store 或任何第三方订阅转换服务。单个 Docker 容器即可运行，SQLite 存业务数据，前端由后端直接托管。

## 目录

- [特性](#特性)
- [架构](#架构)
- [快速开始](#快速开始)
- [环境变量](#环境变量)
- [使用流程](#使用流程)
- [默认检测组](#默认检测组)
- [节点命名与标识](#节点命名与标识)
- [统一订阅](#统一订阅)
- [检测引擎与调度](#检测引擎与调度)
- [批次完整性与发布保护](#批次完整性与发布保护)
- [备份、恢复与升级](#备份恢复与升级)
- [本地开发](#本地开发)
- [API 一览](#api-一览)
- [首版范围与限制](#首版范围与限制)
- [License](#license)

## 特性

- **多机场、独立周期**：每个机场独立拉取（按分钟/小时间隔，或每天固定时刻），支持手动拉取、失败重试、拉取历史。
- **拉取与检测解耦**：检测只读取 SQLite 本地缓存，永远不会因为检测而重新拉取机场订阅。
- **失败保护**：下载失败、空内容、解析失败时旧缓存原样保留；新数据先完整解析校验，再事务性替换。
- **指纹去重**：按协议 + 服务器 + 端口 + 关键参数生成指纹，同名不同参数不会被误合并；默认只在同一机场内去重。
- **延迟检测决定下发**：每轮对全部节点跑一次可配置 URL（默认 `http://www.google.com/generate_204`）的延迟探测，只有延迟不超过上限（默认 500ms）的节点才进入 `/sub/all`。
- **检测组只负责命名**：默认 ChatGPT(G)、Telegram(TG)、YouTube(Y)，可在 UI 增删改自定义检测组；命中哪个组就在节点名里加对应代号，**不参与是否下发的判定**。
- **真实 HTTP 语义检测**：内置 Mihomo 内核，为每个节点生成独立 SOCKS 入口，检测拿到真实状态码与响应体，而不是只做 TCP 握手。
- **随机化与相似节点分散**：每轮重新打乱，按同 IP / 同网段交错排列，配合全局并发、单组并发、同 IP、同网段四层限流。
- **批次完整性**：所有启用检测组处理完本轮目标节点后才算完整批次，完整后才生成并原子替换订阅；中断、崩溃不发布半成品。
- **唯一统一订阅**：`/sub/all`，Token 保护，支持 Clash/Mihomo YAML、sing-box JSON、通用 Base64 三种格式，不暴露机场原始 URL。
- **动态名称标识**：`机场名.检测标识 原始名称`，改机场名不必重写节点记录。
- **可运维**：仪表盘、日志、系统设置、数据库备份/恢复、健康检查、重启自动恢复。

## 架构

```
机场订阅 1..N
    ↓  独立周期 / 手动 / 重试
订阅拉取器  ──→ 下载 → 解析 → 校验 → 事务性写入
    ↓
节点解析器（ss / vmess / vless / trojan / hysteria2 / tuic / socks / http + Clash YAML 导入）
    ↓
SQLite 本地节点池（指纹去重）
    ↓  只读缓存
检测调度器（随机化 + 相似分散 + 四层并发 + 请求间隔）
    ↓
默认检测组 G / TG / Y   +   自定义检测组
    ↓
检测结果与历史（连续失败计数、可用性状态）
    ↓
完整批次汇总（延迟探测 + 检测组） → 按延迟上限下发筛选 → 动态名称标识
    ↓
Clash YAML / sing-box JSON / Base64（格式校验后原子替换）
    ↓
/sub/all  （Token 保护）
    ↓
Clash / Mihomo / sing-box / v2rayN 等客户端
```

三个模块只通过数据库与文件交换状态：**拉取**、**检测**、**输出**互不阻塞。

## 快速开始

### Docker Compose（推荐）

```bash
mkdir -p /opt/nodepool/{data,config,logs,backup}
cd /opt/nodepool
curl -fsSLO https://raw.githubusercontent.com/laoxuhome/NodePoolME/main/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/laoxuhome/NodePoolME/main/.env.example -o .env
# 编辑 .env：设置 NODEPOOL_ADMIN_PASSWORD、NODEPOOL_SECRET、NODEPOOL_IMAGE
docker compose up -d
```

打开 `http://<主机IP>:8080`，用 `.env` 中的管理密码登录。

### docker run

```bash
docker run -d --name nodepool --restart unless-stopped \
  -p 8080:8080 \
  -e NODEPOOL_ADMIN_PASSWORD='change-me' \
  -e NODEPOOL_SECRET='long-random-string' \
  -v /opt/nodepool/data:/data \
  -v /opt/nodepool/config:/config \
  -v /opt/nodepool/logs:/logs \
  -v /opt/nodepool/backup:/backup \
  ghcr.io/laoxuhome/nodepoolme:latest
```

### 镜像

| 来源 | 地址 |
| --- | --- |
| GHCR（GitHub Actions 构建，多架构） | `ghcr.io/laoxuhome/nodepoolme:latest` |
| 版本标签 | `ghcr.io/laoxuhome/nodepoolme:v1.0.0`、`sha-xxxxxxx` |

支持 `linux/amd64` 与 `linux/arm64`（含 Mihomo 内核）。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NODEPOOL_DATA_DIR` | `/data`（镜像内） | 数据根目录，SQLite 与订阅文件所在 |
| `NODEPOOL_CONFIG_DIR` | `/config` | 配置与 Mihomo 工作目录 |
| `NODEPOOL_LOG_DIR` | `/logs` | 轮转日志 |
| `NODEPOOL_BACKUP_DIR` | `/backup` | 备份文件 |
| `NODEPOOL_ADMIN_PASSWORD` | `nodepool` | 首次启动的管理密码（登录后可在 UI 修改） |
| `NODEPOOL_SECRET` | 内置默认值 | 会话签名密钥，生产环境务必修改 |
| `NODEPOOL_PORT`（compose） | `8080` | 宿主端口 |
| `NODEPOOL_TEST_ENGINE` | `mihomo` | `mihomo` 全协议检测；`tcp` 仅连通性，用于无内核环境 |
| `NODEPOOL_MIHOMO_BIN` | `/usr/local/bin/mihomo` | 内核路径 |
| `NODEPOOL_MIHOMO_API` | `http://127.0.0.1:9090` | 内核控制接口（仅监听回环） |
| `NODEPOOL_MIHOMO_BASE_PORT` | `31000` | 每个节点 SOCKS 入口起始端口（仅回环） |
| `NODEPOOL_UA` | `clash.meta/... NodePoolME/1.0` | 拉取订阅时使用的 UA |

持久化目录：

```
/opt/nodepool/
├── config/     # 配置、Mihomo 工作目录（可重建）
├── data/       # nodepool.db（SQLite）、sub/（订阅输出）
├── logs/       # nodepool.log（5MB×3 轮转）
└── backup/     # 手动/自动备份、恢复前安全副本
```

容器重建不会丢数据；镜像升级不清除机场、缓存、检测历史与订阅设置。

## 使用流程

1. **添加机场**：机场管理 → 添加机场，填写名称与订阅地址，设置拉取周期（或每天定时），保存后点「立即拉取」。
2. **等待缓存**：拉取成功会显示节点数；失败会保留旧缓存并在列表中显示失败原因。
3. **确认检测组**：默认已有 G / TG / Y。需要时新增自定义组（名称、URL、标识、方法、超时、状态码、响应内容、最大延迟、周期）。
4. **运行检测**：仪表盘「立即检测」，或等待调度按批次间隔自动运行。检测进度实时显示。
5. **查看节点池**：按机场、协议、检测组、结果筛选，查看每个节点的各检测项延迟与历史。
6. **复制订阅**：订阅输出页复制 `/sub/all` 地址到客户端；需要固定格式时加 `&format=clash|singbox|base64`。

## 默认检测组

| 名称 | 标识 | 目标 | 成功条件 |
| --- | --- | --- | --- |
| ChatGPT | `G` | `https://chatgpt.com/` | 状态码 200–399 |
| Telegram | `TG` | `https://telegram.org/` | 状态码 200–399 |
| YouTube | `Y` | `https://www.youtube.com/generate_204` | 状态码 204 |

Google 与 Gemini **不**默认启用，需要时通过「新增检测组」添加。自定义组可使用任意标识，标识顺序按「排序值、ID」稳定输出。

一个组是否通过不影响其他组；停用的组不参与命名，其标识也不出现在名称中。组结果与下发无关，只决定名称里的代号。

## 节点命名与标识

```
机场名称.检测标识 原始节点名称
```

默认顺序 `G.TG.Y`，自定义标识按排序值追加在后面：

| 检测结果 | 输出名称 |
| --- | --- |
| 三项全过 | `机场A.G.TG.Y 香港01` |
| 仅 ChatGPT | `机场A.G 日本02` |
| Telegram + YouTube | `机场B.TG.Y 新加坡01` |
| 增加自定义 CL 且通过 | `机场A.G.TG.Y.CL 香港02` |

- 标识随结果动态变化，检测失败即移除该标识；原始节点名称始终不改写。
- 改机场名只影响输出组合，不动节点记录。
- 同一客户端配置内名称自动去重（重复时追加 `#2`）。

## 统一订阅

- 只有一个地址：`/sub/all`，**不会**按检测项目拆成多个订阅。
- 访问保护：`?token=<订阅Token>` 或 `/sub/all/<token>`，Token 可在订阅输出页修改/重新生成。
- 下发规则：以「延迟检测」为准——检测链接、期望状态码、超时、延迟上限都在系统设置里可改；延迟 ≤ 上限且探测成功的节点才下发。检测组命中与否只影响名称代号。
- 另可叠加：最大节点数、连续失败过滤（关闭「包含不可用」时）。
- 内容不含机场原始订阅 URL，也不含机场凭据。
- 格式：

| 参数 | 格式 | 说明 |
| --- | --- | --- |
| `format=clash`（默认给 Clash UA） | Mihomo / Clash YAML | `proxies` + `proxy-groups`（选择/自动测速）+ `MATCH` |
| `format=singbox` | sing-box JSON | `mixed` 入站 + selector / urltest 出站 |
| `format=base64` | 通用 Base64 节点列表 | 供 v2rayN 等客户端导入 |

不带参数时按 `User-Agent` 自动选择：含 `sing-box` → JSON，含 `clash`/`mihomo`/`meta` → YAML，其余 → Base64。

新订阅先写入临时文件，格式校验通过后再原子替换；客户端始终读到上一版完整订阅，直到新版发布完成。

## 定时下发

系统设置里可开启「定时下发」，两种方式任选：

- **每天固定时刻**（默认 `04:30`）：到点触发。
- **固定间隔**（默认 180 分钟，最小 5 分钟）：保存设置或上一次下发成功后开始倒计时，到点触发。

触发后按顺序执行：

1. 暂停机场拉取：调度器跳过本轮拉取，手动拉取返回提示；已在途的拉取会先等它结束（最长等 120 秒）。
2. 对全部节点重新跑一遍延迟检测与节点标签检测（强制批次，不依赖已有缓存时间）。
3. 批次完整后原子替换三份订阅文件；发布失败不覆盖上一版。

固定时刻模式下，到点后 15 分钟为有效窗口：上一轮还在跑则下一分钟再试；批次或发布失败也在窗口内重试，成功才记为当天已完成（每天一次）。

固定间隔模式下，失败会在下一分钟重试，成功后重新开始计时；失败重试不会把整个间隔浪费掉。改动任何定时下发设置都会清空下次执行时间，按新配置从当前时刻重新计时。手动「立即检测」「重建订阅」不受影响。

## 检测引擎与调度

### 引擎

- **mihomo（默认）**：容器内启动 Mihomo 内核，按工作块（每 200 个节点）生成配置，为块内每个节点绑定一个回环 SOCKS 监听口，然后并发发起真实 HTTP 请求，记录状态码、响应体与耗时。
  - 支持 ss / vmess / vless(含 REALITY) / trojan / hysteria2 / tuic / socks5 / http。
  - 内核只监听 `127.0.0.1`，不对外暴露任何端口。
- **tcp（降级）**：内核不可用时自动回退，只验证 `server:port` 能否建立 TCP 连接（不校验状态码/响应体，日志会标注）。也可用 `NODEPOOL_TEST_ENGINE=tcp` 强制启用。

### 默认限流参数

| 项目 | 默认 | 可配置范围 |
| --- | --- | --- |
| 单检测组并发 | 5 | 1–20（系统设置） |
| 全局并发 | 10 | 1–300，优先级最高 |
| 同 IP 并发 | 1 | 1–10 |
| 同 /24 并发 | 2 | 1–20 |
| 随机请求间隔 | 200–800 ms | 0–10000 ms |
| 单次检测超时 | 8 s | 1–60 s |
| 检测批次间隔 | 30 min | 1–1440 min |
| 相似分散粒度 | /24 | /32、/24、/16、/8 |
| 连续失败阈值 | 3 | 1–20 |
| 历史保留 | 30 天 | 7–365 天 |

调度顺序：每轮取节点 → 打乱 → 按分散键分桶 → 桶间轮转交错（相同/相近地址不会连续出现）→ 受四层信号量约束 → 每次请求前随机间隔。域名节点按主机名分桶，IP 节点按网段分桶。

### 失败处理

- 单次失败**不删除**节点。
- 连续失败达到阈值标记 `unavailable`，任意一次成功立即恢复 `available` 并清零计数。
- 错误分类：`timeout`、`connect`、`status`、`content`、`slow`、`engine`。
- 检测历史写入 `results`，按保留期自动清理。

## 批次完整性与发布保护

1. 一轮检测创建批次，记录目标数（节点数 × 参与组数）。
2. 每个启用组处理块内全部目标；成功/失败/超时都算「已处理」，不会因为一个节点失败无限等待。
3. 所有参与组完成后批次才 `complete`，随后汇总最近一次结果、计算标识、筛选节点、生成三种格式并校验。
4. 校验成功才原子替换线上文件；任一步失败则保留旧订阅并记录错误。
5. 进程崩溃或重启时，遗留 `running` 批次自动标记 `aborted`，上一版订阅继续可用。
6. 若本轮没有任何节点合格且上一版订阅非空，**跳过发布**并告警，避免把空订阅推给客户端。

## 备份、恢复与升级

**备份**

- UI：系统设置 → 「创建备份」/「下载数据库」。
- 命令行：`docker exec nodepool python -c "import sqlite3; s=sqlite3.connect('/data/nodepool.db'); b=sqlite3.connect('/backup/manual.db'); s.backup(b)"`

**恢复**

- UI：系统设置 → 选择 `.db` 文件 → 「上传并恢复」。会校验 SQLite 表结构，覆盖前把旧库保存为 `backup/pre-restore-<时间>.db`，恢复后自动重建订阅。

**升级**

```bash
cd /opt/nodepool
docker compose pull
docker compose up -d
```

数据卷不变，配置与历史全部保留。升级后可在订阅输出页「立即重新生成」以套用新版本逻辑。

## 本地开发

```bash
# 后端
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
NODEPOOL_DATA_DIR=./data NODEPOOL_TEST_ENGINE=tcp \
  .venv/bin/uvicorn backend.main:app --reload --port 8080

# 前端（Vite 开发服务器，代理 /api 到 8080）
cd frontend && npm install && npm run dev

# 生产构建（输出到 backend/static，由后端托管）
cd frontend && npm run build
```

测试：

```bash
python tests/test_parsers.py        # 解析、指纹、输出格式
python tests/test_engine_live.py    # 需要 /tmp/mihomo 内核，验证真实 HTTP 检测链路
```

## API 一览

所有管理接口需登录 Cookie（`/api/auth/login`），订阅接口使用订阅 Token。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` `/logout` `/api/auth/password` | 登录 / 退出 / 改密码 |
| GET | `/api/dashboard` | 统计、检测组通过数、运行状态、日志 |
| GET/POST/PATCH/DELETE | `/api/airports[/{id}]` | 机场增删改查 |
| POST | `/api/airports/{id}/fetch` | 立即拉取 |
| GET | `/api/airports/{id}/logs` | 拉取历史 |
| GET | `/api/nodes` | 搜索、筛选、排序、分页 |
| GET | `/api/nodes/{id}` | 节点详情与检测历史 |
| POST | `/api/nodes/{id}/test` | 手动检测单个节点 |
| GET/POST/PATCH/DELETE | `/api/groups[/{id}]` | 检测组管理 |
| POST | `/api/run` | 手动触发批次（可指定组） |
| GET | `/api/batches` `/api/runtime` | 批次历史 / 实时状态 |
| GET/PATCH | `/api/subscription` | 订阅设置与链接 |
| POST | `/api/subscription/rebuild` | 立即重新生成 |
| GET | `/api/subscription/preview?fmt=` | 预览输出 |
| GET/PATCH | `/api/settings` | 调度参数 |
| GET | `/api/logs` | 系统日志 |
| POST/GET | `/api/system/backup` `/download` `/restore` | 备份 / 下载 / 恢复 |
| GET | `/sub/all?token=...&format=...` | 统一订阅 |
| GET | `/healthz` | 健康检查 |

## 首版范围与限制

**包含**：多机场管理、独立拉取、缓存与去重、三种默认检测组、自定义检测组、随机化与分散、四层并发、连续失败保护、完整批次与原子发布、动态标识、唯一统一订阅、三种输出、备份恢复、健康检查、多架构镜像。

**暂不包含**：下载/上传速度测试、ISP 与流媒体解锁检测、复杂节点评分、多订阅输出、第三方订阅转换、独立 Redis/MySQL、多用户权限体系。

**已知限制**：

- 单进程单容器设计，面向个人/小规模使用；数千节点时检测批次会变长（每个工作块需要重载内核配置）。
- 相似分散只能降低集中请求风险，无法识别共用机房、反代或同一上游出口。
- 输出格式兼容性以 Clash/Mihomo、sing-box、v2rayN 实测为准，其他客户端可能需要额外适配。
- 检测通过 ≠ 服务一定可用（目标站点风控、地区策略会变化），建议同时观察延迟与连续失败。

## License

MIT
