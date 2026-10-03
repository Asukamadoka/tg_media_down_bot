# WMS 补充规格：去重、归档、出库、星标与分享、基于 events 的增量盘点

`CC_BRIEF.md` §5 列了五项「提过但没写成规格的功能」，要求先写规格再实现。本文件就是那份规格。所有功能都服从 `ARCHITECTURE.md` 的六条铁律，下面只写各自特有的部分。

---

## 1. 按 hash 去重

**目的**：同一个文件在网盘里存了多份（重复转存、重复离线下载），只留一份。

**判定**：两个**文件**（不是目录）的 `hash` 相同、且都非空，视为重复。大小不同而 hash 相同的情况视为数据异常，不处理并在计划的备注里列出。

**保留哪一份**：按以下顺序取第一个：

1. 位于规则指定的「优先目录」下的（`keep_under`，比如 `/Media`：已上架的优先保留）；
2. 创建时间最早的；
3. 路径最短的（通常是整理过的那份）。

**动作**：其余副本 → `trash`（只进回收站，铁律 2）。

**入口**：`wms organize --dedupe [--scope /Media] [--keep-under /Media]`，默认 dry-run。计划里按组展示：保留哪份、丢弃哪几份、能省多少空间。

**幂等**：第二次运行时，被丢弃的副本已不在索引里（盘点不含回收站），组里只剩一份，不再产生动作。

---

## 2. 归档

**目的**：把旧文件按时间移进归档目录，比如 `/Archive/2026-09`。

**实现方式**：不是新动作，而是一条规则模板：匹配器 `older_than` + 动作 `move`，目标路径里用日期过滤器：

```yaml
- name: 按月归档
  scope: /Inbox
  match:
    kind: file
    older_than: 30d
  actions:
    - move:
        to: '/Archive/{created|date:%Y-%m}'
        create_missing: true
```

`{created|date:%Y-%m}` 取文件在网盘里的创建时间（即转存 / 离线完成的时间），按配置的时区格式化。

**入口**：放进 `config/rules.example.yaml` 作为预置模板（M6 的要求之一，默认关闭）；由 `wms organize` 执行。

---

## 3. 出库

**目的**：把网盘里的文件取到网盘之外。三种目的地：

| `outbound.downloader` | 行为 |
|---|---|
| `none`（默认） | 只输出直链，不下载 |
| `aria2` | 通过 JSON-RPC 调用 `aria2.addUri` 把直链交给 aria2 |
| `local` | bot 自己把文件下载到 `outbound.local_dir`（NAS 媒体目录），M6「定向下载」用的就是这个 |

**直链**：`get_download_url(file_id)` 返回的 `web_content_link`，没有时取 `medias[].link.url`。直链有时效，所以**计划里不存直链**，只存 file id，执行时现取。

**aria2**：`rpc_url` 在配置里，`secret` 只从环境变量 `ARIA2_SECRET` 读（铁律：凭据不入配置文件）。每个文件一次 `addUri`，`dir` 与 `out`（文件名）一并传。

**local**：流式写入 `<local_dir>/<相对路径>`，先写 `.part` 临时文件，完成后改名；已存在同名同大小的文件则跳过（幂等）。

**dry-run**：只列出将要出库的文件、总大小、目的地；`--apply` 才取直链并下发。

**两个入口**：`wms outbound <路径…> [--to 子目录]`；以及规则里的 `outbound` 动作（`- outbound: {to: '子目录模板'}`），M6 的「定向下载」把一句话翻译成带 `outbound` 动作的临时规则，走同一条计划流水线。

**审计**：出库不改变网盘，但仍写审计（动作 `outbound`，`after` 里记目的地），便于回答「这个文件什么时候被取走过」。

---

## 4. 星标与分享

两个动作原语，规则里可用：

```yaml
actions:
  - star: {}
  - share:
      need_password: true
      days: 7          # -1 表示永久
```

- `star`：`file_batch_star`。撤销（undo）= `file_batch_unstar`。
- `share`：`file_batch_share`，返回的分享链接写进审计的 `after`。**分享不能撤销**（PikPak SDK 没有取消分享的接口），`undo` 对它会明确拒绝并说明原因。
- 两者都幂等性较弱（重复分享会得到新链接），所以规则里用 `share` 时建议配 `newer_than`，只作用于新入库的文件。

---

## 5. 基于 `events` 接口的增量盘点

**现状**：M1 的增量盘点依赖「目录的 `modified_time` 会随子孙变化而刷新」这一假设，是否成立需要实测（见 `docs/HANDOFF.md`「WMS M1」待决问题 1）。

**设想**：PikPak 有 `drive/v1/events` 接口（pikpakapi 的 `events()`，文档只写「获取最近添加事件列表」）。如果它能列出最近的增、删、改、移动，就可以只重新列出这些事件涉及的目录，而不依赖时间戳传播。

**为什么本阶段不实现**：这个接口的返回字段没有文档，本环境也无法调用真实账号。按字段名猜着写解析，会在猜错时悄悄漏掉变化，比现在的方案更糟（`CC_BRIEF.md` 红线 8：拿不准就停）。

**本阶段做的**：

1. `wms events --raw [--limit N]`：把接口的原始返回原样打印成 JSON，不做任何解析。
2. Cowork 在 NAS 上：在网盘里新增、改名、移动、删除各做一次，然后跑 `wms events --raw`，把输出（去掉链接、缩略图地址等敏感字段后）贴进 `docs/HANDOFF.md` 的待决问题。
3. 有了真实样本，再按样本实现：从事件里取出受影响的父目录 id，把它们（以及移动事件的新旧父目录）加入待列出的队列；其余目录照常按时间戳判断。事件列表翻页到上次盘点时间为止。

**兜底**：无论哪种增量方式，调度里都保留每天一次全量盘点（`schedule.jobs` 里的 `stocktake-full`）。
