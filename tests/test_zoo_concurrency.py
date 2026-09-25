"""并发审核:同一记录的并行修订与互斥拼合确认在 SQLite 串行化下恰有一方成功。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.service import ServiceError
from app.zoo_service import ZooService


def _setup_project(client, owner, project):
    return project["id"], owner["user"]["id"]


def _revision_payload(record, **changes):
    current = record["current"]
    payload = {k: current[k] for k in (
        "taxon_path", "element", "side", "age_stage", "portion", "burning", "cut_marks",
        "context_unit", "layer", "grid", "bag", "confidence", "fragment_count", "note",
    )}
    payload.update(changes)
    payload["base_version_no"] = current["version_no"]
    return payload


def test_concurrent_revisions_exactly_one_wins(client, owner, project, zoo):
    record = zoo.create_record(record_no="CC-1").json()
    project_id, actor_id = _setup_project(client, owner, project)
    payload = _revision_payload(record, note="并发修订")

    def attempt(marker):
        service = ZooService()  # 每线程独立的 SQLite 连接
        try:
            revised = service.revise_record(project_id, record["id"], {**payload, "note": marker}, actor_id)
            return ("ok", revised["current"]["version_no"])
        except ServiceError as exc:
            return ("err", exc.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["甲", "乙"]))

    outcomes = sorted(kind for kind, _ in results)
    assert outcomes == ["err", "ok"]
    assert ("err", "version_conflict") in results
    final = ZooService().get_record(project_id, record["id"])
    assert final["current"]["version_no"] == 2  # 只有一次修订生效
    history = ZooService().get_record_history(project_id, record["id"])
    assert [event["event_type"] for event in history["events"]] == ["create", "revise"]


def test_concurrent_confirms_on_mutually_exclusive_candidates(client, owner, project, zoo):
    zoo.create_record(record_no="X", bag="B1", portion=0.4)
    zoo.create_record(record_no="Y", bag="B2", portion=0.5)
    zoo.create_record(record_no="Z", bag="B3", portion=0.6)
    project_id, actor_id = _setup_project(client, owner, project)
    ZooService().detect_refits(project_id, {}, actor_id)
    candidates = ZooService().list_refits(project_id, actor_id)["data"]
    assert len(candidates) == 2

    def attempt(candidate_id):
        service = ZooService()
        try:
            return ("ok", service.decide_refit(project_id, candidate_id, "confirm", actor_id)["status"])
        except ServiceError as exc:
            return ("err", exc.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [candidates[0]["id"], candidates[1]["id"]]))

    outcomes = sorted(kind for kind, _ in results)
    assert outcomes == ["err", "ok"]
    assert ("err", "candidate_not_proposed") in results
    states = sorted(c["status"] for c in ZooService().list_refits(project_id, actor_id)["data"])
    assert states == ["blocked", "confirmed"]


def test_concurrent_create_same_record_no(client, owner, project, zoo):
    project_id, actor_id = _setup_project(client, owner, project)
    payload = {
        "record_no": "RACE-1", "taxon_path": "Mammalia", "element": "femur", "side": "left",
        "age_stage": "adult", "portion": 1.0, "burning": "none", "cut_marks": False,
        "context_unit": "H1", "layer": "L1", "grid": "", "bag": "",
        "confidence": 1.0, "fragment_count": 1, "note": "", "status": "auto",
    }

    def attempt(_):
        service = ZooService()
        try:
            return ("ok", service.create_record(project_id, payload, actor_id)["record_no"])
        except ServiceError as exc:
            return ("err", exc.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [1, 2]))
    outcomes = sorted(kind for kind, _ in results)
    assert outcomes == ["err", "ok"]
    assert ("err", "record_exists") in results
