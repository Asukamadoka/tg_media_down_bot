# M9: traffic metering, budgets and in/out control

> Drafted by Cowork, 2026-10-02. Implemented by Claude Code. Baseline: b4e8ee6 (M8.3 plus the path fix).
> The bot's UI text is Chinese; code, comments, docs and commit messages are English.

## Why

The proxy service is billed **per GB** for each node, and the price is part of the node name, for example `🖤东京京X06｜0.01元/G｜Reality｜` or `😈英格兰002｜0.09元/G｜hy2｜`. Saki can't currently see what the bot spends, and can't stop it.

Facts measured on the NAS on 2026-10-02:

- **mihomo setup.** mihomo runs in the `proxy` container in TUN mode. `bot` and `ollama` share its network namespace, so **every** packet from the bot passes through mihomo. The controller is at `http://127.0.0.1:9090` and is reachable from `bot` without a secret.
- **Rules:**
  - LAN goes DIRECT;
  - `mypikpak.com` and `mypikpak.net` go DIRECT, covering login (`user.`), API (`api-drive.`) and downloads (`dl-*.`);
  - `bujidao.cc` (the subscription) goes DIRECT;
  - Telegram domains and CIDRs go to group `TG`, a fallback over `TG-OTHER` (Canada/England, 0.07–0.09 元/G) and `TG-TOKYO` (0.01–0.02 元/G);
  - everything else is `MATCH,PROXY`, a url-test group.
- **Proxy logs, last 96 h:**
  - 871 connections to `api-drive.mypikpak.com` and 38 to `dl-*.mypikpak.com`, all DIRECT;
  - Telegram connections all via `英格兰002 (0.09元/G)`;
  - 93 connections to `*.r2.cloudflarestorage.com`, plus `registry.ollama.ai`, via PROXY. These are Ollama model pulls, which are large and were billed.
- **Counters.** mihomo's `uploadTotal` and `downloadTotal` were 0.1 GB up and 81.6 GB down over 4 days, but there is no per-category split. mihomo keeps no history, and its counters reset whenever the proxy restarts.

So the traffic that costs money is Telegram media (download and upload) plus anything that falls through to `MATCH`. PikPak traffic is free but uses NAS bandwidth.

## A. Meter (`tgmd/traffic/`, new package)

1. **Polling.** A background task polls `GET {MIHOMO_API}/connections` every `TRAFFIC_POLL_SECONDS` (default 5).
   - Keep the last `(upload, download)` for each connection `id`. Each poll, add the delta.
   - When a connection disappears, its bytes up to the last poll are already counted.
   - Also track the delta of the global `uploadTotal`/`downloadTotal`. The difference from the per-connection sum is stored as category `unattributed` (short-lived connections that never appeared in a poll). Its outbound is unknown, so it is shown separately and not priced.
   - A counter that goes backwards means the proxy restarted: rebaseline and log once.
2. **Classification.** Each connection gets three keys:
   - **outbound:** `direct`, or `proxy`, with `node` taken from the last element of `chains`. `group` is the first element of `chains`.
   - **category**, first match wins:
     - `telegram`: rule target or group is `TG`/`TG-*`, or the host/IP is in a Telegram CIDR or domain;
     - `pikpak`: host ends with `mypikpak.com` or `mypikpak.net`;
     - `model`: host is `ollama.com`, `ollama.ai` or `*.r2.cloudflarestorage.com`, or the destination is the Mac model host `<LAN_IP>:11434`;
     - `lan`: RFC1918 or loopback;
     - `proxy-sub`: `bujidao.cc`;
     - `other`.
   - **direction:** upload and download are kept apart.
3. **Price.** Parse `(\d+(?:\.\d+)?)\s*元\s*/\s*G` from the node name.
   - Bytes per G come from `TRAFFIC_BYTES_PER_GB`, default 1073741824. Unknown price falls back to `TRAFFIC_DEFAULT_PRICE` (default 0.10).
   - Cost is computed on upload + download.
   - Direct traffic costs 0.
4. **Storage.**
   - Hourly buckets go in `data/db/traffic.sqlite3`, table `traffic_hour(hour_utc, category, outbound, node, up_bytes, down_bytes, cost_cny)`, with a unique key on the first four columns. Upsert on each flush. Flush every minute and on shutdown.
   - A second table `traffic_host_day(day_local, host, category, outbound, bytes)` keeps the top hosts. Only keep hosts that move more than 1 MB a day, to limit rows.
   - Retention: 400 days for hours, 90 days for hosts.
   - Days and months use Asia/Shanghai.
5. **Robustness.**
   - If mihomo is unreachable, log a warning once per outage, back off up to 60 s, and never raise into the bot.
   - The meter must use well under 1% CPU on the N100. Use plain `urllib` or the existing HTTP client in a thread, with no new heavy dependency.

## B. Report

1. **`/traffic`** (owner only; add it to the command menu as `流量`).
   - Default view is **today**. Buttons: `今天` `本周` `本月` `暂停下载` / `恢复下载` `限速`.
   - Layout:
     ```
     代理流量（计费）  今天 1.84 GB ≈ ¥0.16
       Telegram 下载 1.70 GB · 上传 0.05 GB   英格兰002 0.09元/G
       其他       0.09 GB                      东京X07 0.02元/G
     直连流量（不计费） 今天 18.2 GB
       PikPak 18.1 GB · 局域网/模型 0.1 GB
     未归属 0.02 GB
     预算：今日 ¥0.16 / ¥2.00 · 本月 ¥3.10 / ¥30.00
     当前出口：TG → 英格兰002 (0.09元/G)
     下载闸门：开启（不限速）
     ```
   - Then the top 5 hosts by proxied bytes today, with node and GB.
2. **Daily summary.** Pushed to the owner at `TRAFFIC_DAILY_REPORT_AT` (default `09:00`, empty = off), covering yesterday. Skip it when proxied bytes are 0 and no alert fired.
3. **`wms`-style CLI for Cowork:** `python -m tgmd.traffic report --period today|7d|month [--json]`, run inside the container, same numbers.

## C. Budgets and alerts

1. **Budget settings:** `TRAFFIC_BUDGET_DAILY_CNY`, `TRAFFIC_BUDGET_MONTHLY_CNY` and `TRAFFIC_BUDGET_DAILY_PROXY_GB`. Each defaults to empty (off). They cover proxied traffic only.
2. **Budget alerts.** At 80% and 100% of any budget, alert the owner once per period.
3. **Spike alerts:**
   - proxied rate above `TRAFFIC_SPIKE_MBPS` (default 0 = off) for 5 consecutive minutes;
   - any single proxied connection over `TRAFFIC_CONN_ALERT_MB` (default 500).
   - Name the host, category and node in the alert.
4. **Route-leak alert.** A connection classified `pikpak`, `lan` or `model` with a Mac destination whose outbound is `proxy` means the mihomo rules are wrong. Alert once per host per day.
5. **Unknown-heavy-host note.** Any `other` host over 100 MB via proxy in a day is listed in the daily summary as "consider a rule".

## D. Control (in/out)

1. **Gate (`tgmd/traffic/gate.py`).** States are `open`, `paused` (manual) and `over_budget` (automatic).
   - **Who checks it.** Telegram media **downloads** check the gate before each file and between parallel parts, in `downloader.py` and `parallel.py`. Telegram **uploads** check it before each send (`delivery.py` and the cache chat). Gated work waits; it is not failed or dropped. The task's status line shows `已暂停（流量闸门）`.
   - **When it reopens.** `/traffic` → `恢复下载` reopens it. `over_budget` clears automatically when the period rolls over or the budget is raised.
   - **Budget behaviour.** `TRAFFIC_ON_BUDGET=pause` (default) or `warn`.
   - **What it never blocks:** bot commands and replies, PikPak API calls, WMS planning, or the PikPak → NAS outbound download. That download is DIRECT, so it is not billed.
   - **Optional direct cap.** `TRAFFIC_DIRECT_DAILY_GB` (default off) gates the WMS outbound download the same way, for NAS bandwidth.
2. **Rate limit.**
   - A token bucket covers the Telegram media download path (shared across workers and parts) and, separately, uploads.
   - Settings `TG_MEDIA_RATE_LIMIT_MBPS` and `TG_UPLOAD_RATE_LIMIT_MBPS`, default 0 = unlimited.
   - `/traffic` → `限速` offers buttons `不限` `2` `5` `10` `20` MB/s. The runtime value persists in the bot DB and overrides env until changed.
3. **Persistence.** State survives restarts: `paused`, the limits, and alerts already sent this period.
4. **Read-only mihomo.** The bot only **reads** mihomo (`/connections`, `/proxies`, `/providers/proxies`). It never changes groups, configs or providers. Route changes are a deploy-time config edit done by Cowork. This keeps Telegram's single exit stable (AuthKeyDuplicatedError risk) and respects red line 4.

## E. Out of scope

- No changes to Telegram session handling. No `TG_DIRECT_MEDIA`.
- No mihomo config edits from code.
- No per-process accounting outside the proxy namespace.

## F. Tests

- Price parser: the names above, a name without a price, and full-width variants.
- Classifier: one case per category, including a `dl-a10b-123.mypikpak.com` DIRECT case and a `pikpak` connection via proxy, which must trigger a route leak.
- Delta accounting: a connection that grows, disappears or reappears with a new id; counter reset; unattributed remainder.
- Hour bucket rollover, Shanghai day and month boundaries, and retention.
- Budget transitions: 80% and 100% alerts fire once; `pause` vs `warn`; rollover clears `over_budget`.
- Gate in the download path: a gated download waits and resumes; a PikPak outbound is not gated unless the direct cap is set.
- Token bucket average rate within 10% over a simulated window.
- `/traffic` rendering snapshot for today, week and month.
- The meter survives mihomo being down, using a fake HTTP server.

Full suite green, ruff clean, CI green.

## G. Deploy notes (for Cowork, already partly done)

Already applied on the NAS on 2026-10-02 (proxy config backups `config.yaml.bak8-1002` and `bak9-1002`):

- the subscription URL was replaced;
- provider `main` got `proxy: DIRECT`;
- rule `DOMAIN-SUFFIX,bujidao.cc,DIRECT` was added before `MATCH`;
- `PROXY` got `exclude-filter: '充值|分割线|群 |官网|失联|0[.]10元'`.

After merge:

1. Pull the image and restart `bot` only.
2. Set the env. Suggested starting values: `TRAFFIC_BUDGET_DAILY_CNY=2`, `TRAFFIC_BUDGET_MONTHLY_CNY=30`, `TRAFFIC_CONN_ALERT_MB=500`.
3. Check `/traffic` and the CLI report after 10 minutes.

Open decisions for Saki, listed in HANDOFF:

1. Whether Telegram should prefer `TG-TOKYO` (0.01–0.02 元/G) over `TG-OTHER` (0.07–0.09 元/G). That cuts Telegram cost by 4–9×; latency is higher.
2. Whether Ollama pulls on the NAS should go DIRECT or be blocked. The NAS model service is normally stopped; the Mac is the model host.

## Acceptance

- [ ] `/traffic` shows today, week and month, with proxied cost by category and node, and direct GB.
- [ ] A forced small budget pauses a Telegram media download; `恢复下载` continues it, and the file completes intact.
- [ ] The rate limit holds within 10% for a large Telegram file.
- [ ] A PikPak → NAS outbound download is unaffected by the proxy budget.
- [ ] The daily summary arrives once.
- [ ] HANDOFF has an M9 section with env vars and the two open decisions.
