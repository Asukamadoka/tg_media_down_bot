> Universal v4.1.0 — universal rules: ~/Developer/agents-md/core/AGENTS.md (read the kernel at boot, blocks on demand via its route table). Project folder: ~/Developer/pikpak-wms (see its AGENTS.md for the other blocks).

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
Vault:              ~/Developer/pikpak-wms/notes (vault link ~/Documents/claude/pikapk_WMS)
Folder:             ~/Developer/pikpak-wms (v5): repo, private values, notes, local study
```

## Habits

- Before a push: `selfcheck.sh <files>`; tests, ruff and gitleaks clean.
- Log Saki's orders with `evolve.py log order … --words`; `learn.py recall "<topic>"` before decisions.
- Coding goes to Claude Code in cmux via `cc.sh dispatch`; the dispatcher verifies before deploying.
- Jev only on synthetic phrase sets or issue reports Saki allows; never live bot traffic, media,
  signed URLs, tokens or logs.

<!-- genes:start -->
## Saki's genes for this project
_Compiled 2026-10-11 from agents-md global genes + genes.yaml (hub) + tg_media_down_bot/ops/gene/genes.yaml (domains=[], tags=[]). Orders in REDESIGN.md override these. Edit genes in the hub, then re-run compile-agents.py._

- **G-AGT-005** (must) Every derived project ships agent instruction files written for Saki (AGENTS.md canonical, CLAUDE.md importing it, others as needed), compiled from this gene bank plus the project's orders.
- **G-AGT-010** (must) After changing anything that belongs to another conversation, project or agent, end the reply with a quick verification guide Saki can run there in full - files touched, commands, expected output, and how to undo.
- **G-SYS-003** (must) Global genes live in agents-md (ops/gene/genes.yaml) as the single source for all projects; skill-builder reads them from there. Since 2026-10-04 (G-SYS-009) skill-builder proposes changes through Saki's handoffs instead of writing the bank.
- **G-SYS-005** (must) agents-md sets the behaviour rules for every agent on Saki's Mac and its architect session outranks the other agents; it may modify ChatGPT/Codex, Claude and Claude Code configs and write baselines and rules for them, but every change, iteration and edit to another baseline is recorded in agents-md (core/ + ops/rules/baseline.md), linked and synchronized with the target, and the affected agents/projects are notified.
- **G-SYS-006** (must) agents-md is the universal baseline and every project and agent is under its jurisdiction, the skill-builder architect included. Inside its own project, each project's architect is that project's baseline agent and keeps priority over execution and design there. Any change to rules or sets between projects (e.g. skill-builder and agents-md) is logged in both projects and notified to the other project's baseline agent. This is a universal rule.
- **G-SYS-009** (must) From 2026-10-04 agents-md holds full control of rule design for every agent and project, including global agent configs (Claude Code, Codex) and the shared engine. skill-builder's architect manages its own project only; its new designs reach agents-md only when Saki hands them over, otherwise they stay project rules on top of the agreed universal rules. skill-builder is "advanced" in session depth and thinking, not in rank.
  - Saki: “我说过你控制全局agents的配置 codex的调整也由你负责 不是推给skill-builder | 你提到advance 这个冲突处 实际不存在 我说skill-builder 的agent advanced 是在session深度 以及思考程度上 而你拥有全局set control 给所有agent定调 写规则 skill-builder 不会跟你在意见上有不一致 今天之前你的一切都来自skill-builder project baseline agent 从此时此刻起你掌舵 获得全部控制权 交接skill-builder agent 在规则设计上的所有 由与skill-builder agent 管理他自己的skill-builder project 此后在他那里做出的新设计 新变动 需要你吸收的我会主动跟他提出并交接给你 如果没有 那么那些新的改动 就只是在他自己的项目内 在你们已经达成一致的通用规则基础上附加项目专用的一些规则和设定”
- **G-SYS-010** (must) Every skill ships in three forms — Claude Code CLI skill, Claude desktop/Cowork .plugin (from SKILL.cowork.md) and Codex skill — each with a working script and a verification. agents-md builds them (scripts/skills-three.py, sync-targets.py apply).
- **G-UX-001** (must) "Hover" means: text is selected AND the pointer rests on it for 5 seconds -> the related popup appears. Plain pointer-over is not hover.
  - Saki: “我认为的悬停是选中鼠标悬停 5 秒就显示相关弹窗”
- **G-AGT-004** (default) Redesign runs in one of three modes per decision — Saki-led (concrete orders), co-design (iterate together), Claude-led (Saki gives a concept; Claude researches and proposes options beyond Saki's knowledge).
- **G-AGT-008** (default) When Saki names a derived project, that name (as a lowercase slug) is used for the repo, folder, notes and agent files instead of <upstream>-local; the upstream stays recorded in SKILLBUILDER.md and the registry.
- **G-AGT-009** (default) AGENTS.md is enough for Codex and other agents; add an agent-specific section only when a real difference shows up.
- **G-AGT-015** (default) When a project starts in (or is upgraded to) a Claude CC project, the coordinator first reviews the whole project and then pushes its open tasks to threads or Claude Code in cmux.
- **G-ENV-002** (default) Microsoft Edge is the day-to-day browser; browser integrations target Edge (Chromium).
- **G-ENV-005** (default) Saki works with Claude (Cowork and Claude Code) and Codex; outputs must be usable by all of them.
- **G-ENV-009** (default) lazygit is installed and is Saki's git TUI; suggest it for interactive git work.
- **G-UX-002** (default) Floating panels and popups should feel system-level, like macOS's own (native look, system-wide availability), not an app-bound overlay.

Stated in core (ids only; wording and Saki's words: `~/Developer/agents-md/ops/gene/genes.yaml`): G-AGT-001 (kernel §1), G-AGT-002 (kernel §2.1-2), G-AGT-003 (kernel §2.3), G-AGT-006 (kernel §5 + block git), G-AGT-007 (block roles), G-AGT-011 (kernel §2.7), G-AGT-013 (block cc), G-ENV-006 (kernel §5), G-ENV-008 (kernel §5), G-SEC-002 (kernel §5), G-SYS-001 (block evolve), G-SYS-002 (block evolve), G-AGT-012 (kernel §2.8), G-AGT-014 (kernel §9), G-ENV-001 (block mac), G-ENV-003 (block mac), G-ENV-004 (block mac), G-ENV-007 (block mac + cc), G-ENV-010 (block mac), G-ENV-011 (block mac), G-SEC-001 (kernel §5), G-SYS-004 (kernel header), G-SYS-008 (block evolve)

### Candidate genes (unconfirmed — treat as hints, ask Saki)
- **G-WMS-001** (must) Never edit data/db or wms.yaml from the NAS host; edit inside the container (docker compose exec -T bot python -), because host edits break the ACLs the container user needs.
- **G-WMS-002** (must) Never touch Telegram sessions or auth keys; TG_DIRECT_MEDIA stays off.
- **G-WMS-003** (must) wms do and wms outbound only plan by default; --apply, moves and deletes only on Saki's word; never empty PikPak trash.
- **G-WMS-005** (must) Restart only the bot service, never proxy (bot shares the proxy's network namespace); back up a config file before changing it.
- **G-WMS-004** (default) An NL model is accepted at model-only >= 80 %, with rules >= 95 %, dangerous = 0 and <= 5 s per sentence.
<!-- genes:end -->
