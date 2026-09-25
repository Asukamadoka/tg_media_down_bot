"""A drive shaped like the real one (docs/wms/M7 §0), built straight into a Store.

The numbers are the 2026-09-25 stocktake's:

* 80,964 entries, about 9.7 TiB; 45 top-level folders, no file at the root;
* 751 files directly in a top-level folder, in 19 of them, 467 in the
  largest: 698 mp4, 28 mov, 16 zip/7z/rar, 2 jpg (and 7 mkv, which the
  brief's breakdown leaves unnamed);
* 435 second-level folders, 40 of them over 50 GiB, 6.4 TiB together;
* 155 files over 4 GiB, 1,070 GiB together;
* 20 distinct shared files.

Everything comes from one seeded generator: the same tree every time, so
plans over it can be compared with stored snapshots. Names mix the kinds
the brief calls out: series with episode numbers, site tags in brackets,
resolution tags, camera names, hashes, bare numbers.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from pikpak_wms.core.models import FileNode, Kind

GiB = 1024**3
TiB = 1024**4

PROTECTED = ["收藏", "Cosplaytales Nako EP#1-24", "小千"]
INBOXES = ["Telegram", "Pack From Shared"]
OTHERS = [
    "写真", "Cosplay", "Nako", "Yuki合集", "旅行", "电影", "动漫", "纪录片", "音乐会", "舞蹈",
    "健身", "教程", "游戏录像", "综艺", "短视频", "直播回放", "MV", "演唱会", "体育", "课程",
    "Vlog", "花絮", "访谈", "广告片", "素材", "婚礼", "宝宝", "宠物", "美食", "风景",
    "航拍", "街拍", "模特", "摄影", "日剧", "韩剧", "美剧", "动画电影", "杂项", "收录",
]
TOPS = PROTECTED + INBOXES + OTHERS
assert len(TOPS) == 45

SERIES = ["Nako", "小千", "Yuki", "Mika", "夏日祭", "Cosplay_Rin", "旅行vlog", "某某合集",
          "Summer Beach", "Tokyo Walk", "舞蹈练习", "Aqua", "黑丝", "JK日常"]
SITES = ["[www.xxdm.com]", "【高清】", "[TG@channel]", "(nicovideo)", "", "", "", ""]
TAGS = ["1080p", "4K", "x265", "HEVC", "720p", "", "", "", ""]


@dataclass
class Fixture:
    nodes: list[FileNode] = field(default_factory=list)
    shared: list[str] = field(default_factory=list)
    """file_ids of the 20 shared files."""
    stats: dict[str, int] = field(default_factory=dict)


class _Builder:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.nodes: list[FileNode] = []
        self.ids = 0
        self.names: dict[str, set[str]] = {}

    def _id(self) -> str:
        self.ids += 1
        return f"f{self.ids:06d}"

    def _unique(self, parent: str, name: str) -> str:
        taken = self.names.setdefault(parent, set())
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        candidate, n = name, 1
        while candidate in taken:
            n += 1
            candidate = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
        taken.add(candidate)
        return candidate

    def folder(self, parent: FileNode | None, name: str) -> FileNode:
        parent_path = parent.path if parent else ""
        name = self._unique(parent_path or "/", name)
        node = FileNode(file_id=self._id(), parent_id=parent.file_id if parent else "",
                        name=name, kind=Kind.FOLDER, path=f"{parent_path}/{name}",
                        created_time="2025-06-01T00:00:00+00:00",
                        modified_time="2025-06-01T00:00:00+00:00")
        self.nodes.append(node)
        return node

    def file(self, parent: FileNode, name: str, size: int, *, hash: str = "",
             when: str = "2026-03-01T00:00:00+00:00") -> FileNode:
        name = self._unique(parent.path, name)
        node = FileNode(file_id=self._id(), parent_id=parent.file_id, name=name,
                        kind=Kind.FILE, path=f"{parent.path}/{name}", size=size,
                        hash=hash or f"h{self.ids:06d}", created_time=when, modified_time=when)
        self.nodes.append(node)
        return node

    def episode_name(self, ext: str) -> str:
        rng = self.rng
        style = rng.random()
        if style < 0.55:
            series = rng.choice(SERIES)
            ep = rng.choice([f"EP{rng.randint(1, 40):02d}", f"第{rng.randint(1, 40)}集",
                             f"_{rng.randint(1, 40):02d}", f" ({rng.randint(1, 9)})",
                             f"-part{rng.randint(1, 5)}"])
            return (f"{rng.choice(SITES)} {series} {ep} {rng.choice(TAGS)}".strip()
                    + f".{ext}").replace("  ", " ")
        if style < 0.70:
            return f"{rng.getrandbits(64):016x}.{ext}"
        if style < 0.80:
            return f"{rng.randint(1, 9999):04d}.{ext}"
        if style < 0.90:
            return f"{rng.choice(['IMG', 'VID', 'DSC'])}_{rng.randint(1000, 9999)}.{ext}"
        words = ["海边", "日落", "Party", "Night", "Studio", "Room", "现场", "彩排", "花絮"]
        return f"{rng.choice(words)}{rng.choice(words)} {rng.randint(1, 99)}.{ext}"


def build(seed: int = 20260925) -> Fixture:
    b = _Builder(seed)
    rng = b.rng
    tops = {name: b.folder(None, name) for name in TOPS}

    # ---- 751 loose files in 19 top-level folders, 467 in the largest
    loose_exts = ["mp4"] * 698 + ["mov"] * 28 + ["zip"] * 6 + ["7z"] * 5 + ["rar"] * 5 \
        + ["jpg"] * 2 + ["mkv"] * 7
    rng.shuffle(loose_exts)
    loose_homes = ["Pack From Shared", *rng.sample(OTHERS, 18)]
    counts = [467] + [0] * 18
    rest = 751 - 467
    for i in range(1, 19):
        counts[i] = 1 if i < 18 else rest - 17
    for _ in range(rest - 18):  # spread the rest, the last one keeps what is left
        counts[rng.randint(1, 17)] += 1
        counts[18] -= 1
    exts = iter(loose_exts)
    for home, count in zip(loose_homes, counts, strict=True):
        for _ in range(count):
            ext = next(exts)
            size = rng.randint(50, 900) * 1024**2 if ext != "jpg" else rng.randint(1, 5) * 1024**2
            b.file(tops[home], b.episode_name(ext), size)

    # ---- 435 second-level folders; 40 big ones (6.4 TiB together)
    homes = [n for n in TOPS if n != "Telegram"]
    seconds: list[FileNode] = []
    fixed = ["Pack From Shared"] * 14 + [name for name in PROTECTED for _ in range(2)]
    for index in range(435):
        if index < len(fixed):
            home = fixed[index]
        else:
            home = homes[index % len(homes)] if index < 200 else rng.choice(homes)
        name = rng.choice([f"{rng.choice(SERIES)} 合集", f"{rng.choice(SERIES)} Vol.{index}",
                           f"album_{index}", f"素材{index}", f"{rng.choice(SERIES)}"])
        seconds.append(b.folder(tops[home], name))
    big_dirs = [s for s in seconds if s.path.split("/")[1] in ("Pack From Shared",)][:10]
    big_dirs += [s for s in seconds if s.path.split("/")[1] in PROTECTED][:3]
    big_dirs += [s for s in seconds if s.path.split("/")[1] in OTHERS
                 and s not in big_dirs][:27]
    assert len(big_dirs) == 40
    big_total = int(6.4 * TiB)
    shares = [rng.uniform(0.6, 1.4) for _ in big_dirs]
    big_sizes = [int(big_total * s / sum(shares)) for s in shares]
    big_sizes[-1] += big_total - sum(big_sizes)

    # ---- 155 files over 4 GiB (1,070 GiB): 60 in big folders, the rest elsewhere
    huge_total = 1070 * GiB
    weights = [rng.uniform(0.7, 1.3) for _ in range(155)]
    huge_sizes = [max(int(huge_total * w / sum(weights)), 4 * GiB + 1) for w in weights]
    huge_sizes[-1] += huge_total - sum(huge_sizes)

    small_dirs = [s for s in seconds if s not in big_dirs]
    huge_iter = iter(huge_sizes)
    for folder, target in zip(big_dirs, big_sizes, strict=True):
        inner = b.folder(folder, rng.choice(["disc1", "正片", "raw", "合集"]))
        used = 0
        for _ in range(2):
            size = next(huge_iter)
            b.file(inner, b.episode_name("mkv"), size)
            used += size
        # Fill the rest of the folder's size with ordinary videos (< 4 GiB).
        remaining = target - used
        per = 3 * GiB
        n = max(-(-remaining // per), 1)  # the last one is the smallest, never over 3 GiB
        for i in range(n):
            size = per if i < n - 1 else remaining - per * (n - 1)
            b.file(folder, b.episode_name("mp4"), size)
    huge_rest = list(huge_iter)  # 75 left
    for size in huge_rest:
        host = rng.choice(small_dirs)
        b.file(host, b.episode_name("mkv"), size)

    # ---- slimming material: empty folders, single chains, junk
    for i in range(30):
        host = rng.choice(small_dirs)
        empty = b.folder(host, f"空目录{i}")
        if i % 3 == 0:
            b.folder(empty, "sub")
    for i in range(25):
        host = rng.choice(small_dirs)
        chain = b.folder(b.folder(host, f"链{i}"), "inner")
        for j in range(rng.randint(1, 4)):
            b.file(chain, f"clip{j}.mp4", 100 * 1024**2)
    for i in range(200):
        host = rng.choice(small_dirs)
        if i % 2:
            b.file(host, f"广告{i}.{rng.choice(['url', 'html', 'txt', 'lnk', 'apk'])}", 2048)
        else:
            b.file(host, f"{rng.choice(['最新地址', '防屏蔽', '更多资源'])}{i}.mp4", 300 * 1024)

    # ---- duplicates: 40 groups of 2 or 3, some with a protected copy
    for g in range(40):
        size = rng.randint(100, 900) * 1024**2
        where = rng.sample(small_dirs, 3 if g % 4 == 0 else 2)
        if g % 5 == 0:
            where[0] = next(s for s in seconds if s.path.split("/")[1] in PROTECTED)
        for host in where:
            b.file(host, f"dup{g}.mp4", size, hash=f"DUP{g:03d}")

    # ---- fill up to 80,964 entries with ordinary files (and a few folders)
    target_total = 80_964
    filler_dirs = [s for s in small_dirs]
    while len(b.nodes) < target_total:
        host = rng.choice(filler_dirs)
        if rng.random() < 0.03 and len(b.nodes) < target_total - 1:
            filler_dirs.append(b.folder(host, f"sub{len(b.nodes)}"))
            continue
        b.file(host, b.episode_name(rng.choice(["mp4", "mp4", "mp4", "jpg", "mov", "srt"])),
               rng.randint(1, 67) * 1024**2)

    fixture = Fixture(nodes=b.nodes)
    candidates = [n for n in b.nodes if not n.is_folder and n.path.split("/")[1] in OTHERS]
    fixture.shared = sorted(n.file_id for n in rng.sample(candidates, 20))
    fixture.stats = stats(b.nodes)
    return fixture


def stats(nodes: list[FileNode]) -> dict[str, int]:
    files = [n for n in nodes if not n.is_folder]
    size_by_second: dict[str, int] = {}
    for node in files:
        parts = node.path.split("/")
        if len(parts) >= 4:
            key = "/".join(parts[:3])
            size_by_second[key] = size_by_second.get(key, 0) + node.size
    seconds = [n for n in nodes if n.is_folder and n.path.count("/") == 2]
    big = [s for s in seconds if size_by_second.get(s.path, 0) >= 50 * GiB]
    huge = [n for n in files if n.size >= 4 * GiB]
    loose = [n for n in files if n.path.count("/") == 2]
    return {
        "entries": len(nodes),
        "tops": sum(1 for n in nodes if n.is_folder and n.path.count("/") == 1),
        "root_files": sum(1 for n in files if n.path.count("/") == 1),
        "loose": len(loose),
        "loose_tops": len({n.path.split("/")[1] for n in loose}),
        "loose_max": max(sum(1 for n in loose if n.path.split("/")[1] == t)
                         for t in {n.path.split("/")[1] for n in loose}),
        "loose_mp4": sum(1 for n in loose if n.name.endswith(".mp4")),
        "loose_mov": sum(1 for n in loose if n.name.endswith(".mov")),
        "loose_archives": sum(1 for n in loose
                              if n.name.rsplit(".", 1)[-1] in ("zip", "7z", "rar")),
        "loose_jpg": sum(1 for n in loose if n.name.endswith(".jpg")),
        "seconds": len(seconds),
        "big_seconds": len(big),
        "big_seconds_gib": sum(size_by_second[s.path] for s in big) // GiB,
        "huge_files": len(huge),
        "huge_files_gib": sum(n.size for n in huge) // GiB,
        "total_gib": sum(n.size for n in files) // GiB,
    }


async def load_into(store, nodes: list[FileNode]) -> None:
    """Write the nodes into the index in one transaction (a stocktake would
    take minutes against a fake drive of this size)."""
    stamp = "2026-09-25T00:00:00+00:00"
    rows = [(n.file_id, n.parent_id, n.path, n.name, str(n.kind), n.size, n.mime, n.hash,
             n.created_time, n.modified_time, stamp) for n in nodes]

    def work(conn):
        conn.executemany(
            "INSERT INTO files (file_id, parent_id, path, name, kind, size, mime, hash, "
            "created_time, modified_time, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    await store._write(work)  # noqa: SLF001 - a test fixture filling the index
