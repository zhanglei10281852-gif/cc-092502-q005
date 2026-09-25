"""动物遗存模块的 SQLite 表结构。

- ``zoo_records``：鉴定记录头版本；来源（unit/layer）不完整时状态为 ``staged``，
  不进入已发布统计。
- ``zoo_record_versions``：每次写入的完整快照，历史报告按版本引用。
- ``zoo_review_events``：审核事件（创建、修订、拼合确认/撤销），只追加。
- ``zoo_rulesets``：可版本化量化规则，版本内不可变。
- ``zoo_refit_links``：跨袋拼合候选及其确认/互斥/撤销状态。
- ``zoo_reports``：固定记录版本、规则快照与拼合状态的量化报告。
"""

ZOO_SCHEMA = """
CREATE TABLE IF NOT EXISTS zoo_records (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 record_key TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL CHECK(status IN ('staged','published')),
 taxon_path TEXT NOT NULL,
 taxon_rank TEXT NOT NULL DEFAULT '',
 element TEXT NOT NULL,
 side TEXT NOT NULL CHECK(side IN ('left','right','axial','unknown')),
 age_stage TEXT NOT NULL DEFAULT 'unknown',
 portion REAL NOT NULL,
 fragment_count INTEGER NOT NULL DEFAULT 1,
 burned INTEGER NOT NULL DEFAULT 0,
 cut_marks INTEGER NOT NULL DEFAULT 0,
 unit TEXT NOT NULL DEFAULT '',
 layer TEXT NOT NULL DEFAULT '',
 square TEXT NOT NULL DEFAULT '',
 bag TEXT NOT NULL DEFAULT '',
 confidence REAL NOT NULL DEFAULT 1,
 notes TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id, record_key)
);
CREATE TABLE IF NOT EXISTS zoo_record_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 record_id INTEGER NOT NULL REFERENCES zoo_records(id) ON DELETE CASCADE,
 version INTEGER NOT NULL,
 payload_json TEXT NOT NULL,
 event_id INTEGER,
 created_at TEXT NOT NULL,
 UNIQUE(record_id, version)
);
CREATE TABLE IF NOT EXISTS zoo_review_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
 record_id INTEGER REFERENCES zoo_records(id) ON DELETE CASCADE,
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 action TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '',
 payload_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS zoo_rulesets (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 version INTEGER NOT NULL,
 name TEXT NOT NULL,
 rules_json TEXT NOT NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS zoo_refit_links (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 record_a INTEGER NOT NULL REFERENCES zoo_records(id) ON DELETE CASCADE,
 record_b INTEGER NOT NULL REFERENCES zoo_records(id) ON DELETE CASCADE,
 status TEXT NOT NULL CHECK(status IN ('suggested','confirmed','excluded','revoked')),
 excluded_by INTEGER,
 decided_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id, record_a, record_b)
);
CREATE TABLE IF NOT EXISTS zoo_reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 name TEXT NOT NULL,
 group_by_json TEXT NOT NULL,
 ruleset_version INTEGER NOT NULL,
 rules_json TEXT NOT NULL,
 pinned_json TEXT NOT NULL,
 result_json TEXT NOT NULL,
 input_hash TEXT NOT NULL,
 result_hash TEXT NOT NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zoo_records_project ON zoo_records(project_id, status);
CREATE INDEX IF NOT EXISTS idx_zoo_events_record ON zoo_review_events(record_id, id);
CREATE INDEX IF NOT EXISTS idx_zoo_refit_project ON zoo_refit_links(project_id, status);
"""
