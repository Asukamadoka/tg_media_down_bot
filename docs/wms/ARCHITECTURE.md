# 架构设计

> **并入说明（2026-09-24）**：本文件迁自 `Asukamadoka/pikpak-wms`（`main`，停在 M0，提交 `b8caf3f`），
> 按 `CC_BRIEF.md` §5 作为 WMS 的规格使用。并入 `tg_media_down_bot` 后有以下变化，其余内容原样保留：
>
> - 包位置：仓库顶层 `pikpak_wms/`，与 `tgmd/` 并列。**`pikpak_wms` 不得 import `tgmd`**；`tgmd` 只通过 `pikpak_wms.ops` 调用它。
> - PikPak 客户端：`core.client` 接收一个「给我一个已登录 `PikPakApi`」的回调。bot 装配时注入 `PikPakService.client(...)`，复用用户已连接的账号；CLI 单独运行时从环境变量或 token 文件登录。
> - 存储：独立 SQLite 文件，默认 `$DATA_DIR/wms.sqlite3`（容器内即 `/data/db/wms.sqlite3`），不和 bot 的表混在一起。token 文件默认 `$DATA_DIR/wms-token.json`。
> - 数据库访问沿用 bot 的做法（标准库 `sqlite3` + 线程），不引入 `aiosqlite`；凭据直接读环境变量，不引入 `pydantic-settings`。
> - 命令行：`python -m pikpak_wms`，镜像内另有 `wms` 快捷命令（M3）。
> - 接入层在 M4 是 Telegram Mini App（复用 `tgmd.miniapp` 的 initData 校验），不是独立的 FastAPI 面板；M5 是 bot 的 `/wms` 命令族；另有 M6 自然语言指令（`docs/wms/M6-natural-language.md`）。

## 1. 分层

```
┌──────────────────────────────────────────────┐
│  接入层  cli/  ·  api/(M4)  ·  bot/(M5)       │  只做参数解析与展示
└───────────────────┬──────────────────────────┘
                    │  只允许调用 ops
┌───────────────────▼──────────────────────────┐
│  编排层  ops/                                 │  幂等动作，支持 dry-run
│  inbound · outbound · organize · cleanup      │
│  layout  · stocktake                          │
└──────┬───────────────────────────┬───────────┘
       │                           │
┌──────▼─────────┐        ┌────────▼───────────┐
│  规则层 rules/  │        │  状态层 store/      │
│  matcher       │        │  SQLite 索引/审计   │
│  rename        │        └────────────────────┘
│  retention     │
└──────┬─────────┘
       │
┌──────▼───────────────────────────────────────┐
│  适配层  core/                                │  唯一接触网络的地方
│  client · auth · ratelimit · models           │
└───────────────────┬──────────────────────────┘
                    │
              pikpakapi SDK → PikPak API
```

**跳层即违规**。接入层出现 `from pikpakapi import` 一律打回。

## 2. 目录结构

```
pikpak_wms/
├── core/
│   ├── client.py        # PikPakClient：SDK 薄封装 + 重试 + 统一异常
│   ├── auth.py          # token 持久化、自动刷新回调、多账号
│   ├── ratelimit.py     # 令牌桶，全局限流，避免风控
│   └── models.py        # FileNode / Task / ShareLink / Quota 领域模型
├── ops/
│   ├── stocktake.py     # 盘点：目录树 → SQLite 索引（增量）
│   ├── inbound.py       # 入库：分享转存、磁力/URL 离线
│   ├── layout.py        # 建仓：按目录模板创建存取文件夹
│   ├── organize.py      # 整理：批量重命名、上架移动、去重
│   ├── cleanup.py       # 清退：保留策略执行
│   └── outbound.py      # 出库：直链导出、下发 aria2/本地
├── rules/
│   ├── schema.py        # 规则文件的 pydantic 模型
│   ├── matcher.py       # 条件求值
│   ├── rename.py        # 重命名模板引擎
│   └── retention.py     # 保留策略求值
├── scheduler/
│   ├── runner.py        # APScheduler 常驻进程
│   └── jobs.py          # job 注册与并发闸门
├── store/
│   ├── db.py            # 连接、迁移
│   └── repo.py          # files / tasks / audit 三张表的仓储方法
├── cli/
│   └── main.py          # Typer 入口
├── config.py            # 配置加载与校验
└── __init__.py
```

## 3. 数据模型（SQLite）

### `files` —— 本地目录索引（盘点产物）

| 字段 | 说明 |
|---|---|
| `file_id` PK | PikPak file id |
| `parent_id` | 父目录 id |
| `path` | 冗余全路径，便于规则按路径匹配 |
| `name` / `kind` / `size` / `mime` | 基础属性 |
| `created_time` / `modified_time` | 网盘侧时间 |
| `hash` | 内容指纹，用于去重 |
| `synced_at` | 本次盘点时间，用于识别已删除项 |

### `tasks` —— 入库任务流水

`task_id` / `type`(share_restore \| offline) / `source` / `target_path` / `phase` / `file_id` / `retries` / `created_at` / `finished_at` / `error`

### `audit` —— 操作审计（铁律 5）

`id` / `action`(rename \| move \| trash \| delete \| create_folder) / `file_id` / `before`(JSON) / `after`(JSON) / `rule_name` / `dry_run` / `at`

> 有了 `before` 快照，误操作至少可查、可人工回滚；后续可做 `wms undo <audit_id>`。

## 4. 规则引擎

规则文件是本项目的**真正接口**。一份示例：

```yaml
version: 1

rules:
  - name: 剧集入库上架
    enabled: true
    scope: /Downloads          # 只在此子树生效
    match:
      kind: file
      name_regex: '(?P<show>.+?)[. ]S(?P<s>\d{2})E(?P<e>\d{2})'
      min_size: 100MB
    actions:
      - rename:
          template: '{show|title}.S{s}E{e}.{ext}'
      - move:
          to: '/Media/剧集/{show|title}/S{s}'
          create_missing: true

  - name: 清退临时区
    scope: /Temp
    match:
      older_than: 30d
    actions:
      - trash: {}             # 铁律 2：只进回收站
```

- **匹配器**：`name_regex` / `kind` / `min_size` / `max_size` / `older_than` / `newer_than` / `mime` / `path_glob`，全部在本地索引上求值（铁律 4）。
- **命名模板**：支持正则命名捕获组 + 过滤器（`title` / `upper` / `pad2` / `date:%Y-%m`）。
- **动作原语**：`rename` / `move` / `copy` / `trash` / `star` / `share` / `create_folder`。每个原语必须实现 `plan()` 与 `apply()` 两个方法 —— 这是 dry-run 能力的落点（铁律 1）。

## 5. 执行流水线

```
盘点 stocktake → 规则求值 plan → 变更计划 Plan[] → 人工/自动确认 → apply → 写审计
```

`Plan` 是可序列化对象。定时任务把 plan 落盘，Web 面板（M4）可以直接渲染它并提供「确认执行」按钮，Bot（M5）同理 —— 三种形态共用一条流水线，不各写一套。

## 6. 风控与限流

- 全局令牌桶，默认 ≤ 4 req/s，可配置；批量接口优先于循环单条调用。
- 指数退避重试（SDK 自带 `request_max_retries` / `request_initial_backoff`，上层再包一层 429/5xx 处理）。
- 盘点走增量：只重扫 `modified_time` 变化的子树。
- 单次任务的动作数设上限（默认 500），超出则分批并在日志里提示。

## 7. 配置与凭据

- `config/wms.yaml` —— 账号引用、限流、路径模板、调度表
- `config/rules.yaml` —— 规则集
- 凭据只从环境变量读：`PIKPAK_USERNAME` / `PIKPAK_PASSWORD`，或 `PIKPAK_ENCODED_TOKEN`
- token 缓存写 `data/token.json`（0600 权限），通过 SDK 的 `token_refresh_callback` 自动落盘
