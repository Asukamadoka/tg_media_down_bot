# HANDOFF · 给 Cowork 的部署说明

按阶段追加，最新的在最下面。每一节都写清：NAS 上要改什么、用户要在 Telegram 里做什么、怎么回滚、待决问题。

执行方是 Claude Code（编码、测试、推送）；核验与部署方是 Cowork（NAS、Telegram 客户端）。规格见 `CC_BRIEF.md`。

---

## 阶段 0 · 测试门禁

### 这一阶段做了什么

- **镜像只在测试通过后才构建。** `publish-image.yml` 的 `build-and-push` 现在依赖 `checks`，而 `checks` 调用的就是 `ci.yml`：ruff + pytest（Python 3.11 与 3.12 各跑一遍）。任何一项失败，镜像都不会构建，NAS 上的 `:latest` 保持原样。
- **只有生产分支能推镜像。** 以前从任意分支手动触发都会覆盖 `:latest`；现在其他分支只构建、不推送。
- **ruff 配置落地，告警清零。** `pyproject.toml` 显式列出规则集，ruff 版本钉死在 `requirements-dev.txt`，CI 与本地共用同一处。
- **修了一个真 bug**（`tgmd/setup.py`，ruff RUF006 报出来的）：配置向导的过期清理有竞态，用户在对话过期后立刻重新发 `/setup`，新开的对话会被旧的清理任务误关。另外那个后台任务没有被持有引用，可能在运行前被回收。已修，并有测试证明旧代码会失败、新代码会通过。

**这一阶段没有任何运行时行为变化，除了上面那个竞态修复。** 其余改动全部是等价改写（已逐条核对语义）。

### NAS 上要改什么

**无。** 环境变量、compose、mihomo 规则、`/data` 卷布局、镜像入口一律未动。

照常 pull 新镜像重启即可。在 NAS 的部署目录里执行（受限网络拓扑，即
`deploy/restricted-network/` 那一套；两份 compose 里服务名都叫 `bot`）：

```bash
docker compose pull bot
docker compose up -d bot
```

只重启 `bot`，不要动 `proxy`：`bot` 共享 `proxy` 的网络栈，重启 `proxy` 会让
`bot` 一并断网。

### 用户需要在 Telegram 里做什么

**无。**

### 怎么回滚

镜像每次构建都带一个短 SHA 标签。回到上一个版本：

```bash
# 在 GHCR 的 Packages 页面或 Actions 的运行摘要里找到上一个 sha-xxxxxxx 标签
docker pull ghcr.io/asukamadoka/tg_media_down_bot:sha-<上一个>
# 把 compose 里的 image 临时改成该标签，再 up -d
```

门禁本身的回滚：`git revert` 本阶段的提交即可，工作流会恢复成无门禁的旧版。

### 给 Cowork 的核验手段

**验证门禁确实会拦截**（不推送任何坏代码，也不会影响 `:latest`）：

1. GitHub → Actions → **Publish container image** → **Run workflow**
2. 分支选 `claude/telegram-media-downloader-bot-samm1v`
3. 勾选 **simulate_test_failure**，运行
4. 预期：`checks / test` 两个 Python 版本都失败并标红，**`build-and-push` 显示为 skipped**，GHCR 上没有新镜像。

即使门禁失效，这次运行也只会用当前的好代码重建一次，不会发布坏东西。所以这个验收可以随时重复。

### 线上验收结果（Claude Code 已跑过，均在提交 `2424ad2` 上）

| 运行 | 触发 | lint | test 3.11 | test 3.12 | build-and-push |
|---|---|---|---|---|---|
| [35879176176](https://github.com/Asukamadoka/tg_media_down_bot/actions/runs/35879176176) | push | 通过 | 通过 | 通过 | **运行并推送**（`checks` 全部完成后才开始） |
| [35879362757](https://github.com/Asukamadoka/tg_media_down_bot/actions/runs/35879362757) | 手动，`simulate_test_failure: true` | 通过 | **失败** | **失败** | **skipped** |
| [35879183825](https://github.com/Asukamadoka/tg_media_down_bot/actions/runs/35879183825) | PR #1 的 CI | 通过 | 通过 | 通过 | （CI 不构建镜像） |

第二行就是简报要求的「故意弄挂一个测试，确认镜像不会被构建」。

### 待决问题

1. **`ruff format` 没有强制。** 按它的默认风格会重排 32 个文件。简报只要求 `ruff check`，而一次纯排版的大 diff 会淹没阶段 1 的审计改动。建议：阶段 1 审计完成、模块合并之后，再决定是否在 CI 里加 `ruff format --check`。
2. **行宽定为 100，不是 ruff 默认的 88。** 实测：88 列下全仓 115 处超长，100 列下只有 2 处。代码事实上一直按 100 列在写，定 100 是如实反映，而不是为了少改。
3. **简报说 `i18n.py` 的中文会触发 E501，实际不会。** ruff 确实按显示宽度计数（已验证：66 个码位的中文行被判为 126 列），但 `i18n.py` 的中文条目已经拆得足够短，最长的行是 92 列的英文行。该预期在 88 列下成立，在 100 列下不成立。
4. **真正被中文触发的是 RUF001，不是 E501。** 92 处，全部在 `tgmd/i18n.py`：ruff 把中文全角标点（「，」「：」）当成长得像 ASCII 的可疑字符。已对该文件单独豁免；其余文件保持检查，因为那里混进全角字符（比如命令名里）才是真 bug。
5. **两条规则留给阶段 1 审计，未在本阶段处理：**
   - **UP042**：`(str, Enum)` 改 `StrEnum`。这是语义变更，不是 lint 清理，而且这类枚举的格式化行为在 3.11 和 3.12 之间本就不同。
   - **BLE001**：18 处 `except Exception`。多数是有意的健壮性边界（worker 不能因一个任务而死、装饰性调用失败不能阻塞启动），但每一处都值得单独判断，一次性加 18 个 `noqa` 等于替审计下结论。
6. **`stop()` 在等待已取消的子任务时吞掉 `CancelledError`**（`tasks.py`、`webserver.py`）。如果调用 `stop()` 的任务本身正在被取消，这也会把外层的取消一并吞掉。本阶段只是把写法换成 `contextlib.suppress`，语义没变；是否需要区分“子任务的取消”和“自己的取消”，留给阶段 1。

---

## 阶段 1 · 全仓审计

完整审计见 `docs/AUDIT.md`：简报点名的 A1–A8 逐条给了结论，另外新发现 15 条（B1–B15），阶段 0 遗留的 4 条（C1–C4）也有结论。

### 这一阶段做了什么

- **A1** 启动日志和 `python -m tgmd.verify` 不再误报「没有管理员」「没有读取账号」。NAS 上这两件事都是在聊天里完成的（`/claim`、`/setup telegram`），以前每次启动都有两条假警告，`verify` 还会因此判失败。
- **A2** `HTTP_ENABLED=true` 而没有公网地址时不再崩溃循环，降为警告，只关闭「Telegram 媒体转存 PikPak」。
- **A5** 删除了一次性 PikPak 登录链接，只保留 Mini App 与聊天内登录。理由：它要求的 HTTPS 条件与 Mini App 完全相同，所以在任何真实部署上它都不会被发出。
- **B1（安全）** `/setup pikpak` 以前可以在群里发起，接下来的密码就发在群里。现在只能私聊。
- **B2–B7** 几个真 bug：文件名带 `&` 时最终状态消息写不出去；下载 / 投递失败会在 NAS 上留下文件；PikPak 还没拉完的文件永远不删；任务里的意外异常让进度消息卡住；用户自己的 PikPak 会话失效后悄悄改用共享账号；PikPak 状态查询偶发失败会让正在进行的转存失败。
- **A3** 给 `tasks.py`、`delivery.py`、`downloader.py`、`reporter.py`、`verify.py` 补了行为层面的测试，这是阶段 2 提速改动的安全网。测试 545 → 613，覆盖率 61% → 77%。
- **行数** `tgmd/` 8,171 → 7,830（−341）。

### NAS 上要改什么

**必须做的：无。** 所有现有环境变量照常生效，`/data` 布局与数据库结构未动（没有迁移），镜像入口未变。照常拉新镜像、只重启 `bot`：

```bash
docker compose pull bot
docker compose up -d bot
```

**可选：**

- `PIKPAK_LOGIN_LINK_TTL` 如果在 `.env` 或 compose 里设过，现在不再起作用，可以删；不删也照常启动。
- 如果 NAS 的 compose 因为当初的崩溃把 `HTTP_ENABLED` 设成了 `"false"`：只要 Tailscale Funnel 的地址可用，就可以设 `HTTP_ENABLED: "true"` 和 `PUBLIC_BASE_URL: "https://ugreen-nas.tail212e43.ts.net"`，这样 `/pikpak login` 会直接弹出 Mini App，Telegram 媒体也能转存 PikPak。即使地址暂时不可用，现在也只是一条警告，不会再崩溃循环。**改之前先看一眼 NAS 上现在的值，不要盲改。**

### 用户需要在 Telegram 里做什么

**无。** 下面是 Cowork 核验时可以顺手做的。

### 给 Cowork 的核验手段

1. **A1 假警告消失**：`docker compose logs bot --since 10m | grep -iE "no admin configured|no user session configured"`，应当没有输出。（如果确实没有读取账号，会有另一条 `no user session yet: ... /setup telegram`，那条是真话。）
2. **verify 读得到数据库**：`docker compose exec bot python -m tgmd.verify`。期望 `access control` 为 ✓，`user session` 为 ✓ 且写着 `from an in-chat login`。`http server` 一行可能提示端口被占用，因为 bot 本身在跑，这是预期的。
3. **B10**：在任意群里发 `/pikpak login`，应收到「请私聊我来连接 PikPak」。
4. **A5**：私聊发 `/pikpak login`。有 HTTPS 地址时应弹出「🔐 连接 PikPak」按钮（Mini App）；没有时应提示用 `/setup pikpak`。
5. **B12**：私聊发 `/setup`，「上传缓存」一行若未配置，应提示在频道里发 `/cache`，而不是去设 `CACHE_CHAT_ID`。
6. **B2**：`/mode local` 后转发一个文件名含 `&` 的文件（或让它下载一个），最后的状态消息应正常显示「saved to …」，而不是停在下载中。

### 怎么回滚

回到阶段 0 的镜像 `sha-2424ad2`（阶段 0 之后的 `fdd04ae` 只改了文档，没有构建镜像）：

```bash
docker pull ghcr.io/asukamadoka/tg_media_down_bot:sha-2424ad2
# 把 compose 里的 image 临时改成该标签，再 up -d bot
```

数据库没有迁移，所以前后两个版本可以用同一个 `/data` 来回切换。

### 待决问题

1. **`PIKPAK_LOGIN_LINK_TTL` 永久保留为空操作，还是在某个大版本里删掉？** 红线 2 要求现有变量名不动，所以本阶段只是不再使用它。建议永久保留：一行解析代码的成本远低于让某个旧部署起不来。
2. **`/setup pikpak` 对所有已授权用户开放了（B11）。** 这是我按一致性做的决定：Mini App 本来就允许任何已授权用户连接**自己的** PikPak，聊天内登录写入的也只是发起人自己的令牌。如果主人希望聊天内登录仍只限管理员，改回来只需要一行（`handlers.py` `on_setup()`），测试 `test_any_allowed_user_may_connect_their_own_pikpak` 也要随之改。
3. **`ruff format` 仍不强制（C4）。** 阶段 2–4 会重写大半文件，建议阶段 4 结束后再决定。
4. **`resolver.py`（覆盖率 19%）与 `handlers.py`（36%）的测试没有补齐。** 前者阶段 2a 转发快路会改，后者阶段 3 命令面会变，届时连同新代码一起补。

---

## 阶段 2a · 转发快路

### 这一阶段做了什么

新增 `tgmd/forwarder.py`。telegram 模式下，一个文件在「查上传缓存」之后、「下载」之前，先试零字节路径：

1. 源消息和所在聊天都允许转发（`noforwards` 两个标志都为假）；
2. **读取账号**把消息转发进缓存频道；
3. **bot** 在缓存频道里用自己的文件引用读到这条消息，再 `send_file` 发给用户（用户收到的没有「Forwarded from」头），同时写入 `media_cache`，同一链接第二次请求直接走缓存。

降级规则（仿 tdl 的 direct → clone）：

| 情况 | 结果 |
|---|---|
| 可转发，且有缓存频道 | **零字节**，日志 `forwarded, nothing downloaded`，回复「⚡ … 由 Telegram 直接复制，未下载」 |
| 源受限（消息或聊天 `noforwards`，或 Telegram 回 `CHAT_FORWARDS_RESTRICTED`） | 下载再上传 |
| 可转发，但**没有缓存频道** | 下载再上传，回复末尾提示「管理员在私有频道里发 /cache 就能秒转」（一个任务只提示一次） |
| 转发报错（读取账号不在频道里、不能发言、bot 看不到转发件等） | 下载再上传，原因写进日志 |
| 没有读取账号（bot 自己读公开频道） | bot 直接用自己的引用重发，不需要缓存频道 |

只有 telegram 模式走快路。local 和 pikpak 模式本来就要字节，不受影响。

**账号安全（红线 6）**：读取账号每个文件只做一次普通的 `messages.forwardMessages`，是正常用户行为。一个坑已经处理：聊天内登录得到的会话重启后不记得任何频道，第一次转发前会列一次对话列表来找到缓存频道，之后记住；**如果读取账号根本不在缓存频道里，10 分钟内不会再列第二次**，避免每个文件都把主账号的对话列表整个读一遍。

`/verify` 和 `python -m tgmd.verify` 新增一行 `forward fast path`，直接告诉你快路能不能用、不能用差在哪。

### NAS 上要改什么

**无。** 没有新增环境变量，没有迁移。照常 `docker compose pull bot && docker compose up -d bot`。

### 用户需要在 Telegram 里做什么

**目前没有缓存频道，所以快路还没有生效**，可转发的视频仍然会下载再上传（回复里会有 `/cache` 的提示）。要打开它：

1. 用**读取账号所在的那个 Telegram 账号**（即用户本人的主账号）新建一个**私有频道**。频道创建者天然有发言权。
2. 把 bot（`@pikpak_WMS_bot`）加为这个频道的**管理员**。
3. 在频道里发一条 `/cache`。bot 会回复已设为缓存频道。
4. 私聊 bot 发 `/verify`，确认 `forward fast path` 一行是 ✅。

如果频道不是用读取账号创建的，就需要把读取账号拉进频道并设为管理员、打开「发布消息」权限。`/verify` 会分别指出「看不到频道」和「在频道里但不能发言」。

### 给 Cowork 的核验手段（对应 §4 阶段 2 验收第一条）

1. 按上面四步设好缓存频道，`/verify` 里 `forward fast path` 为 ✅。
2. 找一个**允许转发**的频道里的大视频（几百 MB），把消息链接发给 bot（`/mode telegram`）。
   - 期望：几秒内收到视频；回复是「⚡ … 未下载」。
   - 日志：`docker compose logs bot --since 5m | grep -E "forwarded, nothing downloaded|downloading"`，只应有前者。
   - 磁盘：`/data/downloads` 下没有新文件。
3. 同一个链接再发一次：回复「♻️ … 命中缓存」，读取账号不再转发（缓存频道里没有新增消息）。
4. 找一个**开启了「限制保存内容」**的频道里的视频：应走下载再上传，缓存频道里不出现转发件。
5. 对照：`/cache off` 之后再发第 2 步的链接，应下载再上传，回复末尾带 `/cache` 提示。核验完再在频道里发一次 `/cache` 恢复。

### 怎么回滚

回到阶段 1 的镜像 `sha-8ae52d6`。数据库无迁移，`media_cache` 里快路写入的条目在旧版本里同样有效（旧版本本来就读这张表）。

### 待决问题

1. **相册是逐条转发的。** 一个 10 张的相册会产生 10 次 `forwardMessages`。可以改成一次转发整组，但要同时改造「每条消息各自投递、各自报告进度」的结构，收益只在相册上。建议等阶段 2 其余部分完成后按实测决定。
2. **缓存频道里会留下转发件。** 这是有意的，它们就是缓存本身，删掉会让第二次请求变回下载。频道会持续变大，但 Telegram 不对频道存储计费。如果主人希望定期清理，需要另定规则（清理后对应的 `media_cache` 条目会在下次命中时自动失效并回落到下载，已有逻辑能处理）。
