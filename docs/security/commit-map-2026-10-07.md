# Commit map after the history rewrite (2026-10-07)

The public history was rewritten with `git filter-repo` to remove deployment values and personal
data (`history-rewrite-plan.md`). Every commit got a new id. Documents written before the rewrite
quote the old ids; this table maps each old short id to the new one.

| old | new | subject |
|---|---|---|
| `0061a94` | `22c4bdd` | docs(wms): M8.2 brief — Mac model eval analysis, NL normalization & time-direction guard |
| `0758524` | `09de45c` | ci: 构建多架构镜像并推送到 GHCR |
| `0a8d986` | `dcd5f62` | docs(wms): M8.3 简报——M8.1/M8.2 验收、模型跑飞致离线误判、双边界时间方向、事件流真实样本 |
| `0ab6294` | `02a901a` |  |
| `1085bee` | `62fc711` | fix(deploy): 修正 restricted-network compose 注释里的一个错字 |
| `12009ef` | `cb46f2a` | feat(wms): 阶段 3 M2——规则引擎、计划流水线、审计与撤销、五个业务模块、定时调度 |
| `1594fd5` | `f6b7716` |  |
| `179d5a4` | `437a462` | feat(traffic): M9.1 - node selection, direct-first routing, two-link adaptive PikPak fetch |
| `19b2d80` | `dd28fc7` | feat: 阶段 2c 直连媒体线路——TG_DIRECT_MEDIA 优先走 media_only 端点，失败自动回落 |
| `1a1f197` | `5d556ad` | test/docs: 中文化与派发修复的测试，以及 TGMD_LANG 的说明 |
| `1b20121` | `f54d5f6` |  |
| `23e11b5` | `dcc586f` | i18n: 加消息目录层，bot 文案支持中英切换（第一批）；并修复 on_message 误杀配置向导 |
| `2424ad2` | `e6880f2` | ci: 阶段 0 测试门禁——镜像只在 lint 与测试全部通过后构建 |
| `2522974` | `3189ad2` | feat: 阶段 2a 转发快路——可转发的媒体零字节送达，受限时自动降级为下载 |
| `2d14c5a` | `bc44d51` |  |
| `2d97e36` | `57aa43e` |  |
| `2ee686b` | `b8ece2e` | feat: 新增拉取 GHCR 镜像的 compose，供 NAS 等无构建能力的宿主使用 |
| `3d50f04` | `8f89350` | Add Telegram media downloader bot with PikPak transfer |
| `4df6892` | `843aa72` | feat(wms): 阶段 3 M4——仓储面板（Telegram Mini App，仅 admin）与 /wms |
| `5136be4` | `9fc0dab` | Finish setup inside Telegram, and add one-click deploy |
| `52b697e` | `41662c8` | feat(wms,tgmd): M7.2——大目录只按总大小判定（用户更正）、缓存频道作为投递入口、直连 v2 手动端点 |
| `54e3f70` | `2e245ee` | fix(traffic): M9.1a - provider healthcheck latency, browser UA, probe and direct-test CLI |
| `585836c` | `c812d75` | docs(wms): M8.1 简报——/do 查不到当天转存：增量盘点漏新文件，改为事件流同步 |
| `58a8c9e` | `1a918d9` | docs(wms): M7.2 简报——大目录只按总大小判定（用户更正），缓存频道作为投递入口 |
| `58c1013` | `d77dad0` | docs(wms): M7.1 补充——真实索引抽查发现的两处误判（重复嵌套、广告文件）与测速结果 |
| `5cd792e` | `72ae0fa` |  |
| `6275fa7` | `b7e4864` | Claim admin and cache channel at runtime, dropping a deploy cycle |
| `6414f95` | `a8ac40b` | feat(wms): M7——按用户规则整理网盘：白名单、散落文件归集、精简与大文件、入口上架、定期去重 |
| `658ead7` | `aa3832a` |  |
| `681f91c` | `a0415bd` | docs(wms): M7 整理规则代码设计简报（白名单、散落文件归集、精简与大文件、入口上架、定期去重） |
| `6b77466` | `d8aa7cc` |  |
| `6ba289b` | `d5f2ce9` | feat(wms): 阶段 3 M6——自然语言指令：一句话 → Query → 计划 → 确认 → 审计 |
| `6e8beae` | `9d6b105` | docs(wms): M7.2 补充 C——直连 v2 允许手动指定媒体端点（NAS 实测：代理出口拿到的配置里没有可直连的端点） |
| `72249ad` | `1ad21ce` | feat(wms): 阶段 3 M1——pikpak_wms 并入：限流、客户端、本地索引、全量/增量盘点、CLI |
| `82c6ee1` | `6db87d4` | feat(traffic): M9 — proxy traffic meter, /traffic report, budgets, download gate and rate limit |
| `8600a44` | `8ff04ef` |  |
| `8753ff6` | `906ded6` |  |
| `8ae52d6` | `e5db44d` | fix: 阶段 1 全仓审计——A1–A8 逐条结论，另修 15 处问题，补齐核心模块测试 |
| `8d5b0fe` | `2c72681` | docs(wms): M8.2 brief — fill in content (previous commit had a placeholder) |
| `93d0c4f` | `d1d4d12` | Add setup verification and PikPak login links |
| `96d07fe` | `9bf6f71` |  |
| `9907c91` | `847ad9b` | docs(wms): M8 简报——局域网模型主机（Mac/Win PC）、小模型输出规范化、M7.2 验收遗留 |
| `9edb961` | `5d697a3` | feat(wms): M8.1 + M8.2——索引靠事件流保持最新；自然语言结果的规范化与安全护栏 |
| `a46f095` | `8b2bac6` | fix(pikpak): 按任务列表判断离线任务状态，不再把「排队中」当成「已完成」 |
| `a4fd03d` | `e977dc6` | docs(wms): add M6 natural-language command brief |
| `a634064` | `0390939` | docs(wms): M7.1 修订简报——大目录整体移动、全局“其他”、直连媒体线路 v2（独立 auth key） |
| `b067192` | `0961233` | feat: 阶段 2e 流式——PIKPAK_STREAM 让 PikPak 直接从 Telegram 取字节，不落盘 |
| `b40b5e2` | `8a5fb18` | deploy: 增加受限网络（Telegram 不可直连）环境的部署方案 |
| `b4a1e90` | `0fb17ae` | Add a handoff brief for a Cowork session with computer use |
| `b4e8ee6` | `331f506` | fix(wms): default library layout is 资源/整理/... |
| `bfa7d63` | `8187abf` | feat(wms,tgmd): M8——局域网模型主机（多个、可离线）、小模型输出规范化、M7.2 遗留 |
| `c19d658` | `4369a7f` | fix(deploy): 把末尾注释的那个字改对（掐断） |
| `c758318` | `b9f6958` | feat: 阶段 2b 并行分片下载——大文件多连接同时拉取，任何异常自动降级为单连接 |
| `cb65da6` | `2adeff8` | fix(wms): M7.1 A2——/其他 自动加入 layout.ensure |
| `cbbc6b8` | `e625991` |  |
| `ccddad6` | `c6020dc` | feat(i18n): 阶段 4 中文化第二批——setup、verify、登录 Mini App 与用户可见的异常 |
| `d0d227d` | `6421fe2` | feat: 阶段 2d 智能路由——/mode auto 能转发的秒传、受限的落 NAS 媒体目录 |
| `d2c6129` | `4e89c27` |  |
| `d338f20` | `04bbe89` | fix: 频道里的 /cache 无响应；读取账号会话被吊销时 bot 崩溃循环 |
| `d3cabb3` | `2c82fc0` | feat(wms): 阶段 3 M5——/wms 命令族与入库后自动上架 |
| `daa8e12` | `236a6e5` | feat(wms): M8.3 — background runs, library layout, model labels, runaway and direction guards |
| `dd947d2` | `59eb53c` |  |
| `e4caa0f` | `c66ec4e` | Boot the whole application in a test |
| `e60ea98` | `618670a` | feat(wms): 阶段 3 M3——同一个镜像：bot 内置 WMS 调度，wms 命令用 bot 的账号 |
| `f548c4a` | `91746b1` | docs: 给 Claude Code 的第二阶段简报（提速 / WMS 并入 / 全仓审计 / 中文化第二批） |
| `f77737c` | `c6b86a7` | feat(wms,tgmd): M7.1——大目录整体移动、全局 /其他、防嵌套、广告单独成计划、--sample --json；直连媒体线路 v2（独立 auth key） |
| `f951cb8` | `ae410d4` | docs(wms): M8.3 修订版——确认执行无反馈、默认落盘 资源库/整理/年/年.月/年.月.日、模型标注与更换、补充后条件合并错误 |
| `fae282e` | `bfac97d` | Set the command menu and profile text from the bot itself |
| `fdd04ae` | `a930f5a` | docs(handoff): 阶段 0 门禁的线上验收结果 |
