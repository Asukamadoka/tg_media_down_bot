# M8：局域网模型主机（Mac / Windows PC）＋ M7.2 验收遗留

> 起草：Cowork（2026-09-27）。执行方：Claude Code。基线：`52b697e`。

## 背景

- 用户倾向本地模型。NAS 是 N100、8 GB 内存，Qwen2.5-3B 在 94 条评测句上准确率 12.8%，平均每句 38 秒，已停用。
- 改用同一局域网内算力更强的设备：
  - 用户的 Mac（Apple Silicon）通过直连网线接在 NAS 上，NAS 看到的 Mac 地址是 `<MODEL_HOST>`；
  - 家里还有一台 Windows PC，在 `192.168.0.x` 网段。
- 这两台都**不是 7×24 小时开机**，所以模型后端必须能容忍主机离线。

## A. openai 后端支持多个主机，并快速判断谁在线

1. `NL_OPENAI_BASE_URL` 可以写多个地址，用逗号分隔，按顺序优先，比如 `http://<MODEL_HOST>:11434/v1,http://192.168.0.50:11434/v1`。`NL_OPENAI_MODEL` 同样可以逐个对应，写一个就表示所有主机共用。
2. **判断在线**：请求 `GET <base>/models`，超时 1.5 秒。结果缓存 60 秒；判定离线的主机 60 秒内不再尝试。
3. **全部离线时**：只用 rules 解析。解析不了的句子回复「模型主机离线（Mac/PC 未开机），这句我没听懂，换个说法或稍后再试」，不能让用户干等。
4. **超时分开设**：
   - 连接超时 1.5 秒；
   - 生成超时由 `NL_OPENAI_TIMEOUT` 控制，默认 60 秒；
   - 翻译过程中主机掉线的，按离线处理。
5. `/verify` 和 `wms doctor` 各加一行，显示每个模型主机在线与否、模型名、上次响应的延迟。
6. `eval --backend openai` 支持 `--base-url` 参数，一次只测一台主机。

## B. 小模型输出的鲁棒性（评测里出现的真实错误）

3B 模型输出的 `schedule.cron` 出现过 `'0 0 * * ? *'`（Quartz 格式）和字符串 `'null'`，导致整句失败。修法：

- 在 schema 校验之前做一次宽松的规范化：
  - 字符串 `"null"`、`""`、`"none"` 当作 null；
  - 6 位或 7 位的 Quartz 表达式，去掉秒位和年位，并把 `?` 换成 `*`；
  - 规范化之后仍然不合法，才报错。
- 系统提示里给出 3 条正确示例和 2 条错误示例（few-shot），并写明「不确定就填 needs_clarification」。
- 评测时同时统计「规范化前失败」和「规范化后失败」两项。

## C. M7.2 验收遗留

1. **直连 v2 的结论是做不通，但 bench 的崩溃要修**：
   - 在 NAS 上实测，`TG_DIRECT_ENDPOINTS` 里的 `149.154.166.110`、`.111`、`.120` 在 TCP 层面能连上；
   - 用 MTProto 的 TcpFull、Intermediate、Abridged、Obfuscated 四种传输方式做 DH 握手，**全部超时**，第一次还收到过 `HTTP/1.1 404` 的回包。说明直连路径上的 MTProto 流量被识别并干扰了；
   - `.110` 返回的是 `*.telegram.org` 的网页证书。
   
   结论：从这台 NAS 直连 Telegram 媒体是做不通的，**v2 保持 `off`，不再投入**。但有一个 bug 要修：`_negotiate()` 遇到 `asyncio.IncompleteReadError` 时直接冒泡，把 bench 进程弄崩了。应当当作这个端点失败，冷却后回落到代理路线。需要补一个测试。
2. **待决问题**（CC 在 M7.2 的 HANDOFF 里提出）：
   - 加 `CHANNEL_REQUESTS=true|false` 开关；
   - 频道里的普通网址和 PikPak 分享链接也要处理，走现有的 URL 转存和 restore_share；
   - 缓存命中时不要再往频道里发第二份，直接复制到 admin 私聊。

## D. 验收

- 单元测试：多个主机时按顺序选中在线的；离线的主机有冷却期；全部离线时回复不卡住；cron 的规范化覆盖上面两种真实错误；`IncompleteReadError` 会回落到代理。
- Cowork 在 Mac 上装好模型后，运行 `eval --backend openai --base-url http://<MODEL_HOST>:11434/v1`，报告准确率和平均延迟。目标：准确率 ≥ 80%，平均每句 ≤ 5 秒。
