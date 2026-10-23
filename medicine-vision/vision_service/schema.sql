PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS vision_tasks (
    task_id TEXT PRIMARY KEY,
    question TEXT,
    expected_frames INTEGER NOT NULL CHECK(expected_frames BETWEEN 1 AND 4),
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    error_code TEXT,
    message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS vision_frames (
    task_id TEXT NOT NULL REFERENCES vision_tasks(task_id),
    frame_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    quality_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(task_id, frame_id),
    UNIQUE(task_id, sha256)
);
CREATE TABLE IF NOT EXISTS vision_results (
    task_id TEXT PRIMARY KEY REFERENCES vision_tasks(task_id),
    result_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vision_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES vision_tasks(task_id),
    event TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vision_tasks_status ON vision_tasks(status, created_at);
