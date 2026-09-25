"""WMS M3: the warehouse inside the bot's image and process.

* WMS borrows the bot's PikPak account (no second login);
* ``WMS_ENABLED`` runs the scheduler in the bot, off by default;
* ``python -m tgmd.wms`` (the image's ``wms``) runs the command line;
* the index and the token live on the data volume, so a restart keeps both.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pikpakapi import PikPakApi
from wms_fakes import FakeDrive

from pikpak_wms import __version__
from pikpak_wms.config import config_path, rules_path
from pikpak_wms.ops.embed import AccountUnavailable
from tgmd import wms
from tgmd.config import Config, PikPakConfig, load_config
from tgmd.db import Database
from tgmd.pikpak import PikPakError, PikPakService, strip_credentials, user_token_key

ROOT = Path(__file__).resolve().parent.parent


class FakeService:
    """Stands in for PikPakService: who has a session, and what client they get."""

    def __init__(self, drive=None, *, sessions=(), shared=True):
        self.drive = drive or FakeDrive()
        self.sessions = set(sessions)
        self.shared = shared
        self.asked: list[int | None] = []

    async def has_user_session(self, user_id):
        return user_id in self.sessions

    async def client(self, user_id=None):
        self.asked.append(user_id)
        if user_id is None and not self.shared:
            raise PikPakError("no PikPak account is connected")
        return self.drive


def bot_config(**wms_settings) -> Config:
    config = Config()
    for key, value in wms_settings.items():
        setattr(config.wms, key, value)
    return config


# ------------------------------------------------------------------ config


class TestSettings:
    def test_off_by_default_so_old_deployments_do_not_change(self):
        config = load_config(None)
        assert config.wms.enabled is False and config.wms.account is None

    def test_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("WMS_ENABLED", "true")
        monkeypatch.setenv("WMS_ACCOUNT", "12345")
        config = load_config(None)
        assert config.wms.enabled is True and config.wms.account == 12345

    def test_wms_files_on_the_data_volume_are_found_without_settings(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert config_path() == Path("config/wms.yaml")
        (tmp_path / "wms.yaml").write_text("{}")
        (tmp_path / "rules.yaml").write_text("{}")
        assert config_path() == tmp_path / "wms.yaml"
        assert rules_path() == tmp_path / "rules.yaml"
        monkeypatch.setenv("WMS_RULES", "/elsewhere.yaml")
        assert rules_path() == Path("/elsewhere.yaml")


# ----------------------------------------------------------------- account


class TestAccount:
    async def test_an_explicit_account_wins(self):
        service = FakeService(sessions={7})
        config = bot_config(account=99)
        config.access.admin_user_ids = [7]
        assert await wms.account_for(config, service) == 99

    async def test_else_the_first_admin_who_connected_one(self):
        service = FakeService(sessions={8})
        config = bot_config()
        config.access.admin_user_ids = [7, 8]
        assert await wms.account_for(config, service) == 8

    async def test_shared_can_be_chosen_explicitly(self, monkeypatch):
        monkeypatch.setenv("WMS_ACCOUNT", "shared")
        config = load_config(None)
        config.access.admin_user_ids = [7]
        assert await wms.account_for(config, FakeService(sessions={7})) is None

    async def test_else_the_shared_account(self):
        config = bot_config()
        config.access.admin_user_ids = [7]
        assert await wms.account_for(config, FakeService()) is None

    async def test_no_account_at_all_is_a_clear_wms_error(self):
        provider = wms.provider_for(bot_config(), FakeService(shared=False))
        with pytest.raises(AccountUnavailable) as info:
            await provider()
        assert "no PikPak account" in info.value.display()

    async def test_the_token_the_bot_stored_is_what_wms_uses(self, tmp_path, monkeypatch):
        """Restart safety for the token: it is the bot's, in the bot's database."""
        path = tmp_path / "bot.sqlite3"
        db = Database(path)
        await db.connect()
        # What the bot stores after a login: an encoded token, no password.
        logged_in = PikPakApi(username="me@example.com", password="secret")
        logged_in.access_token, logged_in.refresh_token = "stored-token", "refresh"
        logged_in.user_id = "u1"
        logged_in.encode_token()
        await db.kv_set_json(user_token_key(7), strip_credentials(logged_in.to_dict()))
        await db.close()

        async def probe(self):
            return {"quota": {}}

        monkeypatch.setattr(PikPakApi, "get_quota_info", probe)
        reopened = Database(path)  # a new process, as after a restart
        await reopened.connect()
        try:
            config = bot_config()
            config.access.admin_user_ids = [7]
            service = PikPakService(PikPakConfig(), reopened)
            client = await wms.provider_for(config, service)()
            assert client.access_token == "stored-token"
        finally:
            await reopened.close()


# ------------------------------------------------------------- in the bot


def write_wms_config(directory: Path, jobs: str) -> None:
    (directory / "wms.yaml").write_text(
        "ratelimit: {requests_per_second: 100000, burst: 100000}\n"
        f"schedule:\n  timezone: Asia/Shanghai\n  jobs:\n{jobs}"
    )


class TestInBot:
    async def test_disabled_does_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        inbot = wms.WmsInBot(bot_config(), FakeService())
        await inbot.start()
        assert inbot.embedded is None
        assert not (tmp_path / "wms.sqlite3").exists()

    async def test_enabled_schedules_the_configured_jobs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        write_wms_config(tmp_path, "    - {name: stocktake, cron: '*/30 * * * *'}\n"
                                   "    - {name: organize, cron: '15 * * * *'}\n")
        inbot = wms.WmsInBot(bot_config(enabled=True), FakeService())
        await inbot.start()
        try:
            assert inbot.embedded is not None
            # Listed by next run time, so compare as a set.
            # The configured two, plus the four M7 jobs (schedule.builtin).
            assert sorted(inbot.embedded.scheduled()) == [
                "big-report", "dedupe", "organize", "organize-inbox", "organize-tree",
                "stocktake",
            ]
            assert (tmp_path / "wms.sqlite3").exists()
        finally:
            await inbot.stop()
        assert inbot.embedded is None

    async def test_a_job_runs_on_the_bots_account(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        write_wms_config(tmp_path, "    - {name: stocktake, cron: '0 * * * *'}\n")
        drive = FakeDrive()
        drive.add("/Inbox/a.mkv")
        service = FakeService(drive, sessions={7})
        config = bot_config(enabled=True)
        config.access.admin_user_ids = [7]
        inbot = wms.WmsInBot(config, service)
        await inbot.start()
        try:
            result = await inbot.embedded.run_job("stocktake")
            assert "stocktake" in result.summary
            assert service.asked and set(service.asked) == {7}
        finally:
            await inbot.stop()

    async def test_a_broken_wms_config_does_not_stop_the_bot(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "wms.yaml").write_text("schedule:\n  jobs:\n    - {name: nonsense, cron: x}\n")
        inbot = wms.WmsInBot(bot_config(enabled=True), FakeService())
        await inbot.start()  # must not raise
        assert inbot.embedded is None
        assert "WMS could not start" in caplog.text


# -------------------------------------------------------------- the `wms`


class TestCommand:
    def test_the_index_survives_a_restart(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("WMS_LANG", "en")
        write_wms_config(tmp_path, "    []\n")
        drive = FakeDrive()
        drive.add("/Media/Show/e1.mkv")
        monkeypatch.setattr(wms, "PikPakService", lambda _config, _db: FakeService(drive))

        assert wms.main(["stocktake"]) == 0
        assert (tmp_path / "wms.sqlite3").exists()
        calls = len(drive.calls)
        # A second, separate run (as after a container restart) reads the
        # index from the volume without asking PikPak.
        assert wms.main(["ls", "/Media"]) == 0
        assert "Show" in capsys.readouterr().out
        assert len(drive.calls) == calls

    def test_errors_are_reported_not_raised(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("WMS_LANG", "en")
        monkeypatch.setattr(wms, "PikPakService",
                            lambda _config, _db: FakeService(shared=False))
        assert wms.main(["quota"]) == 1
        assert "no PikPak account" in capsys.readouterr().out

    def test_doctor_says_the_bots_account_is_used(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("WMS_LANG", "en")
        assert wms.main(["doctor"]) == 0
        assert "the bot's connected PikPak account" in capsys.readouterr().out

    def test_the_module_runs_as_the_image_shim_does(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TG_", "WMS_"))}
        env["DATA_DIR"] = str(tmp_path)
        done = subprocess.run(
            [sys.executable, "-m", "tgmd.wms", "version"], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert done.returncode == 0, done.stderr
        assert __version__ in done.stdout

    def test_the_dockerfile_installs_the_shim(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        assert "exec python -m tgmd.wms" in dockerfile
        assert "/usr/local/bin/wms" in dockerfile


# ---------------------------------------------------------------- boundary


def test_tgmd_reaches_wms_only_through_ops():
    """CC_BRIEF §5: tgmd calls pikpak_wms.ops, nothing below it."""
    offenders = []
    for path in (ROOT / "tgmd").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("pikpak_wms"):
                if not node.module.startswith("pikpak_wms.ops"):
                    offenders.append(f"{path.name}: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("pikpak_wms") and not alias.name.startswith(
                        "pikpak_wms.ops"
                    ):
                        offenders.append(f"{path.name}: import {alias.name}")
    assert offenders == []
