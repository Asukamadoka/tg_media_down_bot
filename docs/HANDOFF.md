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

### 待决问题

1. **`ruff format` 没有强制。** 按它的默认风格会重排 32 个文件。简报只要求 `ruff check`，而一次纯排版的大 diff 会淹没阶段 1 的审计改动。建议：阶段 1 审计完成、模块合并之后，再决定是否在 CI 里加 `ruff format --check`。
2. **行宽定为 100，不是 ruff 默认的 88。** 实测：88 列下全仓 115 处超长，100 列下只有 2 处。代码事实上一直按 100 列在写，定 100 是如实反映，而不是为了少改。
3. **简报说 `i18n.py` 的中文会触发 E501，实际不会。** ruff 确实按显示宽度计数（已验证：66 个码位的中文行被判为 126 列），但 `i18n.py` 的中文条目已经拆得足够短，最长的行是 92 列的英文行。该预期在 88 列下成立，在 100 列下不成立。
4. **真正被中文触发的是 RUF001，不是 E501。** 92 处，全部在 `tgmd/i18n.py`：ruff 把中文全角标点（「，」「：」）当成长得像 ASCII 的可疑字符。已对该文件单独豁免；其余文件保持检查，因为那里混进全角字符（比如命令名里）才是真 bug。
5. **两条规则留给阶段 1 审计，未在本阶段处理：**
   - **UP042**：`(str, Enum)` 改 `StrEnum`。这是语义变更，不是 lint 清理，而且这类枚举的格式化行为在 3.11 和 3.12 之间本就不同。
   - **BLE001**：18 处 `except Exception`。多数是有意的健壮性边界（worker 不能因一个任务而死、装饰性调用失败不能阻塞启动），但每一处都值得单独判断，一次性加 18 个 `noqa` 等于替审计下结论。
6. **`stop()` 在等待已取消的子任务时吞掉 `CancelledError`**（`tasks.py`、`webserver.py`）。如果调用 `stop()` 的任务本身正在被取消，这也会把外层的取消一并吞掉。本阶段只是把写法换成 `contextlib.suppress`，语义没变；是否需要区分“子任务的取消”和“自己的取消”，留给阶段 1。
