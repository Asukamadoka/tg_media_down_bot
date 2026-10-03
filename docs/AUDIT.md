# AUDIT · 阶段 1 全仓审计

对应 `CC_BRIEF.md` §3。审计基线是提交 `fdd04ae`：`tgmd/` 共 8,171 行，`tests/` 共 4,085 行，545 个测试全部通过，行覆盖率 61%。

每条问题写四项：**位置**、**复现或推理**、**严重度**、**处理**。严重度的口径：

- **高**：会丢数据、泄露凭据，或者让功能在生产上静默失效。
- **中**：用户能看到的错误，或者会慢慢吃掉 NAS 磁盘。
- **低**：代码卫生、误导性文案、只在罕见路径上出现的问题。

处理方式有四种：**修**、**删**、**合并**、**保留**（写明理由）。

---

## 一、简报点名的已知问题（A1–A8）

### A1 · 认领或登录之后，每次启动仍然误报「没有管理员」「没有读取账号」

- **位置**：`tgmd/config.py` `Config.validate()`（原 187、200 行）；`tgmd/verify.py` `check_access_control()`、`connect_user()`
- **推理**：`validate()` 在 `app.run_app()` 里、打开数据库**之前**调用，只看环境变量和会话文件。通过 `/claim` 认领的管理员存在 `kv.runtime_admin_ids`，通过 `/setup telegram` 登录的会话存在 `kv.user_session_string`，它都看不见。NAS 上这两项都是在聊天里完成的，所以每次启动日志都有两条假警告。
  同一个毛病在 `python -m tgmd.verify` 里更严重：`check_access_control` 会把「没有管理员」判成 **失败**（退出码 1），`connect_user` 会把读取账号判成「未配置」。
- **严重度**：中。bot 本身能跑，但日志会误导排查，`verify` 会直接给出错误结论。
- **处理**：**修**。
  - `validate()` 只报告配置层面的事实，删掉这两条运行时状态的警告。
  - 运行时状态本来就已经在读库之后报告了：`clients.start_clients()` 在确实没有读取账号时警告，`bootstrap.announce_claim()` 在确实没有管理员时打印认领码。所以删掉就是修好，不需要再加代码。
  - `verify` 打开数据库后先调用 `bootstrap.load_runtime_settings()`，再检查访问控制；读取账号改用 `clients.user_session_source()`，与 bot 启动时的选择逻辑共用一份（同时消掉了一处重复，见 A4）。
  - 测试：`validate()` 不再报这两条；读库之后的 `verify` 能看见认领的管理员和聊天里登录的会话。

### A2 · `HTTP_ENABLED=true` 但没有公网地址时崩溃循环

- **位置**：`tgmd/config.py` `Config.validate()`（原 180–184 行）
- **复现**：`HTTP_ENABLED=true`，不设 `PUBLIC_BASE_URL`，也不在任何托管平台上（平台地址探测不到）。`validate()` 抛 `ConfigError`，`main()` 以退出码 2 退出，compose 的 `restart: unless-stopped` 让它无限重启。
- **严重度**：高。部署当天第一个坑就是它，而且没有公网地址本来就是 NAS 的正常状态（还在申请域名）。
- **处理**：**修**。降为警告，只关闭「Telegram 媒体转存 PikPak」这一项能力：
  - HTTP 服务照常监听，`/healthz` 可用，所以 `docker-compose.ghcr.yml` 里的健康检查不受影响；
  - `HttpConfig.usable` 为假，`FileServer.usable` 为假，`Delivery.to_pikpak` 给出现成的说明；磁力、直链、分享链接转存照常；
  - 原来那条「PikPak 已配置但 HTTP 不可用」的警告与这条合并，不会一次启动报两遍同一件事。
  - 原测试 `test_http_without_a_public_url_is_fatal` 断言的正是要改掉的行为，改写为断言「是警告，并点明关掉的是哪项能力」。

### A3 · 五个核心模块零测试

- **位置**：`tasks.py`（覆盖率 30%）、`delivery.py`（34%）、`downloader.py`（下载主循环 0%，其余部分已有测试）、`verify.py`（12%）、`reporter.py`（27%）
- **推理**：阶段 2 要大改 `tasks.py`、`delivery.py`、`downloader.py`。没有测试，就无法知道提速改动有没有改坏原有行为。
- **严重度**：高（对阶段 2 而言）。
- **处理**：**修**。全部用 fake client，不联网。测试写在行为层面（「给这条消息，用户最后看到什么、库里记成什么、磁盘上留下什么」），不绑实现细节，这样阶段 2 重写内部时测试仍然成立。
  - `tests/test_tasks.py`：三种模式的端到端、缓存命中、批量里部分失败、取消、队列满、意外异常不杀 worker 且会把进度消息收尾、超过上传上限回落到本地。
  - `tests/test_delivery.py`：缓存读写与失效、上传上限、PikPak 的完成 / 失败 / 仍在进行三种结局、HTML 转义。
  - `tests/test_downloader.py` 追加：重试、FloodWait、取消、任何失败都不留半截文件。
  - `tests/test_reporter.py`：节流、强制更新、FloodWait 退避、收尾一定写出。
  - `tests/test_verify.py`：离线检查、读库后的运行时状态、`/verify` 的在线检查。

### A4 · `verify.py` + `identity.py` + `reporter.py` 职责重叠

- **位置**：三个文件，原 693 + 187 + 94 行
- **数据**：
  - `reporter.py` 与另外两个**没有任何关系**。它是任务进度消息（编辑同一条消息、节流、FloodWait 退避），只被 `tasks.py` 使用。名字像「报告」只是巧合。
  - `identity.py` 有两部分：令牌解析与账号描述（被 `clients.py`、`setup.py`、`app.py`、`verify.py` 共用），以及 `Check` / `Report` 检查结果模型（只被 `verify.py` 使用）。
  - 真正的重叠在 `verify.py` 内部：机器人身份核对在 `connect_bot()` 和 `run_live_checks()` 里各写了一遍；读取账号来源在 `verify.connect_user()` 和 `clients.user_session_source()` 里各写了一遍，而且前者漏掉了数据库里的会话（即 A1）。
- **严重度**：低（重复本身），中（重复导致的 A1）。
- **处理**：**不合并文件，只消重复**。
  - `reporter.py` **保留**：它是独立职责，并进去只会让 `tasks.py` 更长。
  - `identity.py` **保留**：把 `Check`/`Report` 挪进 `verify.py` 只是搬行数，不减行数；共用部分被四个模块引用，放在中立模块里更合适。删掉从未被调用的 `BotToken.redacted`。
  - `verify.py` 抽出 `check_bot_identity()` 两处共用；读取账号改走 `clients.user_session_source()`。

### A5 · PikPak 登录有三条路

- **位置**：`portal.py`（551 行）、`miniapp.py`（159 行）、`setup.py` 的 PikPak 对话、`handlers.py` `_pikpak_login()`
- **一次性链接页的实际价值**：
  - 它只在 `unavailable_reason()` 为空时才可用，条件是 HTTP 服务在跑、有公网地址、而且地址是 **HTTPS 或回环**。
  - Mini App 的条件是 HTTP 服务在跑、地址是 **HTTPS**。
  - `handlers._pikpak_login()` 先试 Mini App，可用就直接返回。所以在任何真实部署上（HTTPS），一次性链接**永远不会被发出**。它唯一能出现的场合是 `http://127.0.0.1` 本地开发，而那里聊天内登录一样能用。
  - NAS 的公网入口是 Tailscale Funnel 的 HTTPS 地址，走的是 Mini App。
- **维护成本**：`portal.py` 里约 190 行（`PendingLogin`、一次性令牌的签发 / 校验 / 撤销 / 清扫、表单页、两条路由、尝试次数烧毁逻辑），`test_portal.py` 里约 150 行，`i18n.py` 中英各 2 条，README 一整节。它还和文件服务共用签名密钥与令牌格式，每次改 `signing.py` 都要连带考虑它。
- **严重度**：低（死路径，不是 bug）。
- **处理**：**删**，收敛为「Mini App + 聊天内」两条。
  - 删除 `/pikpak/login/{token}` 两条路由及其全部支撑代码、对应测试、i18n 条目、README 一节。在提交信息里写明理由（红线 7）。
  - `PIKPAK_LOGIN_LINK_TTL` / `pikpak.login_link_ttl` **照常解析、不再使用**。旧 compose 设了它也能起来（红线 2）。是否从文档里彻底删除见「待决问题」。
  - 顺带修两个与此相关的问题：非管理员在 HTTP 不可用时被引导去用 `/setup pikpak`，而那条命令只对管理员开放（见 B11）；在群里发 `/pikpak login` 时 Mini App 按钮会被 Telegram 拒绝（见 B10）。

### A6 · `handlers.py`（743 行）要不要按命令拆分

- **推理**：`BotHandlers` 是一个注册表：12 个处理器共享同一组依赖（bot、config、db、queue、pikpak、portal、wizard）和同一个访问控制入口 `_authorized()`。按命令拆成多个文件，需要把依赖和访问控制复制或抽成基类，行数只增不减；读者追踪「这条命令由谁处理」时要多跳一次文件。
- **严重度**：低。
- **处理**：**保留**为一个文件。可读性问题用 A7 和删死代码解决。删掉一次性链接和死分支后，文件会变短。若阶段 3 让 bot 成为 WMS 的前端、命令数量明显增加，再按「下载类 / 网盘类」两组拆开，那时拆分有实际收益。

### A7 · `on_message` 里的 `if not bundle: if bundle.errors:`

- **位置**：`tgmd/handlers.py` `on_message()`（原 605–616 行）
- **推理**：逻辑正确，靠的是 `LinkBundle.__bool__` 只看「可执行项」而不看 `errors`。读者第一眼会以为是「bundle 为空，那它的 errors 也是空的」，像笔误。
- **严重度**：低。
- **处理**：**修**。改为按优先级排开的四个分支：有链接就提交链接；否则有媒体就收媒体；否则有错误就报错误；否则提示用法。每个分支只看一个条件，不需要解释。

### A8 · 认领之后绝不再打印认领码

- **位置**：`tgmd/bootstrap.py` `announce_claim()`
- **推理**：`app.start()` 先 `load_runtime_settings()`（把库里的管理员合并进 config），再 `announce_claim()`（只在没有管理员时打印）；认领成功时认领码从库里删除。所以认领之后既没有码可打印，也不会进入打印分支。现有测试 `test_a_previous_claim_is_restored_on_the_next_boot` 只断言库里没有码，没有断言日志里没有。
- **严重度**：低（行为正确，缺证明）。
- **处理**：**补测试**。用 `caplog` 断言第二次启动的日志里既没有 `/claim` 也没有 `NO ADMIN YET`。

---

## 二、审计中新发现的问题

### B1 · `/setup pikpak` 可以在群里发起，密码会发进群

- **位置**：`tgmd/setup.py` `begin_pikpak()`
- **推理**：`begin_telegram()` 检查了 `event.is_private`，`begin_pikpak()` 没有。管理员在群里发 `/setup pikpak`，接下来的邮箱和密码就发在群里。bot 读完会尝试删除，但删不删得掉取决于它在群里有没有删除权限；即使删得掉，群成员也可能在这几秒内看到，推送通知里也可能已经带上了。
- **严重度**：高（凭据泄露）。
- **处理**：**修**。与 `begin_telegram()` 相同的私聊检查，补测试。

### B2 · 文件名里带 `&` 时，最终状态消息写不出去

- **位置**：`tgmd/delivery.py` `to_local()`、`to_pikpak()`、`url_to_pikpak()` 的 `summary`
- **复现**：转存一个叫 `Tom & Jerry.mp4` 的文件到 PikPak。`summary` 把文件名原样放进 `<code>…</code>`，任务用 HTML 模式编辑进度消息，Telegram 回 `can't parse entities`。`Reporter.update()` 只记一条 debug 日志，用户看到的进度消息永远停在「交给 PikPak」。
  `sanitize_component()` 去掉了 `<>"`，但没有去掉 `&`（`&` 是合法文件名字符，不应该去掉）。
- **严重度**：中。
- **处理**：**修**。所有插进 HTML 的文件名、路径、任务名都过 `escape_html()`。补测试。

### B3 · 不可重试的下载错误会留下半截文件

- **位置**：`tgmd/downloader.py` `Downloader.download()`
- **推理**：只有 `DownloadCancelled`、`FloodWaitError`、`TimeoutError/ConnectionError/OSError` 三个分支会清理半截文件。其他 RPC 错误（比如 `FileReferenceExpiredError`、`AuthKeyError`）直接穿出去，半截文件留在 `/data/downloads`。文档字符串写着「A partially written file is always removed」，与实现不符。
- **严重度**：中（NAS 磁盘）。
- **处理**：**修**。任何异常路径都先清理再抛出。补测试。

### B4 · 投递失败或 PikPak 尚未拉完时，下载的文件永远留在磁盘上

- **位置**：`tgmd/tasks.py` `_handle_one()`、`tgmd/delivery.py` `to_pikpak()`
- **推理**：
  1. `DELETE_AFTER_DELIVERY` 只在投递成功后执行。上传失败、PikPak 报错时，文件留在磁盘上，用户不知道它在哪，也没有重试入口。
  2. PikPak 在 `task_timeout` 内没拉完时，`to_pikpak()` 返回 `kept_local=True`，因为 URL 还得继续服务。文件服务在 URL 过期后会注销登记，**但从不删除文件**。
- **严重度**：中（NAS 磁盘会被慢慢吃掉，而且没人知道）。
- **处理**：**修**。
  1. 投递抛错时，若开启了 `DELETE_AFTER_DELIVERY`，删除已下载的文件。
  2. `FileServer` 新增 `delete_on_expiry(path)`。PikPak 仍在拉取、且开启了 `DELETE_AFTER_DELIVERY` 时由 `Delivery.to_pikpak()` 调用；URL 过期被清扫时顺带删除文件（同一文件若还有未过期的 URL，则等最后一个过期）。访问一个已过期的 URL 不再当场注销登记，交给清扫统一处理，否则被标记的文件就没人删了。
  超上限回落到本地（`TooLargeToUpload`）和 `local` 模式是有意保留文件，不受影响。补测试。

### B5 · 任务里的意外异常会让进度消息永远停在中途

- **位置**：`tgmd/tasks.py` `_worker()` / `_run()`
- **推理**：`_run()` 创建 `Reporter`，但只有各个 `_run_*_job` 自己认识的异常类型会被收尾。其他异常穿到 `_worker()`，任务被记为失败，但用户看到的消息停在中途。现成的例子：`Resolver.entity()` 只把几种已知错误翻译成 `ResolveError`，`get_entity()` 抛出的超过 60 秒的 `FloodWaitError` 不在其中。它从 `_run_message_job()` 里「逐条消息的 try」之外穿出去，消息永远停在「正在查找…」。
- **严重度**：中。
- **处理**：**修**。`_run()` 兜住意外异常，先把进度消息改成错误信息，再交给 `_worker()` 记录失败。补测试。

### B6 · 用户自己的 PikPak 会话失效后，悄悄改用共享账号

- **位置**：`tgmd/pikpak.py` `PikPakService.client()`
- **推理**：`_user_client()` 的注释写着「不要悄悄回落」，但 `client()` 在它返回 `None` 后继续往下走，直接用共享账号。用户以为文件进了自己的网盘，实际进了运维的网盘。
- **严重度**：中（文件落到别人的网盘里）。单用户 NAS 上无害，多用户部署上是隐私问题。
- **处理**：**修**。用户有会话记录但会话已失效时，抛 `PikPakError`，提示用户重新 `/pikpak login`。每一次都这样，不会从第二次起又回落（`_user_client()` 不再把这类用户记为「无会话」）。补测试。

### B7 · PikPak 状态查询偶发失败会被当成「PikPak 报错」，并导致正在进行的转存失败

- **位置**：`tgmd/pikpak.py` `wait_for_task()`
- **推理**：读了 pikpakapi 0.1.11 的源码：`get_task_status()` 只会返回三种状态之一，`downloading`（任务还在离线列表里）、`done`（文件已存在）、`not_found`。它**只在自己的请求抛 `PikpakException` 时返回 `error`**，也就是说，`error` 的真实含义是「这一次没查到」，不是「PikPak 说失败了」。而网络错误在库里重试几次后也会变成 `PikpakException`。
  我们的 `wait_for_task()` 把 `error` 当最终状态立即返回，`to_pikpak()` 随即注销文件 URL 并告诉用户失败。PikPak 那边的任务其实还在跑，URL 一注销，它接下来的请求全部 404，转存就真的失败了。一次网络抖动就能触发。
- **严重度**：中。
- **处理**：**修**。`error` 视为「这次没查到」：记日志，继续轮询。到时间仍无结论时返回最后一次**确知**的状态（默认 `downloading`），走「PikPak 仍在拉取」的分支，文件留到 URL 过期再删（见 B4）。补测试。

### B8 · Mini App 登录接口不检查访问名单

- **位置**：`tgmd/portal.py` `_handle_miniapp_submit()`
- **推理**：`initData` 证明了「谁」，但没有检查「此人是否被允许使用这个 bot」。实际风险很小：只有 bot 发给已授权用户的按钮才会产生签名过的 `initData`。但这里是整个 bot 唯一一个不经过 `_authorized()` 就能写库的入口。
- **严重度**：低（纵深防御）。
- **处理**：**修**。门户接收一个 `is_allowed` 回调（传 `config.access.is_allowed`；认领后 `admin_user_ids` 会被原地修改，回调看得到），不在名单里就返回 403。补测试。

### B9 · 转义过的文本用纯文本模式发送

- **位置**：`tgmd/handlers.py` `on_mode()` 的 `mode.unknown`、`_submit_inbound()` 的队列已满
- **推理**：文本过了 `escape_html()`，但回复没带 `parse_mode="html"`，用户会看到字面的 `&lt;`。
- **严重度**：低。
- **处理**：**修**。补上 `parse_mode="html"`。

### B10 · 群里发 `/pikpak login` 没有任何回应

- **位置**：`tgmd/handlers.py` `_pikpak_login()`
- **推理**：Telegram 只允许在与 bot 的私聊里发送 Web App 按钮。在群里，回复被拒绝（`BUTTON_TYPE_INVALID`），处理器抛异常，用户什么也收不到。
- **严重度**：低。
- **处理**：**修**。群里直接回复「请私聊我」。补测试。

### B11 · 非管理员被引导去用只对管理员开放的命令

- **位置**：`tgmd/handlers.py` `_pikpak_login()` 的兜底文案、`on_setup()`
- **推理**：Mini App 不可用时，`/pikpak login` 告诉用户「发送 `/setup pikpak`」，而 `on_setup()` 只允许管理员进入 PikPak 对话。可是 Mini App 本来就允许任何已授权用户连接**自己的** PikPak，聊天内的 PikPak 对话写入的也是**发起人自己**的令牌（`user_token_key(user_id)`），不影响共享账号，也不影响其他人。限制管理员的理由（「决定整个 bot 能读什么、文件落到哪」）只适用于 `/setup telegram`。
- **严重度**：低。
- **处理**：**修**。`/setup pikpak` 对已授权用户开放（仍然只限私聊，见 B1）；`/setup telegram` 仍然只限管理员。补测试。

### B12 · `/setup` 清单里的「上传缓存」指引已过时

- **位置**：`tgmd/setup.py` `status_text()`
- **推理**：它仍然让人去设 `CACHE_CHAT_ID`，而 `/cache` 命令早就能在频道里一步设好。
- **严重度**：低。
- **处理**：**修**。改为指向 `/cache`。

### B13 · 文件下载响应头里的非 ASCII 文件名不合规范

- **位置**：`tgmd/webserver.py` `_handle_file()`
- **推理**：实测 aiohttp 会把中文原样按 UTF-8 写进 `Content-Disposition: filename="…"`。这不符合 RFC 6266，不同客户端的解读不一致。PikPak 转存时文件名由我们在 `offline_download(name=…)` 里指定，所以实际影响小。
- **严重度**：低。
- **处理**：**修**。改为 ASCII 兜底的 `filename` 加 `filename*=UTF-8''…`。

### B14 · 设置向导在发送验证码失败时泄漏一个已连接的客户端

- **位置**：`tgmd/setup.py` `_telegram_phone()`
- **推理**：`send_code_request()` 只处理了两种异常。其他异常（网络、`PhoneNumberBannedError` 等）发生时，临时客户端已经 `connect()`，但还没挂到会话上，`cancel()` 关不到它。
- **严重度**：低。
- **处理**：**修**。任何失败都先断开。

### B15 · 死代码

| 位置 | 内容 | 处理 |
|---|---|---|
| `downloader.py` | `Downloader.download_thumbnail()`，从未被调用 | 删 |
| `reporter.py` | `Reporter.say()`、`Reporter.message_id`，从未被调用 | 删 |
| `resolver.py` | `Resolver.join_public()`，从未被调用 | 删 |
| `pikpak.py` | `PikPakService.account_label()`，只被自己的测试调用 | 删（连同测试） |
| `identity.py` | `BotToken.redacted`，从未被调用 | 删 |
| `links.py` | `MessageRef.with_id()`、`LinkBundle.total`，只被自己的测试调用 | 删（连同测试） |
| `buttons.py` | `rows()`、`url_button()`，前者从未被调用，后者只服务于一次性链接（A5） | 删（连同测试） |
| `delivery.py` | `url_to_pikpak(wait=…)` 整个等待分支，没有调用方传 `wait` | 删 |
| `downloader.py` | `MediaInfo.supports_streaming`，算出来但没人读（`delivery.py` 写死了 `True`） | 删 |
| `miniapp.py` | `sign_init_data()`，只被测试使用 | **保留**：它是 `validate_init_data()` 的逆运算，两个测试文件都要用；挪进测试就得造一个靠 `sys.path` 才能导入的共享辅助模块，得不偿失。本地调试 Mini App 时也用得上 |

按红线 7，删除只被自身测试调用的函数时同时删除其测试，并在提交信息里写明理由。

---

## 三、阶段 0 留下的 lint 与写法问题

### C1 · UP042：`(str, Enum)` 改 `StrEnum`

- **位置**：`tgmd/tasks.py` `JobKind`、`JobState`
- **推理**：`(str, Enum)` 的 `format()` 在 3.11 和 3.12 之间结果不同，镜像跑 3.12、开发下限 3.11。`StrEnum` 在两个版本上的 `str()` 和 `format()` 都返回值本身。全仓没有任何地方直接格式化这两个枚举：显示走 `display_state(job.state.value)`，入库用字面字符串。所以改了是纯粹消除隐患，不改变任何输出。
- **严重度**：低。
- **处理**：**修**。并从 `pyproject.toml` 的 `ignore` 里去掉 UP042。

### C2 · BLE001：`except Exception` 逐处判断

18 处，逐处结论：

| 位置 | 判断 | 处理 |
|---|---|---|
| `verify.py` 共 10 处 | 诊断工具的本职就是把任何错误变成一行「失败」，而不是崩溃 | 保留；该文件整体豁免 BLE001，并在配置里写明理由 |
| `botconfig.py` 2 处 | 菜单与简介是装饰性的，限流不能阻止启动 | 保留，逐行注明理由 |
| `delivery.py` 读缓存 | 缓存消息失效的原因五花八门，一律回落到重新下载 | 保留，注明理由 |
| `handlers.py` `/cache` 检查权限 | 把 Telegram 给的任何原因原样告诉管理员 | 保留，注明理由 |
| `reporter.py` 编辑进度 | 进度编辑是装饰性的，失败不能拖垮任务 | 保留，注明理由 |
| `setup.py` 两步验证密码 | 任何失败都让用户重试或退出 | 保留，注明理由 |
| `portal.py` 解析 JSON | 服务端的 `Request.json()` 不检查 Content-Type，只可能抛 `JSONDecodeError`（`ValueError` 的子类） | **收窄**为 `ValueError` |
| `downloader.py` 缩略图 | 所在函数是死代码 | **删**（B15） |

之后在 `pyproject.toml` 里**启用 BLE001**。以后新增的笼统捕获必须写出理由，否则 CI 失败。

### C3 · `stop()` 吞掉外层的取消

- **位置**：`tgmd/tasks.py` `JobQueue.stop()`、`tgmd/webserver.py` `FileServer.stop()`
- **推理**：`with suppress(CancelledError): await worker`。如果调用 `stop()` 的任务本身正在被取消，取消信号会在 `await worker` 处抛出，然后被 `suppress` 一并吞掉，调用方继续往下跑，就像没被取消一样。
- **严重度**：低（只发生在关机路径上）。
- **处理**：**修**。改为 `await asyncio.gather(*workers, return_exceptions=True)`：子任务自己的取消作为返回值收下，调用方的取消照常传播。补测试，已确认旧写法下该测试失败。

### C4 · `ruff format` 是否进 CI

- **判断**：按默认风格要重排 32 个文件，而阶段 2–4 会重写其中大半。现在格式化会制造一次纯排版的大 diff，把后面真正的改动淹没在 blame 里。
- **处理**：**暂不启用**。阶段 4 结束后再决定。已写入 `HANDOFF.md` 待决问题。

---

## 四、看过、结论为「不改」的地方

逐文件读过，以下几处第一眼像问题，结论是保留：

- **`signing.py` 的令牌被文件服务和登录门户共用同一密钥。** 两种令牌载荷格式不同，互相冒用只会在对方的查找表里查不到，结果是 404 / 410。A5 删掉登录链接后只剩文件服务一个用途。
- **`utils.unique_path()` 在两个 worker 同时下载同名文件时有竞态。** 需要同一条消息被同时提交两次，且 `delete_after_delivery` 关闭。代价是一个文件覆盖另一个同内容文件。阶段 2d 会重做本地落盘路径，届时一并处理。
- **`Downloader` 在最后一次尝试遇到 FloodWait 时仍然会先睡再放弃。** 阶段 2b 会重写下载循环（分片并发、FloodWait、FILE_MIGRATE），现在修等于修一段马上要删掉的代码。
- **`Reporter` 的 FloodWait 退避会被 `close()` 的强制更新绕过。** 两个客户端都设了 `flood_sleep_threshold=60`，60 秒以内的等待 Telethon 自己睡掉，不会抛出；能抛出来的都是长等待。收尾消息在长等待里丢一次，比让任务线程睡几分钟好。
- **`resolver.py` 覆盖率只有 19%。** 不在简报 A3 的名单里；阶段 2a 的转发快路会改它，届时连同新路径一起补测试。
- **`handlers.py` 覆盖率 23%。** A7、B9、B10、B11 的修复都附带处理器级别的测试；全面补齐留给阶段 3，那时命令面会变。
- **`login.py`、`__main__.py` 覆盖率 0%。** 都是交互式 / 进程入口，测试价值低。

---

## 五、结果

### 行数

| | 审计前（`fdd04ae`） | 审计后 | 变化 |
|---|---:|---:|---:|
| `tgmd/` | 8,171 | 7,830 | **−341（−4.2%）** |
| `tests/` | 4,085 | 5,455 | +1,370 |
| 测试数 | 545 | 613 | +68 |
| 行覆盖率 | 61% | 77% | +16 个百分点 |

逐文件：

| 文件 | 前 | 后 | 变化 | 主要原因 |
|---|---:|---:|---:|---|
| `portal.py` | 551 | 300 | −251 | A5 删除一次性链接 |
| `buttons.py` | 70 | 33 | −37 | B15 死代码 |
| `handlers.py` | 743 | 715 | −28 | A5、A7 |
| `i18n.py` | 792 | 772 | −20 | A5 删除的条目 |
| `links.py` | 435 | 415 | −20 | B15 死代码 |
| `downloader.py` | 289 | 274 | −15 | B15 死代码，B3 合并了三个清理分支 |
| `reporter.py` | 94 | 81 | −13 | B15 死代码 |
| `verify.py` | 693 | 684 | −9 | A4 消重复 |
| `resolver.py` | 263 | 255 | −8 | B15 死代码 |
| `identity.py` | 187 | 181 | −6 | B15 死代码 |
| `config.py` | 452 | 448 | −4 | A1、A2 |
| `delivery.py` | 324 | 320 | −4 | B15 删掉等待分支，抵掉 B2、B4 的新增 |
| `app.py` | 275 | 276 | +1 | B8 传入访问名单 |
| `pikpak.py` | 409 | 415 | +6 | B6、B7 |
| `setup.py` | 538 | 551 | +13 | B1、B14 |
| `tasks.py` | 638 | 658 | +20 | B4、B5 |
| `webserver.py` | 181 | 215 | +34 | B4 到期删除、B13 响应头 |

覆盖率变化最大的是 A3 点名的五个文件：`tasks.py` 30% → 89%，`delivery.py` 34% → 92%，`downloader.py` 69% → 99%，`reporter.py` 27% → 100%，`verify.py` 12% → 61%。

### 消失的功能（全部有意为之）

只有一项：**一次性 PikPak 登录链接**（A5）。它的两条路由 `/pikpak/login/{token}` 现在返回 404，有测试断言这一点。它的全部测试随之删除（红线 7）。它能做的事，Mini App（HTTPS 时）和 `/setup pikpak`（任何时候）都能做。

另外删除了十余处从未被调用、或只被自身测试调用的函数、属性和分支（B15），它们不对应任何用户可见的功能。

### 行为变化（用户能察觉到的）

- `HTTP_ENABLED=true` 而没有公网地址时，bot 正常启动，只是 Telegram 媒体转存 PikPak 不可用（A2）。
- 启动日志和 `python -m tgmd.verify` 不再误报「没有管理员」「没有读取账号」（A1）。
- `/setup pikpak` 对所有已授权用户开放，但只能在私聊里用（B1、B11）。`/setup telegram` 仍只限管理员。
- 群里发 `/pikpak login` 会收到「请私聊我」，而不是没有回应（B10）。
- 用户自己的 PikPak 会话失效后，转存会失败并提示重新登录，不再悄悄改用共享账号（B6）。
- PikPak 状态查询偶发失败不再导致转存失败（B7）。

两个 Python 版本（3.11、3.12）上测试全部通过，`ruff check` 零告警（新启用 BLE001，UP042 不再豁免）。
