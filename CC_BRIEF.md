# CC_BRIEF · 第二阶段：提速、WMS 并入、全仓审计、中文化第二批

> 起草：Cowork 会话（2026-09-23）。执行：Claude Code。
> 上一份简报是你写给 Cowork 的 `COWORK_BRIEF.md`；这一份方向反过来。

## 0. 分工与现状

**分工**

- **你（Claude Code）**：编码、测试、推送。你有原生 git，这是这次交给你的原因。
- **Cowork**：有 NAS 的 SSH 和 Telegram 客户端的操作权，负责部署到 NAS、实测、独立核验。Cowork **不能** git push（代理拒绝），所以代码一律由你推。
- **你不碰 NAS**。需要改 NAS 上的 compose、mihomo 配置、环境变量时，写进 `docs/HANDOFF.md`（见 §8），由 Cowork 执行。

**生产现状**（截至 `c19d658`）

- 分支 `claude/telegram-media-downloader-bot-samm1v`，每次 push 触发 `publish-image.yml` 构建 `ghcr.io/asukamadoka/tg_media_down_bot:latest`（amd64+arm64）。目前**没有测试门禁**。
- 543 个测试全绿。`tgmd/` 约 8,100 行。
- 用户已完成：`/claim`（admin 存在 DB 里）、PikPak 账号连接、Telegram 读取账号登录。`TGMD_LANG=zh`。
- **NAS 拓扑**（UGREEN DXP4800，Debian 12，x86_64，中国大陆电信宽带）：
  - `proxy`：mihomo TUN 模式。NAS 直连不了 Telegram，所有 bot 出网都经它走 VLESS/Hysteria2 节点。
  - `bot`：`network_mode: "service:proxy"`，共享 proxy 的网络栈。
  - `tunnel`：cloudflared，待命（用户还没有域名，在申请 eu.org）。
  - 公网入口：Tailscale Funnel `https://ugreen-nas.tail212e43.ts.net` → `127.0.0.1:8080`。
  - 数据卷：`./data:/data`（`sessions/`、`db/tgmd.sqlite3`、`downloads/`），容器内 uid 10001。
- 这套受限网络部署的来龙去脉见 `deploy/restricted-network/README.md`。

**用户已确认的决定**

1. WMS（PikPak 网盘管理主线）**并入本仓库**，作为独立子包；bot 是它的第一个前端。旧仓库 `Asukamadoka/pikpak-wms` 归档。
2. 四项全做：提速、WMS 核心、全仓审计、中文化第二批。
3. 受限内容：**能直连就直连**；如果必须下载，就落盘到 NAS，用户直接在 NAS 上观看，不必再推到云端；全程不落盘只在确实更快时才采用。

---

## 1. 红线（优先级高于本文其余所有内容）

1. **凭据绝不入库**：`.env`、`.cloudflared.env`、mihomo `config.yaml`、session string、PikPak token。测试绝不联网；`conftest.py` 已经拦截 PikPak，Telegram 一律用 fake client。
2. **部署契约不破**：现有环境变量的名字和语义、`/data` 卷布局、镜像入口 `python -m tgmd` 一律不动。新增配置必须有默认值，**旧 compose 一字不改也必须能起来**。
3. **数据库只做向前兼容的迁移**：只加表、只加列，不删表列、不改名。NAS 上的 SQLite 里有真实数据（admin 认领、PikPak token、读取账号 session），丢了就要用户重新走一遍登录。
4. **i18n 三条红线**（见 `tgmd/i18n.py` 模块文档）：命令名和参数关键词不翻译、落库的值不翻译、日志不翻译。
5. **WMS 六条铁律**（见 §5）。
6. **用户主账号的安全**：读取账号就是用户本人的主账号。并行下载默认保守；必须遵守 FloodWait；不引入任何批量加群、批量抓取的默认行为。账号被封的代价远大于慢几秒。
7. **测试全绿才推**。只有在删除某个功能时，才允许同时删除它的上游测试，并在提交信息里写明理由。
8. **拿不准就停**。遇到与本简报描述不符的情况，写进 `docs/HANDOFF.md` 的「待决问题」，不要自行发挥。

---

## 2. 阶段 0：测试门禁（最先做）

现在每次 push 都直接构建生产镜像，没有测试拦截。

- 新增 `.github/workflows/ci.yml`：在 PR 和 push 时跑 `ruff check` 和 `pytest`。
- `publish-image.yml` 只在测试通过后构建（用 `needs:` 或 `workflow_run`，选你认为更稳的）。
- 仓库没有 ruff 配置。请加上，并先把现有告警清到零。注意 E501 按**显示宽度**计算，CJK 字符占 2 列，`tgmd/i18n.py` 的中文目录会触发。

**验收**：故意弄挂一个测试，确认镜像不会被构建。

---

## 3. 阶段 1：全仓审计（先审，再改）

先产出 `docs/AUDIT.md`，再动手改代码。逐个文件审查，每条问题写清四项：位置、复现方法或推理过程、严重度、处理方式（修 / 删 / 合并 / 保留并说明理由）。总目标是**不丢功能的前提下净减行数**，审计完成后报告改动前后的行数。

下面是已知问题，**每条都要给出明确结论**：

| # | 问题 | 位置 | 建议 |
|---|---|---|---|
| A1 | `Config.validate()` 只检查环境变量。通过 `/claim` 认领、或通过 `/setup` 登录之后，每次启动仍然误报 "no admin configured" 和 "no user session configured"。 | `config.py` 约 189、201 行 | `validate` 只报告配置层面的事实，运行时状态在读取 DB 之后由 app 报告 |
| A2 | `HTTP_ENABLED=true` 但没有配公网地址时，抛 `ConfigError` 并以退出码 2 退出，容器陷入崩溃循环。部署当天踩到的第一个坑就是它。 | `config.py` 约 182 行 | 降级为 warning，关闭「Telegram 媒体转存 PikPak」这一项能力，其余功能照常启动 |
| A3 | 零测试覆盖：`tasks.py`、`delivery.py`、`downloader.py`、`verify.py`、`reporter.py`。`handlers.py` 刚有了第一个测试文件 `test_handlers_dispatch.py`。 | — | 用 fake client 补齐核心路径的测试。阶段 2 要大改这三个文件，没有测试就是盲改 |
| A4 | 职责重叠：`verify.py`(691) + `identity.py`(187) + `reporter.py`(94) | — | 用数据评估是否合并，不强制 |
| A5 | PikPak 登录有三条路：Mini App、一次性链接、聊天内输入。`portal.py`(551) + `miniapp.py`(159) | — | 评估能否收敛为「Mini App + 聊天内」两条。把一次性链接页的价值和维护成本写清楚，再决定 |
| A6 | `handlers.py`(740) 是否按命令拆分 | — | 你来判断 |
| A7 | `on_message` 里的 `if not bundle: if bundle.errors:` 依赖 `LinkBundle.__bool__` 的语义，逻辑正确，但读起来像笔误 | `handlers.py` | 改写成不需要解释的形式 |
| A8 | 认领码在未认领状态下每次启动都会打印到日志（这是设计如此） | `bootstrap.py` | 确认认领之后绝不再打印，并补一个测试 |

**验收**：交付 `AUDIT.md`，说明每条问题的处理，给出行数变化，测试全绿，并且没有任何功能在没有说明的情况下消失。

---

## 4. 阶段 2：提速（用户的核心诉求）

**现状**：Telegram 投递流程是 `download_media`（单连接顺序下载）→ 落盘 → `send_file` 重新上传。PikPak 流程是整个文件下到 NAS 硬盘 → `web.FileResponse` → PikPak 经 Funnel 从 NAS 拉一遍。**任何情况下都没有服务器端转发**。唯一的省流量路径是同一链接第二次请求时走的 `delivery.send_from_cache`。

按下面的顺序做。每一项都要有测试，并且给 Cowork 留一个实测手段。

### 2a. 转发快路（参考 tdl 的 direct → clone 自动降级）

- 先判断源消息能不能转发：看消息和所在聊天的 `noforwards` 标志。
- **可以转发 → 走零字节路径**。要注意：bot 和读取账号是两个账号，文件引用（`access_hash` / `file_reference`）按账号隔离，bot 不能直接用读取账号拿到的媒体引用。做法如下：
  1. 读取账号把消息 `forward` 到缓存频道。前提是读取账号是该频道成员并且能发言，bot 是该频道的管理员。
  2. bot 在缓存频道里拿到这条消息，用的是 bot 自己的引用。
  3. bot 用 `send_file(media)` 发给用户（这样不带「Forwarded from」头），同时写入 `media_cache`。
- **没有配置缓存频道**时，降级到 clone 路径，并在回复里提示用户「设置 `/cache` 可以秒转」。用户目前**还没有设置**缓存频道。
- **被拒**（`CHAT_FORWARDS_RESTRICTED`、`noforwards` 或其他转发错误）→ 降级到 clone。
- 测试要覆盖四个分支：可转发 / 受限 / 无缓存频道 / 转发报错。

### 2b. 并行分片下载（受限内容唯一的提速手段）

- 多连接并行调用 `upload.getFile`，每条连接拉不同的分片。**自己实现，不要复制第三方代码**。许可证的原则和之前对 SaveAny-Bot（AGPL）的约定一样：只看思路。
- 配置：`DOWNLOAD_CONNECTIONS`，默认 4，上限 8。小文件（比如小于 10 MB）用单连接。
- 必须处理这几种情况，或者明确降级回 Telethon 默认下载：`FloodWait`、`FILE_MIGRATE`（文件在其他 DC）、`file_reference` 过期后重新获取、CDN 重定向（`upload.fileCdnRedirect`）。
- 给 Cowork 一个实测手段，比如 `python -m tgmd.bench <消息链接>`，或者在日志里打印每个文件的平均速率和 `dc_id`。Cowork 会在 NAS 上对同一个受限视频分别用单连接和并行测一遍。

### 2c. 直连媒体线路（实测发现，优先级高）

Cowork 在 2026-09-23 做了如下实测：在 bot 容器里用未登录的连接调用 `help.getConfig`，拿到一份权威的 `dc_options`；然后从 NAS 宿主机**不经代理**逐个建立 TCP 连接。结果是 19 个端点里只有 3 个能连上，**而且全部是 `media_only` 端点**：

```
OK  dc4  149.154.166.111:443               media        ← IPv4，最容易用上
OK  dc2  2001:67c:4e8:f002::b:443          v6,media
OK  dc4  2001:67c:4e8:f004::b:443          v6,media
--  其余 16 个（所有非 media 端点，以及 dc1/dc3/dc5 的全部端点）
```

这意味着可以把流量**拆成两路**：控制流量（API 调用、读消息）继续走代理，**存放在 DC2 / DC4 的文件，字节流完全可以绕开代理节点直连下载**。

阻碍在 Telethon。已读源码确认（1.45.0，`telegrambaseclient.py` 的 `_get_dc`）：它挑 DC 时只匹配 `id`、`ipv6`、`cdn` 三个字段，取第一个命中项，**完全不区分 `media_only`**。所以即使直连线路是通的，bot 也用不上。

要求：

1. **先验证，再实现**。TCP 能连上不等于能持续传输，防火墙可能在握手之后重置连接。请先做一个实验开关，让 Cowork 能在 NAS 上对比同一个 DC4 文件的两种下载方式：走代理的普通端点 vs 直连的媒体端点。
2. 验证通过后，下载用的 sender 优先选择 `media_only` 端点；连接失败或超时时，自动回退到普通端点。注意两点：媒体端点与同一 DC 的普通端点共用 auth key；**文件所在 DC 恰好是账号主 DC 时**，默认走的是主连接而不是 exported sender，也需要处理。
3. 配置项 `TG_DIRECT_MEDIA=auto|off`，默认 `off`，实测通过后再改成 `auto`。
4. 在日志里记录每个文件的 `dc_id`。这样才知道用户常看的频道有多少文件在 DC2/DC4。这条优化能覆盖多少流量，完全取决于这个分布。
5. mihomo 需要加一条 `IP-CIDR,149.154.166.111/32,DIRECT,no-resolve`，放在 `MATCH` 规则之前。请更新 `deploy/restricted-network/mihomo/config.example.yaml`；NAS 上的真实配置由 Cowork 同步。IPv6 的两个端点需要容器网络开启 IPv6，目前没有开（mihomo `ipv6: false`），**作为后续项**写进 HANDOFF，这一阶段先不做。

### 2d. 智能路由（用户新需求）

用户原话的意思：受限内容如果必须下载，落盘之后就可以直接在 NAS 上看，不必再推到云端。

- 新增 `auto` 模式。默认模式不变，由用户通过 `/mode auto` 自己选择。规则是：**可转发** → 走 2a 的快路发回 Telegram；**受限** → 落盘到 NAS 媒体目录，回复访问路径。`MODES` 是落库值，按 i18n 红线，`auto` 这个值本身不翻译，并且要加进 `display_mode`。
- local 模式改进：
  - 支持把下载目录映射到 NAS 共享文件夹。新增 `MEDIA_DIR`，默认值是现在的 `DOWNLOAD_DIR`，以保证向后兼容。
  - 文件名保留原文件名。
  - `delete_after_delivery` 对 local 模式永远不生效。
  - 新增可选的 `LOCAL_URL_PREFIX`（比如 `smb://10.10.10.2/<共享名>/`），回复里给出可以直接复制的路径。
- 媒体目录的宿主机路径由 Cowork 和用户确定，写进 HANDOFF 的待决问题，你不要猜。

### 2e. 流式与流水线

先讲清楚现实：**不落盘本身不会让下载变快**。瓶颈是从 Telegram 取字节的带宽。不落盘的价值在于：下载和上传可以重叠（整体耗时最多缩短到接近一半），并且节省一次完整的等待。SaveAny-Bot 的文档也承认了它的 stream 模式不能多线程、更慢、更易失败，所以**并行下载（2b）的优先级高于流式**。

- **PikPak**：在 webserver 里加一个流式端点。PikPak 请求签名 URL 时，按 Range 直接从 Telegram 取对应分片转发，不落盘（思路参考 TG-FileStreamBot）。必须支持 Range（PikPak 可能多段并发拉取）、HEAD、准确的 `Content-Length`。配置项 `PIKPAK_STREAM`，**默认关闭**，实测通过后再打开。落盘模式保留为回退。
- **Telegram clone 路径**：评估能否边下边传。媒体的大小事先已知，`upload.saveBigFilePart` 可以按分片上传。如果复杂度过高，就保留「下完再传」，但必须用上 2b 的并行下载。在 AUDIT 或 HANDOFF 里写明选择和理由。
- **local 路径**：按定义就是要落盘。落盘就是它的目的。

**阶段 2 验收**（由 Cowork 在 NAS 上实测）：可转发频道的视频几秒内返回，并且日志里没有下载记录；受限频道的视频走并行下载并落到媒体目录，实际速率有前后对比的数字；DC4 文件在开启 `TG_DIRECT_MEDIA` 前后的速率有对比数字。

---

## 5. 阶段 3：WMS 并入（主线）

**来源**：`Asukamadoka/pikpak-wms` 的 `main` 分支停在 M0。已有的内容：`pikpak_wms/config.py`、`core/models.py`、`store/schema.sql`、`cli/main.py`（12 个命令的骨架，其中只有 `version` 和 `doctor` 能跑）；`config/wms.example.yaml`、`config/rules.example.yaml`；`docs/ARCHITECTURE.md`、`ROADMAP.md`、`REFERENCES.md`。这些设计文档是本阶段的规格，请先迁到 `docs/wms/`，再动手写代码。

**六条铁律**（来自原设计，所有 WMS 代码按它评审）

1. **先计划，后执行**。所有写操作默认 dry-run，只打印将要发生的变更；显式 `--apply` 才真正执行。
2. **删除永远可逆**。默认只进回收站。永久删除需要独立开关加二次确认，并且**不得出现在任何定时任务的默认配置里**。
3. **规则即配置**。业务逻辑写在 YAML 里，代码只提供匹配器和动作原语。
4. **本地索引先行**。先把目录树同步进 SQLite，规则在本地索引上求值。
5. **幂等 + 全审计**。每个动作写入审计表，包括动作前的快照。任务重跑不产生重复副作用。
6. **分层不跳层**。CLI、Web、Bot 都只调用 `ops` 层。

**布局**

- 新增顶层包 `pikpak_wms/`，与 `tgmd/` 并列。**`pikpak_wms` 不得 import `tgmd`**；`tgmd` 只通过 `pikpak_wms.ops` 调用它。
- **PikPak 客户端怎么来**：`pikpak_wms.core.client` 接收一个已登录的 `PikPakApi` 实例（或者一个 token provider 回调）。`tgmd` 装配时注入 `PikPakService.client(...)`，复用用户已经连接的账号。CLI 单独运行时，按原设计从环境变量或 token 文件登录。
- **存储**：独立的 SQLite 文件 `/data/db/wms.sqlite3`，避免和 bot 的表迁移耦合。
- 依赖（typer、rich、pydantic、apscheduler 等）加进 requirements。请留意镜像体积。

**里程碑（用户要求全部上线）**

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M1 | `core`：鉴权、令牌桶限流（默认 4 req/s）、异常收敛。`store`：`files` / `tasks` / `audit` 三张表。盘点：全量 + 增量。CLI：`login` / `stocktake` / `ls` / `quota` | 上千文件的目录树完整同步；二次盘点明显快于首次，给出具体数字 |
| M2 | 规则引擎：8 种匹配器、7 种动作，每种动作都有 `plan()` 和 `apply()`。五个业务模块：inbound / layout / organize / cleanup / outbound。定时调度（APScheduler）。审计。`undo <audit_id>` | 写一份 rules.yaml → dry-run 输出 Plan → apply 生效 → 审计可回溯 → undo 能撤销 rename / move，能从回收站还原 |
| M3 | 同一个镜像。`python -m tgmd` 启动时按配置启用 WMS 调度。`docker compose run --rm bot wms ...` 能跑 CLI | 重启不丢 token，也不丢索引 |
| M4 | Web 面板，用 Telegram Mini App 实现，复用 `miniapp.py` 的 initData 校验做身份认证，只对 admin 开放。功能：待执行 Plan 列表、确认执行、审计列表 | 在手机 Telegram 里打开面板，确认一个 Plan，执行后审计里有记录 |
| M5 | Bot 命令族 `/wms`：stocktake / plan / apply / undo / rules / status。**入库之后自动上架**：磁力、分享链接、TG 媒体转存 PikPak 成功后，按规则生成 Plan。默认是 dry-run 通知加一键确认按钮，用户可以在配置里打开自动 apply | 在手机上转发一条磁力链接，文件按规则落到正确目录 |

**提过但没写成规格的功能**（本次一并做：先在 `docs/wms/` 里写规格，再实现）

- 按 hash 去重：输出重复文件组，动作是把多余副本进回收站（默认 dry-run）
- 归档
- 基于 `events` 接口的增量盘点
- 出库推送给外部下载器（aria2 RPC，可选）
- `star` / `share` 动作

**已知风险**

- `file_rename` 没有批量接口，只能并发加限流。几百个文件的批量重命名会不会触发风控，需要实测；给 Cowork 留一个可以控制批量大小的 dry-run 入口。
- `pikpakapi` 没有声明许可证。作为运行时依赖引入，风险可控。
- 仓库是私有的，但 GHCR 镜像是公开的。**镜像里的代码等于公开**，不要在代码里写任何不应公开的东西。

---

## 6. 阶段 4：中文化第二批

范围：`setup.py`、`portal.py`、`verify.py`，加上约 76 条异常消息。这些异常消息实际上是用户直接看到的文案。请在阶段 1 审计、合并模块之后再做，避免翻译两遍。

- **异常的处理方式**：异常类携带 `key + kwargs`，在展示给用户时调用 `t()`；`str(exc)` 保持英文，供日志使用。这样用户看到中文，日志保持英文（红线）。
- `portal.py` 里的 Mini App 文案嵌在 JS 字符串中，注入时要用 JSON 编码。
- `<html lang="en">` 有两处硬编码。
- `verify.py` 还有 CLI 输出路径：中文是双宽字符，`ljust` 会导致列对不齐。
- **WMS 新增的所有用户文案从一开始就走 `t()`**，不要先写英文后补翻译。
- `tests/test_i18n.py` 的占位符一致性测试和命令名不翻译测试必须继续通过。

---

## 7. 顺序

```
0 CI 门禁
1 审计 AUDIT.md → 修复与瘦身
2 提速：2a 转发快路 → 2b 并行下载 → 2c 直连媒体线路（先实验） → 2d 智能路由 → 2e 流式
3 WMS：M1 → M2 → M3 → M4 → M5
4 中文化第二批
```

阶段之间有依赖：阶段 2 要大改 `tasks` / `delivery` / `downloader`，所以这三个文件必须先在阶段 1 被测试覆盖；阶段 4 要等阶段 1 的模块合并完成。

---

## 8. 每个阶段怎么交付

1. 推送。提交信息里写验收证据：测试数量、行数变化、基准数据。
2. 在 `docs/HANDOFF.md` 追加一节给 Cowork 看的部署说明：
   - NAS 上要改哪些环境变量、compose、mihomo 规则
   - 用户需要在 Telegram 里做什么
   - 怎么回滚
   - 待决问题（你拿不准、需要用户或 Cowork 决定的事）
3. 如果新增了需要 Cowork 实测的东西（基准命令、实验开关），把用法写进 HANDOFF。

**Cowork 会怎么核验**：从远端重新 checkout 代码跑全套测试；在 NAS 上 pull 镜像、按 HANDOFF 改配置、重启、核对日志；用真实链接实测。实测清单见 §4 和 §5 各阶段的验收项。

---

## 9. 参考

- [iyear/tdl](https://docs.iyear.me/tdl/guide/forward/)：`forward` 默认走 direct（官方转发），遇到不允许转发的聊天或消息就自动降级到 clone（下载后重传）。2a 的教科书实现。
- [krau/SaveAny-Bot](https://github.com/krau/SaveAny-Bot)：AGPL-3.0，**只看架构，不复制代码**。它的文档承认 stream 模式不能多线程、更慢、更易失败，非 stream 模式默认 4 线程。
- [EverythingSuckz/TG-FileStreamBot](https://github.com/EverythingSuckz/TG-FileStreamBot)：给 Telegram 文件生成 HTTP 直链，边读边传。2e 的 PikPak 流式端点参考它的思路。
- Telethon 1.45.0 `client/telegrambaseclient.py` 的 `_get_dc`，以及 `client/downloads.py` 里的 exported sender 逻辑：2c 要改造的位置。
