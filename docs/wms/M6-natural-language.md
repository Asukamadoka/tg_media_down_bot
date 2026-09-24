# CC_BRIEF 补充 · WMS M6：自然语言指令 + 整理 / 分类 / 归档 / 定向下载

> 起草：Cowork（2026-09-24）。和 `CC_BRIEF.md` 配合使用，排在 **阶段 3 WMS M2 之后**。
> M6 依赖 M2 的规则引擎和 Plan 流水线，不要提前开工。
> 请把本文件原样提交到仓库的 `docs/wms/M6-natural-language.md`。

## 1. 用户要的是什么

用户原话的意思是：希望 bot 能听懂一句话并执行，比如「下载今天转存到网盘的所有大于1GB的视频」。
下面这些都要覆盖：

- **整理**：全网盘范围的目录整理
- **分类**：按类型（视频 / 图片 / 音频 / 文档 / 压缩包）、扩展名、名称规则归类
- **归档**：按时间把旧文件移入归档目录，比如 `/Archive/2026-09`
- **自动化**：用一句话创建定时任务，比如「每天凌晨把新文件按类型归档」
- **定向下载**：按日期、格式、名称、大小、目录筛选，下载到 NAS 媒体目录（也就是原设计里的出库模块）

## 2. 核心原则：模型只翻译，不执行

```
一句话 ──▶ 翻译器 ──▶ Query（pydantic 校验）──▶ 规则引擎求值 ──▶ Plan ──▶ 用户确认 ──▶ apply ──▶ 审计
            ▲ 可插拔                               └──────── 全部复用 M1/M2，已有铁律全部适用 ────────┘
```

- 翻译器**唯一**的输出是一个经过 schema 校验的 `Query` 对象。模型**永远不直接调用** PikPak，也不直接调用任何 ops。
- `Query` 不合法，或者 `needs_clarification` 不为空时，**反问用户**，不要猜。
- 执行前一定展示计划，然后等用户确认。计划里要写明：**翻译器是怎么理解这句话的**（时间按哪个时区、「转存」对应哪个时间字段），以及命中多少个文件、总大小、前几个文件名、目标位置。
- 删除类意图只能进回收站，遵守铁律 2。定时任务的默认配置里不得出现永久删除。
- 发给外部模型的内容**只有用户那一句话、schema 和当前日期**。默认**不**发送文件列表或文件名，这个隐私边界要写进 README。

## 3. Query schema（草案，可调整，但必须能映射到 M2 的匹配器）

```json
{
  "intent": "download | move | rename | classify | archive | trash | list | schedule",
  "scope": {"path": "/", "recursive": true},
  "filters": {
    "created_after": "2026-09-24T00:00:00+08:00",
    "created_before": null,
    "min_size": 1073741824,
    "max_size": null,
    "kinds": ["video"],
    "extensions": [],
    "name_contains": [],
    "name_regex": null
  },
  "action_args": {"dest": null, "template": null},
  "schedule": null,
  "needs_clarification": null
}
```

与 M2 匹配器的映射：
- `created_after` / `created_before` → `newer_than` / `older_than`
- `min_size` / `max_size` → 同名匹配器
- `kinds` / `extensions` → `mime` / `kind`
- `name_*` → `name_regex`
- `scope` → `path_glob`

`schedule` 不为空时，生成的是一条写入规则文件的**定时规则**，而不是一次性的 Plan。

## 4. 三个后端，配置切换（用户已确认：两个模型后端都做）

```
NL_BACKEND = rules | claude | ollama      # 默认 rules
NL_FALLBACK = none | claude | ollama      # 规则解析失败时交给谁，默认 none
```

1. **`rules`（确定性解析器，永远第一个跑）**。覆盖最常见的说法：
   - 时间：今天 / 昨天 / 本周 / 上周 / 本月 / 最近 N 天 / 具体日期 / 日期区间
   - 大小：大于 / 小于 / 超过 / 不到 N GB / MB
   - 类型：视频 / 图片 / 音频 / 文档 / 压缩包，以及 mkv / mp4 等扩展名
   - 名称：包含 / 以……开头 / 以……结尾
   - 目录：在 X 里 / X 目录下
   - 意图关键词：下载 / 移动 / 归档 / 分类 / 重命名 / 删除 / 列出 / 每天 / 每周
   零成本，毫秒级返回。能解析就直接出 Query，不调用任何模型。
2. **`claude`**：通过 Anthropic API 调用，模型名可配置。环境变量 `ANTHROPIC_API_KEY` 和 `NL_CLAUDE_MODEL`。用 tool use / 结构化输出，强制返回符合 schema 的 JSON。NAS 上的流量走现有的 mihomo 代理。
3. **`ollama`**：连接本地 Ollama 服务。环境变量 `OLLAMA_URL` 和 `NL_OLLAMA_MODEL`，建议 3B 级别的模型。使用 Ollama 的 `format`（JSON schema 约束解码）。NAS 是纯 CPU 推理，会慢；compose 示例里要给 Ollama 加内存上限，免得挤掉 bot 和 mihomo。

接口：`Translator.translate(text, now, tz) -> Query | Clarification`。三个实现共用同一套 schema 校验和同一套测试。

## 5. Bot 入口

- 私聊里，任何**不是链接、也不是命令**的纯文本，只对 admin 进入翻译器。现有的「发链接就下载」行为不变：`extract_links` 优先，命中链接就不进翻译器。
- 另外提供显式命令 `/do <一句话>`。
- 计划消息附内联按钮：[确认执行] [修改] [取消]。点「修改」后用户补一句话，与原句合并后重新翻译。
- 所有用户文案走 `t()`，中文优先。

## 6. 预置规则模板（放进 `config/rules.example.yaml`）

- 按类型分类：视频 → `/Media/视频`，图片 → `/Media/图片`，其余类推
- 按月归档：超过 N 天的文件 → `/Archive/{YYYY-MM}`
- 剧集 / 电影上架（原设计已有示例）
- 清理广告文件和空目录（只进回收站）

## 7. 验收

- **评测集**：`tests/nl/cases.yaml`，至少 60 条中文指令，每条附期望的 Query。必须包含用户那句原话，也必须包含有歧义、应该触发反问的句子。
- 评测集上 `rules` 的覆盖率：能直接解析的比例 ≥ 70%，并且**解析出来的结果零错误**（宁可交给模型，也不能解析错）。
- 模型后端：测试一律用 fake，不联网。另外提供 `python -m pikpak_wms.nl.eval --backend claude|ollama`，由 Cowork 在 NAS 上实跑，报告准确率和平均延迟。
- **端到端**：在手机上发「下载今天转存到网盘的所有大于1GB的视频」→ 收到计划，计划里写明时区、字段解释、命中数、总大小 → 点确认 → 文件落到 NAS 媒体目录 → 审计有记录。

## 8. 以后再说

- 把 WMS 的 ops 包装成一个 MCP server，这样用户在 Claude 应用里说一句话就能管理网盘。公网暴露前必须先设计好鉴权，本阶段不做，只在 HANDOFF 里记一笔。
