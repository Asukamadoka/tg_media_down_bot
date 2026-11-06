"""WMS M1: rate limit, client, store, stocktake, the login token, the CLI.

The drive is :class:`wms_fakes.FakeDrive`, so every test can count exactly
how many PikPak requests an operation cost.
"""

from __future__ import annotations

import ast
import json
import os
import stat
from pathlib import Path

import pytest
from pikpakapi.PikpakException import PikpakException
from typer.testing import CliRunner
from wms_fakes import FakeDrive, provider_for

from pikpak_wms.cli import main as cli
from pikpak_wms.config import Config, Credentials
from pikpak_wms.core import auth
from pikpak_wms.core.client import WmsClient
from pikpak_wms.core.errors import AuthError, NotFoundError, RateLimitedError, WmsError
from pikpak_wms.core.models import Action, ActionType, FileNode, Kind
from pikpak_wms.core.ratelimit import TokenBucket
from pikpak_wms.ops import listing
from pikpak_wms.ops.context import Context
from pikpak_wms.ops.stocktake import stocktake, verify
from pikpak_wms.store.db import Store


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def unlimited() -> TokenBucket:
    return TokenBucket(1e9, 1_000_000)


@pytest.fixture
async def store(tmp_path):
    async with Store(tmp_path / "wms.sqlite3") as opened:
        yield opened


def client_for(drive: FakeDrive, **kwargs) -> WmsClient:
    kwargs.setdefault("limiter", unlimited())
    return WmsClient(provider_for(drive), **kwargs)


# --------------------------------------------------------------- rate limit


class TestTokenBucket:
    async def test_a_burst_is_free_then_the_rate_holds(self):
        clock = Clock()
        bucket = TokenBucket(4, 8, clock=clock, sleep=clock.sleep)
        for _ in range(8):
            await bucket.acquire()
        assert clock.now == 0
        for _ in range(8):
            await bucket.acquire()
        # Eight more at four a second: two seconds.
        assert clock.now == pytest.approx(2.0)

    def test_nonsense_settings_are_refused(self):
        with pytest.raises(ValueError):
            TokenBucket(0, 1)


# ------------------------------------------------------------------ client


class TestClient:
    async def test_every_request_waits_for_the_bucket(self):
        clock = Clock()
        drive = FakeDrive()
        client = client_for(drive, limiter=TokenBucket(4, 1, clock=clock, sleep=clock.sleep))
        for _ in range(5):
            await client.quota()
        assert clock.now == pytest.approx(1.0)
        assert client.calls == 5

    async def test_rate_limit_refusals_back_off_and_retry(self):
        clock = Clock()
        drive = FakeDrive()
        drive.fail_next = [PikpakException("request too frequent"), PikpakException("429")]
        client = client_for(drive, backoff=3.0, sleep=clock.sleep)
        quota = await client.quota()
        assert quota.limit > 0
        assert clock.slept == [3.0, 6.0]

    async def test_it_gives_up_after_the_retries(self):
        drive = FakeDrive()
        drive.fail_next = [PikpakException("too frequent")] * 5
        client = client_for(drive, max_retries=2, sleep=Clock().sleep)
        with pytest.raises(RateLimitedError):
            await client.quota()

    async def test_login_problems_become_auth_errors(self):
        drive = FakeDrive()
        drive.fail_next = [PikpakException("Invalid username or password")]
        with pytest.raises(AuthError):
            await client_for(drive).quota()

    async def test_anything_else_becomes_a_wms_error(self):
        drive = FakeDrive()
        drive.fail_next = [PikpakException("file not found")]
        with pytest.raises(WmsError, match="file not found"):
            await client_for(drive).quota()

    async def test_listing_follows_every_page(self):
        drive = FakeDrive(page_size_cap=10)
        for index in range(25):
            drive.add(f"/Box/f{index}.bin", size=index)
        client = client_for(drive)
        nodes = [n async for n in client.list_folder(drive.id_at("/Box"), parent_path="/Box")]
        assert len(nodes) == 25
        assert drive.calls.count("file_list") == 3
        assert nodes[0].path.startswith("/Box/")

    async def test_a_missing_path_is_not_found(self):
        with pytest.raises(NotFoundError):
            await client_for(FakeDrive()).resolve_path("/nope")

    async def test_quota_parses_pikpaks_strings(self):
        quota = await client_for(FakeDrive()).quota()
        assert (quota.used, quota.in_trash) == (1000, 10)


# ------------------------------------------------------------------- store


def node(file_id, parent, path, *, folder=False, size=0, modified="t1"):
    return FileNode(
        file_id=file_id,
        parent_id=parent,
        name=path.rsplit("/", 1)[-1],
        kind=Kind.FOLDER if folder else Kind.FILE,
        path=path,
        size=size,
        modified_time=modified,
    )


class TestStore:
    async def test_a_folder_listing_replaces_what_was_there(self, store):
        await store.replace_children("", "/", [node("a", "", "/a"), node("b", "", "/b")], "s1")
        await store.replace_children("", "/", [node("a", "", "/a")], "s2")
        assert [n.file_id for n in await store.children("")] == ["a"]

    async def test_a_vanished_folder_takes_its_subtree_with_it(self, store):
        await store.replace_children("", "/", [node("d", "", "/d", folder=True)], "s")
        await store.replace_children("d", "/d", [node("x", "d", "/d/x")], "s")
        await store.replace_children("", "/", [], "s")
        assert await store.count_files() == 0

    async def test_a_renamed_folder_re_roots_its_contents(self, store):
        await store.replace_children("", "/", [node("d", "", "/old", folder=True)], "s")
        await store.replace_children("d", "/old", [node("x", "d", "/old/x")], "s")
        await store.replace_children("", "/", [node("d", "", "/new", folder=True)], "s")
        assert (await store.node("x")).path == "/new/x"

    async def test_like_wildcards_in_names_are_literal(self, store):
        # "a_b" must not match "axb": _ is a LIKE wildcard unless escaped.
        folders = [node("u", "", "/a_b", folder=True), node("v", "", "/axb", folder=True)]
        await store.replace_children("", "/", folders, "s")
        await store.replace_children("u", "/a_b", [node("1", "u", "/a_b/f")], "s")
        await store.replace_children("v", "/axb", [node("2", "v", "/axb/f")], "s")
        assert [n.file_id for n in await store.nodes_under("/a_b")] == ["1"]

    async def test_non_recursive_is_one_level(self, store):
        await store.replace_children("", "/", [node("d", "", "/d", folder=True)], "s")
        await store.replace_children("d", "/d", [node("e", "d", "/d/e", folder=True)], "s")
        await store.replace_children("e", "/d/e", [node("f", "e", "/d/e/f")], "s")
        assert [n.file_id for n in await store.nodes_under("/d", recursive=False)] == ["e"]

    async def test_the_audit_keeps_the_before_snapshot(self, store):
        action = Action(
            ActionType.RENAME, "x", before={"name": "a"}, after={"name": "b"}, rule_name="r"
        )
        audit_id = await store.record(action, dry_run=False)
        entry = await store.audit_entry(audit_id)
        assert entry["before"] == {"name": "a"}
        assert entry["dry_run"] is False


# --------------------------------------------------------------- stocktake


def big_drive(*, propagate: bool = True) -> FakeDrive:
    """A little over a thousand files, several levels deep (M1's acceptance)."""
    drive = FakeDrive(propagate=propagate)
    for show in range(12):
        for season in range(4):
            for episode in range(25):
                drive.add(
                    f"/Media/Show{show}/S{season}/E{episode:02d}.mkv",
                    size=100 + episode,
                    hash=f"h{show}-{season}-{episode}",
                )
    drive.add("/Inbox/new.mkv", size=5)
    return drive


class TestStocktake:
    async def test_a_full_stocktake_indexes_everything(self, store):
        drive = big_drive()
        report = await stocktake(client_for(drive), store, full=True)
        files = [i for i in drive.live() if i["kind"] == "drive#file"]
        assert len(files) == 1201
        assert await store.count_files() == len(drive.live())
        assert report.entries == len(drive.live())
        assert (await store.node_at("/Media/Show3/S2/E07.mkv")).size == 107

    async def test_a_second_run_with_no_changes_lists_only_the_root(self, store):
        drive = big_drive()
        first = await stocktake(client_for(drive), store)
        second = await stocktake(client_for(drive), store)
        assert second.folders_listed == 1
        assert second.requests < first.requests / 20
        # The numbers the acceptance asks for, on this fixture:
        assert (first.requests, second.requests) == (first.requests, 1)

    async def test_a_deep_change_is_found_when_pikpak_propagates(self, store):
        drive = big_drive(propagate=True)
        await stocktake(client_for(drive), store)
        drive.add("/Media/Show5/S1/extra.nfo", size=1)
        await stocktake(client_for(drive), store)
        assert await store.node_at("/Media/Show5/S1/extra.nfo") is not None
        assert (await verify(client_for(drive), store)).clean

    async def test_verify_catches_what_incremental_misses_without_propagation(self, store):
        # If PikPak only touches the direct parent, incremental misses deep
        # changes. verify is how Cowork finds out which world we are in.
        drive = big_drive(propagate=False)
        await stocktake(client_for(drive), store)
        drive.add("/Media/Show5/S1/extra.nfo", size=1)
        await stocktake(client_for(drive), store)
        result = await verify(client_for(drive), store)
        assert result.missing == ["/Media/Show5/S1/extra.nfo"]
        await stocktake(client_for(drive), store, full=True)
        assert (await verify(client_for(drive), store)).clean

    async def test_deletions_and_renames_are_picked_up(self, store):
        drive = big_drive()
        await stocktake(client_for(drive), store)
        drive.items[drive.id_at("/Media/Show0")]["name"] = "Renamed"
        drive._touch(drive.id_at("/Media"))  # noqa: SLF001 - the rename touches the parent
        await drive.delete_to_trash([drive.id_at("/Inbox/new.mkv")])
        await stocktake(client_for(drive), store)
        assert await store.node_at("/Media/Renamed/S0/E00.mkv") is not None
        assert await store.node_at("/Media/Show0/S0/E00.mkv") is None
        assert await store.node_at("/Inbox/new.mkv") is None

    async def test_an_interrupted_run_leaves_nothing_hidden(self, store):
        drive = big_drive()
        client = client_for(drive)
        # Fail after a handful of listings: the run stops part-way.
        calls = {"n": 0}
        real = drive.file_list

        async def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 6:
                raise PikpakException("connection reset")
            return await real(*args, **kwargs)

        drive.file_list = flaky
        with pytest.raises(WmsError):
            await stocktake(client, store)
        drive.file_list = real
        await stocktake(client_for(drive), store)
        assert (await verify(client_for(drive), store)).clean

    async def test_one_subtree_can_be_stocktaken_alone(self, store):
        drive = big_drive()
        report = await stocktake(client_for(drive), store, roots=["/Inbox"])
        assert report.entries == 1
        assert await store.node_at("/Inbox/new.mkv") is not None


class TestListing:
    async def test_ls_reads_the_index_not_the_drive(self, store):
        drive = big_drive()
        await stocktake(client_for(drive), store)
        before = len(drive.calls)
        ctx = Context(config=Config(), client=client_for(drive), store=store)
        names = [n.name for n in await listing.ls(ctx, "/Media/Show1")]
        assert names == ["S0", "S1", "S2", "S3"]
        assert len(drive.calls) == before

    async def test_ls_of_an_unknown_folder_says_to_stocktake(self, store):
        ctx = Context(config=Config(), client=client_for(FakeDrive()), store=store)
        with pytest.raises(NotFoundError, match="stocktake"):
            await listing.ls(ctx, "/nowhere")


# ------------------------------------------------------------------- login


class TestToken:
    def test_the_file_holds_the_token_and_never_the_password(self, tmp_path):
        class Fake:
            def encode_token(self):
                pass

            def to_dict(self):
                return {"username": "u", "password": "p", "encoded_token": "tok"}

        path = tmp_path / "token.json"
        auth.write_token(path, Fake())
        assert json.loads(path.read_text()) == {"encoded_token": "tok"}
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    async def test_without_credentials_login_explains_what_to_set(self, tmp_path):
        standalone = auth.StandaloneAuth(tmp_path / "t.json", Credentials())
        with pytest.raises(AuthError, match="PIKPAK_USERNAME"):
            await standalone.login()

    def test_credentials_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("PIKPAK_USERNAME", "me@example.com")
        monkeypatch.setenv("PIKPAK_PASSWORD", "x")
        assert Credentials.from_environment().usable


# --------------------------------------------------------------------- CLI


@pytest.fixture
def cli_drive(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    # The real limit is 4 requests a second; a stocktake here would take ages.
    config = tmp_path / "wms.yaml"
    config.write_text("ratelimit:\n  requests_per_second: 100000\n  burst: 100000\n")
    monkeypatch.setenv("WMS_CONFIG", str(config))
    monkeypatch.setenv("WMS_LANG", "en")
    drive = big_drive()
    monkeypatch.setattr(cli.state, "provider_factory", lambda config: provider_for(drive))
    return drive


runner = CliRunner()


class TestCommandLine:
    def test_stocktake_then_ls(self, cli_drive):
        result = runner.invoke(cli.app, ["stocktake"])
        assert result.exit_code == 0, result.output
        assert "Incremental stocktake" in result.output
        result = runner.invoke(cli.app, ["ls", "/Media"])
        assert result.exit_code == 0
        assert "Show0" in result.output

    def test_verify_exits_non_zero_when_the_index_is_behind(self, cli_drive):
        runner.invoke(cli.app, ["stocktake"])
        cli_drive.add("/Inbox/late.mkv", size=1)
        cli_drive.propagate = False
        result = runner.invoke(cli.app, ["stocktake", "--verify"])
        assert result.exit_code == 1
        assert "/Inbox/late.mkv" in result.output

    def test_quota(self, cli_drive):
        result = runner.invoke(cli.app, ["quota"])
        assert result.exit_code == 0
        assert "10.0 TiB" in result.output

    def test_doctor_needs_no_network(self, cli_drive):
        result = runner.invoke(cli.app, ["doctor"])
        assert result.exit_code == 0
        assert cli_drive.calls == []

    def test_ls_before_any_stocktake_is_a_clear_error(self, cli_drive):
        result = runner.invoke(cli.app, ["ls", "/Media"])
        assert result.exit_code == 1
        assert "stocktake" in result.output

    def test_chinese_output(self, cli_drive, monkeypatch):
        monkeypatch.setenv("WMS_LANG", "zh")
        result = runner.invoke(cli.app, ["stocktake"])
        assert "增量盘点" in result.output


# ---------------------------------------------------------------- boundary


def test_pikpak_wms_never_imports_tgmd():
    """CC_BRIEF §5: tgmd calls WMS, never the other way round."""
    root = Path(__file__).resolve().parent.parent / "pikpak_wms"
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for item in ast.walk(tree):
            names = []
            if isinstance(item, ast.Import):
                names = [alias.name for alias in item.names]
            elif isinstance(item, ast.ImportFrom) and item.level == 0:
                names = [item.module or ""]
            if any(name == "tgmd" or name.startswith("tgmd.") for name in names):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []
