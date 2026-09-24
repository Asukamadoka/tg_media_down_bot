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

---

## 阶段 2b · 并行分片下载

### 这一阶段做了什么

- 新增 `tgmd/parallel.py`（按 MTProto 文档中 `upload.getFile` 的规则自行实现，没有参考或复制任何第三方代码）。大于 10 MB 的文档分成 1 MiB 的分片，由多条连接同时拉取，按偏移写入同一个文件。
- **连接怎么来**：每条连接是一个独立的 `MTProtoSender`，复用 Telethon 已经握有的授权密钥。文件在账号主 DC 上就用会话本身的密钥；在其他 DC 上就用 Telethon 为该 DC 导出的密钥（Telethon 本来就会建一次）。**不产生新的登录，不做新的密钥交换**，也不会在「设备」列表里多出条目。
- **必须处理的四种情况**：

| 情况 | 处理 |
|---|---|
| `FloodWait` | **所有连接一起暂停**（限制是按账号算的，其他连接继续打只会更糟），等完继续；超过 300 秒不等，直接报错给用户 |
| `FILE_REFERENCE_EXPIRED` | 重新读取这条消息拿到新引用，继续；多条连接同时撞上只刷新一次；同一文件最多刷新两次 |
| `FILE_MIGRATE` | 降级为 Telethon 默认的单连接下载 |
| CDN 重定向（`upload.fileCdnRedirect`） | 降级为单连接。实际上我们不声明支持 CDN，Telegram 不会发这个；防御性处理 |

  另外：连接打不开或反复断开 → 降级；能开几条算几条（比如要 4 条只开出 2 条，就用 2 条）。**任何降级都会先删掉半截文件，再从头走单连接**，结果总是正确的，只是慢。
- 每个文件下载完在日志里打一行：文件名、大小、耗时、平均速率、**所在 DC**、实际用了几条连接。阶段 2c 需要的 DC 分布就从这行来。
- 新增 `python -m tgmd.bench <消息链接>`：对同一个文件按不同连接数各下一次，打印对比表，然后删掉副本。
- Telethon 没有公开「开额外连接」的接口，用到了几个私有属性。有一个测试专门核对这些属性在当前 Telethon 版本里存在，升级 Telethon 时如果它们变了，会先在 CI 里失败，而不是在用户下载到一半时失败。

### NAS 上要改什么

**可选。** 新增环境变量 `DOWNLOAD_CONNECTIONS`，**默认 4**，上限 8（写更大会被压到 8，并在启动日志里警告），写 1 等于关闭并行、完全回到以前的行为。旧 compose 不写这个变量，就是 4。

照常 `docker compose pull bot && docker compose up -d bot`。

### 用户需要在 Telegram 里做什么

**无。**

### 给 Cowork 的核验手段（对应 §4 阶段 2 验收第二条）

1. 找一个**受限频道**（开启了「限制保存内容」）里几百 MB 的视频，复制消息链接。
2. 在 NAS 上跑：
   ```bash
   docker compose exec bot python -m tgmd.bench '<消息链接>' --connections 1,4,8
   ```
   输出的格式如下（数字是占位，不是实测结果）：
   ```
   connections        size   seconds          rate  dc  endpoint
             1   <大小>     <秒数>      <速率>   <dc>  Telethon default
             4   <大小>     <秒数>      <速率>   <dc>  <ip>:443
   ```
   把这张表原样贴进验收记录，就是「前后对比的数字」。第一行 `connections` 如果显示的是 1 而你要的是 4，说明并行降级了，原因在日志里（`parallel download not possible (...)`）。
3. **不要循环跑。** 每一行都是用户主账号的一次真实下载。一次跑 2–3 个连接数就够了；工具本身限制一次最多 6 行。
4. 平时的下载日志：`docker compose logs bot | grep "downloaded "`，每个文件一行，含 DC 与连接数。可以顺手统计用户常看的频道文件落在哪些 DC，阶段 2c 要用。

### 怎么回滚

- 只关并行：设 `DOWNLOAD_CONNECTIONS=1`，重启 `bot`。代码路径与阶段 2a 完全一致。
- 整体回滚：镜像 `sha-2522974`（阶段 2a）。

### 待决问题

1. **默认 4 条连接是否保守到位。** 简报给的默认值是 4，我照做了。每条连接都是用户主账号的，而 `concurrent`（同时处理的任务数，默认 2）会与之相乘：两个大文件同时下载就是 8 条连接。如果 Cowork 实测中看到 FloodWait 日志（`flood wait of ...s during a parallel download`），建议先把 `DOWNLOAD_CONNECTIONS` 降到 2–3，再看数字。
2. **照片与小文件不走并行。** 照片通常只有几百 KB，10 MB 以下的文件多开连接得不偿失。这个阈值写在代码里（`MIN_PARALLEL_SIZE`），暂不做成配置项。

---

## 阶段 2c · 直连媒体线路

### 这一阶段做了什么

- **Cowork 实测的发现落地了**：NAS 不经代理能连上的只有三个 `media_only` 端点，其中 IPv4 的是 DC4 的 `149.154.166.111:443`。Telethon 挑端点时根本不看 `media_only`，所以以前即使线路通也用不上。现在下载用的连接是我们自己开的（阶段 2b），端点也由我们选。
- 新配置 `TG_DIRECT_MEDIA=auto|off`，**默认 `off`**，按简报要求实测通过后再改成 `auto`。
- `auto` 时的行为：
  - 先试该 DC 的 `media_only` IPv4 端点（排除 IPv6、CDN、需要混淆的 `tcpo_only`），**与普通端点共用同一个授权密钥**，不产生新登录。
  - **连不上**：每个端点最多等 10 秒，然后自动改用普通端点（经代理）。
  - **连上了但传输中被重置**（简报担心的「防火墙在握手后重置」）：本文件自动改走普通端点重下一次。
  - 以上任一情况都会把「该 DC 的直连」记为失败，**30 分钟内**所有下载直接走代理，不再每个文件都先撞一次墙。
  - **文件恰好在账号主 DC 时**：Telethon 默认走主连接（经代理）。现在只要该 DC 有可用的媒体端点，即使是 10 MB 以下的小文件，也用我们自己的一条连接直连下载。没有媒体端点的 DC（实测 DC1/3/5）照旧交给 Telethon。
- 实验开关：`python -m tgmd.bench <链接> --route normal|media|both`。`normal` 强制走 Telethon 会选的普通端点（经代理），`media` 只走媒体端点（直连失败时该行会显示 `Telethon default`，不会悄悄走代理冒充直连），`both` 对同一个文件每种连接数各跑一遍，两种路线相邻对比。
- 每个文件的下载日志已含 `DC`，走直连时末尾会有 `via 149.154.166.111:443 media`。
- `deploy/restricted-network/mihomo/config.example.yaml` 新增 `IP-CIDR,149.154.166.111/32,DIRECT,no-resolve`，位于 `MATCH` 之前。

### NAS 上要改什么（按顺序）

1. **同步 mihomo 规则**。在 NAS 真实的 mihomo `config.yaml` 的 `rules:` 里，`MATCH,PROXY` **之前**加一行：
   ```yaml
   - IP-CIDR,149.154.166.111/32,DIRECT,no-resolve
   ```
2. 拉新镜像，然后**两个一起重启**（`bot` 共享 `proxy` 的网络栈，单独重启 `proxy` 会让 `bot` 断网）：
   ```bash
   docker compose pull bot
   docker compose up -d --force-recreate proxy bot
   ```
3. **先不要改 `TG_DIRECT_MEDIA`**，保持默认 `off`，先做下面的实测。

### 给 Cowork 的核验手段（对应 §4 阶段 2 验收第三条）

1. **找一个 DC4 的文件**。看平时的下载日志：
   ```bash
   docker compose logs bot | grep -o "from DC [0-9]" | sort | uniq -c
   ```
   这同时就是用户常看频道的 DC 分布，请把统计结果贴进验收记录，它决定这条优化能覆盖多少流量。挑一条日志里 `from DC 4` 的消息链接；或者直接对候选链接跑 bench，看 `dc` 列。
2. **对比两条线路**，同一个文件：
   ```bash
   docker compose exec bot python -m tgmd.bench '<DC4 文件的链接>' --route both --connections 1,4
   ```
   `route=normal` 的行是经代理，`route=media` 的行是直连。如果 `media` 行的 `endpoint` 列显示 `Telethon default`，说明直连没通（日志里会有 `could not open a connection to 149.154.166.111:443` 或 `direct media download broke`）。
3. **直连稳定且更快**，再在 compose 里设 `TG_DIRECT_MEDIA: "auto"`，重启 `bot`。之后下载日志里 DC4 的文件应带 `via 149.154.166.111:443 media`。
4. 回退验证（可选）：临时去掉 mihomo 那条规则、重启两个容器，DC4 文件应在约 10 秒后自动改走代理并正常完成，日志里有 `direct media route to DC 4 failed; using the proxy for a while`。

### 怎么回滚

- 只关直连：`TG_DIRECT_MEDIA=off`（或不设），重启 `bot`。mihomo 那条规则留着无害：bot 不再连那个 IP。
- 整体回滚：镜像 `sha-c758318`（阶段 2b）。

### 后续项（本阶段不做）

- **IPv6 的两个媒体端点**（DC2 `2001:67c:4e8:f002::b`、DC4 `2001:67c:4e8:f004::b`）。要用上它们，需要 mihomo `ipv6: true`、容器网络开启 IPv6，并在代码里放开对 IPv6 端点的过滤（`media_endpoints()` 里一行）。这会让 DC2 的文件也能直连。等 IPv4 这条实测有结论后再评估。

### 待决问题

1. **`TG_DIRECT_MEDIA` 何时改成默认 `auto`。** 按简报，等 Cowork 的实测数字。如果实测直连明显更快且稳定，下一个阶段我把默认值改成 `auto`，或者只在 `deploy/restricted-network/docker-compose.yml` 里设 `auto`（后者不影响其他部署方式，我倾向这个）。
2. **30 分钟的「直连失败冷却」是拍的数。** 如果 NAS 的线路时好时坏，可能需要调短；目前没有做成配置项。

---

## 阶段 2d · 智能路由与媒体目录

### 这一阶段做了什么

- **新模式 `auto`**（默认模式不变，用户自己 `/mode auto` 选）：
  - 能转发的 → 走 2a 的快路发回 Telegram（没有缓存频道时下载再上传，和 telegram 模式一样）；
  - 受限的 → 下载到 **NAS 媒体目录**，回复访问路径，不再推到任何地方；
  - 直接发给 bot 的媒体 → 存进媒体目录（发回给发送者没有意义）。
  - `auto` 是落库值，按 i18n 红线本身不翻译；显示名为「auto（能转发就秒传，受限的存 NAS）」，帮助文案里 `/mode auto` 保留英文关键词。
- **媒体目录**：
  - 新增 `MEDIA_DIR`，**默认等于 `DOWNLOAD_DIR`**，不设时文件的位置与以前一致。
  - local 模式、auto 模式里受限的文件、超过 2 GB 上传上限而留下的文件，都落在这里。要留下的文件**直接下载到媒体目录**，不在工作目录里转一手。
  - **保留原文件名**：新增 `MEDIA_TEMPLATE`，默认 `{chat}/{name}`（按频道名分文件夹，文件名就是原名；同名时自动加 ` (1)`，不覆盖）。`FILENAME_TEMPLATE` 只管工作目录里的临时文件，行为不变。
  - `DELETE_AFTER_DELIVERY` 对留下的文件永远不生效。
  - 新增可选 `LOCAL_URL_PREFIX`，例如 `smb://10.10.10.2/media/`。设了之后，回复里给出的是「前缀 + 媒体目录内的相对路径」（已做 URL 编码，空格是 `%20`），可以直接粘进文件管理器；不设就显示容器内路径。
- 顺带修正 `deploy/restricted-network/docker-compose.yml` 里一条过时注释：没有公网地址时开 `HTTP_ENABLED` 已经不会 exit 2 了（阶段 1 A2）。

**一处可察觉的行为变化**：以前 local 模式的文件名带消息编号前缀（`频道/123_视频.mp4`），现在是原文件名（`频道/视频.mp4`），这是简报 2d 的要求。已有的旧文件不动。想要旧格式，设 `MEDIA_TEMPLATE={chat}/{message_id}_{name}`。

### NAS 上要改什么

**必须做的：无。** 不设 `MEDIA_DIR` 时一切照旧。

**要把文件放进 NAS 共享文件夹时**（需要先定下面的待决问题 1）：

1. 在 NAS 上确定媒体共享目录的宿主机路径，并让容器用户能写：`chown -R 10001:10001 <该目录>`，或给 uid 10001 写权限。
2. 在 `deploy/restricted-network/docker-compose.yml` 的 `bot` 服务里，取消注释并填好（模板里已留好占位）：
   ```yaml
   environment:
     MEDIA_DIR: "/media"
     LOCAL_URL_PREFIX: "smb://<NAS 地址>/<共享名>/"
   volumes:
     - ./data:/data
     - <NAS 上的媒体共享目录>:/media
   ```
3. `docker compose up -d bot`（改了 volumes，`up -d` 会重建 `bot`；`proxy` 不用动）。

### 用户需要在 Telegram 里做什么

想用智能路由时发一次 `/mode auto`。不发就保持原来的模式。

### 给 Cowork 的核验手段

1. `/mode auto`，发一个**可转发**频道的视频链接：应秒到（有缓存频道时）或下载后上传（没有时），媒体目录里不出现新文件。
2. 发一个**受限**频道的视频链接：回复「saved to …」，文件出现在 `<媒体目录>/<频道名>/<原文件名>`；设了 `LOCAL_URL_PREFIX` 时回复里是 `smb://…` 形式，复制到电脑的文件管理器里应能直接打开。
3. 同一个受限链接再发一次：出现 `原文件名 (1).扩展名`，旧文件不被覆盖。
4. 验收第二条「受限频道的视频走并行下载并落到媒体目录」：看日志里这个文件的 `downloaded ...` 行，`over 4 connection(s)`，路径在媒体目录下。

### 怎么回滚

- 不想用 auto：`/mode telegram`。
- 媒体目录：去掉 `MEDIA_DIR`，文件回到 `DOWNLOAD_DIR`。
- 整体回滚：镜像 `sha-19b2d80`（阶段 2c）。

### 待决问题

1. **媒体目录的宿主机路径**（简报明确要求不猜）。需要用户和 Cowork 确定：NAS 上哪个共享文件夹、SMB 共享名是什么、局域网访问地址（用于 `LOCAL_URL_PREFIX`）。compose 模板里是占位。
2. **按频道分文件夹是否符合用户习惯。** 默认 `{chat}/{name}`；也可以是按日期（`{date}/{name}`）或全部平铺（`{name}`）。一行配置就能改，需要用户选。

---

## 阶段 2e · 流式与流水线

### 这一阶段做了什么

**PikPak 流式端点（`PIKPAK_STREAM`，默认关闭）**

- 文件服务器新增 `/s/<签名令牌>/<文件名>`。PikPak 请求这个 URL 时，bot **按它要的字节范围直接从 Telegram 读取、原样转发**，不落盘。
- 支持 PikPak 需要的全部 HTTP 语义：`HEAD`（只回大小，不读 Telegram）、`Range` 单段请求（`bytes=a-b`、`bytes=a-`、`bytes=-n`）、准确的 `Content-Length` 与 `Content-Range`（来自 Telegram 报告的文件大小，不是估计）、越界返回 `416`。多段 Range 按规范可以忽略，回整个文件。
- 读 Telegram 时对齐到 512 KiB（Telethon 的单次上限，满足 `upload.getFile` 的全部对齐规则），多读的头尾裁掉。只读 PikPak 要的那一段。
- 每个流同时最多 4 个 PikPak 请求在读（PikPak 可能分段并发拉取；每一段都是用户主账号的一次读取，所以设了上限，超出的排队）。
- 只对文档生效。照片没有事先可知的单一大小，照旧走落盘；`PIKPAK_STREAM` 关闭时一切照旧（落盘模式保留为回退）。
- 端到端测试：真实文件服务器 + 真实投递 + 真实 `Downloader.stream`，假的 PikPak 真的去拉这个 URL（先 `HEAD`，再两段并发），拼回来的字节与原文件逐字节相同，磁盘上没有任何文件。

**Telegram clone 路径：保留「下完再传」，不做边下边传。** 理由如下，按简报要求写明：

1. **收益有上限，而且在 NAS 上更小。** 边下边传最多把总耗时缩到接近一半，前提是上传和下载速度相当、且互不抢带宽。NAS 上两者走的是同一条代理出口，互相竞争，实际重叠收益会明显小于一半。
2. **代价是重写 Telethon 的上传。** 要自己用 `upload.saveBigFilePart` 分片上传，再自己组 `inputFileBig`、媒体属性、缩略图，并处理 `FILE_PART_X_MISSING` 之类的部分失败。这是面向用户的主路径，出错的代价是用户收不到文件。
3. **这条路径已经很窄了。** 2a 之后，可转发的内容走零字节快路（设了缓存频道时）；2d 之后，`auto` 模式把受限内容留在 NAS。只有「telegram 模式 + 受限内容」还会走 clone，而这部分已经用上了 2b 的并行下载。
4. 如果以后实测发现 clone 路径仍是大头，再评估；到那时有 2b 的下载日志和 bench 数据可以对照。

**local 路径**：按定义就是落盘，不变。

### NAS 上要改什么

**必须做的：无。** `PIKPAK_STREAM` 默认关闭。

要试流式（前提是 PikPak 能访问到 bot 的公网 HTTPS 地址，即 `HTTP_ENABLED=true` 且 `PUBLIC_BASE_URL` 可用）：在 compose 里设 `PIKPAK_STREAM: "true"`，重启 `bot`。

### 用户需要在 Telegram 里做什么

**无。**

### 给 Cowork 的核验手段

1. 打开 `PIKPAK_STREAM`，`/mode pikpak`，发一个受限频道里几百 MB 的视频链接。
2. 期望：回复「saved to PikPak …」或「PikPak is still fetching …」；`/data/downloads` 与媒体目录里**都没有**新文件；日志里**没有**这个文件的 `downloaded ...` 行（字节没有落盘，也就不走下载器）。
3. 和关闭流式时同一个文件对比「从发链接到 PikPak 里出现文件」的总时长，记录两个数字。
4. 如果 PikPak 那边报错或一直停在「still fetching」：关掉 `PIKPAK_STREAM` 即回到落盘模式，并把 bot 日志里 `/s/` 相关的行贴进待决问题。

### 怎么回滚

- `PIKPAK_STREAM=false`（或不设），重启 `bot`。
- 整体回滚：镜像 `sha-d0d227d`（阶段 2d）。

### 更正

阶段 2d 的提交信息里写的行数是估计值、写错了。实测是 `tgmd/` 9,060 行、`tests/` 6,805 行（2d 之前是 8,973 / 6,623）。

### 待决问题

1. **流式失败时不会自动回落到落盘。** 回落需要知道「PikPak 拉失败了」，而 PikPak 只会在超时后报 `error`，那时再下载一遍等于总时长翻倍。目前的设计是：流式默认关闭，实测稳定再开；不稳定就整体关掉。如果实测结论是「大多数时候行、偶尔不行」，再考虑做自动回落。
2. **阶段 2 整体收尾。** 2a–2e 都已交付。三条验收（转发秒到、受限并行落盘的前后数字、DC4 直连前后数字）都需要 Cowork 在 NAS 上实测，命令分别在 2a、2b、2c 三节里。

---

## 阶段 3 · WMS M1：核心链路

阶段 3 的规格是 `CC_BRIEF.md` §5 与 `docs/wms/`（本阶段已从 `Asukamadoka/pikpak-wms` 迁入：`ARCHITECTURE.md`、`ROADMAP.md`、`REFERENCES.md`，各加了一段「并入说明」，其余原样；`config/wms.example.yaml`、`config/rules.example.yaml` 迁到仓库根的 `config/`）。M6（自然语言）按其文件要求排在 M2 之后；它的机器人入口依赖 M3–M5 的集成，所以实际顺序是 M1 → M2 → M3 → M4 → M5 → M6。

### 这一阶段做了什么

新增顶层包 `pikpak_wms/`（与 `tgmd/` 并列，**不 import `tgmd`**，有测试扫描源码保证这一点）：

- `core/`：
  - `ratelimit.py` 全局令牌桶，默认 4 req/s、突发 8；
  - `client.py` 唯一接触 PikPak 的地方：每个请求先取令牌；限流类错误指数退避重试（3、6、12 秒）；SDK 异常统一收敛成 `WmsError` / `AuthError` / `RateLimitedError` / `NotFoundError`；SDK 返回的 dict 转成领域模型。它不自己登录，而是接收一个「给我一个已登录客户端」的回调，这样 bot 以后可以直接注入用户已连接的账号；
  - `auth.py` CLI 单独运行时的登录：从 `PIKPAK_USERNAME` / `PIKPAK_PASSWORD` 或 `PIKPAK_ENCODED_TOKEN` 登录一次，只把 token 写进 0600 文件（密码剥掉，不落盘），之后自动刷新。
- `store/`：独立 SQLite，默认 `$DATA_DIR/wms.sqlite3`（容器内 `/data/db/wms.sqlite3`）；`files` / `tasks` / `audit` 三张表按原设计，另加一张 `meta`。只加不改（红线 3）。
- `ops/stocktake.py`：全量与增量盘点；`--verify` 只读地把索引与网盘逐条核对。
- CLI：`python -m pikpak_wms` → `version` / `doctor` / `login` / `stocktake [--full] [--verify] [--root] [--json]` / `ls` / `quota`。所有输出走 WMS 自己的 `t()`（中英两套；语言跟随 `WMS_LANG`，未设则跟随 bot 的 `TGMD_LANG`），命令名与参数保持英文。
- 依赖：`pydantic`、`typer`（带 `rich`）、`APScheduler<4`。**镜像会变大几 MB**（主要是 `pydantic-core`）。没有引入 `aiosqlite`、`pydantic-settings`，沿用 bot 的 `sqlite3` + 线程做法。

### 增量盘点的一个假设（需要 Cowork 在真实账号上确认）

按原设计，增量盘点只重新列出 `modified_time` 变了的目录，一个没变的目录**连同整个子树**都跳过。这只有在 PikPak「子孙有变化时会刷新祖先目录的 `modified_time`」的前提下才正确。我在本环境无法验证 PikPak 的行为，所以：

- 做了 `wms stocktake --verify`：不写任何东西，把整个网盘走一遍，与本地索引逐条比对，报告缺、多、变了的条目，不一致时退出码为 1；
- 测试里把两种情况都跑了：会传播时增量能发现深层变化；不传播时增量会漏，而 `--verify` 能报出来，`--full` 能补齐。

中断保护：一次盘点中途失败，未列完的目录会被标记，下次增量会从任何祖先处发现并补完，不会因为祖先的时间戳没变而永远跳过（测试覆盖，这个 bug 是写测试时抓到并修掉的）。

### 验收数字（测试夹具上）

夹具：1,202 个文件、1,263 条（含目录），分 12 部剧 × 4 季 × 25 集。

| 盘点 | 请求数 | 按 4 req/s 折算 |
|---|---:|---:|
| 首次 | 63 | 约 15.8 秒 |
| 二次（无变化） | 1 | 约 0.25 秒 |
| 深层新增一个文件后 | 4 | 约 1 秒 |

**真实网盘上的数字需要 Cowork 实测**（见下）。

### NAS 上要改什么

**无。** 这一阶段 bot 还不调用 WMS；镜像里多了 `pikpak_wms/` 和 `config/`，不影响 bot 运行。

### 给 Cowork 的核验手段

bot 当前是通过 Mini App 连接的 PikPak，环境变量里没有 PikPak 密码，所以**独立 CLI 在 NAS 上暂时登不上**。M3 会让镜像里的 `wms` 命令直接复用 bot 已连接的账号，届时按 M3 一节的命令实测即可，无需再输一次密码。

如果现在就想测，需要临时提供凭据（**用完即删**）：

```bash
docker compose run --rm -e PIKPAK_USERNAME='…' -e PIKPAK_PASSWORD='…' bot python -m pikpak_wms login
docker compose run --rm bot python -m pikpak_wms stocktake --full    # 记下耗时与请求数
docker compose run --rm bot python -m pikpak_wms stocktake           # 记下耗时与请求数
docker compose run --rm bot python -m pikpak_wms stocktake --verify  # 期望：一致
```

然后在 PikPak 网页端某个深层目录里新建一个文件，再跑一次增量盘点和 `--verify`：如果 `--verify` 报「缺 1」，说明 PikPak 不向上刷新时间戳，增量盘点需要改为依赖 `events` 接口或定期全量（待决问题 1）。

### 怎么回滚

不涉及运行中的 bot。回滚镜像到 `sha-b067192`（阶段 2e）即可。

### 待决问题

1. **PikPak 是否向上传播目录的 `modified_time`**，决定增量盘点能否按原设计工作。Cowork 按上面的步骤测一次即可定论。如果不传播，计划是：M2 做「基于 `events` 接口的增量盘点」（简报里「提过但没写成规格的功能」之一），并让定时任务每天做一次全量兜底。
2. **镜像体积**：新增依赖约增加十几 MB（未实测，需要 CI 构建后在 GHCR 上看）。如果在意，`typer`/`rich` 可以换成标准库 `argparse`，代价是 CLI 表格输出变朴素。

## 阶段 3 · WMS M2：规则引擎、计划流水线、审计与撤销

规格：`CC_BRIEF.md` §5 的 M2 行、`docs/wms/ARCHITECTURE.md` §4–§5，以及本阶段新写的 `docs/wms/EXTRAS.md`（简报里「提过但没写成规格」的五项：按 hash 去重、归档、出库、star / share、基于 `events` 的增量盘点）。

### 这一阶段做了什么

- **规则层 `pikpak_wms/rules/`**
  - `schema.py`：规则文件的 pydantic 校验。**未知字段一律报错**（比如把 `min_size` 拼成 `min_sise`，如果静默忽略，规则就会扩大到整个范围）。**规则文件里写不了永久删除。**
  - `matcher.py`：原设计的 8 种匹配器 `kind` / `name_regex` / `path_glob` / `mime` / `min_size` / `max_size` / `older_than` / `newer_than`，另加 4 种：
    - `extensions`；
    - `category`：视频 / 图片 / 音频 / 文档 / 压缩包 / 字幕，按 mime 或扩展名判断；
    - `empty`：空目录；
    - `time_field`：默认用 `created`，即文件进网盘的时间。
    - 这些是 M6 映射 Query 时要用的。
  - 时长 `30d`，日期 `2026-09-01`，都**按 `schedule.timezone`（默认 Asia/Shanghai）解释，不按容器时区**。
  - `template.py`：命名模板 `{show|title}`。过滤器有 `title` / `upper` / `lower` / `strip` / `spaces` / `pad2` / `date:%Y-%m`。模板里缺字段就报错，不会填成空串。
  - `actions.py`：动作原语，每个都有 `plan()` 和 `apply()`（铁律 1），另有 `check()`（执行前确认还该不该做）和 `inverse()`（撤销）。
    - 规则里可用的：`rename` / `move` / `copy` / `trash` / `star` / `share` / `create_folder` / `outbound`。
    - 内部用的：`untrash` / `unstar` / `delete_forever` / `inbound`。
  - `engine.py`：规则加本地索引生成 Plan，**不发任何请求**。求值顺序固定：
    - 规则按文件顺序求值，一个文件只归第一条命中它的规则；
    - 命中的目录带走它里面的东西；
    - 一条规则的各步骤横向执行（先全部改名，再全部移动），这样同一目标目录的移动能合成一个批量请求；
    - 目标名被占、模板缺字段时，跳过该文件并在计划里写一条备注，不猜。
- **计划流水线 `ops/plans.py`**（CLI、M4 面板、M5 bot 共用这一条）
  - 新增 `plans` 表（只加表），计划存为「待确认」，内容相同的待确认计划不重复存。
  - `apply` 支持断点续跑：每次最多执行 `runtime.max_actions_per_run`（默认 500）个动作，`--limit N` 可以更少，下次从断点接着做。遇到限流或登录失败立即停下并记住位置。
  - 每个动作执行前对照索引：已经做过的跳过（`done`），计划之后文件又变了的跳过（`changed`）。
  - 执行后**当场更新索引**，所以紧接着再出一次计划就是空的（铁律 5，测试覆盖）。已执行或已丢弃的计划不能再执行。
  - 某个文件失败时，只跳过这个文件后面的步骤，其余文件照做。
  - 批量接口一次最多 100 个 id。
- **审计与撤销**
  - `audit` 表加两列 `plan_id`、`undo_of`。迁移只加列（红线 3），旧库启动时自动补上，有测试。
  - `wms undo <id>` 默认只预览，加 `--apply` 才执行：
    - rename 改回原名；
    - move 移回原目录；
    - trash 从回收站还原（目录还原后，下次增量盘点会重新列出它）；
    - star 取消星标；
    - create_folder 把新建的目录放回回收站。
  - `wms undo` 会**拒绝**以下情况，并说明原因：
    - 文件在那之后又变过；
    - 已经撤销过；
    - share（PikPak 没有取消分享的接口）；
    - copy（新副本的 id 拿不到）；
    - 永久删除；
    - 目录原本就存在，或者已经被放进了东西。
- **五个业务模块**
  - `organize`：跑 `stage: organize` 的规则。`--dedupe` 是按 hash 去重：保留 `--keep-under` 下的那份，没有就保留最早的，再没有就保留路径最短的；其余进回收站。hash 相同但大小不同的不处理，只写备注。
  - `cleanup`：跑 `stage: cleanup` 的规则，只进回收站。`--forever` 需要**同时满足**三个条件：配置 `allow_permanent_delete: true`、命令行 `--forever`、交互确认（或 `--yes`）。**任何定时任务都走不到永久删除**，有测试。
  - `layout`：建出 `layout.ensure` 里缺的目录。执行时先查一次目录是否本来就在：本来就在的，撤销时不会把它放进回收站。
  - `inbound`：
    - 磁力 / URL 走离线下载，PikPak 分享链接走转存；
    - 同一个来源只入库一次（`tasks` 表唯一约束）；
    - `--poll` 或定时任务 `inbound-poll` 用一次请求更新离线任务状态。
  - `outbound`：`none` 只给直链，`aria2` 通过 JSON-RPC 交给 aria2（secret 只从 `ARIA2_SECRET` 读），`local` 先下到 `.part` 文件再改名，已存在同名同大小的文件就跳过。**直链不写进计划，也不写进审计。**
- **调度 `scheduler/runner.py`**
  - APScheduler，cron 按 `schedule.timezone` 解释；
  - 一把锁保证任务串行；上一轮没跑完时本轮跳过，不叠加；
  - 单个任务出错只记日志，不影响调度，更不会拖垮 bot（M3 会把它放进 bot 进程）。
  - 可用任务：`stocktake` / `stocktake-full` / `inbound-poll` / `layout` / `organize` / `cleanup`。
  - organize / cleanup 会先对规则涉及的目录做一次增量盘点；`apply: false` 时只存计划，等人确认。
- **`wms events --raw`**：原样打印 PikPak `events` 接口的返回。基于它的增量盘点**没有实现**，原因见 `EXTRAS.md` §5：字段没有文档，猜着解析，猜错时会悄悄漏掉变化。示例配置里加了每天一次的 `stocktake-full` 作兜底。
- **CLI 新命令**：`rules [--check]`、`organize [--rule] [--dedupe --scope --keep-under]`、`cleanup [--forever --yes]`、`layout`、`inbound <链接…> [--to] [--pass-code] [--poll]`、`outbound <路径…> [--to] [--downloader]`、`plans [--all]`、`plan <id>`、`apply <id> [--limit] [--forever]`、`discard <id>`、`audit [--plan] [--json]`、`undo <id>`、`run`、`events --raw`。
  - 写操作一律默认 dry-run，`--apply` 才执行。配置里 `runtime.dry_run: false` 可以改默认，`--dry-run` 始终可以强制只出计划。
- **i18n**：新增文案全部走 WMS 的 `t()`，中英两套 key 完全一致。
  - 计划备注、冲突原因、拒绝撤销的原因**以 key 加参数的形式落库**，展示时才翻译（红线 4），所以同一份计划切换语言后显示对应语言。
  - `WmsError` 可以带 key：`str(exc)` 是英文，供日志用；`exc.display()` 是用户的语言。这就是简报 §6 要求的做法，WMS 从一开始就这样写。
- **配置**
  - `rules.example.yaml` 补齐 M6 要求的预置模板：按类型分类（视频 / 图片 / 音频 / 文档 / 压缩包 → `/Media/…`）、按月归档（`/Archive/{created|date:%Y-%m}`，默认关闭）、星标示例（默认关闭）；清退规则标了 `stage: cleanup`。
  - `wms.example.yaml` 修了一个会在 M3 踩到的坑：原样例的 `store.database: data/wms.db` 是相对路径，在容器里会落到临时层，重建就丢。现在默认留空，也就是 `$DATA_DIR/wms.sqlite3`。
  - 新增 `rules_file`、`outbound.local_dir`（默认沿用 bot 的 `MEDIA_DIR`）。
- **依赖**：加了 `tzdata`（本地安装实测 2.8 MB），slim 镜像万一缺时区数据时，`Asia/Shanghai` 仍然可用。

### 验收证据

`CC_BRIEF` 的 M2 验收流程是「写一份 rules.yaml → dry-run 输出 Plan → apply 生效 → 审计可回溯 → undo 能撤销 rename / move，能从回收站还原」。这个流程就是 `tests/test_wms_m2.py::TestAcceptance`，全程计数写请求：dry-run 阶段为 0。

用仓库自带的 `config/rules.example.yaml`，在假网盘上实测：`/Inbox` 放 100 集剧、1 部电影、100 个短视频、40 张图、1 个广告文件，共 243 个文件。

- 盘点后出计划，**0 个请求**。计划有 348 个动作，涉及 242 个文件，共 132.9 GiB。
- 执行共 **117 个请求**：
  - 101 次改名（PikPak 没有批量改名接口）；
  - 5 次批量移动；
  - 1 次批量进回收站；
  - 10 次建目录与查目录。
- 执行完立刻再出计划：**0 个动作**。

按 4 req/s 折算，执行约 30 秒。**规模和耗时的主要来源是改名**，这正是简报「已知风险」里说的。可以用 `wms apply <id> --limit 50` 分批执行，观察是否触发风控。

测试：新增 78 个（`test_wms_rules.py` 42 个，`test_wms_m2.py` 36 个），全套 790 → 868 个，3.11 与 3.12 均全绿；`ruff check` 零告警。

### NAS 上要改什么

**无。** bot 仍然不调用 WMS（M3 才接入），镜像里只是多了代码。

### 给 Cowork 的核验手段

独立 CLI 在 NAS 上登录受限的问题同 M1（见上一节）。M3 之后可以直接用 bot 的账号。到时候按这个顺序跑：

```bash
wms stocktake
wms rules --check                   # 规则文件有效
wms organize                        # 只出计划：看每一行是不是你想要的
wms apply <计划号> --limit 20       # 先小批量，观察有无风控
wms audit                           # 每条改动都在
wms undo <审计号>                   # 预览撤销
wms undo <审计号> --apply           # 真撤销：rename / move / trash 各试一次
wms events --raw --limit 20         # 贴样本（见待决问题 2）
```

### 怎么回滚

不涉及运行中的 bot。回滚镜像到 M1 的提交 `sha-72249ad` 即可。WMS 库多出的 `plans` 表和 `audit` 的两列，旧代码不读，不影响。

### 待决问题

1. **分享链接转存后文件落在哪**：pikpakapi 的 `restore` 不接受目标目录，所以 `wms inbound <分享链接> --to X` 里的 `--to` 对分享链接不生效，文件会落在 PikPak 默认的位置。bot 现有的转存（`tgmd/pikpak.py`）也是这样。请 Cowork 转存一次，看落在哪个目录。如果不在 `/Inbox` 下，M5 的「入库后自动上架」需要把那个目录也写进规则的 `scope`。
2. **`events` 接口样本**：请在网盘里新增、改名、移动、删除各做一次，然后跑 `wms events --raw --limit 20`，把输出里的链接和缩略图地址去掉后贴到这里。拿到样本之后，再决定是否实现基于事件的增量盘点（`EXTRAS.md` §5）。
3. **批量改名的风控阈值**：上面实测的计划里，改名占了 101 个请求。建议第一次用 `--limit 20` 执行，然后逐步加大，看 PikPak 是否返回「操作频繁」。一旦返回，apply 会停下并记住位置，重跑同一条命令即可继续。
4. **M1 的待决问题 1（目录时间戳是否向上传播）依然有效**。M2 的定时 organize 在出计划前会先做增量盘点；如果时间戳不传播，增量盘点可能看不到新文件，要靠每天的 `stocktake-full` 兜底。

## 阶段 3 · WMS M3：同一个镜像、同一个账号、同一个卷

### 这一阶段做了什么

- **WMS 用 bot 已连接的 PikPak 账号**，不需要第二次登录，也不需要在 `.env` 里放密码。用哪个账号：
  1. `WMS_ACCOUNT`：一个 Telegram 用户 id；或写 `shared`，固定用 `.env` 里的共享账号；
  2. 否则用第一个自己连接了 PikPak（Mini App 或聊天内登录）的 admin；
  3. 否则用 `.env` 里的共享账号。
  - 账号是**每次调用时现选**的，所以 bot 运行中 admin 刚连上账号，WMS 下一次就会用上。
  - 用的就是 bot 数据库里的 token，没有另存一份。token 刷新后照旧写回 bot 的库。
- **`WMS_ENABLED=true`**：bot 启动时顺带启动 WMS 调度器，按 `wms.yaml` 里的 `schedule.jobs` 定时跑。
  - 默认关闭，旧 compose 一字不改也照常启动（红线 2）。
  - WMS 配置写错时只记一条错误日志，**bot 照常启动**（有测试）。
  - 停止 bot 时调度器一起停。
- **镜像里有 `wms` 命令**：`docker compose run --rm bot wms <命令>`，通过 `python -m tgmd.wms` 用 bot 的账号运行 M1、M2 的全部 CLI 命令。
  - `wms doctor` 的「凭据」一行会写「bot 已连接的 PikPak 账号」。
  - 没有可用账号时，给出一句说明并以退出码 1 结束。
- **配置、规则、索引都在数据卷上**：
  - 不设 `WMS_CONFIG` / `WMS_RULES` 时，先找 `$DATA_DIR/wms.yaml` / `$DATA_DIR/rules.yaml`（容器里是 `/data/db/`，也就是宿主机的 `./data/db/`），找不到才用 `config/`；
  - 索引与审计是 `/data/db/wms.sqlite3`。
  - 所以重建镜像、重启容器，token、索引、计划、审计、规则都不丢。
- **边界**：tgmd 只 import `pikpak_wms.ops`，有 AST 扫描测试。入口是新的 `pikpak_wms/ops/embed.py`，由它再调用调度器和 CLI。
- **顺手修的 M1 bug**：CLI 的 `main()` 在非 standalone 模式下丢了 click 返回的退出码，出错的命令也以 0 结束。CliRunner 测的是 typer app 本身，所以没有测出来；换成 `python -m tgmd.wms` 的测试后才暴露。

### 验收证据

在本环境用真实 Docker 构建了镜像。为了让 pip 信任本沙箱的 HTTPS 代理，构建用的是临时 Dockerfile，只多加一张 CA 证书，仓库里的 Dockerfile 没改。实测结果：

- `docker run --rm IMAGE wms version` → `pikpak_wms 0.2.0`。`/usr/local/bin/wms` 就是那两行 shim。
- 带卷 `-v m3data:/data` 跑 `wms doctor`：生成了 `/data/db/wms.sqlite3`，属主 `tgmd`；换一个新容器再挂同一个卷，文件还在。
- 在没有任何 PikPak 账号的容器里跑 `wms quota`：输出「WMS has no PikPak account to use: …」，退出码 1。
- **镜像体积（M1 待决问题 2 的答案）**：本地构建阶段 2e（`b067192`）与本阶段的镜像，解压后分别是 225 MB 和 267 MB，**WMS 带来约 42 MB**；按压缩后大小算是 52.5 MB 和 61.5 MB，**约 9 MB**，也就是 NAS 实际要多拉取的量。

测试：新增 19 个（`tests/test_wms_m3.py`），全套 868 → 887，Python 3.11 与 3.12 均全绿；ruff 零告警。其中两个测试直接对应「重启不丢」：

- **token**：bot 数据库里存的 token，关库、重开（模拟重启）之后，WMS 拿到的客户端用的就是它；
- **索引**：`wms stocktake` 之后，另起一次 `wms ls` 直接读卷上的索引，不向 PikPak 发请求。

### NAS 上要改什么

**默认什么都不用改**，这一版的行为和上一版相同。想用 WMS 时按下面的步骤来。

1. `docker compose pull && docker compose up -d`，更新镜像。
2. 把配置和规则放到数据卷上，文件名要对（宿主机路径就是 compose 里 `./data` 挂载的那个目录）：

   ```bash
   docker compose run --rm bot sh -c 'cp config/wms.example.yaml /data/db/wms.yaml && cp config/rules.example.yaml /data/db/rules.yaml'
   ```

   然后在宿主机上编辑 `./data/db/rules.yaml`，把规则改成你要的目录结构。
3. 先手动试（此时 bot 不需要改任何环境变量）：

   ```bash
   docker compose run --rm bot wms doctor         # 凭据一行应为「bot 已连接的 PikPak 账号」
   docker compose run --rm bot wms stocktake --full
   docker compose run --rm bot wms stocktake --verify
   docker compose run --rm bot wms organize       # 只出计划
   ```

4. 确认计划没问题之后，想让它定时跑，就在 compose 的 `environment` 里取消注释 `WMS_ENABLED: "true"`，然后 `docker compose up -d bot`。
   - 日志里会出现 `WMS started: config /data/db/wms.yaml, database /data/db/wms.sqlite3, N job(s) scheduled`。
   - `wms.yaml` 示例里 organize / cleanup 都是 `apply: false`，也就是只存计划等确认；定时生成的计划用 `wms plans` 查看，用 `wms apply <号>` 执行。

`docker compose run --rm bot wms ...` 和运行中的 bot 同时访问同一个 SQLite 没有问题（WAL 模式）。但**不要在 bot 开着 `WMS_ENABLED` 的同时再手动 `wms apply` 同一个计划**：两边会各执行一部分。已执行过的动作会被跳过，结果仍然正确，只是多花请求。

### 用户需要在 Telegram 里做什么

无。WMS 的 bot 命令（`/wms`）在 M5。

### 怎么回滚

- 只想关掉 WMS：去掉 `WMS_ENABLED` 或设为 `false`，重启 bot。
- 回滚镜像：回到 M2 的 `sha-12009ef`。`/data/db/wms.*` 可以留着，旧版本不会读它们。

### 待决问题

1. **管理哪个账号**：默认是「第一个连了 PikPak 的 admin」，用户在 Mini App 里连的就是自己的主账号时这是对的。如果要管 `.env` 里的共享账号，设 `WMS_ACCOUNT=shared`。请用户确认要管哪一个。
2. M1、M2 的待决问题（目录时间戳是否传播、`events` 样本、分享链接转存后文件落在哪、批量改名的风控阈值）依然有效。现在不需要临时密码就能在 NAS 上实测了，命令见上面第 3 步和 M2 一节。

## 阶段 3 · WMS M4：仓储面板（Telegram Mini App）

### 这一阶段做了什么

- `tgmd/wms_panel.py`：挂在 bot 自己的 HTTP 服务器上，`GET /wms/app` 返回页面，`POST /wms/api` 处理操作。
  - 身份校验和 PikPak 登录 Mini App 完全一样，用 Telegram 签名的 `initData`。**只有 admin 能用**，普通白名单用户会被拒绝（403）。
  - 功能：
    - 待确认的计划列表（带索引条数、上次盘点时间）；
    - 查看计划全文；
    - 确认执行 / 丢弃；
    - 审计列表，每条都可以撤销：先预览，确认后执行。
  - 全部调用和 `wms apply` / `wms undo` 同一条流水线（铁律 6）。所有写操作都拿调度器的同一把锁，所以面板上的操作不会和定时任务交叉执行。
  - **面板里碰不到永久删除**：含永久删除动作的计划会被拒绝，即使配置开关已经打开也一样（铁律 2），有测试。
  - 页面文案全部走 `t()`，以一个 JSON 对象注入页面（阶段 4 对 Mini App 文案的要求，这里从一开始就这样做）。有测试验证：文案里即使带 `</script>` 也关不掉脚本。`<html lang>` 跟随语言。
- `/wms` 命令（admin）：回复仓储状态，并附「打开仓储面板」按钮。没有公网 HTTPS 地址时，改为说明为什么没有按钮。WMS 没开时提示设置 `WMS_ENABLED`。命令已加入菜单和帮助（中英）。M5 会在它下面加子命令。
- `FileServer` 新增 `routes=[...]` 参数，用来挂面板这类额外路由（旧的 `portal=` 参数不变）。
- WMS 的语言跟随 bot 的语言。bot 的语言可能写在 `config.yaml` 里而不是环境变量里，所以启动时显式同步一次。

### 验收证据

- 测试：新增 17 个（`tests/test_wms_m4.py` 15 个；另外两个是菜单测试随新命令自动多出来的参数化用例），全套 887 → 904，3.11 与 3.12 全绿；ruff 零告警。
  - 面板测试走真实 socket：伪造的 initData 返回 401，非 admin 返回 403，WMS 关闭时返回 503；从列表到查看、执行、审计、撤销的完整流程，以及重复撤销被拒；丢弃之后不能再执行；永久删除计划被拒且网盘没有收到删除请求。
- **真实浏览器实测**：本环境的 Chromium（Playwright）打开中文面板，注入一个签过名的 `Telegram.WebApp` 替身，完整点了一遍：
  - 列表显示「计划 1（organize）：8 个动作，涉及 4 个文件，共 2.6 GiB」；
  - 查看计划、确认执行，页面显示「计划 1：执行 8，跳过 0，失败 0，剩余 0」；
  - 切到审计，撤销最新一条（入回收站），确认框里是「确定撤销这处改动吗？从回收站还原 /Inbox/最新地址广告.txt」，执行后显示「完成。」；
  - **页面脚本零报错**。截图在会话里核对过：手机宽度下布局正常，长路径自动换行。
- 改动：10 个文件，代码 +500 / −10 行（`tgmd/wms_panel.py` 275 行），测试 293 行。
- 更正：M3 提交信息里写的「改动：15 个文件」不对，实际是 17 个（计数之后又改了 `.env.example` 和 HANDOFF）。

### NAS 上要改什么

面板需要 bot 有**公网 HTTPS 地址**，跟 PikPak 登录 Mini App 的前提相同：`HTTP_ENABLED=true` 加 `PUBLIC_BASE_URL=https://…`。NAS 目前没有公网地址（阶段 2d / 2e 的待决问题），所以：

- **现在**：`/wms` 可以用（显示状态，并说明为什么没有面板按钮）；计划的确认用 `docker compose run --rm bot wms apply <号>`。
- **有了 HTTPS 地址之后**（比如阶段 1 一节里的 Tailscale Funnel 地址可用时）：不需要任何额外设置，`/wms` 自动带上面板按钮。

### 用户需要在 Telegram 里做什么

以 admin 身份私聊 bot 发 `/wms`。有 HTTPS 地址时点「打开仓储面板」，在「计划」页查看并确认执行，在「审计」页撤销。

### 怎么回滚

回滚镜像到 M3 的 `sha-e60ea98`。M4 没有新的数据库改动。

### 待决问题

1. **公网 HTTPS 地址**：面板和 PikPak 登录 Mini App 都卡在这一点上，M4 的手机端验收（CC_BRIEF：「在手机 Telegram 里打开面板，确认一个 Plan」）要等它解决。阶段 1 一节记录过一个 Tailscale Funnel 地址；如果它可用，按那一节的说明设 `HTTP_ENABLED` 和 `PUBLIC_BASE_URL` 即可，面板不需要额外配置。**改之前先看一眼 NAS 上现在的值。**

## 阶段 3 · WMS M5：`/wms` 命令族与入库后自动上架

### 这一阶段做了什么

- **`/wms` 命令族**（只对 admin 开放，普通用户会被明确拒绝）：
  - `/wms`（或 `/wms status`）：状态，有 HTTPS 地址时附面板按钮；
  - `/wms stocktake [full]`：刷新索引；
  - `/wms plan`：按 organize 规则出计划，附 [确认执行] [丢弃] 按钮；`/wms plan <号>`：查看已有计划；
  - `/wms apply <号>`：执行计划；
  - `/wms undo <审计号>`：先预览，附 [确认撤销] 按钮；
  - `/wms rules`：列出规则文件（显示实际路径）；
  - `/wms help`：用法。
  - 按钮点下去之后，原消息被改成执行结果，按钮随之消失，所以不会重复执行。旧消息上的按钮再点，会弹出拒绝原因，比如「已执行」。
  - bot 里碰不到永久删除（铁律 2）。
- **入库后自动上架**：
  - 触发条件：一个任务把文件送进 PikPak 并成功结束，包括磁力 / URL 离线下载、分享链接转存、pikpak 模式下的 Telegram 媒体。
  - 前提：送进的必须是 **WMS 管理的那个网盘**。普通用户连了自己的账号，文件进的是他们自己的网盘，不会被动。
  - 之后按 `WMS_AUTO_SHELVE` 处理：
    - `plan`（默认）：先对规则涉及的目录做增量盘点，再出计划，把计划连同 [确认执行] [丢弃] 按钮发到这个任务所在的聊天；
    - `apply`：直接执行，然后发执行结果；
    - `off`：不处理。
  - **防抖**：最后一个任务结束后等 20 秒才上架，所以一次发一批链接只会得到一份计划。
  - **目录没有被任何规则覆盖时**：转存落在的目录（`PIKPAK_FOLDER`，默认 `/TelegramMedia`）如果不在任何 organize 规则的 scope 里，上架永远找不到这些文件。这时 bot **提示一次**，说明怎么改，而不是默默不做事（有测试）。
- **顺手修的 M2 bug**：organize 任务出计划前要对每条规则的 scope 做增量盘点。如果 scope 目录在网盘里还不存在（比如还没有 `/Inbox`），整个任务会失败，其他规则也跟着不跑。现在跳过不存在的 scope（有测试）。
- 实现方式：JobQueue 新增 `after_pikpak` 钩子，钩子出错只记日志，不影响任务本身的结果；按钮用的是新的 `callback_buttons`（Telethon 1.45 的 layer 229 写法，与现有 `webview_button` 放在一起，有序列化测试）。

### 验收证据

`CC_BRIEF` 的 M5 验收是「在手机上转发一条磁力链接，文件按规则落到正确目录」。去掉手机这一步，就是 `tests/test_wms_m5.py::test_a_magnet_link_ends_up_where_the_rules_say`：

- 磁力链接走**真实的 JobQueue**，交给假的 PikPak；假 PikPak 把文件放进 `/Inbox`；
- 钩子触发自动上架（apply 模式），文件最终出现在 `/Media/Lost/S01/Lost.S01E01.mkv`，聊天里收到「已按规则上架」。

测试：新增 19 个（`tests/test_wms_m5.py` 18 个，`test_wms_m2.py` 1 个），全套 904 → 923，3.11 与 3.12 全绿；ruff 零告警。覆盖了：

- 防抖：两个任务只出一份计划，两处聊天都收到；
- 两种模式；没有东西可上架时什么都不发；别人的网盘不动；
- 目录不被规则覆盖时只提示一次；
- `/wms` 各子命令，包括只有 admin 能用、WMS 关闭时的提示；
- 按钮：执行后按钮消失，再点给出拒绝原因；丢弃、撤销；拒绝不合规的撤销；非 admin 点按钮被拒绝；乱码数据被忽略。

### NAS 上要改什么

在 M3 那一节的步骤之后（`WMS_ENABLED=true`，规则在 `./data/db/rules.yaml`）：

1. **让转存落在规则能看到的地方**。示例规则的 scope 是 `/Inbox`，所以二选一：
   - 设 `PIKPAK_FOLDER=/Inbox`（compose 里有注释占位）。**先看一眼 NAS 上现在的值**：如果已经设了别的目录、而且用户习惯那个目录，就改用第二种；
   - 或者把 `rules.yaml` 里各条 organize 规则的 `scope` 改成现在的 `PIKPAK_FOLDER`。
2. `WMS_AUTO_SHELVE` 不用设，默认 `plan`，也就是先发计划等确认。观察几天没问题，再考虑改成 `apply`。
3. `docker compose up -d bot`。

### 用户需要在 Telegram 里做什么

- 像平常一样发磁力链接或分享链接，或在 pikpak 模式下发媒体。转存完成大约 20 秒后，bot 发来一份整理计划，点 [确认执行] 或 [丢弃]。
- 随时可以发 `/wms`、`/wms plan`、`/wms rules` 等命令，`/wms help` 看全部用法。

### 怎么回滚

- 只关自动上架：`WMS_AUTO_SHELVE=off`。
- 整个 WMS 都关：`WMS_ENABLED=false`。
- 镜像回滚到 M4 的 `sha-4df6892`。本阶段没有数据库改动。

### 更正

M4 提交信息里写的「10 个文件」不对，实际是 12 个（计数之后又加了 HANDOFF 和 ROADMAP）。M3、M4 连续两次出现同样的错误，原因相同。本次提交的数字是在最后一次 `git add` 之后统计的。

### 待决问题

1. **离线下载的完成时机**：bot 报告磁力任务「完成」时，PikPak 那边有时还没下完。这时的计划里不会有这个文件，它会在下一次定时 organize（示例配置是每小时）或下一次入库时被补上。如果 Cowork 实测发现磁力的自动上架经常什么都找不到，可以考虑让 bot 等 PikPak 任务真正完成后再触发。
2. 分享链接转存后文件落在哪（M2 待决问题 1）仍然未知，所以分享链接那一路不参与「目录没有被规则覆盖」的检查。
3. M4 的公网 HTTPS 地址仍是面板的前提；`/wms` 命令和计划按钮**不需要**它，现在就能用。

## 阶段 3 · WMS M6：自然语言指令

规格：`docs/wms/M6-natural-language.md`（Cowork 起草，原样在仓库里）。

### 这一阶段做了什么

- **核心原则照办：模型只翻译，不执行。**
  - 翻译器唯一的输出是经过 pydantic 校验的 `Query`（`pikpak_wms/nl/query.py`），字段按 M6 §3。
  - Query 编译成 M2 的规则（映射按 §3），走同一条流水线：计划 → 确认 → 执行 → 审计。
  - 有歧义就反问，不猜。永久删除不可达。
- **三个后端，一个接口** `Translator.translate(text, now, tz) -> Query | Clarification | None`（`pikpak_wms/nl/translator.py`）：
  - `rules`（`nl/rules_parser.py`，永远第一个跑）：确定性的中文解析器，覆盖 §4 列的全部说法。时间：今天 / 昨天 / 前天 / 本周 / 上周 / 本月 / 上个月 / 今年 / 去年 / 最近 N 天·周·个月 / N 天前 / 具体日期 / 日期区间；此外还有大小（含区间）、类型、扩展名、名称（包含 / 开头 / 结尾）、目录、意图关键词、每天·每周·每月·每小时（含几点）。
    - **零误判的做法**：只有句子里**每一个**词都被认出来才接；有任何没认出的词（包括「不要」「除了」这类否定）就不接，交给模型；自相矛盾的（「今天和昨天」「两个目录」）也不接；路径里夹着指令词的（「/Media并删除图片」）也不接。
  - `claude`：官方 `anthropic` SDK，结构化输出（`output_config.format` 绑定 Query 的 JSON Schema），`effort: low`。默认模型 `claude-opus-5`，可用 `NL_CLAUDE_MODEL` 改。用默认模型时带上服务端的拒答回退（`fallbacks: "default"`），被安全策略拒答时由服务端换模型重试；换成其他模型时不带这个参数。
  - `ollama`：`/api/chat`，`format` 传同一份 JSON Schema（约束解码），`temperature 0`。默认 `qwen2.5:3b`，地址 `OLLAMA_URL`。
  - 配置：`NL_BACKEND=rules|claude|ollama`（默认 rules），`NL_FALLBACK=none|claude|ollama`。顺序永远是 rules → NL_BACKEND → NL_FALLBACK；某个模型挂了就记日志、跳过，不影响 rules 能接的句子。
- **Bot 入口**：
  - `/do <一句话>`；admin 在私聊里发的纯文本（不是链接、不是命令）也进翻译器。发链接下载的老行为不变：链接优先匹配。普通用户和群聊不受影响。
  - 计划消息附 [确认执行] [修改] [取消]。
  - 反问之后直接回复就行，回复会接在原句后面重新理解；点 [修改] 同理。
  - 按钮只对发起人有效，用过就作废；[取消] 会把存下的计划一起丢弃。
- **计划里写明怎么理解的**：
  - 意图；
  - 范围；
  - 时间的起止和时区；
  - 一句「「转存 / 入库」按文件进网盘的时间（created_time）判断」；
  - 大小、类型、扩展名、名称条件；
  - 目的地（下载会写出 NAS 上的完整路径）；
  - 命中多少个文件、共多大、前几个文件名（新的在前）。
  - 这些说明以 key 加参数的形式落库，展示时才翻译（红线 4）。
- **定时**：「每天……」「每周……」这类句子不生成一次性计划，而是生成规则。
  - 确认后以追加文本的方式写进规则文件，原文件的注释和格式都保留；原句会作为注释写在规则上方。写完立即校验，失败就还原原文件。
  - 写入后立即排进调度，不用重启；每条规则可以有自己的 `schedule: {cron, apply}`。
  - 为了铁律 1，`apply` 默认是 false：每次到点只生成计划，然后**主动发给所有 admin**，附确认按钮。同一份计划只发一次；手动触发的运行不会重复通知。
- **预置规则模板**：M2 已经放进 `config/rules.example.yaml`。M6 的「分类 / 归档 / 下载」分别对应哪个目录，写在 `wms.yaml` 的 `nl:` 段（`classify`、`archive_root`、`download_to`），有默认值。
  - 「按类型分类」会跳过已经在分类目录里的文件，不会把 `/Media/视频` 里的东西再搬一遍。
- **定向下载 = 出库 local**：规则的 `outbound` 动作新增 `via`，NL 的「下载」固定走 `local`，也就是下载到 NAS 媒体目录下的 `PikPak/` 子目录，不受 `outbound.downloader` 默认值影响。
- **命令行**：`wms do "<一句话>" [--apply]`。有了它，Cowork 不用手机也能测完整流程。
- **README 写明了隐私边界**：模型只收到那句话、schema 和当前日期时间时区。文件名、目录列表等网盘内容一概不发。默认的 `NL_BACKEND=rules` 什么都不外发。
- **依赖**：`anthropic>=1.8,<2`。

### 验收证据

- **评测集** `tests/nl/cases.yaml`：70 条中文，其中 48 条给出期望的 Query（包括用户原话）、14 条应当反问、8 条应当拒绝。三类里一共混有 5 条专门挑出来的易错句（否定句、一句两个动作、两个时间、路径吞掉后半句），用作回归。
- **rules 后端**：覆盖率 **80.0%**（56/70），**零错误**，平均 0.4 ms。这项有测试（`test_rules_covers_seventy_percent_with_zero_wrong`），将来谁改坏了 CI 会拦住。
  - 过程中抓到并修掉了 4 类误判：路径被转成小写；「昨天下载的图片」被当成下载指令；路径吞掉后半句；后面的条件悄悄覆盖前面的条件。前 3 类都已写进评测集回归；第 4 类由 `删除今天和昨天的视频` 等条目覆盖。
- **端到端**（`test_wms_m6.py::TestTheUsersSentence`）：「下载今天转存到网盘的所有大于1GB的视频」→ rules 解析 → 计划里写着「进网盘时间晚于 2026-09-24 00:00（Asia/Shanghai）」「按文件进网盘的时间（created_time）判断」「命中 2 个文件，共 5.0 GiB」和两个文件名；昨天的、不到 1GB 的、今天的大图片都没算进去 → 执行 → 两个文件落到 NAS 媒体目录的 `PikPak/` 下 → 审计里有两条 `outbound`，`downloader: local`。
- **Bot 流程**（`test_wms_m6_bot.py`，12 个）：`/do`、admin 私聊纯文本、普通用户和群聊不受影响、WMS 关闭时的行为、反问后合并、修改、取消、别人的按钮无效、定时规则写入并排程、定时计划发给 admin 且只发一次、PikPak 出错时给出说明而不是崩溃。
- **模型后端**都用假的测（不联网）：
  - 请求里只有「Now: … (time zone …)」和那句话；
  - schema 只用结构化输出支持的写法；
  - 有拒答、乱码、没有凭据、模型反问这几种情况的测试。
- **真实镜像**（本地 Docker）：`python -m pikpak_wms.nl.eval --backend rules` 在镜像里跑，结果同上；`anthropic 1.8.0` 可导入；没有 key 时 `--backend claude` 70 条都报「没有凭据」的错，程序不崩。
  - **镜像体积**：M3 时 267 MB → 294 MB（解压后），61.5 → 64.6 MB（压缩后），主要是 anthropic SDK。
  - 本次构建时 Docker Hub 限流（429），基础镜像改从 `mirror.gcr.io/library/python:3.12-slim`（Docker 官方镜像在 Google 上的镜像）拉取；仓库里的 Dockerfile 没改。
- 测试 923 → 961（+38：`test_wms_m6.py` 24、`test_wms_m6_bot.py` 12；另外 2 个是菜单测试随 `/do` 自动多出的参数化用例），3.11 与 3.12 全绿；ruff 零告警。

### NAS 上要改什么

前提是 M3 那一节的 `WMS_ENABLED=true`。

- **只用 rules 后端**：什么都不用加。admin 私聊发一句话，或者 `/do 一句话`，就能用。
- **加 Claude**：在 `.env` 里加 `ANTHROPIC_API_KEY=…`（**只放 .env，绝不入库**）和 `NL_BACKEND=claude`，然后重启 bot。流量走现有的 mihomo 代理。
- **加 Ollama**：compose 文件末尾有一段注释掉的 `ollama` 服务，带 `mem_limit: 4g`、`cpus: 2`。取消注释后：
  1. `docker compose up -d ollama`；
  2. `docker compose exec ollama ollama pull qwen2.5:3b`；
  3. bot 的环境变量里设 `NL_BACKEND=ollama`（或者把它作为 `NL_FALLBACK`）和 `OLLAMA_URL`（见待决问题 1）。
- 「下载」会落到 bot 的 `MEDIA_DIR`（没设就是 `DOWNLOAD_DIR`）下的 `PikPak/`。**媒体目录的宿主机路径仍是阶段 2d 的待决问题**，在它确定之前，文件落在容器的 `/data/downloads/PikPak/`，也就是宿主机的 `./data/downloads/PikPak/`。

### 用户需要在 Telegram 里做什么（端到端验收，M6 §7）

1. 以 admin 身份私聊 bot，发：`下载今天转存到网盘的所有大于1GB的视频`。
2. 收到计划：核对时区（Asia/Shanghai）、「按文件进网盘的时间判断」这一句、命中数、总大小、文件名，看是否和 PikPak 里今天的文件一致。
3. 点 [确认执行]。文件会出现在 NAS 媒体目录的 `PikPak/` 下。
4. `/wms`，或者面板的「审计」页，能看到 `outbound` 记录。

### 给 Cowork 的核验手段

```bash
docker compose run --rm bot python -m pikpak_wms.nl.eval --backend rules          # 期望：覆盖率 0.8，wrong 0
docker compose run --rm bot python -m pikpak_wms.nl.eval --backend claude --json  # 需要 .env 里的 ANTHROPIC_API_KEY
docker compose run --rm bot python -m pikpak_wms.nl.eval --backend ollama         # 需要 OLLAMA_URL，CPU 上会慢
docker compose run --rm bot wms do 下载今天转存到网盘的所有大于1GB的视频          # 只出计划，不执行
```

eval 报告里有模型后端的准确率和平均延迟，请把两个模型的这两个数字贴回这里。rules 后端「不接」的句子，正是模型后端要处理的部分。

### 怎么回滚

- 只关自然语言：没有单独的开关；不设 `NL_BACKEND` 就只剩本地解析器，什么都不外发。要完全不接收纯文本，关掉 `WMS_ENABLED`。
- 删掉 `/do` 生成的定时规则：在 `./data/db/rules.yaml` 里删掉那几条（每条上方都有原句注释），重启 bot。
- 镜像回滚到 M5 的 `sha-d3cabb3`。本阶段没有数据库改动。

### 以后再说（M6 §8）

把 WMS 的 ops 包装成 MCP server，让用户在 Claude 应用里说一句话就能管网盘。这一阶段按要求**不做**。要做的话，公网暴露之前必须先设计鉴权：至少要有单用户令牌和来源限制，并且与 Telegram 的 admin 身份打通。

### 待决问题

1. **bot 容器能不能访问 Ollama**：bot 用的是 `network_mode: service:proxy`（和 mihomo 共用网络栈），compose 服务名 `ollama` 能不能解析、mihomo 的 TUN 会不会拦截 `11434` 端口，我没法在这里验证。建议先试 `OLLAMA_URL=http://<NAS 局域网地址>:11434`；如果 mihomo 拦截了，就在 mihomo 规则里给这个地址加一条 `DIRECT`。
2. **Claude 模型的选择**：默认 `claude-opus-5`（效果最好）。句子很短，按一句几百 token 算单次花费很低，但用户如果想更省，可以设 `NL_CLAUDE_MODEL=claude-haiku-4-5`。这要用户来决定，我没有替用户降级。
3. **「最近 N 个月」按 N×30 天、「去年」按日历年**，是 rules 后端的约定，计划里会写明。用户若希望「上个月」「最近一个月」表示别的意思，告诉我改。
4. **媒体目录的宿主机路径**（阶段 2d 的待决问题）决定了「下载到 NAS」最终落在哪里。

## 阶段 4 · 中文化第二批

范围按 CC_BRIEF §6：`setup.py`、`portal.py`、`verify.py`，以及用户会直接看到的异常消息。

### 这一阶段做了什么

- **异常：带 key，展示时翻译**（`tgmd/i18n.py` 的 `Explained` 和 `describe()`）。
  - 用户会看到的异常类都继承 `Explained`：`ResolveError`、`PikPakError`、`LinkError`、`DeliveryError`（含 `TooLargeToUpload`）、`DownloadError`、`BotTokenError`、`ClaimError`、`QueueFull`，另外新增一个 `SessionError`（`/setup telegram` 收尾时接管会话失败）。
  - 抛出时只写 `key` 和参数，比如 `ResolveError(key="err.resolve.no_username", name=...)`。英文原文只在目录里存一份，`str(exc)` 取的就是这份英文，所以**日志和数据库里仍是英文**（红线 4）。
  - 展示给人看的地方一律用 `describe(exc)`，按当前语言出文本。涉及 handlers、tasks、setup、verify、portal、links，以及 WMS 的「没有账号」提示。
  - 包在外面的错误会把里面的错误一起翻译。例如 PikPak 的错误经 `DeliveryError` 转一手，读者看到的是整句中文。
  - 共 68 处抛出点，覆盖 CC_BRIEF 估的约 76 条；差额是阶段 1 删掉的那部分，加上几处重复文案合并成同一个 key。
  - 仍保持英文、只进日志的有：`ConfigError`、`TokenError`、`InitDataError`，这是原计划。
  - 顺手修了一处：`QueueFull` 以前把**已翻译**的文字写进 `jobs.error`，现在写英文。批量任务的失败摘要同理：给人看的是译文，落库的是英文。
- **`setup.py`**：整个向导（状态、PikPak 登录、Telegram 登录、超时、重试）改成走 `t()`。新增 43 条目录项，英文原文一字未改。
- **`verify.py` 和 `/verify`**：
  - 每条检查结果和总评都走 `t()`。
  - 检查名分两层：`Check.name` 仍是稳定的英文 id（测试和代码都按它找），另加一个 `Check.label` 负责显示。
  - `python -m tgmd.verify` 也按 `TGMD_LANG` 或配置里的 `language` 说话。配置加载失败时的报错也一样。
- **命令行对齐**：`ljust` 按字符数补空格，中文一个字占两列，列就歪了。新增 `tgmd.utils.display_width`（全角和宽字符记 2 列，组合符记 0 列），按显示宽度补齐。实际输出：

  ```
  ✓ 配置        已加载，各项配置互相一致
  ✓ 机器人令牌  格式正确，对应机器人 ID 123456789
  ! 用户会话    未配置。没有它，只能读取机器人自己所在的聊天。请在 Telegram 里用 /setup telegram 登录一个。
  - 缓存聊天    未配置；每次请求都会重新下载（设置 CACHE_CHAT_ID）
  ✓ HTTP 服务   已绑定 0.0.0.0:8080
  ```
- **PikPak 登录 Mini App（`portal.py`）**：
  - 页面文案走 `t()` 并做 HTML 转义。
  - JS 要用的文案放在 `<script type="application/json">` 里，以 JSON 注入，`</` 已转义，做法与 M4 仓储面板一致。
  - 连接成功后的页面改用 DOM 节点加 `textContent` 拼出来，不再拼 `innerHTML`。
  - `<html lang>` 跟随语言。CC_BRIEF 说的两处硬编码，另一处在阶段 1 已随「一次性登录链接」页面一起删除，现存的只有这一处。
  - 接口返回的错误和 `unavailable_reason()` 都已翻译。
- **投递结果摘要**（已发送、已保存到…、已存入 PikPak、PikPak 仍在拉取、已加入离线下载）也进了目录。这几条原本不在 §6 的清单上，但用户每次下载都会看到。
- **目录规模**：178 → 397 条（+219：setup 43、异常 64、verify 88、portal 22、会话 2），en 和 zh 两边键完全一致。

### 验收证据

- 新增 `tests/test_i18n_batch2.py`，26 个测试：
  - **AST 扫描 `tgmd/` 全部源码**：上面每个异常类的每一处抛出都必须带 `key=`、不带位置参数的英文消息，而且 key 在 en 和 zh 里都存在（目前 68 处）。
  - **AST 扫描所有 `t("…")` 字面量**：key 必须存在于目录中。当前零缺失。
  - `str(exc)` 是英文、`describe(exc)` 是中文；嵌套错误整句翻译；外部库的错误原样透传；链接解析错误以中文进入回复。
  - verify 报告：检查 id 不变而标签翻译；en 和 zh 下详情列按显示宽度对齐；`TGMD_LANG=zh` 时命令行输出「配置错误：…」。
  - Mini App 页：`lang` 跟随语言；中文页上除 PikPak、Telegram 和 `/pikpak logout` 以外没有可见的英文；译文里写 `</script>` 也关不掉脚本；标题里的 HTML 会被转义。
- `tests/test_tasks.py` 加了 1 个测试：中文环境下任务失败时，状态消息是中文，`jobs.error` 是英文。
- `tests/test_i18n.py` 的占位符一致性和命令名不翻译两项测试照常通过（`{value!r}`、`{needed:.0f}` 这类带转换或格式的占位符，两种语言也一致）。
- 测试 961 → 988（+27），3.11 与 3.12 全绿；ruff 零告警。
- 改动（代码与测试，不含 README 和本文件）：21 个文件，+1384 / −470 行。大头是 `tgmd/i18n.py` 的目录（+759）和新测试（+259）。

### NAS 上要改什么

什么都不用改。语言仍由 `TGMD_LANG`（或 `BOT_LANG`、配置里的 `language`）决定，没有新增环境变量。已经设了 `TGMD_LANG=zh` 的，更新镜像、重启之后，向导、`/verify` 和登录页就是中文。

### 用户需要在 Telegram 里做什么

1. `/verify`：检查名和说明都应是中文，末尾一句中文总评。
2. `/setup`：状态页和每一步提示都是中文。
3. `/pikpak login` 打开 Mini App：标题「连接你的 PikPak 账号」；故意输错密码，红字提示应以「PikPak 拒绝了这组凭据：」开头，冒号后面是 PikPak 返回的原文。
4. 发一个不存在的频道链接，比如 `https://t.me/nosuch_channel_xyz/1`：应回复「不存在名为 @nosuch_channel_xyz 的聊天」或「无法解析 @nosuch_channel_xyz」，具体是哪一句取决于 Telegram 返回哪种错误。

### 给 Cowork 的核验手段

```bash
docker compose run --rm -e TGMD_LANG=zh bot python -m tgmd.verify   # 中文、列对齐
docker compose run --rm -e TGMD_LANG=en bot python -m tgmd.verify   # 英文输出与阶段 3 相同
docker compose logs bot | grep -P '[\x{4e00}-\x{9fff}]'              # 红线 4：除文件名、聊天标题这类用户数据外，日志里不应有中文
```

### 怎么回滚

镜像回滚到 M6 的 `sha-6ba289b`。本阶段没有数据库或配置改动。

### 待决问题

1. **Mini App 用哪种语言**：现在跟随 bot 的全局语言，和仓储面板一致。Telegram 的 initData 里带有用户自己的 `language_code`，但页面在拿到它之前就已经渲染好了。要不要做成按用户语言显示，需要用户决定；目前全局只有一个语言，我没有扩大范围。
2. **外部原文不翻**：PikPak 库和 Telethon 返回的错误原文（如 `invalid_grant`、RPC 错误名）会原样出现在中文句子里。这些文本来自外部，没法可靠地翻译。
3. **`python -m tgmd.verify` 的「配置提示」行仍是英文**：内容来自 `Config.validate()`，写给运维看，里面全是环境变量名，和 `ConfigError` 一样按原计划只用英文。如果希望这些提示也出中文，需要再开一批。
