"""WMS's message catalogue: what a person reads, in English or Chinese.

Separate from ``tgmd.i18n`` because this package must not import ``tgmd``;
the rules are the same (see that module):

* command names and their arguments are never translated (``wms ls``,
  ``--apply``, ``/wms plan``);
* stored values are never translated (audit actions, rule names, plan
  sources); they are translated at the moment of display;
* log lines are never translated.

The language is ``WMS_LANG``, else the bot's ``TGMD_LANG`` / ``BOT_LANG``,
else English, so WMS speaks whatever the bot speaks without extra setup.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "en"
LANGUAGES = ("en", "zh")


def normalize(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw.startswith("zh") or raw in ("cn", "chinese", "中文"):
        return "zh"
    return DEFAULT_LANGUAGE


def _from_environment() -> str:
    for name in ("WMS_LANG", "TGMD_LANG", "BOT_LANG"):
        value = os.environ.get(name, "").strip()
        if value:
            return normalize(value)
    return DEFAULT_LANGUAGE


_language: str | None = None


def set_language(value: str | None) -> str:
    """Fix the language for this process; ``None`` goes back to the environment."""
    global _language
    _language = normalize(value) if value is not None else None
    return language()


def language() -> str:
    return _language or _from_environment()


def t(key: str, /, lang: str | None = None, **kwargs: object) -> str:
    chosen = normalize(lang) if lang is not None else language()
    text = CATALOG.get(chosen, {}).get(key)
    if text is None:
        text = CATALOG[DEFAULT_LANGUAGE].get(key)
    if text is None:
        log.warning("no WMS catalogue entry for %r", key)
        return key
    if not kwargs:
        return text
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        log.warning("could not format WMS catalogue entry %r", key)
        return text


CATALOG: dict[str, dict[str, str]] = {
    "en": {
        # ---- actions, as shown in a plan
        "action.rename": "rename  {old}  →  {new}",
        "action.move": "move    {old}  →  {new}",
        "action.copy": "copy    {old}  →  {new}",
        "action.trash": "trash   {path}",
        "action.untrash": "restore {path}",
        "action.create_folder": "mkdir   {path}",
        "action.star": "star    {path}",
        "action.share": "share   {path}",
        "action.delete_forever": "DELETE FOREVER  {path}",
        "action.other": "{action}  {path}",
        "action.unstar": "unstar  {path}",
        "action.outbound": "fetch   {path}  →  {dest}",
        "action.inbound": "take in {source}  →  {path}",
        # ---- plans
        "plan.header": (
            "Plan {id} ({source}): {actions} action(s) on {files} file(s), {size}"
        ),
        "plan.empty": "  Nothing to do.",
        "plan.more": "  … and {count} more",
        "plan.note": "{text}",
        "plan.rule_matched": "rule “{rule}” matched {count}",
        "plan.not_found": "There is no plan {id}.",
        "plan.closed": "Plan {id} is {status}; it cannot be applied or discarded again.",
        "plan.status.pending": "pending",
        "plan.status.partial": "partly applied",
        "plan.status.applied": "applied",
        "plan.status.discarded": "discarded",
        "apply.summary": (
            "Plan {id}: {applied} applied, {skipped} skipped, {failed} failed, "
            "{remaining} left"
        ),
        "conflict.bad_name": "rule “{rule}”: {file} would get the unusable name “{name}”",
        "conflict.taken": "rule “{rule}”: {file} not moved, {path} is taken",
        "conflict.into_itself": "rule “{rule}”: cannot move {path} into itself",
        "conflict.no_folder": (
            "rule “{rule}”: {file} not moved, {path} does not exist (create_missing is off)"
        ),
        "conflict.outbound_folder": "rule “{rule}”: {path} is a folder; outbound takes files",
        "conflict.template": "rule “{rule}”: {file} skipped: {error}",
        "dedupe.group": "keep {keep}, trash {count} copy/copies ({size})",
        "dedupe.size_mismatch": "same hash {hash} but different sizes, left alone: {paths}",
        "dedupe.total": "duplicates take {size}",
        "forever.refused": (
            "Permanent deletion is off. Set runtime.allow_permanent_delete: true in the "
            "config and pass --forever."
        ),
        # ---- undo
        "undo.no_entry": "There is no audit entry {id}.",
        "undo.failed": "The undo failed: {error}",
        "undo.refused.generic": "“{action}” cannot be undone.",
        "undo.refused.already": "Entry {id} was already undone (entry {by}).",
        "undo.refused.changed": (
            "The file changed since entry {id} ({state}); nothing was done."
        ),
        "undo.refused.dry_run": "Entry {id} was a dry run; it changed nothing.",
        "undo.refused.copy": "A copy is not undone automatically: trash {path} yourself.",
        "undo.refused.forever": "A permanent deletion cannot be undone.",
        "undo.refused.share": (
            "PikPak has no way to cancel a share from here; cancel it in the PikPak app."
        ),
        "undo.refused.outbound": "A file taken out of the drive cannot be undone.",
        "undo.refused.inbound": "To undo an inbound, trash {path} yourself.",
        "undo.refused.existed": "{path} existed before; undo will not trash it.",
        "undo.refused.not_empty": "{path} is not empty; undo what was put in it first.",
        # ---- errors
        "error.not_indexed": "{path} is not a folder in the index; run wms stocktake first.",
        "error.no_folder": "{path} does not exist.",
        "error.no_outbound": "No outbound destination is configured.",
        "error.no_account": "WMS has no PikPak account to use: {detail}",
        # ---- inbound / outbound / jobs
        "inbound.unknown": "Not a magnet, URL or PikPak share link: {source}",
        "inbound.share_unusable": "The share link is not usable (status {status}).",
        "inbound.share_empty": "The share link contains no files.",
        "outbound.missing": "{path} is not in the index",
        "outbound.no_local_dir": (
            "No local folder: set outbound.local_dir in the config, or MEDIA_DIR."
        ),
        "outbound.short": "{path}: got {got} of {size} bytes; the partial file was removed",
        "outbound.aria2_error": "aria2 refused: {error}",
        "outbound.unknown": "Unknown downloader “{mode}”: use none, aria2 or local.",
        "job.nothing": "{name}: nothing to do",
        "job.planned": "{name}: plan {id} with {actions} action(s) is waiting for confirmation",
        "job.no_rules": "{name}: no enabled rules",
        "job.polled": "{checked} download(s) checked, {finished} finished, {failed} failed",
        # ---- stocktake
        "stocktake.full": (
            "Full stocktake of {roots}: {entries} entries, {listed} folder(s) listed, "
            "{requests} request(s), {seconds}s"
        ),
        "stocktake.incremental": (
            "Incremental stocktake of {roots}: {entries} entries, {listed} folder(s) "
            "listed, {skipped} unchanged subtree(s) skipped, {requests} request(s), "
            "{seconds}s"
        ),
        "verify.clean": "the index matches the drive ({checked} entries checked)",
        "verify.dirty": (
            "{missing} missing, {stale} stale, {changed} changed, out of {checked} entries"
        ),
        # ---- command line
        "cli.help": "PikPak warehouse management: stocktake, plan, apply, audit.",
        "cli.version": "pikpak_wms {version}",
        "cli.doctor.title": "Readiness",
        "cli.doctor.item": "Item",
        "cli.doctor.state": "State",
        "cli.doctor.config": "Config file",
        "cli.doctor.config_missing": "missing, using defaults",
        "cli.doctor.credentials": "Credentials",
        "cli.doctor.credentials_env": "in the environment",
        "cli.doctor.credentials_token": "saved token",
        "cli.doctor.credentials_bot": "the bot's connected PikPak account",
        "cli.doctor.credentials_none": (
            "none: set PIKPAK_USERNAME and PIKPAK_PASSWORD, or PIKPAK_ENCODED_TOKEN"
        ),
        "cli.doctor.dry_run": "Dry run by default",
        "cli.doctor.forever": "Permanent delete",
        "cli.doctor.on": "on",
        "cli.doctor.off": "off",
        "cli.doctor.allowed": "allowed",
        "cli.doctor.disabled": "disabled",
        "cli.doctor.rate": "Rate limit",
        "cli.doctor.rate_value": "{rate} requests/s",
        "cli.doctor.database": "Database",
        "cli.doctor.index": "Index",
        "cli.doctor.index_value": "{count} entries, last stocktake {when}",
        "cli.doctor.never": "never",
        "cli.login.done": "Logged in. Token saved to {path}; the password is not stored.",
        "cli.error": "Error: {error}",
        "cli.stocktake.verify_clean": "Verified: {summary}.",
        "cli.stocktake.verify_dirty": "The index differs from the drive: {summary}.",
        "cli.stocktake.missing": "missing from the index",
        "cli.stocktake.stale": "gone from the drive",
        "cli.stocktake.changed": "changed",
        "cli.ls.name": "Name",
        "cli.ls.size": "Size",
        "cli.ls.modified": "Modified",
        "cli.ls.empty": "{path} is empty.",
        "cli.ls.folder": "folder",
        "cli.quota.line": "Used {used} of {limit} ({percent}%), {trash} in the trash.",
        "cli.todo": "Not implemented yet: {what} (planned for {milestone}).",
        "cli.plan.dry_run_hint": "Dry run: nothing changed. Apply it with: wms apply {id}",
        "cli.plan.status": "Status: {status}, {progress}/{total} handled.",
        "cli.plan.discarded": "Plan {id} discarded.",
        "cli.plans.none": "No plans waiting.",
        "cli.plans.id": "Plan",
        "cli.plans.source": "From",
        "cli.plans.status": "Status",
        "cli.plans.actions": "Done",
        "cli.plans.created": "Made",
        "cli.apply.failed": "failed: {path}: {error}",
        "cli.apply.stopped": "Stopped early: {reason}. Run the same command again to continue.",
        "cli.forever.confirm": "Delete permanently? This cannot be undone.",
        "cli.rules.valid": "The rules file is valid: {count} rule(s).",
        "cli.rules.name": "Rule",
        "cli.rules.stage": "Stage",
        "cli.rules.scope": "Scope",
        "cli.rules.actions": "Actions",
        "cli.rules.enabled": "Enabled",
        "cli.inbound.plan": "Would take in {source} ({kind}) to {target}",
        "cli.inbound.hint": "Dry run: nothing sent. Add --apply to take them in.",
        "cli.inbound.done": "Taken in {source} ({kind}): {names}",
        "cli.inbound.existing": "Already taken in earlier ({phase}): {source}",
        "cli.audit.none": "No changes recorded yet.",
        "cli.audit.id": "Entry",
        "cli.audit.at": "When (UTC)",
        "cli.audit.what": "Change",
        "cli.audit.rule": "Rule",
        "cli.audit.undo_of": "[undo of {id}]",
        "cli.undo.hint": "Dry run: nothing changed. Undo it with: wms undo {id} --apply",
        "cli.undo.done": "Entry {id} undone (recorded as entry {new}).",
        "cli.run.no_jobs": "No enabled jobs under schedule.jobs in the config.",
        "cli.run.started": "Running {count} job(s) on {tz} time. Ctrl-C stops.",
        "cli.events.raw_only": (
            "Only --raw exists for now: the format is undocumented (docs/wms/EXTRAS.md §5)."
        ),
    },
    "zh": {
        "action.rename": "重命名  {old}  →  {new}",
        "action.move": "移动    {old}  →  {new}",
        "action.copy": "复制    {old}  →  {new}",
        "action.trash": "入回收站  {path}",
        "action.untrash": "从回收站还原  {path}",
        "action.create_folder": "建目录  {path}",
        "action.star": "加星标  {path}",
        "action.share": "分享    {path}",
        "action.delete_forever": "永久删除  {path}",
        "action.other": "{action}  {path}",
        "action.unstar": "取消星标  {path}",
        "action.outbound": "出库    {path}  →  {dest}",
        "action.inbound": "入库    {source}  →  {path}",
        "plan.header": "计划 {id}（{source}）：{actions} 个动作，涉及 {files} 个文件，共 {size}",
        "plan.empty": "  没有要做的。",
        "plan.more": "  …… 还有 {count} 个",
        "plan.note": "{text}",
        "plan.rule_matched": "规则「{rule}」命中 {count} 个",
        "plan.not_found": "没有编号为 {id} 的计划。",
        "plan.closed": "计划 {id} 已{status}，不能再执行或丢弃。",
        "plan.status.pending": "待确认",
        "plan.status.partial": "部分执行",
        "plan.status.applied": "执行完毕",
        "plan.status.discarded": "丢弃",
        "apply.summary": (
            "计划 {id}：执行 {applied}，跳过 {skipped}，失败 {failed}，剩余 {remaining}"
        ),
        "conflict.bad_name": "规则「{rule}」：{file} 会得到不可用的名字「{name}」",
        "conflict.taken": "规则「{rule}」：{file} 没有移动，{path} 已被占用",
        "conflict.into_itself": "规则「{rule}」：不能把 {path} 移进它自己",
        "conflict.no_folder": (
            "规则「{rule}」：{file} 没有移动，{path} 不存在（create_missing 已关闭）"
        ),
        "conflict.outbound_folder": "规则「{rule}」：{path} 是目录，出库只接受文件",
        "conflict.template": "规则「{rule}」：跳过 {file}：{error}",
        "dedupe.group": "保留 {keep}，{count} 个副本进回收站（{size}）",
        "dedupe.size_mismatch": "hash {hash} 相同但大小不同，不处理：{paths}",
        "dedupe.total": "重复文件共占 {size}",
        "forever.refused": (
            "永久删除未开启。需要在配置里设 runtime.allow_permanent_delete: true，"
            "并且传 --forever。"
        ),
        "undo.no_entry": "没有编号为 {id} 的审计记录。",
        "undo.failed": "撤销失败：{error}",
        "undo.refused.generic": "「{action}」不能撤销。",
        "undo.refused.already": "记录 {id} 已经撤销过了（记录 {by}）。",
        "undo.refused.changed": "记录 {id} 之后文件又变了（{state}），没有做任何事。",
        "undo.refused.dry_run": "记录 {id} 只是 dry-run，本来就没改动任何东西。",
        "undo.refused.copy": "复制不自动撤销：请自己把 {path} 放进回收站。",
        "undo.refused.forever": "永久删除无法撤销。",
        "undo.refused.share": "这里没法取消分享（PikPak 没有这个接口），请在 PikPak 应用里取消。",
        "undo.refused.outbound": "已经取出网盘的文件无法撤销。",
        "undo.refused.inbound": "要撤销入库，请自己把 {path} 放进回收站。",
        "undo.refused.existed": "{path} 原本就存在，撤销不会把它放进回收站。",
        "undo.refused.not_empty": "{path} 不是空的，请先撤销放进去的东西。",
        "error.not_indexed": "本地索引里没有 {path} 这个目录，请先运行 wms stocktake。",
        "error.no_folder": "{path} 不存在。",
        "error.no_outbound": "没有配置出库目的地。",
        "error.no_account": "WMS 没有可用的 PikPak 账号：{detail}",
        "inbound.unknown": "不是磁力、URL 或 PikPak 分享链接：{source}",
        "inbound.share_unusable": "分享链接不可用（状态 {status}）。",
        "inbound.share_empty": "分享链接里没有文件。",
        "outbound.missing": "本地索引里没有 {path}",
        "outbound.no_local_dir": "没有本地目录：请在配置里设 outbound.local_dir，或设 MEDIA_DIR。",
        "outbound.short": "{path}：只收到 {got} / {size} 字节，已删除残缺文件",
        "outbound.aria2_error": "aria2 拒绝：{error}",
        "outbound.unknown": "未知的下载器「{mode}」：可选 none、aria2、local。",
        "job.nothing": "{name}：没有要做的",
        "job.planned": "{name}：计划 {id}（{actions} 个动作）等待确认",
        "job.no_rules": "{name}：没有启用的规则",
        "job.polled": "检查了 {checked} 个下载，完成 {finished}，失败 {failed}",
        "stocktake.full": (
            "全量盘点 {roots}：{entries} 条，列出 {listed} 个目录，{requests} 次请求，{seconds} 秒"
        ),
        "stocktake.incremental": (
            "增量盘点 {roots}：{entries} 条，列出 {listed} 个目录，跳过 {skipped} 个未变的子树，"
            "{requests} 次请求，{seconds} 秒"
        ),
        "verify.clean": "本地索引与网盘一致（核对了 {checked} 条）",
        "verify.dirty": "共 {checked} 条中：缺 {missing}、多 {stale}、不同 {changed}",
        "cli.help": "PikPak 网盘仓储管理：盘点、出计划、执行、审计。",
        "cli.version": "pikpak_wms {version}",
        "cli.doctor.title": "就绪检查",
        "cli.doctor.item": "项",
        "cli.doctor.state": "状态",
        "cli.doctor.config": "主配置",
        "cli.doctor.config_missing": "缺失，用默认值",
        "cli.doctor.credentials": "凭据",
        "cli.doctor.credentials_env": "来自环境变量",
        "cli.doctor.credentials_token": "已保存的 token",
        "cli.doctor.credentials_bot": "bot 已连接的 PikPak 账号",
        "cli.doctor.credentials_none": (
            "没有：请设置 PIKPAK_USERNAME 和 PIKPAK_PASSWORD，或 PIKPAK_ENCODED_TOKEN"
        ),
        "cli.doctor.dry_run": "默认只出计划",
        "cli.doctor.forever": "永久删除",
        "cli.doctor.on": "开",
        "cli.doctor.off": "关",
        "cli.doctor.allowed": "已允许",
        "cli.doctor.disabled": "禁用",
        "cli.doctor.rate": "限流",
        "cli.doctor.rate_value": "每秒 {rate} 个请求",
        "cli.doctor.database": "数据库",
        "cli.doctor.index": "本地索引",
        "cli.doctor.index_value": "{count} 条，上次盘点 {when}",
        "cli.doctor.never": "从未",
        "cli.login.done": "已登录。token 已保存到 {path}，密码不保存。",
        "cli.error": "出错：{error}",
        "cli.stocktake.verify_clean": "核对通过：{summary}。",
        "cli.stocktake.verify_dirty": "本地索引与网盘不一致：{summary}。",
        "cli.stocktake.missing": "索引里缺少",
        "cli.stocktake.stale": "网盘上已没有",
        "cli.stocktake.changed": "有变化",
        "cli.ls.name": "名称",
        "cli.ls.size": "大小",
        "cli.ls.modified": "修改时间",
        "cli.ls.empty": "{path} 是空的。",
        "cli.ls.folder": "文件夹",
        "cli.quota.line": "已用 {used} / 共 {limit}（{percent}%），回收站占 {trash}。",
        "cli.todo": "尚未实现：{what}（计划在 {milestone}）。",
        "cli.plan.dry_run_hint": "只是计划，没有改动任何东西。确认执行：wms apply {id}",
        "cli.plan.status": "状态：{status}，已处理 {progress}/{total}。",
        "cli.plan.discarded": "计划 {id} 已丢弃。",
        "cli.plans.none": "没有等待确认的计划。",
        "cli.plans.id": "计划",
        "cli.plans.source": "来源",
        "cli.plans.status": "状态",
        "cli.plans.actions": "进度",
        "cli.plans.created": "生成时间",
        "cli.apply.failed": "失败：{path}：{error}",
        "cli.apply.stopped": "提前停止：{reason}。再运行一次同样的命令会接着做。",
        "cli.forever.confirm": "确定永久删除？无法撤销。",
        "cli.rules.valid": "规则文件有效：{count} 条规则。",
        "cli.rules.name": "规则",
        "cli.rules.stage": "阶段",
        "cli.rules.scope": "范围",
        "cli.rules.actions": "动作",
        "cli.rules.enabled": "启用",
        "cli.inbound.plan": "将入库 {source}（{kind}）到 {target}",
        "cli.inbound.hint": "只是计划，什么都没发送。加 --apply 才会入库。",
        "cli.inbound.done": "已入库 {source}（{kind}）：{names}",
        "cli.inbound.existing": "之前已经入库过（{phase}）：{source}",
        "cli.audit.none": "还没有任何改动记录。",
        "cli.audit.id": "记录",
        "cli.audit.at": "时间（UTC）",
        "cli.audit.what": "改动",
        "cli.audit.rule": "规则",
        "cli.audit.undo_of": "[撤销 {id}]",
        "cli.undo.hint": "只是预览，没有改动。确认撤销：wms undo {id} --apply",
        "cli.undo.done": "记录 {id} 已撤销（记为记录 {new}）。",
        "cli.run.no_jobs": "配置的 schedule.jobs 里没有启用的任务。",
        "cli.run.started": "按 {tz} 时间运行 {count} 个定时任务，Ctrl-C 停止。",
        "cli.events.raw_only": (
            "目前只有 --raw：这个接口的格式没有文档（见 docs/wms/EXTRAS.md §5）。"
        ),
    },
}
