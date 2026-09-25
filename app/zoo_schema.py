"""动物遗存鉴定与量化模块的数据库结构。

设计要点:
- zoo_record_versions 不可变追加,鉴定修订只产生新版本,历史报告可引用当时版本;
- zoo_review_events 记录每一次创建/修订/发布/撤回审核事件;
- zoo_rules 按 (rule_code, version_no) 版本化,参数快照随版本冻结;
- zoo_reports + zoo_report_inputs 冻结报告生成时的记录版本集合与结果摘要;
- zoo_refit_candidates / zoo_refit_decisions 支持拼合候选的互斥确认与撤销。
"""
from __future__ import annotations

from app.database import connection

ZOO_SCHEMA = """
CREATE TABLE IF NOT EXISTS zoo_records (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 record_no TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'staged' CHECK(status IN ('staged','published')),
 current_version_id INTEGER,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id, record_no)
);
CREATE TABLE IF NOT EXISTS zoo_record_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 record_id INTEGER NOT NULL REFERENCES zoo_records(id) ON DELETE CASCADE,
 version_no INTEGER NOT NULL,
 taxon_path TEXT NOT NULL,
 element TEXT NOT NULL,
 side TEXT NOT NULL CHECK(side IN ('left','right','axial','unknown')),
 age_stage TEXT NOT NULL DEFAULT 'unknown',
 portion REAL NOT NULL CHECK(portion > 0 AND portion <= 1),
 burning TEXT NOT NULL DEFAULT 'none',
 cut_marks INTEGER NOT NULL DEFAULT 0,
 context_unit TEXT NOT NULL DEFAULT '',
 layer TEXT NOT NULL DEFAULT '',
 grid TEXT NOT NULL DEFAULT '',
 bag TEXT NOT NULL DEFAULT '',
 confidence REAL NOT NULL DEFAULT 1 CHECK(confidence >= 0 AND confidence <= 1),
 fragment_count INTEGER NOT NULL DEFAULT 1 CHECK(fragment_count >= 1),
 note TEXT NOT NULL DEFAULT '',
 event_id INTEGER,
 created_at TEXT NOT NULL,
 UNIQUE(record_id, version_no)
);
CREATE TABLE IF NOT EXISTS zoo_review_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 record_id INTEGER NOT NULL REFERENCES zoo_records(id) ON DELETE CASCADE,
 event_type TEXT NOT NULL CHECK(event_type IN ('create','revise','publish','unpublish')),
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 base_version_id INTEGER,
 new_version_id INTEGER,
 reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS zoo_rules (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 rule_code TEXT NOT NULL,
 version_no INTEGER NOT NULL,
 params_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(project_id, rule_code, version_no)
);
CREATE TABLE IF NOT EXISTS zoo_reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 rule_id INTEGER NOT NULL REFERENCES zoo_rules(id),
 grouping_json TEXT NOT NULL,
 filters_json TEXT NOT NULL DEFAULT '{}',
 input_hash TEXT NOT NULL,
 result_hash TEXT NOT NULL,
 result_json TEXT NOT NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS zoo_report_inputs (
 report_id INTEGER NOT NULL REFERENCES zoo_reports(id) ON DELETE CASCADE,
 record_id INTEGER NOT NULL,
 version_id INTEGER NOT NULL,
 PRIMARY KEY(report_id, record_id)
);
CREATE TABLE IF NOT EXISTS zoo_refit_candidates (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 record_a INTEGER NOT NULL REFERENCES zoo_records(id) ON DELETE CASCADE,
 record_b INTEGER NOT NULL REFERENCES zoo_records(id) ON DELETE CASCADE,
 reason TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','confirmed','rejected','blocked')),
 blocked_by INTEGER REFERENCES zoo_refit_candidates(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id, record_a, record_b)
);
CREATE TABLE IF NOT EXISTS zoo_refit_decisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 candidate_id INTEGER NOT NULL REFERENCES zoo_refit_candidates(id) ON DELETE CASCADE,
 action TEXT NOT NULL CHECK(action IN ('confirm','reject','undo')),
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 undoes_id INTEGER,
 reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zoo_versions_record ON zoo_record_versions(record_id, version_no);
CREATE INDEX IF NOT EXISTS idx_zoo_events_record ON zoo_review_events(record_id, id);
CREATE INDEX IF NOT EXISTS idx_zoo_records_project ON zoo_records(project_id, status);
CREATE INDEX IF NOT EXISTS idx_zoo_refit_project ON zoo_refit_candidates(project_id, status);
"""


def init_zoo_db() -> None:
    connection().executescript(ZOO_SCHEMA)
