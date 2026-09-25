"""An in-memory PikPak drive for the WMS tests.

It answers the pikpakapi methods WmsClient calls, with the same shapes:
paged ``file_list``, ``path_to_id``, batch move / trash, and so on. Every
call is counted, so tests can assert how many requests an operation cost.

``propagate`` decides whether a change inside a folder also touches the
``modified_time`` of every folder above it. Incremental stocktake relies on
that; the tests run it both ways.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

from pikpakapi.PikpakException import PikpakException

FOLDER = "drive#folder"
FILE = "drive#file"


class FakeDrive:
    def __init__(self, *, propagate: bool = True, page_size_cap: int = 100) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.propagate = propagate
        self.page_size_cap = page_size_cap
        self.calls: list[str] = []
        self.fail_next: list[Exception] = []
        self._ids = itertools.count(1)
        self._clock = datetime(2026, 9, 1, tzinfo=UTC)
        self.quota = {"limit": "10995116277760", "usage": "1000", "usage_in_trash": "10"}
        self.tasks: list[dict[str, Any]] = []
        self.shares: list[dict[str, Any]] = []
        """What ``GET /drive/v1/share/list`` returns, one dict per share."""
        self.share_page = 100

    # ------------------------------------------------------------ building

    def _tick(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.isoformat()

    def _touch(self, folder_id: str) -> None:
        stamp = self._tick()
        current = folder_id
        while current:
            self.items[current]["modified_time"] = stamp
            if not self.propagate:
                return
            current = self.items[current]["parent_id"]

    def add(
        self,
        path: str,
        *,
        folder: bool = False,
        size: int = 0,
        mime: str = "",
        hash: str = "",
        created: str | None = None,
    ) -> str:
        """Create ``path`` (and any missing parents); return its id."""
        parts = [p for p in path.split("/") if p]
        parent = ""
        for index, name in enumerate(parts):
            last = index == len(parts) - 1
            existing = self._child(parent, name)
            if existing is not None and not (last and not folder):
                parent = existing["id"]
                continue
            item_id = f"id{next(self._ids)}"
            stamp = created or self._tick()
            is_folder = folder or not last
            self.items[item_id] = {
                "id": item_id,
                "parent_id": parent,
                "name": name,
                "kind": FOLDER if is_folder else FILE,
                "size": "0" if is_folder else str(size),
                "mime_type": "" if is_folder else mime,
                "hash": "" if is_folder else hash,
                "created_time": stamp,
                "modified_time": stamp,
                "trashed": False,
            }
            if parent:
                self._touch(parent)
            parent = item_id
        return parent

    def _child(self, parent_id: str, name: str) -> dict | None:
        for item in self.items.values():
            if item["parent_id"] == parent_id and item["name"] == name and not item["trashed"]:
                return item
        return None

    def path_of(self, item_id: str) -> str:
        parts = []
        current = item_id
        while current:
            parts.append(self.items[current]["name"])
            current = self.items[current]["parent_id"]
        return "/" + "/".join(reversed(parts))

    def id_at(self, path: str) -> str:
        current = ""
        for name in [p for p in path.split("/") if p]:
            child = self._child(current, name)
            if child is None:
                raise KeyError(path)
            current = child["id"]
        return current

    def live(self) -> list[dict]:
        return [item for item in self.items.values() if not self._in_trash(item["id"])]

    def _in_trash(self, item_id: str) -> bool:
        current = item_id
        while current:
            if self.items[current]["trashed"]:
                return True
            current = self.items[current]["parent_id"]
        return False

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_next:
            raise self.fail_next.pop(0)

    # ------------------------------------------------------------ the API

    async def file_list(self, size=100, parent_id=None, next_page_token=None, **_kw):
        self._record("file_list")
        parent = parent_id or ""
        children = sorted(
            (i for i in self.items.values() if i["parent_id"] == parent and not i["trashed"]),
            key=lambda i: i["id"],
        )
        start = int(next_page_token or 0)
        page = children[start : start + min(size, self.page_size_cap)]
        after = start + len(page)
        return {
            "files": [dict(item) for item in page],
            "next_page_token": str(after) if after < len(children) else "",
        }

    async def path_to_id(self, path, create=False):
        self._record("path_to_id")
        found = []
        current = ""
        for name in [p for p in path.split("/") if p]:
            child = self._child(current, name)
            if child is None:
                if not create:
                    return found
                child = self.items[self.add(self.path_of(current) + "/" + name if current
                                            else "/" + name, folder=True)]
            found.append({"id": child["id"], "name": child["name"], "file_type": "folder"})
            current = child["id"]
        return found

    async def get_quota_info(self):
        self._record("get_quota_info")
        return {"quota": dict(self.quota)}

    async def create_folder(self, name="New", parent_id=None):
        self._record("create_folder")
        base = self.path_of(parent_id) if parent_id else ""
        item_id = self.add(f"{base}/{name}", folder=True)
        return {"file": dict(self.items[item_id])}

    async def file_rename(self, id, new_file_name):
        self._record("file_rename")
        if self._child(self.items[id]["parent_id"], new_file_name) is not None:
            raise PikpakException("file name already exists")
        self.items[id]["name"] = new_file_name
        if self.items[id]["parent_id"]:
            self._touch(self.items[id]["parent_id"])
        return dict(self.items[id])

    async def file_batch_move(self, ids, to_parent_id=None):
        self._record("file_batch_move")
        target = to_parent_id or ""
        for item_id in ids:
            old_parent = self.items[item_id]["parent_id"]
            self.items[item_id]["parent_id"] = target
            if old_parent:
                self._touch(old_parent)
        if target:
            self._touch(target)
        return {}

    async def file_batch_copy(self, ids, to_parent_id=None):
        self._record("file_batch_copy")
        for item_id in ids:
            source = self.items[item_id]
            base = self.path_of(to_parent_id) if to_parent_id else ""
            self.add(f"{base}/{source['name']}", size=int(source["size"]),
                     mime=source["mime_type"], hash=source["hash"])
        return {}

    async def delete_to_trash(self, ids):
        self._record("delete_to_trash")
        for item_id in ids:
            self.items[item_id]["trashed"] = True
            if self.items[item_id]["parent_id"]:
                self._touch(self.items[item_id]["parent_id"])
        return {}

    async def untrash(self, ids):
        self._record("untrash")
        for item_id in ids:
            self.items[item_id]["trashed"] = False
            if self.items[item_id]["parent_id"]:
                self._touch(self.items[item_id]["parent_id"])
        return {}

    async def delete_forever(self, ids):
        self._record("delete_forever")
        for item_id in ids:
            self.items.pop(item_id, None)
        return {}

    async def file_batch_star(self, ids):
        self._record("file_batch_star")
        return {}

    async def file_batch_unstar(self, ids):
        self._record("file_batch_unstar")
        return {}

    async def file_batch_share(self, ids, need_password=False, expiration_days=-1):
        self._record("file_batch_share")
        return {"share_url": "https://mypikpak.com/s/SHARE", "pass_code": ""}

    async def get_download_url(self, file_id):
        self._record("get_download_url")
        return {"web_content_link": f"https://download.example/{file_id}"}

    async def offline_download(self, file_url, parent_id=None, name=None):
        self._record("offline_download")
        base = self.path_of(parent_id) if parent_id else ""
        item_id = self.add(f"{base}/{name or file_url.rsplit('/', 1)[-1] or 'download'}",
                           size=1024)
        task = {"id": f"task-{item_id}", "file_id": item_id,
                "file_name": self.items[item_id]["name"], "phase": "PHASE_TYPE_RUNNING"}
        self.tasks.append(task)
        return {"task": dict(task)}

    async def offline_list(self, size=10000, next_page_token=None, phase=None):
        self._record("offline_list")
        wanted = phase or ["PHASE_TYPE_RUNNING", "PHASE_TYPE_ERROR"]
        return {"tasks": [dict(t) for t in reversed(self.tasks) if t["phase"] in wanted][:size],
                "next_page_token": ""}

    async def get_share_info(self, share_link, pass_code=None):
        self._record("get_share_info")
        return {
            "share_status": "OK",
            "pass_code_token": "tok",
            "files": [{"id": "shared-1", "name": "shared.mkv"}],
        }

    async def restore(self, share_id, pass_code_token, file_ids):
        self._record("restore")
        return {}

    async def _request_get(self, url, params=None):
        """pikpakapi's authenticated GET; only the share list is spoken here."""
        self._record("share_list")
        if not url.endswith("/drive/v1/share/list"):
            raise PikpakException(f"FakeDrive does not answer {url}")
        params = params or {}
        start = int(params.get("page_token") or 0)
        size = min(int(params.get("limit") or 100), self.share_page)
        page = self.shares[start : start + size]
        after = start + len(page)
        return {"data": [dict(s) for s in page],
                "next_page_token": str(after) if after < len(self.shares) else ""}

    async def events(self, size=100, next_page_token=None):
        self._record("events")
        return {"events": [], "next_page_token": ""}


def provider_for(drive: FakeDrive):
    async def provider():
        return drive

    return provider
