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
    global _language
    _language = normalize(value)
    return _language


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
    },
}
