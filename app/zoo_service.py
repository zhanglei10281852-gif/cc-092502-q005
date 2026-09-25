"""动物遗存鉴定、审核、量化报告与拼合决策的业务服务。

约定:
- 鉴定记录不可变版本化,修订通过审核事件追加,并发修订以乐观锁拒绝冲突;
- 来源字段(context_unit、layer)不完整的记录只能暂存(staged),
  只有已发布(published)记录进入量化统计;
- 报告生成时冻结记录版本快照(zoo_report_inputs)与输入/结果摘要,
  之后记录再修订也不影响历史报告的重算校验;
- 拼合候选只标记不合并,确认具有互斥性,撤销通过决策事件回退。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.database import connection, now, transaction
from app.security import stable_json
from app.service import ResearchService, ServiceError
from app.zoo_quant import (
    PROVENANCE_FIELDS,
    apply_filters,
    find_refit_pairs,
    input_hash,
    normalize_rules,
    quantize,
    result_hash,
)

READ_ROLES = {"owner", "researcher", "recorder", "reviewer", "viewer"}
RECORD_ROLES = {"owner", "researcher", "recorder"}
REVIEW_ROLES = {"owner", "researcher", "reviewer"}
RULE_ROLES = {"owner", "researcher"}

REQUIRED_PROVENANCE = ("context_unit", "layer")

VERSION_FIELDS = (
    "taxon_path", "element", "side", "age_stage", "portion", "burning", "cut_marks",
    "context_unit", "layer", "grid", "bag", "confidence", "fragment_count", "note",
)


class ZooService:
    def __init__(self, db: sqlite3.Connection | None = None):
        self.db = db or connection()
        self.base = ResearchService(self.db)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _get_record_row(self, project_id: int, record_id: int) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM zoo_records WHERE id=? AND project_id=?", (record_id, project_id)
        ).fetchone()
        if row is None:
            raise ServiceError("record_not_found", "鉴定记录不存在", 404)
        return row

    @staticmethod
    def _version_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version_id": row["id"],
            "version_no": row["version_no"],
            "taxon_path": row["taxon_path"],
            "element": row["element"],
            "side": row["side"],
            "age_stage": row["age_stage"],
            "portion": row["portion"],
            "burning": row["burning"],
            "cut_marks": bool(row["cut_marks"]),
            "context_unit": row["context_unit"],
            "layer": row["layer"],
            "grid": row["grid"],
            "bag": row["bag"],
            "confidence": row["confidence"],
            "fragment_count": row["fragment_count"],
            "note": row["note"],
            "event_id": row["event_id"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _snapshot_dict(row: sqlite3.Row) -> dict[str, Any]:
        """报告输入快照(与 verify 重建共用一个格式,保证摘要一致)。"""
        return {
            "record_id": row["record_id"],
            "version_id": row["id"],
            "record_no": row["record_no"],
            "taxon_path": row["taxon_path"],
            "element": row["element"],
            "side": row["side"],
            "age_stage": row["age_stage"],
            "portion": row["portion"],
            "burning": row["burning"],
            "cut_marks": bool(row["cut_marks"]),
            "context_unit": row["context_unit"],
            "layer": row["layer"],
            "grid": row["grid"],
            "bag": row["bag"],
            "confidence": row["confidence"],
            "fragment_count": row["fragment_count"],
            "note": row["note"],
        }

    @staticmethod
    def _clean_fields(payload: dict[str, Any]) -> dict[str, Any]:
        data = {field: payload.get(field) for field in VERSION_FIELDS}
        for field in ("taxon_path", "element", "context_unit", "layer", "grid", "bag", "age_stage", "burning", "note"):
            data[field] = str(data.get(field) or "").strip()
        data["element"] = data["element"].lower()
        data["cut_marks"] = 1 if data.get("cut_marks") else 0
        data["portion"] = float(data["portion"])
        data["confidence"] = float(data["confidence"])
        data["fragment_count"] = int(data["fragment_count"])
        return data

    @staticmethod
    def _provenance_missing(data: dict[str, Any]) -> list[str]:
        return [field for field in REQUIRED_PROVENANCE if not data.get(field)]

    def _insert_event(
        self,
        db: sqlite3.Connection,
        project_id: int,
        record_id: int,
        event_type: str,
        actor_id: int,
        base_version_id: int | None,
        reason: str,
    ) -> int:
        cursor = db.execute(
            "INSERT INTO zoo_review_events(project_id,record_id,event_type,actor_id,base_version_id,reason,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (project_id, record_id, event_type, actor_id, base_version_id, reason, now()),
        )
        return int(cursor.lastrowid)

    def _insert_version(
        self,
        db: sqlite3.Connection,
        record_id: int,
        version_no: int,
        data: dict[str, Any],
        event_id: int,
    ) -> int:
        columns = ",".join(VERSION_FIELDS)
        placeholders = ",".join("?" for _ in VERSION_FIELDS)
        cursor = db.execute(
            f"INSERT INTO zoo_record_versions(record_id,version_no,{columns},event_id,created_at)"
            f" VALUES(?,?,{placeholders},?,?)",
            (record_id, version_no, *(data[field] for field in VERSION_FIELDS), event_id, now()),
        )
        return int(cursor.lastrowid)

    # ------------------------------------------------------------------
    # 鉴定记录
    # ------------------------------------------------------------------
    def create_record(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, RECORD_ROLES)
        data = self._clean_fields(payload)
        record_no = str(payload.get("record_no") or "").strip()
        if not record_no:
            raise ServiceError("record_no_required", "记录编号不能为空", 400)
        missing = self._provenance_missing(data)
        requested = payload.get("status") or "auto"
        if requested == "published" and missing:
            raise ServiceError(
                "provenance_incomplete",
                f"来源字段不完整({','.join(missing)}),只能暂存,不得发布",
                400,
            )
        status = "published" if requested == "published" or (requested == "auto" and not missing) else "staged"
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO zoo_records(project_id,record_no,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (project_id, record_no, status, stamp, stamp),
                )
                record_id = int(cursor.lastrowid)
                event_id = self._insert_event(db, project_id, record_id, "create", actor_id, None, str(payload.get("reason") or ""))
                version_id = self._insert_version(db, record_id, 1, data, event_id)
                db.execute("UPDATE zoo_records SET current_version_id=? WHERE id=?", (version_id, record_id))
                db.execute("UPDATE zoo_review_events SET new_version_id=? WHERE id=?", (version_id, event_id))
                self.base.audit(
                    "zoo.record.create", "zoo_record", str(record_id),
                    {"record_no": record_no, "status": status},
                    project_id=project_id, actor_id=actor_id,
                )
        except sqlite3.IntegrityError as exc:
            raise ServiceError("record_exists", "记录编号在项目内已存在", 409) from exc
        return self.get_record(project_id, record_id)

    def revise_record(self, project_id: int, record_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        """以审核事件追加鉴定修订;base_version_no 过期时拒绝(乐观锁)。"""
        self.base.require_role(project_id, actor_id, REVIEW_ROLES)
        data = self._clean_fields(payload)
        base_version_no = payload.get("base_version_no")
        reason = str(payload.get("reason") or "")
        with transaction(immediate=True) as db:
            record = db.execute(
                "SELECT * FROM zoo_records WHERE id=? AND project_id=?", (record_id, project_id)
            ).fetchone()
            if record is None:
                raise ServiceError("record_not_found", "鉴定记录不存在", 404)
            current = db.execute(
                "SELECT * FROM zoo_record_versions WHERE id=?", (record["current_version_id"],)
            ).fetchone()
            if base_version_no != current["version_no"]:
                raise ServiceError(
                    "version_conflict",
                    f"记录已被他人修订,当前版本为 {current['version_no']},请基于最新版本重新提交",
                    409,
                )
            event_id = self._insert_event(db, project_id, record_id, "revise", actor_id, current["id"], reason)
            version_id = self._insert_version(db, record_id, current["version_no"] + 1, data, event_id)
            updated = db.execute(
                "UPDATE zoo_records SET current_version_id=?, updated_at=? WHERE id=? AND current_version_id=?",
                (version_id, now(), record_id, current["id"]),
            )
            if updated.rowcount != 1:
                raise ServiceError("version_conflict", "记录已被他人修订,请重新提交", 409)
            db.execute("UPDATE zoo_review_events SET new_version_id=? WHERE id=?", (version_id, event_id))
            self.base.audit(
                "zoo.record.revise", "zoo_record", str(record_id),
                {"base_version_no": base_version_no, "new_version_no": current["version_no"] + 1, "reason": reason},
                project_id=project_id, actor_id=actor_id,
            )
        return self.get_record(project_id, record_id)

    def set_record_status(self, project_id: int, record_id: int, action: str, actor_id: int, reason: str = "") -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, REVIEW_ROLES)
        with transaction(immediate=True) as db:
            record = db.execute(
                "SELECT * FROM zoo_records WHERE id=? AND project_id=?", (record_id, project_id)
            ).fetchone()
            if record is None:
                raise ServiceError("record_not_found", "鉴定记录不存在", 404)
            if action == "publish":
                version = db.execute(
                    "SELECT * FROM zoo_record_versions WHERE id=?", (record["current_version_id"],)
                ).fetchone()
                missing = [field for field in REQUIRED_PROVENANCE if not version[field]]
                if missing:
                    raise ServiceError(
                        "provenance_incomplete",
                        f"来源字段不完整({','.join(missing)}),不得发布",
                        400,
                    )
                if record["status"] == "published":
                    raise ServiceError("already_published", "记录已处于发布状态", 409)
                new_status = "published"
            else:
                if record["status"] != "published":
                    raise ServiceError("not_published", "记录未处于发布状态", 409)
                new_status = "staged"
            db.execute("UPDATE zoo_records SET status=?, updated_at=? WHERE id=?", (new_status, now(), record_id))
            self._insert_event(db, project_id, record_id, action, actor_id, record["current_version_id"], reason)
            self.base.audit(
                f"zoo.record.{action}", "zoo_record", str(record_id),
                {"reason": reason}, project_id=project_id, actor_id=actor_id,
            )
        return self.get_record(project_id, record_id)

    def get_record(self, project_id: int, record_id: int) -> dict[str, Any]:
        record = self._get_record_row(project_id, record_id)
        current = self.db.execute(
            "SELECT * FROM zoo_record_versions WHERE id=?", (record["current_version_id"],)
        ).fetchone()
        return {
            "id": record["id"],
            "project_id": record["project_id"],
            "record_no": record["record_no"],
            "status": record["status"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "current": self._version_dict(current),
        }

    def get_record_history(self, project_id: int, record_id: int) -> dict[str, Any]:
        record = self.get_record(project_id, record_id)
        versions = self.db.execute(
            "SELECT * FROM zoo_record_versions WHERE record_id=? ORDER BY version_no", (record_id,)
        ).fetchall()
        events = self.db.execute(
            "SELECT * FROM zoo_review_events WHERE record_id=? ORDER BY id", (record_id,)
        ).fetchall()
        record["versions"] = [self._version_dict(row) for row in versions]
        record["events"] = [dict(row) for row in events]
        return record

    def list_records(self, project_id: int, actor_id: int, status: str | None = None) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        sql = (
            "SELECT r.*, v.id AS v_id, v.version_no, v.taxon_path, v.element, v.side, v.age_stage,"
            " v.confidence, v.fragment_count, v.context_unit, v.layer, v.grid, v.bag"
            " FROM zoo_records r JOIN zoo_record_versions v ON v.id=r.current_version_id"
            " WHERE r.project_id=?"
        )
        params: list[Any] = [project_id]
        if status:
            sql += " AND r.status=?"
            params.append(status)
        sql += " ORDER BY r.record_no"
        rows = self.db.execute(sql, params).fetchall()
        return {
            "data": [
                {
                    "id": row["id"],
                    "record_no": row["record_no"],
                    "status": row["status"],
                    "version_no": row["version_no"],
                    "taxon_path": row["taxon_path"],
                    "element": row["element"],
                    "side": row["side"],
                    "age_stage": row["age_stage"],
                    "confidence": row["confidence"],
                    "fragment_count": row["fragment_count"],
                    "context_unit": row["context_unit"],
                    "layer": row["layer"],
                    "grid": row["grid"],
                    "bag": row["bag"],
                }
                for row in rows
            ]
        }

    # ------------------------------------------------------------------
    # 量化规则(可版本化)
    # ------------------------------------------------------------------
    def create_rule(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, RULE_ROLES)
        rule_code = str(payload.get("rule_code") or "").strip()
        if not rule_code:
            raise ServiceError("rule_code_required", "规则编码不能为空", 400)
        try:
            params = normalize_rules(payload.get("params") or {})
        except ValueError as exc:
            raise ServiceError("invalid_rule", str(exc), 400) from exc
        with transaction(immediate=True) as db:
            previous = db.execute(
                "SELECT MAX(version_no) AS max_version FROM zoo_rules WHERE project_id=? AND rule_code=?",
                (project_id, rule_code),
            ).fetchone()["max_version"] or 0
            db.execute(
                "UPDATE zoo_rules SET status='superseded' WHERE project_id=? AND rule_code=? AND status='active'",
                (project_id, rule_code),
            )
            cursor = db.execute(
                "INSERT INTO zoo_rules(project_id,rule_code,version_no,params_json,status,created_by,created_at)"
                " VALUES(?,?,?,?,'active',?,?)",
                (project_id, rule_code, previous + 1, stable_json(params), actor_id, now()),
            )
            rule_id = int(cursor.lastrowid)
            self.base.audit(
                "zoo.rule.create", "zoo_rule", str(rule_id),
                {"rule_code": rule_code, "version_no": previous + 1, "params": params},
                project_id=project_id, actor_id=actor_id,
            )
        return self.get_rule(project_id, rule_id)

    def get_rule(self, project_id: int, rule_id: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM zoo_rules WHERE id=? AND project_id=?", (rule_id, project_id)
        ).fetchone()
        if row is None:
            raise ServiceError("rule_not_found", "量化规则不存在", 404)
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "rule_code": row["rule_code"],
            "version_no": row["version_no"],
            "params": json.loads(row["params_json"]),
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def list_rules(self, project_id: int, actor_id: int) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        rows = self.db.execute(
            "SELECT * FROM zoo_rules WHERE project_id=? ORDER BY rule_code, version_no", (project_id,)
        ).fetchall()
        return {
            "data": [
                {
                    "id": row["id"],
                    "rule_code": row["rule_code"],
                    "version_no": row["version_no"],
                    "status": row["status"],
                    "params": json.loads(row["params_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }

    # ------------------------------------------------------------------
    # 量化报告(快照 + 可复现校验)
    # ------------------------------------------------------------------
    def _published_snapshot(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT v.*, r.record_no FROM zoo_records r"
            " JOIN zoo_record_versions v ON v.id=r.current_version_id"
            " WHERE r.project_id=? AND r.status='published' ORDER BY r.record_no",
            (project_id,),
        ).fetchall()
        return [self._snapshot_dict(row) for row in rows]

    def create_report(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, REVIEW_ROLES)
        rule = self.get_rule(project_id, int(payload["rule_id"]))
        grouping = [str(field) for field in (payload.get("grouping") or [])]
        filters = payload.get("filters") or {}
        snapshot = self._published_snapshot(project_id)
        try:
            records = apply_filters(snapshot, filters)
            result = quantize(records, grouping, rule["params"])
        except ValueError as exc:
            raise ServiceError("invalid_report_request", str(exc), 400) from exc
        digest_input = input_hash(records, grouping, rule["params"], filters)
        digest_result = result_hash(result)
        with transaction(immediate=True) as db:
            cursor = db.execute(
                "INSERT INTO zoo_reports(project_id,rule_id,grouping_json,filters_json,input_hash,result_hash,result_json,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    project_id, rule["id"], stable_json(grouping), stable_json(filters),
                    digest_input, digest_result, stable_json(result), actor_id, now(),
                ),
            )
            report_id = int(cursor.lastrowid)
            for rec in records:
                db.execute(
                    "INSERT INTO zoo_report_inputs(report_id,record_id,version_id) VALUES(?,?,?)",
                    (report_id, rec["record_id"], rec["version_id"]),
                )
            self.base.audit(
                "zoo.report.create", "zoo_report", str(report_id),
                {"rule_id": rule["id"], "grouping": grouping, "records": len(records), "result_hash": digest_result},
                project_id=project_id, actor_id=actor_id,
            )
        return self.get_report(project_id, report_id)

    def get_report(self, project_id: int, report_id: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM zoo_reports WHERE id=? AND project_id=?", (report_id, project_id)
        ).fetchone()
        if row is None:
            raise ServiceError("report_not_found", "量化报告不存在", 404)
        inputs = self.db.execute(
            "SELECT record_id, version_id FROM zoo_report_inputs WHERE report_id=? ORDER BY record_id",
            (report_id,),
        ).fetchall()
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "rule_id": row["rule_id"],
            "grouping": json.loads(row["grouping_json"]),
            "filters": json.loads(row["filters_json"]),
            "input_hash": row["input_hash"],
            "result_hash": row["result_hash"],
            "result": json.loads(row["result_json"]),
            "inputs": [dict(item) for item in inputs],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def list_reports(self, project_id: int, actor_id: int) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        rows = self.db.execute(
            "SELECT id,rule_id,grouping_json,filters_json,input_hash,result_hash,created_by,created_at"
            " FROM zoo_reports WHERE project_id=? ORDER BY id",
            (project_id,),
        ).fetchall()
        return {
            "data": [
                {
                    "id": row["id"],
                    "rule_id": row["rule_id"],
                    "grouping": json.loads(row["grouping_json"]),
                    "filters": json.loads(row["filters_json"]),
                    "input_hash": row["input_hash"],
                    "result_hash": row["result_hash"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }

    def verify_report(self, project_id: int, report_id: int) -> dict[str, Any]:
        """按报告冻结的记录版本重算,校验历史报告仍可复现。"""
        report = self.get_report(project_id, report_id)
        rule = self.get_rule(project_id, report["rule_id"])
        rows = self.db.execute(
            "SELECT v.*, r.record_no FROM zoo_report_inputs ri"
            " JOIN zoo_record_versions v ON v.id=ri.version_id"
            " JOIN zoo_records r ON r.id=ri.record_id"
            " WHERE ri.report_id=? ORDER BY r.record_no",
            (report_id,),
        ).fetchall()
        records = [self._snapshot_dict(row) for row in rows]
        recomputed_input = input_hash(records, report["grouping"], rule["params"], report["filters"])
        recomputed_result = result_hash(quantize(records, report["grouping"], rule["params"]))
        return {
            "report_id": report_id,
            "record_count": len(records),
            "consistent": recomputed_input == report["input_hash"] and recomputed_result == report["result_hash"],
            "stored_input_hash": report["input_hash"],
            "recomputed_input_hash": recomputed_input,
            "stored_result_hash": report["result_hash"],
            "recomputed_result_hash": recomputed_result,
        }

    # ------------------------------------------------------------------
    # 拼合候选(只标记不合并,支持互斥确认与撤销)
    # ------------------------------------------------------------------
    def detect_refits(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, RULE_ROLES)
        rule_id = payload.get("rule_id")
        params = self.get_rule(project_id, int(rule_id))["params"] if rule_id else None
        snapshot = self._published_snapshot(project_id)
        pairs = find_refit_pairs(snapshot, params)
        created: list[dict[str, Any]] = []
        skipped = 0
        with transaction(immediate=True) as db:
            for pair in pairs:
                record_a = int(pair["record_a"]["record_id"])
                record_b = int(pair["record_b"]["record_id"])
                try:
                    cursor = db.execute(
                        "INSERT INTO zoo_refit_candidates(project_id,record_a,record_b,reason,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (project_id, record_a, record_b, pair["reason"], now(), now()),
                    )
                    created.append({"id": int(cursor.lastrowid), "record_a": record_a, "record_b": record_b})
                except sqlite3.IntegrityError:
                    skipped += 1
            if created:
                self.base.audit(
                    "zoo.refit.detect", "zoo_refit", str(project_id),
                    {"created": len(created), "skipped_existing": skipped},
                    project_id=project_id, actor_id=actor_id,
                )
        return {"created": created, "skipped_existing": skipped}

    def list_refits(self, project_id: int, actor_id: int, status: str | None = None) -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, READ_ROLES)
        sql = (
            "SELECT c.*, ra.record_no AS record_no_a, rb.record_no AS record_no_b"
            " FROM zoo_refit_candidates c"
            " JOIN zoo_records ra ON ra.id=c.record_a"
            " JOIN zoo_records rb ON rb.id=c.record_b"
            " WHERE c.project_id=?"
        )
        params: list[Any] = [project_id]
        if status:
            sql += " AND c.status=?"
            params.append(status)
        sql += " ORDER BY c.id"
        rows = self.db.execute(sql, params).fetchall()
        return {"data": [dict(row) for row in rows]}

    def _refresh_candidate_status(self, db: sqlite3.Connection, candidate_id: int) -> None:
        """撤销后重算候选状态:若与已确认候选共享记录则阻塞,否则恢复提议。"""
        row = db.execute("SELECT * FROM zoo_refit_candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None or row["status"] not in ("proposed", "blocked"):
            return
        conflict = db.execute(
            "SELECT id FROM zoo_refit_candidates WHERE status='confirmed' AND id<>?"
            " AND (record_a IN (?,?) OR record_b IN (?,?)) LIMIT 1",
            (candidate_id, row["record_a"], row["record_b"], row["record_a"], row["record_b"]),
        ).fetchone()
        if conflict:
            db.execute(
                "UPDATE zoo_refit_candidates SET status='blocked', blocked_by=?, updated_at=? WHERE id=?",
                (conflict["id"], now(), candidate_id),
            )
        else:
            db.execute(
                "UPDATE zoo_refit_candidates SET status='proposed', blocked_by=NULL, updated_at=? WHERE id=?",
                (now(), candidate_id),
            )

    def decide_refit(self, project_id: int, candidate_id: int, action: str, actor_id: int, reason: str = "") -> dict[str, Any]:
        self.base.require_role(project_id, actor_id, REVIEW_ROLES)
        with transaction(immediate=True) as db:
            candidate = db.execute(
                "SELECT * FROM zoo_refit_candidates WHERE id=? AND project_id=?", (candidate_id, project_id)
            ).fetchone()
            if candidate is None:
                raise ServiceError("candidate_not_found", "拼合候选不存在", 404)
            if action == "confirm":
                if candidate["status"] != "proposed":
                    raise ServiceError(
                        "candidate_not_proposed",
                        f"候选当前状态为 {candidate['status']},不能确认(可能存在互斥的已确认候选)",
                        409,
                    )
                db.execute(
                    "UPDATE zoo_refit_candidates SET status='confirmed', updated_at=? WHERE id=? AND status='proposed'",
                    (now(), candidate_id),
                )
                db.execute(
                    "INSERT INTO zoo_refit_decisions(candidate_id,action,actor_id,reason,created_at) VALUES(?,?,?,?,?)",
                    (candidate_id, "confirm", actor_id, reason, now()),
                )
                others = db.execute(
                    "SELECT id FROM zoo_refit_candidates WHERE project_id=? AND status='proposed' AND id<>?"
                    " AND (record_a IN (?,?) OR record_b IN (?,?))",
                    (project_id, candidate_id, candidate["record_a"], candidate["record_b"],
                     candidate["record_a"], candidate["record_b"]),
                ).fetchall()
                for other in others:
                    db.execute(
                        "UPDATE zoo_refit_candidates SET status='blocked', blocked_by=?, updated_at=? WHERE id=?",
                        (candidate_id, now(), other["id"]),
                    )
            elif action == "reject":
                if candidate["status"] not in ("proposed", "blocked"):
                    raise ServiceError("candidate_not_rejectable", f"候选当前状态为 {candidate['status']},不能否决", 409)
                db.execute(
                    "UPDATE zoo_refit_candidates SET status='rejected', updated_at=? WHERE id=?",
                    (now(), candidate_id),
                )
                db.execute(
                    "INSERT INTO zoo_refit_decisions(candidate_id,action,actor_id,reason,created_at) VALUES(?,?,?,?,?)",
                    (candidate_id, "reject", actor_id, reason, now()),
                )
            else:  # undo
                last = db.execute(
                    "SELECT * FROM zoo_refit_decisions WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
                    (candidate_id,),
                ).fetchone()
                if last is None or last["action"] == "undo":
                    raise ServiceError("nothing_to_undo", "候选没有可撤销的决策", 409)
                already = db.execute(
                    "SELECT 1 FROM zoo_refit_decisions WHERE undoes_id=?", (last["id"],)
                ).fetchone()
                if already:
                    raise ServiceError("nothing_to_undo", "该决策已被撤销", 409)
                db.execute(
                    "INSERT INTO zoo_refit_decisions(candidate_id,action,actor_id,undoes_id,reason,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (candidate_id, "undo", actor_id, last["id"], reason, now()),
                )
                db.execute(
                    "UPDATE zoo_refit_candidates SET status='proposed', blocked_by=NULL, updated_at=? WHERE id=?",
                    (now(), candidate_id),
                )
                if last["action"] == "confirm":
                    blocked = db.execute(
                        "SELECT id FROM zoo_refit_candidates WHERE blocked_by=? AND status='blocked'",
                        (candidate_id,),
                    ).fetchall()
                    for other in blocked:
                        self._refresh_candidate_status(db, other["id"])
                self._refresh_candidate_status(db, candidate_id)
            self.base.audit(
                f"zoo.refit.{action}", "zoo_refit", str(candidate_id),
                {"reason": reason}, project_id=project_id, actor_id=actor_id,
            )
        return self._get_candidate(project_id, candidate_id)

    def _get_candidate(self, project_id: int, candidate_id: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT c.*, ra.record_no AS record_no_a, rb.record_no AS record_no_b"
            " FROM zoo_refit_candidates c"
            " JOIN zoo_records ra ON ra.id=c.record_a"
            " JOIN zoo_records rb ON rb.id=c.record_b"
            " WHERE c.id=? AND c.project_id=?",
            (candidate_id, project_id),
        ).fetchone()
        return dict(row)
