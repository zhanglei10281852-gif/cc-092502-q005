"""动物遗存模块的业务服务：记录、修订、规则集、量化、报告与拼合。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.database import connection, now, transaction
from app.security import stable_json
from app.service import ServiceError
from app.zooarch import mni

DATA_FIELDS = (
    "taxon_path", "taxon_rank", "element", "side", "age_stage", "portion",
    "fragment_count", "burned", "cut_marks", "unit", "layer", "square", "bag",
    "confidence", "notes",
)

REFIT_PORTION_TOLERANCE = 1e-9


def _bool_int(value: Any) -> int:
    return 1 if value else 0


class ZooarchService:
    def __init__(self, db: sqlite3.Connection | None = None):
        self.db = db or connection()

    # ---------- 记录 ----------

    @staticmethod
    def status_of(payload: dict[str, Any]) -> str:
        complete = str(payload.get("unit", "")).strip() and str(payload.get("layer", "")).strip()
        return "published" if complete else "staged"

    @staticmethod
    def _canonical_payload(payload: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for field in DATA_FIELDS:
            value = payload.get(field)
            if field in ("burned", "cut_marks"):
                value = bool(value)
            elif field in ("portion", "confidence"):
                value = float(value)
            elif field == "fragment_count":
                value = int(value)
            elif isinstance(value, str):
                value = value.strip()
            out[field] = value
        if out["element"]:
            out["element"] = out["element"].lower()
        if out["side"]:
            out["side"] = out["side"].lower()
        return out

    @staticmethod
    def _record_dict(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["burned"] = bool(record["burned"])
        record["cut_marks"] = bool(record["cut_marks"])
        return record

    @staticmethod
    def _engine_view(record: dict[str, Any]) -> dict[str, Any]:
        return {key: record[key] for key in mni.RECORD_FIELDS}

    def _get_record_row(self, record_id: int, project_id: int | None = None) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM zoo_records WHERE id=?", (record_id,)).fetchone()
        if row is None or (project_id is not None and row["project_id"] != project_id):
            raise ServiceError("record_not_found", "鉴定记录不存在", 404)
        return row

    def get_record(self, record_id: int, project_id: int | None = None) -> dict[str, Any]:
        return self._record_dict(self._get_record_row(record_id, project_id))

    def list_records(self, project_id: int, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM zoo_records WHERE project_id=?"
        params: list[Any] = [project_id]
        for field in ("status", "element", "side", "unit", "layer"):
            value = (filters or {}).get(field)
            if value:
                sql += f" AND {field}=?"
                params.append(value)
        taxon = (filters or {}).get("taxon_path")
        if taxon:
            sql += " AND taxon_path=?"
            params.append(taxon)
        sql += " ORDER BY id"
        return [self._record_dict(row) for row in self.db.execute(sql, params).fetchall()]

    def create_record(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        canonical = self._canonical_payload(payload)
        record_key = str(payload["record_key"]).strip()
        existing = self.db.execute(
            "SELECT * FROM zoo_records WHERE project_id=? AND record_key=?", (project_id, record_key)).fetchone()
        if existing is not None:
            if self._canonical_payload(dict(existing)) == canonical:
                return self._record_dict(existing)
            raise ServiceError("record_key_exists", "同一项目内 record_key 已存在且内容不同", 409)
        stamp = now()
        status = self.status_of(canonical)
        with transaction(immediate=True) as db:
            cursor = db.execute(
                "INSERT INTO zoo_records(project_id,record_key,version,status,taxon_path,taxon_rank,element,side,"
                "age_stage,portion,fragment_count,burned,cut_marks,unit,layer,square,bag,confidence,notes,"
                "created_by,created_at,updated_at) VALUES(?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, record_key, status, canonical["taxon_path"], canonical["taxon_rank"],
                 canonical["element"], canonical["side"], canonical["age_stage"], canonical["portion"],
                 canonical["fragment_count"], _bool_int(canonical["burned"]), _bool_int(canonical["cut_marks"]),
                 canonical["unit"], canonical["layer"], canonical["square"], canonical["bag"],
                 canonical["confidence"], canonical["notes"], actor_id, stamp, stamp))
            record_id = cursor.lastrowid
            record = self._record_dict(db.execute("SELECT * FROM zoo_records WHERE id=?", (record_id,)).fetchone())
            event_id = self._event(db, project_id, record_id, actor_id, "create", "", {"record": record})
            db.execute(
                "INSERT INTO zoo_record_versions(record_id,version,payload_json,event_id,created_at) VALUES(?,?,?,?,?)",
                (record_id, 1, stable_json(record), event_id, stamp))
            return record

    def revise_record(self, record_id: int, base_version: int, changes: dict[str, Any],
                      note: str, actor_id: int, project_id: int | None = None) -> dict[str, Any]:
        unknown = set(changes) - set(DATA_FIELDS)
        if unknown:
            raise ServiceError("invalid_field", f"不允许修订的字段: {sorted(unknown)}", 400)
        if not changes:
            raise ServiceError("empty_revision", "修订内容不能为空", 400)
        with transaction(immediate=True) as db:
            row = db.execute("SELECT * FROM zoo_records WHERE id=?", (record_id,)).fetchone()
            if row is None or (project_id is not None and row["project_id"] != project_id):
                raise ServiceError("record_not_found", "鉴定记录不存在", 404)
            if row["version"] != base_version:
                raise ServiceError(
                    "version_conflict",
                    f"记录当前版本为 {row['version']}，与 base_version={base_version} 冲突", 409)
            merged = dict(row)
            merged.update(changes)
            canonical = self._canonical_payload(merged)
            version = row["version"] + 1
            stamp = now()
            db.execute(
                "UPDATE zoo_records SET version=?,status=?,taxon_path=?,taxon_rank=?,element=?,side=?,age_stage=?,"
                "portion=?,fragment_count=?,burned=?,cut_marks=?,unit=?,layer=?,square=?,bag=?,confidence=?,"
                "notes=?,updated_at=? WHERE id=?",
                (version, self.status_of(canonical), canonical["taxon_path"], canonical["taxon_rank"],
                 canonical["element"], canonical["side"], canonical["age_stage"], canonical["portion"],
                 canonical["fragment_count"], _bool_int(canonical["burned"]), _bool_int(canonical["cut_marks"]),
                 canonical["unit"], canonical["layer"], canonical["square"], canonical["bag"],
                 canonical["confidence"], canonical["notes"], stamp, record_id))
            record = self._record_dict(db.execute("SELECT * FROM zoo_records WHERE id=?", (record_id,)).fetchone())
            event_id = self._event(db, record["project_id"], record_id, actor_id, "revise", note,
                                   {"from_version": base_version, "to_version": version, "changes": changes})
            db.execute(
                "INSERT INTO zoo_record_versions(record_id,version,payload_json,event_id,created_at) VALUES(?,?,?,?,?)",
                (record_id, version, stable_json(record), event_id, stamp))
            return record

    def record_events(self, record_id: int, project_id: int | None = None) -> list[dict[str, Any]]:
        self._get_record_row(record_id, project_id)
        rows = self.db.execute(
            "SELECT * FROM zoo_review_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._event_dict(row) for row in rows]

    def record_versions(self, record_id: int, project_id: int | None = None) -> list[dict[str, Any]]:
        self._get_record_row(record_id, project_id)
        rows = self.db.execute(
            "SELECT * FROM zoo_record_versions WHERE record_id=? ORDER BY version", (record_id,)).fetchall()
        return [{"record_id": row["record_id"], "version": row["version"], "event_id": row["event_id"],
                 "created_at": row["created_at"], "record": json.loads(row["payload_json"])} for row in rows]

    # ---------- 规则集 ----------

    def create_ruleset(self, project_id: int, name: str, rules: dict[str, Any], actor_id: int) -> dict[str, Any]:
        try:
            normalized = mni.normalize_ruleset(rules)
        except mni.QuantifyError as exc:
            raise ServiceError("invalid_ruleset", str(exc), 400) from exc
        with transaction(immediate=True) as db:
            row = db.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS v FROM zoo_rulesets WHERE project_id=?",
                (project_id,)).fetchone()
            version = row["v"]
            db.execute(
                "INSERT INTO zoo_rulesets(project_id,version,name,rules_json,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (project_id, version, name, stable_json(normalized), actor_id, now()))
        return {"project_id": project_id, "version": version, "name": name, "rules": normalized}

    def get_ruleset(self, project_id: int, version: int | None = None) -> dict[str, Any]:
        if version is None:
            row = self.db.execute(
                "SELECT * FROM zoo_rulesets WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (project_id,)).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM zoo_rulesets WHERE project_id=? AND version=?", (project_id, version)).fetchone()
        if row is None:
            raise ServiceError("ruleset_not_found", "量化规则集不存在，请先创建规则集", 404)
        return {"project_id": project_id, "version": row["version"], "name": row["name"],
                "rules": json.loads(row["rules_json"]), "created_at": row["created_at"]}

    def list_rulesets(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM zoo_rulesets WHERE project_id=? ORDER BY version", (project_id,)).fetchall()
        return [{"project_id": project_id, "version": row["version"], "name": row["name"],
                 "rules": json.loads(row["rules_json"]), "created_at": row["created_at"]} for row in rows]

    # ---------- 量化 ----------

    def _project_records(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM zoo_records WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        return [self._record_dict(row) for row in rows]

    def _confirmed_refit_pairs(self, project_id: int) -> list[list[int]]:
        rows = self.db.execute(
            "SELECT record_a,record_b FROM zoo_refit_links WHERE project_id=? AND status='confirmed' "
            "ORDER BY record_a,record_b", (project_id,)).fetchall()
        return [[row["record_a"], row["record_b"]] for row in rows]

    def quantify(self, project_id: int, group_by: list[str], ruleset_version: int | None) -> dict[str, Any]:
        ruleset = self.get_ruleset(project_id, ruleset_version)
        records = [self._engine_view(r) for r in self._project_records(project_id)]
        return self._quantify_with(ruleset, group_by, records, self._confirmed_refit_pairs(project_id))

    @staticmethod
    def _quantify_with(ruleset: dict[str, Any], group_by: list[str],
                       records: list[dict[str, Any]], refit_pairs: list[list[int]]) -> dict[str, Any]:
        try:
            result = mni.quantify(records, ruleset["rules"], group_by, refit_pairs)
        except mni.QuantifyError as exc:
            raise ServiceError("quantify_error", str(exc), 400) from exc
        return {"ruleset_version": ruleset["version"], **result}

    # ---------- 报告 ----------

    def create_report(self, project_id: int, name: str, group_by: list[str],
                      ruleset_version: int | None, actor_id: int) -> dict[str, Any]:
        ruleset = self.get_ruleset(project_id, ruleset_version)
        records = self._project_records(project_id)
        refit_pairs = self._confirmed_refit_pairs(project_id)
        result = self._quantify_with(ruleset, group_by, [self._engine_view(r) for r in records], refit_pairs)
        pinned = {
            "records": [{"record_id": r["id"], "version": r["version"]} for r in records],
            "refits": refit_pairs,
        }
        with transaction(immediate=True) as db:
            cursor = db.execute(
                "INSERT INTO zoo_reports(project_id,name,group_by_json,ruleset_version,rules_json,pinned_json,"
                "result_json,input_hash,result_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, name, stable_json(group_by), ruleset["version"], stable_json(ruleset["rules"]),
                 stable_json(pinned), stable_json(result), result["input_hash"], result["result_hash"],
                 actor_id, now()))
            report_id = cursor.lastrowid
        return self.get_report(report_id)

    def _report_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "project_id": row["project_id"], "name": row["name"],
            "group_by": json.loads(row["group_by_json"]), "ruleset_version": row["ruleset_version"],
            "pinned": json.loads(row["pinned_json"]), "result": json.loads(row["result_json"]),
            "input_hash": row["input_hash"], "result_hash": row["result_hash"],
            "created_at": row["created_at"],
        }

    def _get_report_row(self, report_id: int, project_id: int | None = None) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM zoo_reports WHERE id=?", (report_id,)).fetchone()
        if row is None or (project_id is not None and row["project_id"] != project_id):
            raise ServiceError("report_not_found", "量化报告不存在", 404)
        return row

    def get_report(self, report_id: int, project_id: int | None = None) -> dict[str, Any]:
        return self._report_dict(self._get_report_row(report_id, project_id))

    def list_reports(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM zoo_reports WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        return [self._report_dict(row) for row in rows]

    def recompute_report(self, report_id: int, project_id: int | None = None) -> dict[str, Any]:
        row = self._get_report_row(report_id, project_id)
        report = self._report_dict(row)
        pinned = report["pinned"]
        records = []
        for item in pinned["records"]:
            version_row = self.db.execute(
                "SELECT payload_json FROM zoo_record_versions WHERE record_id=? AND version=?",
                (item["record_id"], item["version"])).fetchone()
            if version_row is None:
                raise ServiceError(
                    "version_missing",
                    f"记录 {item['record_id']} 的版本 {item['version']} 快照缺失，无法复现", 500)
            records.append(self._engine_view(json.loads(version_row["payload_json"])))
        ruleset = {"version": report["ruleset_version"], "rules": json.loads(row["rules_json"])}
        result = self._quantify_with(ruleset, report["group_by"], records, pinned["refits"])
        return {
            "report_id": report_id,
            "matches": result["result_hash"] == report["result_hash"] and result == report["result"],
            "stored_result_hash": report["result_hash"],
            "result_hash": result["result_hash"],
            "result": result,
        }

    # ---------- 拼合 ----------

    def scan_refits(self, project_id: int, actor_id: int) -> dict[str, Any]:
        ruleset = self.get_ruleset(project_id, None)
        rules = ruleset["rules"]
        stage_class = mni.stage_class_map(rules)
        records = [r for r in self._project_records(project_id) if r["status"] == "published"]
        for record in records:
            if record["age_stage"] not in stage_class:
                raise ServiceError(
                    "quantify_error",
                    f"记录 {record['id']} 的年龄阶段 {record['age_stage']!r} 未在规则集 age_classes 中映射", 400)
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for record in records:
            key = (record["taxon_path"], record["element"])
            if rules["refit_same_context"]:
                key += (record["unit"], record["layer"])
            groups.setdefault(key, []).append(record)
        candidates: list[tuple[int, int]] = []
        for members in groups.values():
            members = sorted(members, key=lambda r: r["id"])
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    if self._refit_compatible(members[i], members[j], stage_class):
                        candidates.append((members[i]["id"], members[j]["id"]))
        created = 0
        stamp = now()
        with transaction(immediate=True) as db:
            for a, b in candidates:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO zoo_refit_links(project_id,record_a,record_b,status,created_at,updated_at)"
                    " VALUES(?,?,?,'suggested',?,?)", (project_id, a, b, stamp, stamp))
                created += cursor.rowcount
            self._event(db, project_id, None, actor_id, "refit.scan", "",
                        {"created": created, "candidates": len(candidates)})
        return {"created": created, "candidates": len(candidates), "links": self.list_refits(project_id)}

    @staticmethod
    def _refit_compatible(a: dict[str, Any], b: dict[str, Any], stage_class: dict[str, str]) -> bool:
        if a["element"] != b["element"] or a["taxon_path"] != b["taxon_path"]:
            return False
        if not a["bag"] or not b["bag"] or a["bag"] == b["bag"]:
            return False
        if {a["side"], b["side"]} == {"left", "right"}:
            return False
        if a["portion"] + b["portion"] > 1.0 + REFIT_PORTION_TOLERANCE:
            return False
        if a["age_stage"] != "unknown" and b["age_stage"] != "unknown":
            if stage_class[a["age_stage"]] != stage_class[b["age_stage"]]:
                return False
        return True

    def list_refits(self, project_id: int, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM zoo_refit_links WHERE project_id=?"
        params: list[Any] = [project_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY record_a,record_b"
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def _get_link_row(self, db: sqlite3.Connection, link_id: int, project_id: int | None) -> sqlite3.Row:
        link = db.execute("SELECT * FROM zoo_refit_links WHERE id=?", (link_id,)).fetchone()
        if link is None or (project_id is not None and link["project_id"] != project_id):
            raise ServiceError("refit_not_found", "拼合候选不存在", 404)
        return link

    def confirm_refit(self, link_id: int, actor_id: int, project_id: int | None = None) -> dict[str, Any]:
        with transaction(immediate=True) as db:
            link = self._get_link_row(db, link_id, project_id)
            if link["status"] == "confirmed":
                return dict(link)
            if link["status"] == "excluded":
                raise ServiceError("refit_excluded", "该候选已被互斥排除，请先撤销冲突的确认", 409)
            a = self._record_dict(db.execute("SELECT * FROM zoo_records WHERE id=?", (link["record_a"],)).fetchone())
            b = self._record_dict(db.execute("SELECT * FROM zoo_records WHERE id=?", (link["record_b"],)).fetchone())
            ruleset = self.get_ruleset(link["project_id"], None)
            if not self._refit_compatible(a, b, mni.stage_class_map(ruleset["rules"])):
                raise ServiceError("refit_invalid", "记录当前状态不满足拼合条件（部位/侧别/比例/年龄）", 409)
            stamp = now()
            db.execute(
                "UPDATE zoo_refit_links SET status='confirmed',decided_by=?,updated_at=? WHERE id=?",
                (actor_id, stamp, link_id))
            db.execute(
                "UPDATE zoo_refit_links SET status='excluded',excluded_by=?,updated_at=? "
                "WHERE project_id=? AND status='suggested' AND id<>? AND (record_a IN (?,?) OR record_b IN (?,?))",
                (link_id, stamp, link["project_id"], link_id,
                 link["record_a"], link["record_b"], link["record_a"], link["record_b"]))
            self._event(db, link["project_id"], None, actor_id, "refit.confirm", "",
                        {"link_id": link_id, "records": [link["record_a"], link["record_b"]]})
            return dict(db.execute("SELECT * FROM zoo_refit_links WHERE id=?", (link_id,)).fetchone())

    def revoke_refit(self, link_id: int, actor_id: int, project_id: int | None = None) -> dict[str, Any]:
        with transaction(immediate=True) as db:
            link = self._get_link_row(db, link_id, project_id)
            if link["status"] != "confirmed":
                raise ServiceError("refit_not_confirmed", "只有已确认的拼合可以撤销", 409)
            stamp = now()
            db.execute(
                "UPDATE zoo_refit_links SET status='revoked',decided_by=?,updated_at=? WHERE id=?",
                (actor_id, stamp, link_id))
            db.execute(
                "UPDATE zoo_refit_links SET status='suggested',excluded_by=NULL,updated_at=? WHERE excluded_by=?",
                (stamp, link_id))
            self._event(db, link["project_id"], None, actor_id, "refit.revoke", "",
                        {"link_id": link_id, "records": [link["record_a"], link["record_b"]]})
            return dict(db.execute("SELECT * FROM zoo_refit_links WHERE id=?", (link_id,)).fetchone())

    # ---------- 事件 ----------

    @staticmethod
    def _event(db: sqlite3.Connection, project_id: int, record_id: int | None, actor_id: int | None,
               action: str, note: str, payload: dict[str, Any]) -> int:
        cursor = db.execute(
            "INSERT INTO zoo_review_events(project_id,record_id,actor_id,action,note,payload_json,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (project_id, record_id, actor_id, action, note, stable_json(payload), now()))
        return cursor.lastrowid

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        event = dict(row)
        event["payload"] = json.loads(event.pop("payload_json"))
        return event
