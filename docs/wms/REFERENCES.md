> 迁自 `Asukamadoka/pikpak-wms`，原样保留。

# 生态调研（2026-09 抓取自 github.com/topics/pikpak）

## 结论先行

| 用途 | 选定项目 | 理由 |
|---|---|---|
| **直接依赖** | [`Quan666/PikPakAPI`](https://github.com/Quan666/PikPakAPI) · Python · 220★ | 唯一成熟的 Python SDK，接口覆盖度足够（见下），`pip install pikpakapi` 即用 |
| **架构参考** | [`krau/SaveAny-Bot`](https://github.com/krau/SaveAny-Bot) · Go · 2.5k★ · AGPL-3.0 | 多存储后端抽象、批量下载、流式传输、规则过滤的工程范式最值得抄；注意 AGPL，**只读架构不抄代码** |
| **CLI 体验参考** | [`Bengerthelorf/pikpaktui`](https://github.com/Bengerthelorf/pikpaktui) · Rust · 106★ · Apache-2.0 | 28 个子命令的动词命名、JSON 输出与 dry-run 模式，是 CLI 设计的直接蓝本 |
| **接口补漏** | [`lyqingye/pikpak-go`](https://github.com/lyqingye/pikpak-go) · Go · 19★ | Python SDK 缺接口时，对照它的实现补，省去自己抓包 |
| **反面教材** | [`chongchong59699/pikpak-netdisk-skill`](https://github.com/chongchong59699/pikpak-netdisk-skill) · 0★ | 思路是 Python 包 pikpaktui 二进制；多一层进程调用、受限于 CLI 暴露面，**不采用**，但其 agent 友好的 JSON 约定可借鉴 |

## `pikpakapi` 接口覆盖度核对

对照本项目的需求逐条核验，**全部覆盖**：

| 需求 | SDK 方法 |
|---|---|
| 登录 / token 刷新 | `login()` · `refresh_access_token()` · `token_refresh_callback` |
| 自动创建存取文件夹 | `create_folder()` · `path_to_id(path, create=True)` |
| 分享链接转存 | `get_share_info()` → `get_share_folder()` → `restore()` |
| 磁力 / URL 离线下载 | `offline_download()` · `offline_list()` · `offline_file_info()` · `offline_task_retry()` |
| 任务状态轮询 | `get_task_status()` · `delete_tasks()` |
| 目录盘点 | `file_list(parent_id, next_page_token)` · `events()` |
| 批量重命名 | `file_rename()`（逐条，需上层批处理 + 限流） |
| 批量移动 / 复制 | `file_batch_move()` · `file_batch_copy()` · `file_move_or_copy_by_path()` |
| 清理 | `delete_to_trash()` · `untrash()` · `delete_forever()` |
| 出库直链 | `get_download_url()` |
| 对外分享 | `file_batch_share(need_password, expiration_days)` |
| 配额监控 | `get_quota_info()` · `get_transfer_quota()` · `vip_info()` |
| 收藏标记 | `file_batch_star()` · `file_batch_unstar()` · `file_star_list()` |

构造参数还自带 `request_max_retries` / `request_initial_backoff` / `device_id` / `captcha_init`，风控相关的基础设施不用从零写。

**风险**：单条重命名无批量接口，大批量整理必须靠上层并发 + 限流控制；SDK 维护频率一般，接口失效时需要自行 patch —— 因此 `core/client.py` 做薄封装隔离，替换 SDK 时只改一层。

## 完整清单（按 star 排序）

| 项目 | 语言 | ★ | 一句话 | 对本项目 |
|---|---|---|---|---|
| krau/SaveAny-Bot | Go | 2.5k | TG 文件转存到任意存储 | 架构参考 |
| nianzhibai/91 | Go | 1.4k | 多网盘聚合 | 多盘抽象参考 |
| digbug82/PikPak_Enhancement_Master | JS | 302 | Web 端增强脚本 | 无 |
| **Quan666/PikPakAPI** | Python | 220 | **Python SDK** | **直接依赖** |
| akynazh/tg-search-bot | Python | 202 | TG 搜索自动转存 | Bot 阶段参考 |
| **Bengerthelorf/pikpaktui** | Rust | 106 | TUI/CLI 客户端 | **CLI 设计参考** |
| bharathganji/pikpak-plus | Python | 93 | Next.js + Flask 非官方 Web | Web 面板参考 |
| ykxVK8yL5L/pikpak-webdav | Rust | 89 | WebDAV 网关 | 出库可选形态 |
| jdysya/pikpakHelpr-plus | Vue | 85 | 增强脚本 + Aria2 | aria2 下发参考 |
| YinBuLiao/AnimeX | Dart | 74 | 番剧管理（PikPak/115） | 剧集命名规则参考 |
| VGEAREN/pikpak-webdav | Java | 72 | WebDAV | 无 |
| ykxVK8yL5L/pikpak | Vue | 54 | Web 客户端 + Docker | 无 |
| UallenQbit/PikPakWeb | JS | 28 | Web 界面 | 无 |
| **lyqingye/pikpak-go** | Go | 19 | Go SDK | **接口补漏** |
| bharathganji/jackett-search-ui | TS | 16 | Jackett 搜索 UI | 非目标 |
| AnonymousV73X/PIKPAK-TO-GDRIVE-BOT | Python | 15 | rclone 转存 GDrive | 无 |
| gyf304/pikpakdav | Go | 12 | WebDAV | 无 |
| SakerLy/SupportDLPikPak | Python | 11 | 下载支持 | 无 |
| TeamBreakerr/gopeed-extension-pikpak | JS | 11 | 分享链接解析 | 解析逻辑参考 |
| Muione/PikpakAPI | TS | 9 | TS SDK | 无 |

## 许可证注意

- `SaveAny-Bot` 是 **AGPL-3.0**：可以读、可以学架构，**不得复制代码片段**进本项目，否则传染整个仓库。
- `pikpaktui` 是 Apache-2.0，命令命名与 JSON schema 可自由借鉴。
- `pikpakapi` 许可证未在 README 声明，作为运行时依赖引入（非代码复制），风险可控；正式开源前需确认。
