-- pikpak_wms 本地状态库（迁自 Asukamadoka/pikpak-wms，并入后只加不改）
-- 盘点索引、入库流水、操作审计，外加一张 meta 记盘点元数据。
-- 迁移规则同 bot（红线 3）：只加表、只加列，不删不改名。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 盘点产物：所有规则在这张表上求值（铁律 4）
CREATE TABLE IF NOT EXISTS files (
    file_id        TEXT PRIMARY KEY,
    parent_id      TEXT NOT NULL,
    path           TEXT NOT NULL,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('file', 'folder')),
    size           INTEGER NOT NULL DEFAULT 0,
    mime           TEXT NOT NULL DEFAULT '',
    hash           TEXT NOT NULL DEFAULT '',
    created_time   TEXT,
    modified_time  TEXT,
    synced_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_parent   ON files (parent_id);
CREATE INDEX IF NOT EXISTS idx_files_path     ON files (path);
CREATE INDEX IF NOT EXISTS idx_files_hash     ON files (hash) WHERE hash != '';
CREATE INDEX IF NOT EXISTS idx_files_synced   ON files (synced_at);
CREATE INDEX IF NOT EXISTS idx_files_modified ON files (modified_time);

-- 入库任务流水
CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,
    type         TEXT NOT NULL CHECK (type IN ('share_restore', 'offline')),
    source       TEXT NOT NULL,
    target_path  TEXT NOT NULL,
    phase        TEXT NOT NULL DEFAULT 'PENDING',
    file_id      TEXT,
    retries      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    finished_at  TEXT,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_phase  ON tasks (phase);
-- 幂等（铁律 5）：同一个来源不重复入库
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_source ON tasks (type, source);

-- 操作审计：before 快照让误操作可查、可回滚
CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    action     TEXT NOT NULL,
    file_id    TEXT NOT NULL,
    before     TEXT NOT NULL DEFAULT '{}',
    after      TEXT NOT NULL DEFAULT '{}',
    rule_name  TEXT NOT NULL DEFAULT '',
    dry_run    INTEGER NOT NULL DEFAULT 1,
    at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_at      ON audit (at);
CREATE INDEX IF NOT EXISTS idx_audit_file    ON audit (file_id);
CREATE INDEX IF NOT EXISTS idx_audit_dry_run ON audit (dry_run);

-- 盘点等运行时元数据：上次盘点时间、耗时之类
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
