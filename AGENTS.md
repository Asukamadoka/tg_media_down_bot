> Universal v3.0.0 — universal rules: ~/Developer/agents-md/core/AGENTS.md (read §2, §5, §6, §9 at boot)

# AGENTS.md — tg_media_down_bot (pikpak-wms)

Thin project file. The universal rules load globally; this adds the project's facts and red lines.
`CC_BRIEF.md` §1 and the `pikpak-wms-nas-ops` skill beat everything else here.

## 12. Project overlay

```
Project:            pikpak-wms   (repo: Asukamadoka/tg_media_down_bot, branch: claude/telegram-media-downloader-bot-samm1v)
Purpose:            Telegram media downloader + PikPak WMS running on Saki's NAS. Claude Code writes code;
                    the coordinating session briefs, verifies, deploys to the NAS and runs Telegram checks.
Red lines:          CC_BRIEF.md §1 and the pikpak-wms-nas-ops skill beat everything else.
                    Never touch Telegram sessions or auth keys; TG_DIRECT_MEDIA stays off.
                    Never edit data/db or wms.yaml from the host (container uid 10001, ACLs);
                    edit inside the container with `docker compose exec -T bot python -`.
                    `wms do` / `outbound` plan by default; `--apply` only on Saki's word. Never empty PikPak trash.
                    Restart `bot` only, never `proxy` (shared network namespace).
                    Back up a config file before changing it (`<file>.bakN-<date>`).
                    Never ask for passwords; Mac root goes through Touch ID (elevate.sh).
                    The repo and its image are public: no address, host name, id, device model or
                    personal value in code, docs, tests or commits (docs/security/README.md).
Commands:           python -m pytest -q                       # full suite
                    ruff check .                              # lint
                    gitleaks dir . --config .gitleaks.toml --redact   # public-repo hygiene
                    docker compose pull bot && docker compose up -d bot   # deploy (on the NAS)
                    docker compose exec -T bot wms plans --all            # read-only check (on the NAS)
                    docker compose exec -T bot wms events --raw --limit 5 # event feed check (on the NAS)
Machines:           Mac (dev, cmux + Claude Code) · NAS (deploy target, ssh) · GitHub Actions CI.
                    The GHCR image is public; the repo's visibility does not change it.
Docs:               CC_BRIEF.md · COWORK_BRIEF.md · DEPLOY.md · docs/HANDOFF.md · docs/AUDIT.md ·
                    docs/security/README.md · docs/wms/M*.md
Private values:     Saki's private, sops/age-encrypted values repo; never copied here (docs/security/README.md).
Gene bank:          global: ~/Developer/agents-md/ops/gene/genes.yaml + ops/gene/genes.yaml (scope project:pikpak-wms)
Language override:  none — docs and commits in English; replies to Saki in Simplified Chinese.
Quality bar:        NL model: model-only ≥ 80 %, with rules ≥ 95 %, dangerous = 0, ≤ 5 s per sentence.
Vault:              ~/Documents/claude/pikapk_WMS
```

## Habits

- Before a push: `selfcheck.sh <files>`; tests, ruff and gitleaks clean.
- Log Saki's orders with `evolve.py log order … --words`; `learn.py recall "<topic>"` before decisions.
- Coding goes to Claude Code in cmux via `cc.sh dispatch`; the dispatcher verifies before deploying.
- Jev only on synthetic phrase sets or issue reports Saki allows; never live bot traffic, media,
  signed URLs, tokens or logs.

<!-- genes:start -->
## Saki's genes for this project
_Compiled 2026-10-04 from agents-md global genes + ops/gene/genes.yaml (domains=[], tags=[]). Orders in REDESIGN.md override these. Edit genes in the hub, then re-run compile-agents.py._

- **G-AGT-001** (must) Agents reply to Saki in Simplified Chinese; code, docs, commits, briefs stay in English.
  - Saki: “You should always feedback words for me in Chinese but keep codes markdown briefs or other output for you in english”
- **G-AGT-002** (must) Before designing, write a plan with goals and acceptance criteria; don't drift or invent facts until it is done.
  - Saki: “When you design you should give yourself plans and goals to achieve unless you have done with it you should not let thinking go sideways or Hallucinated”
- **G-AGT-003** (must) Links are handled one by one unless Claude decides to bundle some, and says so.
  - Saki: “I may throw several links to you at one time but i want you to deal with it one by one only if you choose to package some of them into one”
- **G-AGT-005** (must) Every derived project ships agent instruction files written for Saki (AGENTS.md canonical, CLAUDE.md importing it, others as needed), compiled from this gene bank plus the project's orders.
  - Saki: “agents.mds claude.mds others.mds desinged just for me or my particular orders”
- **G-AGT-006** (must) Derived work lives in private repos under Asukamadoka; upstream kept as a fetch-only remote.
- **G-AGT-007** (must) The architect session designs rules, mechanisms and code only. Real work runs in other sessions in the skill-builder project, which follow the architect's design strictly. Requests raised in the architect session are executed in a new or matching existing worker session.
  - Saki: “此对话只进行规则 代码的设计 在此项目下新建对话开启主要工作 并严格使用本对话设计的内容进行工作 再次对话中提到的任何要求 都在新启动的对话或可归纳至项目中已存在的对话中运行”
- **G-AGT-010** (must) After changing anything that belongs to another conversation, project or agent, end the reply with a quick verification guide Saki can run there in full - files touched, commands, expected output, and how to undo.
  - Saki: “当你需要修改其他对话 其他项目 其他 agents 的内容时 请在修改完给出一段快捷指引 方便我在别处全量验收”
- **G-AGT-011** (must) Before any command or script reaches the Mac or a push, self-review it the lightest way (scripts/selfcheck.sh — syntax, macOS/BSD differences, paths, learned lessons); never retry a failing call unchanged. The review keeps a history of failures and learns — a mistake that got through becomes a lesson so the same error is caught next time.
  - Saki: “在本地代码被推出去之前要先自审 而不是反复用错误代码请求调用 DC | 语法检查 差异 路径 等代码自审流程 使用最轻量的方式快速完成 严格控制 token的用量 | 批准写入 Core 此为代码编译的校审机制 属于重要工具构成 同时需要具备历史和学习机制 确保相同的错误不会重复出现”
- **G-AGT-013** (must) Opus 5.5 does design, briefs and verification (project coordinator); coding is pushed to Claude Code in cmux running Sonnet 5.5 (cc.sh passes --model sonnet).
  - Saki: “我一直是使用opus5.5进行方案设计 并推给cmux 使用 sonnet5.5 coding”
- **G-ENV-006** (must) Anonymous telemetry / usage analytics / crash upload is turned off in every tool installed or configured for Saki, and in tools skill-builder builds. Check for it as part of setup.
  - Saki: “关掉匿名数据上报 这个功能可以在这个对话完成 因为他属于机制设计的一部分”
- **G-ENV-008** (must) Every privileged request an agent pushes to a terminal, in any project, goes through Touch ID approval (cmux sudo broker via scripts/elevate.sh; pam_tid in /etc/pam.d/sudo_local). Typing the password stays available whenever Touch ID is unavailable. Agents never ask for, see or store the password.
  - Saki: “批准用 touchid替代 但是保留不可用时输入密码的选项 / touch id insted of password typing is a good design i would like to push to all my projects when its pushing requests to terminals”
- **G-SEC-002** (must) Audits and monitors stay as light as possible on tokens: run once per session at boot, print one line when everything is fine, details only on failure; fingerprint only the files that hook or wrap agents; never add live hooks or loops for auditing.
  - Saki: “批准为 hooks 设置审计层 但是严格控制和减少在这个工作上的token 消耗 只做最轻量的审计和监控”
- **G-SYS-001** (must) The rule system (baseline, AGENTS.md, CLAUDE.md, genes.yaml, mechanism and logic files) learns and evolves. Every working session feeds evidence into the journal; reflection turns evidence into proposed mutations; Saki selects; approved mutations are applied, versioned and recompiled into every project.
  - Saki: “学习系统是我们 baseline、agent.md、claude.md、genes.yaml 这些底层代码和规则文件需要增加的机制系统，我认为是进化系统，就像 LLM 现在进入利用模型训练模型的阶段，这些规则文件 机制文件 逻辑文件也必须拥有机器学习 自我学习 迭代进化的机制”
- **G-SYS-002** (must) The rule system keeps full history — an append-only log, a time machine (every rule change is a git commit), and named rewind points that can be restored without losing anything.
  - Saki: “而历史记录对应到我们的机制设计则是 log、timemachine、windback point 此机制跟自我进化学习是同理”
- **G-SYS-003** (must) Global genes live in agents-md (ops/gene/genes.yaml) as the single source for all projects; skill-builder reads them from there. Since 2026-10-04 (G-SYS-009) skill-builder proposes changes through Saki's handoffs instead of writing the bank.
  - Saki: “选择 1 但是要通知 skill-builder 你的行为 同时保留 skill-builder 的写入权限”
- **G-SYS-005** (must) agents-md sets the behaviour rules for every agent on Saki's Mac and its architect session outranks the other agents; it may modify ChatGPT/Codex, Claude and Claude Code configs and write baselines and rules for them, but every change, iteration and edit to another baseline is recorded in agents-md (core/ + ops/rules/baseline.md), linked and synchronized with the target, and the affected agents/projects are notified.
  - Saki: “该项目为所有 agents 的行为规则定调 you have the highest rank beyond on the other agents on this mac you can modify chatgpt codex cluade cluade code and write baseline and rules for them 但需将修改 迭代 和针对其它 baseline 的修改 移动 在 agents 和 project baseline 中 linked and syncronized 并通知到位”
- **G-SYS-006** (must) agents-md is the universal baseline and every project and agent is under its jurisdiction, the skill-builder architect included. Inside its own project, each project's architect is that project's baseline agent and keeps priority over execution and design there. Any change to rules or sets between projects (e.g. skill-builder and agents-md) is logged in both projects and notified to the other project's baseline agent. This is a universal rule.
  - Saki: “agents.md project 是全局通用规则设定项目 虽然他来源于你 但是现在要为他提升权限与全局控制 你现在也属于他的管辖范围 但是你在 skill-builder project 中依然拥有优先执行和设计权 means even you are under that universal baseline's Jurisdiction But under your actual project You still have controls But for sets and rules between projects e.g Skill-Builder and Agents.MD, any changes should be logged and notify to each other's baseline agents, you,for example are skill-builder project's baseline agent This is also a universal rule.”
- **G-SYS-009** (must) From 2026-10-04 agents-md holds full control of rule design for every agent and project, including global agent configs (Claude Code, Codex) and the shared engine. skill-builder's architect manages its own project only; its new designs reach agents-md only when Saki hands them over, otherwise they stay project rules on top of the agreed universal rules. skill-builder is "advanced" in session depth and thinking, not in rank.
  - Saki: “我说过你控制全局agents的配置 codex的调整也由你负责 不是推给skill-builder | 你提到advance 这个冲突处 实际不存在 我说skill-builder 的agent advanced 是在session深度 以及思考程度上 而你拥有全局set control 给所有agent定调 写规则 skill-builder 不会跟你在意见上有不一致 今天之前你的一切都来自skill-builder project baseline agent 从此时此刻起你掌舵 获得全部控制权 交接skill-builder agent 在规则设计上的所有 由与skill-builder agent 管理他自己的skill-builder project 此后在他那里做出的新设计 新变动 需要你吸收的我会主动跟他提出并交接给你 如果没有 那么那些新的改动 就只是在他自己的项目内 在你们已经达成一致的通用规则基础上附加项目专用的一些规则和设定”
- **G-UX-001** (must) "Hover" means: text is selected AND the pointer rests on it for 5 seconds -> the related popup appears. Plain pointer-over is not hover.
  - Saki: “我认为的悬停是选中鼠标悬停 5 秒就显示相关弹窗”
- **G-AGT-004** (default) Redesign runs in one of three modes per decision — Saki-led (concrete orders), co-design (iterate together), Claude-led (Saki gives a concept; Claude researches and proposes options beyond Saki's knowledge).
  - Saki: “sometimes we cowork and redesign it together sometimes rely on you to find some ideas i could just give some concept and ways when its beyond my knowledge-base”
- **G-AGT-008** (default) When Saki names a derived project, that name (as a lowercase slug) is used for the repo, folder, notes and agent files instead of <upstream>-local; the upstream stays recorded in SKILLBUILDER.md and the registry.
  - Saki: “将这个项目命名为 Floater”
- **G-AGT-009** (default) AGENTS.md is enough for Codex and other agents; add an agent-specific section only when a real difference shows up.
  - Saki: “AGENTS.md 足够 (Recommended)”
- **G-AGT-012** (default) Jev (TypeSafe) is a helper for every agent, run by skills and plugins. For bounded judgment calls (routing a batch, reranking a shortlist, checking claims against a source) agents use the jev-workflows skill via typesafe-run, preview first, execute only on data the project allows, never send secrets or personal data, and report model version and usage.
  - Saki: “According to the new helper agent, Jev , It runs by skills and plugins We need to update every agent's baseline rules Protocols Habits.”
- **G-AGT-014** (default) When handoffs are written into long files, end the reply with a short guide for the new session (file path + one paste-ready prompt), and date the handoff file with the day it was written.
  - Saki: “如果你把大量的hadoffs 写在某个文件中 你还需要在对话结尾给新会话一个简单的指引”
- **G-AGT-015** (default) When a project starts in (or is upgraded to) a Claude CC project, the coordinator first reviews the whole project and then pushes its open tasks to threads or Claude Code in cmux.
  - Saki: “在新的 cc projects 中分为 thread area 和Coordinator area 我认为重新在Coordinator 中对之前整个project做一个回顾 并推进各项任务是一个好的选择”
- **G-ENV-001** (default) Target Saki's Mac first — macOS 26, Apple Silicon, Homebrew + uv.
- **G-ENV-002** (default) Microsoft Edge is the day-to-day browser; browser integrations target Edge (Chromium).
- **G-ENV-003** (default) Obsidian (vault ~/Documents/claude) is the knowledge sink; tools that produce notes, history or exports should be able to write there.
- **G-ENV-004** (default) cmux is the long-term terminal, shared between Saki and agents; SSH Term Pro stays the NAS client.
- **G-ENV-005** (default) Saki works with Claude (Cowork and Claude Code) and Codex; outputs must be usable by all of them.
- **G-ENV-007** (default) Coding work is delegated to the real Claude Code CLI running inside cmux; Cowork sessions push briefs with scripts/cc.sh and watch progress through cmux (read-screen, notifications), so Saki can see and step in.
  - Saki: “本应用不是集成了 cc 吗，可以选择推送简报到 cc ... 如果是的话可以实现自动化操作，那我建议是 cmux”
- **G-ENV-009** (default) lazygit is installed and is Saki's git TUI; suggest it for interactive git work.
  - Saki: “lazygit 已部署”
- **G-ENV-010** (default) Orca (agent development environment: parallel agents in git worktrees, terminals, browser) is Saki's cockpit for window management and agent integration; don't add separate window managers or agent multiplexers (aerospace, herdr) unless Saki asks. cmux stays the shared terminal for Cowork-driven automation (scripts/cc.sh).
  - Saki: “about window manage and agents integrate i have another so called orca”
- **G-ENV-011** (default) Terminal toolset: neovim (editor), yazi (file manager), lazygit (git), duanyan-tui with rime-ice (Chinese input in terminals: type ^^ then Tab), pi (extra coding agent). All installed with telemetry off.
  - Saki: “tools we choose and deploy which you judged suitable and choosable”
- **G-SEC-001** (default) Any third-party layer that sits between Saki and an agent (PATH shims, wrappers, injected hooks or flags) is disclosed in the baseline and checked for changes on every update before agents run through it.
  - Saki: “补充一个 cmux 的 issue cmux的 path劫持问题”
- **G-SYS-004** (default) Projects keep a thin AGENTS.md that points to agents-md core (Claude imports it), adding only project rules, the overlay and genes; core changes reach every project at its next session. (Before v3.0.0 - consumers copied core and adopted releases manually.)
  - Saki: “会话采纳时手动拉 (Recommended) | 或者直接让他调用你这边设定的全局规则同时结合自己的做小修改即可”
- **G-SYS-008** (default) Agents learn Hindsight-style on plain files — retain (journal, selfcheck lessons), recall before deciding (learn.py recall, optional Jev rerank), reflect at session end into evidence-counted observations (learn.py observe) and proposals Saki approves; core and baselines are the mental models. No memory server.
  - Saki: “学习和历史机制 发现参照项目https://github.com/vectorize-io/hindsight /skill-creator | 借鉴机制，轻量自建 (Recommended)”
- **G-UX-002** (default) Floating panels and popups should feel system-level, like macOS's own (native look, system-wide availability), not an app-bound overlay.
  - Saki: “about its 悬浮文本翻译和弹窗界面 mac自带的系统层面做的就更好 同时悬停也没完全做到”

### Candidate genes (unconfirmed — treat as hints, ask Saki)
- **G-WMS-001** (must) Never edit data/db or wms.yaml from the NAS host; edit inside the container (docker compose exec -T bot python -), because host edits break the ACLs the container user needs.
- **G-WMS-002** (must) Never touch Telegram sessions or auth keys; TG_DIRECT_MEDIA stays off.
- **G-WMS-003** (must) wms do and wms outbound only plan by default; --apply, moves and deletes only on Saki's word; never empty PikPak trash.
- **G-WMS-005** (must) Restart only the bot service, never proxy (bot shares the proxy's network namespace); back up a config file before changing it.
- **G-WMS-004** (default) An NL model is accepted at model-only >= 80 %, with rules >= 95 %, dangerous = 0 and <= 5 s per sentence.
<!-- genes:end -->
