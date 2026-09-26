# Cowork 执行简报 · tg_media_down_bot 部署收尾

> 这份文件是给一个**在用户本机运行、具备 computer use 能力的 Cowork 会话**的完整交接。
> 你(Cowork)冷启动即可执行,不需要先读其他文件。
> 正文说明用中文,**所有指令、变量名、代码、URL 一律英文原样**,不要翻译它们。

---

## 1. 背景:这是什么

一个**自建的 Telegram 机器人**,把任意 Telegram 消息链接转发给它,它取回链接背后的媒体文件(包括禁止保存的频道),然后发回用户、存到磁盘、或转存进 PikPak 网盘。

- 代码仓库:`https://github.com/Asukamadoka/tg_media_down_bot`
- 开发分支:`claude/telegram-media-downloader-bot-samm1v`
- 机器人:**@pikpak_WMS_bot**(用户已在 BotFather 建好)

**代码已经全部写完并通过 499 项测试,不需要你写任何代码,也不要修改仓库。**

你的任务只有一件:**完成那两步必须由"有登录态的浏览器"才能做的操作**。

---

## 2. 为什么需要你

之前的助手运行在一个云端 Linux 容器里,存在三重限制,所以卡住了:

- 没有 computer use 工具,没有图形界面;
- 出口网络被封,`render.com`、`koyeb.com`、`my.telegram.org` 全部不可达;
- 即使能操作,那也是云容器,**没有用户的登录态,也收不到用户手机上的验证码**。

你运行在**用户自己的机器**上,浏览器里有用户的登录态,用户本人也在旁边,所以这两步你能做。

---

## 3. 任务进度

| 项目 | 状态 |
| --- | --- |
| 全部代码、499 项测试、部署配置 | ✅ 已完成 |
| 在 BotFather 创建机器人、取得 bot token | ✅ 用户已完成 |
| **取得 `api_id` / `api_hash`** | ⬜ **任务 A(你来做)** |
| **部署上线** | ⬜ **任务 B(你来做)** |
| 取回认领码并交还用户 | ⬜ **任务 C(你来做)** |
| `/claim` 认领管理员 | ⬜ 用户在 Telegram 里做,不需要你 |
| `/setup` 签入读取账号、连 PikPak | ⬜ 用户在 Telegram 里做,不需要你 |

机器人的命令菜单、简介、公网地址、端口都由程序启动时**自动设置**,不需要任何人操作。

---

## 4. 安全红线(优先级高于一切)

1. **绝不把 `api_hash`、`TG_BOT_TOKEN`、认领码粘贴进任何聊天框、文档、网页或截图。** 它们只能被直接输入到目标表单里。
2. **Telegram 登录验证码只能输入 `my.telegram.org` 自己的登录页。** 不要输入到任何其他地方。
3. **不要替用户猜测或输入任何账号密码。** 遇到登录墙,停下来请用户本人登录,然后你继续。
4. **不要碰浏览器里其他标签页、其他账号、其他站点。**
5. 任何一步与本文件描述不符时,**停下来问用户**,不要自行发挥。

---

## 5. 任务 A:取得 api_id 与 api_hash

这两个值标识**客户端软件**,不是机器人。必须用户手机号登录才能取。

### 步骤

1. 浏览器打开 `https://my.telegram.org/apps`
2. 填入**将来要用来读取聊天的那个 Telegram 账号**的手机号,带国家码,格式如 `+8613800138000`。不确定用哪个号,问用户。
3. 点 **Next**。Telegram 会把一串验证码**发到该账号的 Telegram 应用里**(是 Telegram 内的消息,不是短信)。
4. 取码方式二选一:
   - 用户机器上开着 Telegram Desktop → 你可以切过去读取;
   - 否则请用户从手机上读给你。
5. 把验证码填入网页,提交。
6. 如果是第一次进入,会出现一个应用信息表单。这样填:
   - `App title`: `media-bot`
   - `Short name`: `mediabot`
   - `URL`: 留空
   - `Platform`: 选 `Other`
   - `Description`: 留空
   
   然后提交。
7. 页面会显示 **`App api_id`**(一串数字)和 **`App api_hash`**(32 位十六进制字符串)。

### 交付物

把这两个值**记在你的工作内存里**,直接用于任务 B 的表单。**不要写进聊天、文件或截图。**

### 已知故障

`my.telegram.org` 经常无故报 `ERROR`,这是 Telegram 自己的老毛病,与操作无关。换浏览器、换网络或等几分钟重试。连续失败 3 次就停下来告诉用户。

---

## 6. 任务 B:部署

**先问用户选哪条路。** 两条都可行,差别如下:

| | B1 · 本机 Docker | B2 · 云端 Render |
| --- | --- | --- |
| 速度 | 最快,几分钟 | 较慢,要注册/登录 |
| 费用 | 免费 | 持久磁盘需付费实例 |
| 机器休眠后 | 停止运行 | 持续运行 |
| PikPak 转存 Telegram 媒体 | 不可用(无公网 HTTPS) | 可用 |
| Telegram 内嵌登录页 | 不可用 | 可用 |
| 其余全部功能 | **都可用** | 都可用 |

想先跑起来看效果就选 **B1**;想长期稳定用就选 **B2**。

---

### B1 · 在用户本机用 Docker 跑

前提:本机装了 Docker Desktop 并正在运行。没装就问用户是否要装,或改走 B2。

终端依次执行:

```bash
git clone https://github.com/Asukamadoka/tg_media_down_bot
cd tg_media_down_bot
git checkout claude/telegram-media-downloader-bot-samm1v
cp .env.example .env
```

然后编辑 `.env`,只填这三项:

```
TG_API_ID=<任务 A 拿到的 api_id>
TG_API_HASH=<任务 A 拿到的 api_hash>
TG_BOT_TOKEN=<用户的 BotFather token>
```

`TG_BOT_TOKEN` 向用户索取。形如 `123456789:AAH...`,**包含冒号**。其余所有项留空,尤其 `ADMIN_USER_IDS` **必须留空**。

启动并查看日志:

```bash
docker compose up -d
docker compose logs -f
```

跳到 **任务 C**。

---

### B2 · 部署到 Render

1. 打开 `https://render.com`。未登录则请用户本人登录(建议用 GitHub 账号登录,后续能直接选仓库)。
2. 打开这个一键部署链接:

   ```
   https://render.com/deploy?repo=https://github.com/Asukamadoka/tg_media_down_bot
   ```

   如果它报错或打不开,改走手动流程:Dashboard → **New** → **Web Service** → 连接 GitHub 仓库 `Asukamadoka/tg_media_down_bot`。

3. **分支必须选 `claude/telegram-media-downloader-bot-samm1v`**,不是默认分支。
4. 服务类型必须是 **Web Service**,**不要选 Background Worker**。只有 Web Service 才会分配公网 HTTPS 地址,PikPak 拉取文件和 Telegram 内嵌登录页都依赖它。
5. 环境变量只填这三个:

   | Key | Value |
   | --- | --- |
   | `TG_API_ID` | 任务 A 的 api_id |
   | `TG_API_HASH` | 任务 A 的 api_hash |
   | `TG_BOT_TOKEN` | 用户的 BotFather token |

   `ADMIN_USER_IDS` **不要填**。仓库里的 `render.yaml` 已经配好其余一切。

6. 磁盘:仓库配置里声明了一块挂载在 `/data` 的持久盘。
   **注意:Render 的持久磁盘需要付费实例类型。** 如果界面拒绝创建磁盘或要求升级付费:
   - **停下来告诉用户**,由用户决定付费还是放弃磁盘;
   - 放弃磁盘也能跑,但每次重新部署会清空数据库,用户需要重新做一次 `/setup`。
   
   不要替用户做付费决定。

7. 点部署,等待构建完成。

---

## 7. 任务 C:取回认领码

部署成功后,机器人**第一次启动时会往运行日志里打印一段认领码**。因为没有管理员时它会拒绝所有人,包括用户自己,所以这一步必不可少。

在日志里找这样一段:

```
====================================================================
  NO ADMIN YET. Open @pikpak_WMS_bot in Telegram and send:

      /claim 5isJppLOdTc

  That makes you the admin. No redeploy needed, and this code stops
  working straight afterwards.
====================================================================
```

- B1 路线:在 `docker compose logs -f` 的输出里找。
- B2 路线:Render 服务页面的 **Logs** 标签页里找。

日志量大时,搜关键词 `NO ADMIN YET` 或 `/claim`。

### 交付物(交还给用户)

把下面这些**直接告诉用户本人**:

1. 那一行完整的 `/claim <码>` 指令;
2. B2 路线还要给出服务的公网地址,形如 `https://xxx.onrender.com`;
3. 明确告诉用户:**下一步请在 Telegram 里打开 @pikpak_WMS_bot,发送那条 `/claim` 指令**。

认领码是一次性的,用过即失效,所以直接交给用户本人是安全的。但仍然**不要写进公开文档或截图外发**。

---

## 8. 成功判据

按顺序自检,全部满足才算完成:

- [ ] 任务 A:拿到了 api_id(纯数字)和 api_hash(32 位十六进制)
- [ ] 任务 B:服务已启动,日志里没有反复重启或 traceback
- [ ] 日志里出现 `bot client started as @pikpak_WMS_bot`
- [ ] 日志里出现 `NO ADMIN YET` 那一段,并取到了认领码
- [ ] 已把 `/claim <码>` 交给用户

---

## 9. 常见失败与处理

| 日志/现象 | 含义 | 处理 |
| --- | --- | --- |
| `configuration error: missing required settings` | 三个值有遗漏 | 检查环境变量拼写,注意 token 要含冒号 |
| `could not sign in as the bot` | token 错,或网络封锁 MTProto | 让用户在 BotFather 用 `API Token` 重新确认 |
| `bot identity mismatch` | token 属于另一个机器人 | 确认拿的是 @pikpak_WMS_bot 的 token |
| 反复重启 | 多半是环境变量问题 | 看第一条 traceback,原样报告给用户 |
| 日志里没有 `NO ADMIN YET` | 可能填了 `ADMIN_USER_IDS` | 清空该变量重新部署 |

**遇到本表没有覆盖的错误:把日志原文交给用户,不要自行改代码或改配置。**

---

## 10. 明确不要做的事

- 不要修改仓库代码,不要提交,不要开 PR。
- 不要替用户在 Telegram 里发 `/claim`、`/setup` 或任何指令。那几步涉及用户账号的登录码和 PikPak 密码,**必须用户本人操作**。
- 不要尝试代替用户登录 my.telegram.org 以外的任何账号。
- 不要把这份文件里的任何凭据占位符当成真实值。

---

## 11. 交还用户后的剩余流程(供你理解全貌,你不执行)

用户拿到认领码后,在 Telegram 里自行完成:

1. `/claim <码>` — 成为管理员
2. `/setup telegram` — 签入一个普通 Telegram 账号,这样机器人才能读私有频道和禁止保存的频道。会依次询问手机号、登录码、两步密码,每条消息读完即删。
3. `/setup pikpak` 或 `/pikpak login` — 连接 PikPak 账号
4. `/verify` — 自检全部配置
5. 之后直接转发 Telegram 消息链接给机器人即可

这些都在 Telegram 应用内完成,不需要再碰托管商。
