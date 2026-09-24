# 路线图

> **并入说明（2026-09-24）**：迁自 `Asukamadoka/pikpak-wms`。并入 `tg_media_down_bot` 后，里程碑以 `CC_BRIEF.md` §5 为准：
>
> | 里程碑 | 内容 |
> |---|---|
> | M1 | core（鉴权、令牌桶 4 req/s、异常收敛）、store（files / tasks / audit）、全量 + 增量盘点、CLI `login` / `stocktake` / `ls` / `quota` |
> | M2 | 规则引擎（8 种匹配器、7 种动作，各有 `plan()` / `apply()`）、inbound / layout / organize / cleanup / outbound、APScheduler 调度、审计、`undo <audit_id>` |
> | M3 | 与 bot 同一个镜像；`python -m tgmd` 按配置启用 WMS 调度；`docker compose run --rm bot wms ...` 可跑 CLI |
> | M4 | Web 面板以 Telegram Mini App 实现，只对 admin 开放：待执行 Plan、确认执行、审计列表 |
> | M5 | bot 命令族 `/wms`；入库之后自动上架（按规则生成 Plan，默认 dry-run 通知 + 一键确认） |
> | M6 | 自然语言指令，见 `M6-natural-language.md` |
>
> **进度**：M1–M4 已完成（见 `docs/HANDOFF.md` 各节）；M4 的手机端验收等公网 HTTPS 地址。
>
> 另外一并做（先写规格再实现）：按 hash 去重、归档、基于 `events` 的增量盘点、出库推送 aria2、`star` / `share` 动作。下面是原路线图，保留作背景。

每个里程碑都带**验收标准** —— 达不到就不进下一阶段。

## M0 · 立项与骨架（当前）

- [x] 生态调研与选型结论
- [x] 架构定调、六条设计铁律
- [x] 仓库创建、目录骨架、配置样例
- [x] `pyproject.toml` + ruff/mypy/pytest 配置
- [x] CI：lint + 类型检查 + 单测

**验收**：`pip install -e .` 后 `wms --help` 可运行。

## M1 · 核心链路跑通

- [ ] `core/`：客户端封装、token 持久化与自动刷新、令牌桶限流
- [ ] `store/`：SQLite 建表与迁移
- [ ] `ops/stocktake.py`：全量 + 增量盘点
- [ ] CLI：`wms login` / `wms stocktake` / `wms ls <path>` / `wms quota`

**验收**：能把一个上千文件的网盘目录树完整同步进本地库，二次盘点耗时显著低于首次。

## M2 · 自动化能力成型

- [ ] `rules/`：规则 schema、匹配器、命名模板、保留策略
- [ ] `ops/inbound.py`：分享链接转存（`get_share_info` → `restore`）、磁力/URL 离线下载与任务轮询
- [ ] `ops/layout.py`：目录模板建仓
- [ ] `ops/organize.py`：批量重命名、上架移动、按 hash 去重
- [ ] `ops/cleanup.py`：保留策略清理（默认只进回收站）
- [ ] `ops/outbound.py`：直链导出、下发 aria2
- [ ] 全链路 `--dry-run` / `--apply` + 审计表写入
- [ ] `scheduler/`：APScheduler 常驻，cron 配置
- [ ] CLI：`wms inbound` / `wms organize` / `wms cleanup` / `wms outbound` / `wms run`

**验收**：写一份 rules.yaml，跑 `wms organize --dry-run` 输出变更计划，`--apply` 后网盘目录结构正确变更，审计表可回溯每一条改动。

## M3 · Docker 化

- [ ] 多阶段 Dockerfile（slim 基镜像）
- [ ] `docker-compose.yml`：配置卷 + 数据卷 + 日志卷
- [ ] 健康检查、优雅退出、时区处理
- [ ] GHCR 镜像发布流水线

**验收**：`docker compose up -d` 后定时任务按配置自动执行，重启容器不丢 token 与索引。

## M4 · Web 管理面板

- [ ] FastAPI：任务列表、变更计划预览与确认、审计查询、手动触发
- [ ] 前端：轻量单页（优先 HTMX / Alpine，避免拖进完整前端工具链）
- [ ] 鉴权：单用户 token，默认只监听 127.0.0.1

**验收**：面板上能看到待执行的 Plan，点确认后真实生效并在审计里留痕。

## M5 · Telegram Bot

- [ ] 转发磁力/分享链接即入库，自动选仓位
- [ ] `/quota` `/tasks` `/recent` 查询
- [ ] 危险操作（清理、永久删除）在 Bot 内一律禁用

**验收**：手机上转一条磁力给 Bot，文件按规则出现在正确目录。

## 非目标（明确不做）

- 不做 PikPak 账号注册 / 邀请裂变相关功能
- 不做资源搜索聚合（磁力站抓取）—— 与仓储管理正交，且法律风险高
- 不做多网盘同步（Alist / rclone 已做得很好，需要时直接对接而非自研）
