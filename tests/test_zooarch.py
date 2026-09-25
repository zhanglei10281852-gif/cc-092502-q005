"""动物遗存鉴定与量化服务的测试。

覆盖：分组边界、低置信记录与规则版本、暂存记录、MNI 配对/年龄/唯一部位、
修订历史与报告版本固定、拼合候选互斥与撤销、并发审核、SQLite 重启前后一致性、
命令行固定输入复现、权限与幂等。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.zooarch.mni import RECORD_FIELDS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TAXON = "Mammalia/Artiodactyla/Bovidae/Ovis aries"


@pytest.fixture()
def project(client, owner):
    resp = client.post("/api/projects", json={"code": "ZOO1", "name": "动物考古", "site_name": "遗址"},
                       headers=owner["headers"])
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture()
def ruleset(client, owner, project):
    resp = client.post(f"/api/zooarch/projects/{project['id']}/rulesets",
                       json={"name": "默认规则", "rules": {}}, headers=owner["headers"])
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_record(client, owner, project_id, key, **overrides):
    payload = {
        "record_key": key,
        "taxon_path": TAXON,
        "taxon_rank": "species",
        "element": "humerus",
        "side": "left",
        "age_stage": "adult",
        "portion": 1.0,
        "fragment_count": 1,
        "unit": "U1",
        "layer": "L1",
        "square": "",
        "bag": f"bag-{key}",
        "confidence": 1.0,
    }
    payload.update(overrides)
    resp = client.post(f"/api/zooarch/projects/{project_id}/records", json=payload, headers=owner["headers"])
    assert resp.status_code == 201, resp.text
    return resp.json()


def quantify(client, owner, project_id, group_by, ruleset_version=None):
    resp = client.post(f"/api/zooarch/projects/{project_id}/quantify",
                       json={"group_by": group_by, "ruleset_version": ruleset_version},
                       headers=owner["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def taxon_entry(result):
    return result["groups"][0]["taxa"][0]


# ---------- 分组边界 ----------

def test_grouping_boundary(client, owner, project, ruleset):
    pid = project["id"]
    add_record(client, owner, pid, "r1", side="left", layer="L1")
    add_record(client, owner, pid, "r2", side="right", layer="L2")

    by_layer = quantify(client, owner, pid, ["layer"])
    assert by_layer["totals"]["mni"] == 2
    groups = {g["key"]["layer"]: g for g in by_layer["groups"]}
    assert groups["L1"]["mni"] == 1 and groups["L1"]["nisp"] == 1
    assert groups["L2"]["mni"] == 1 and groups["L2"]["nisp"] == 1

    by_unit_layer = quantify(client, owner, pid, ["unit", "layer"])
    assert by_unit_layer["totals"]["mni"] == 2

    whole = quantify(client, owner, pid, [])
    assert whole["totals"]["mni"] == 1
    humerus = taxon_entry(whole)["elements"][0]
    assert humerus["classes"]["adult"]["pairs"] == 1
    assert humerus["classes"]["adult"]["units"] == {"left": 1, "right": 1, "other": 0}


def test_grouping_rejects_unknown_field(client, owner, project, ruleset):
    resp = client.post(f"/api/zooarch/projects/{project['id']}/quantify",
                       json={"group_by": ["trench"]}, headers=owner["headers"])
    assert resp.status_code == 422


# ---------- 暂存与低置信记录 ----------

def test_staged_record_excluded_until_provenance_complete(client, owner, project, ruleset):
    pid = project["id"]
    record = add_record(client, owner, pid, "s1", layer="")
    assert record["status"] == "staged"

    result = quantify(client, owner, pid, ["unit", "layer"])
    assert result["totals"]["nisp"] == 0
    assert result["totals"]["excluded"] == {"staged": 1, "low_confidence": 0}

    revised = client.post(f"/api/zooarch/projects/{pid}/records/{record['id']}/revisions",
                          json={"base_version": 1, "layer": "L1", "note": "补全层位"},
                          headers=owner["headers"])
    assert revised.status_code == 200, revised.text
    assert revised.json()["status"] == "published"
    assert revised.json()["version"] == 2

    result = quantify(client, owner, pid, ["unit", "layer"])
    assert result["totals"]["nisp"] == 1
    assert result["totals"]["mni"] == 1
    assert result["totals"]["excluded"] == {"staged": 0, "low_confidence": 0}

    events = client.get(f"/api/zooarch/projects/{pid}/records/{record['id']}/events",
                        headers=owner["headers"]).json()["data"]
    assert [e["action"] for e in events] == ["create", "revise"]
    assert events[1]["note"] == "补全层位"


def test_low_confidence_records_follow_ruleset_version(client, owner, project, ruleset):
    pid = project["id"]
    add_record(client, owner, pid, "c1", confidence=0.3)

    strict = quantify(client, owner, pid, ["layer"], ruleset_version=ruleset["version"])
    assert strict["totals"]["nisp"] == 0
    assert strict["totals"]["excluded"]["low_confidence"] == 1

    relaxed = client.post(f"/api/zooarch/projects/{pid}/rulesets",
                          json={"name": "放宽阈值", "rules": {"min_confidence": 0.2}},
                          headers=owner["headers"])
    assert relaxed.status_code == 201
    assert relaxed.json()["version"] == 2

    included = quantify(client, owner, pid, ["layer"], ruleset_version=2)
    assert included["totals"]["nisp"] == 1
    assert included["totals"]["mni"] == 1
    assert included["ruleset_version"] == 2

    report_strict = client.post(f"/api/zooarch/projects/{pid}/reports",
                                json={"name": "严格", "group_by": ["layer"], "ruleset_version": 1},
                                headers=owner["headers"]).json()
    report_relaxed = client.post(f"/api/zooarch/projects/{pid}/reports",
                                 json={"name": "放宽", "group_by": ["layer"], "ruleset_version": 2},
                                 headers=owner["headers"]).json()
    assert report_strict["result"]["totals"]["nisp"] == 0
    assert report_relaxed["result"]["totals"]["nisp"] == 1
    assert report_strict["ruleset_version"] == 1
    assert report_relaxed["ruleset_version"] == 2


# ---------- MNI/MNE/NISP 口径 ----------

def test_mni_pairing_age_incompatibility_and_unique_elements(client, owner, project, ruleset):
    pid = project["id"]
    add_record(client, owner, pid, "h1", side="left", age_stage="adult")
    add_record(client, owner, pid, "h2", side="left", age_stage="adult")
    add_record(client, owner, pid, "h3", side="right", age_stage="adult")
    add_record(client, owner, pid, "h4", side="left", age_stage="juvenile")
    for i in range(3):
        add_record(client, owner, pid, f"sk{i}", element="skull", side="axial", age_stage="adult")

    result = quantify(client, owner, pid, ["layer"])
    taxon = taxon_entry(result)
    assert taxon["nisp"] == 7
    assert taxon["mne"] == 7
    # 成年：肱骨左右配对后 2 个个体，头骨（唯一部位）3 个 → 取最大 3；幼年：1 → MNI 4
    assert taxon["mni"] == 4
    assert taxon["age_classes"] == {"adult": 3, "juvenile": 1}
    assert taxon["driving_elements"] == {"adult": ["skull"], "juvenile": ["humerus"]}

    elements = {e["element"]: e for e in taxon["elements"]}
    assert elements["humerus"]["classes"]["adult"] == {
        "units": {"left": 2, "right": 1, "other": 0}, "pairs": 1, "unpaired": 1, "mni": 2}
    assert elements["humerus"]["classes"]["juvenile"]["mni"] == 1
    assert elements["skull"]["unique"] is True
    assert elements["skull"]["mni"] == 3
    assert elements["skull"]["classes"]["adult"]["pairs"] == 0


def test_unknown_side_units_pair_conservatively(client, owner, project, ruleset):
    pid = project["id"]
    for i in range(4):
        add_record(client, owner, pid, f"u{i}", side="unknown")
    result = quantify(client, owner, pid, ["layer"])
    humerus = taxon_entry(result)["elements"][0]
    # 四个未知侧单元两两配对 → 2 个个体
    assert humerus["classes"]["adult"] == {
        "units": {"left": 0, "right": 0, "other": 4}, "pairs": 2, "unpaired": 0, "mni": 2}

    add_record(client, owner, pid, "u4", side="left")
    result = quantify(client, owner, pid, ["layer"])
    humerus = taxon_entry(result)["elements"][0]
    # 未知侧先与左侧配对（1 对），余下 3 个未知侧两两配对（1 对） → 3 个个体
    assert humerus["classes"]["adult"] == {
        "units": {"left": 1, "right": 0, "other": 4}, "pairs": 2, "unpaired": 1, "mni": 3}


def test_unmapped_age_stage_rejected(client, owner, project):
    resp = client.post(f"/api/zooarch/projects/{project['id']}/rulesets",
                       json={"name": "窄年龄", "rules": {"age_classes": {"adult": ["adult"]}}},
                       headers=owner["headers"])
    assert resp.status_code == 201
    add_record(client, owner, project["id"], "j1", age_stage="juvenile")
    resp = client.post(f"/api/zooarch/projects/{project['id']}/quantify",
                       json={"group_by": ["layer"]}, headers=owner["headers"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "quantify_error"


# ---------- 修订历史与报告版本固定 ----------

def test_revision_versions_and_report_pinning(client, owner, project, ruleset):
    pid = project["id"]
    r1 = add_record(client, owner, pid, "r1", side="left")
    add_record(client, owner, pid, "r2", side="left")

    report = client.post(f"/api/zooarch/projects/{pid}/reports",
                         json={"name": "发掘季报", "group_by": ["layer"], "ruleset_version": 1},
                         headers=owner["headers"])
    assert report.status_code == 201, report.text
    report = report.json()
    assert report["result"]["totals"]["mni"] == 2
    assert {p["version"] for p in report["pinned"]["records"]} == {1}

    revised = client.post(f"/api/zooarch/projects/{pid}/records/{r1['id']}/revisions",
                          json={"base_version": 1, "element": "femur", "note": "重新鉴定为股骨"},
                          headers=owner["headers"])
    assert revised.status_code == 200
    assert revised.json()["element"] == "femur"
    assert revised.json()["version"] == 2

    live = quantify(client, owner, pid, ["layer"])
    assert live["totals"]["mni"] == 1

    recompute = client.post(f"/api/zooarch/projects/{pid}/reports/{report['id']}/recompute",
                            headers=owner["headers"])
    assert recompute.status_code == 200
    outcome = recompute.json()
    assert outcome["matches"] is True
    assert outcome["result"]["totals"]["mni"] == 2
    assert outcome["result_hash"] == report["result_hash"]

    versions = client.get(f"/api/zooarch/projects/{pid}/records/{r1['id']}/versions",
                          headers=owner["headers"]).json()["data"]
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["record"]["element"] == "humerus"
    assert versions[1]["record"]["element"] == "femur"

    fresh = client.post(f"/api/zooarch/projects/{pid}/reports",
                        json={"name": "修订后", "group_by": ["layer"], "ruleset_version": 1},
                        headers=owner["headers"]).json()
    assert fresh["result"]["totals"]["mni"] == 1


def test_revision_version_conflict(client, owner, project, ruleset):
    pid = project["id"]
    record = add_record(client, owner, pid, "r1")
    first = client.post(f"/api/zooarch/projects/{pid}/records/{record['id']}/revisions",
                        json={"base_version": 1, "notes": "第一次"}, headers=owner["headers"])
    assert first.status_code == 200
    stale = client.post(f"/api/zooarch/projects/{pid}/records/{record['id']}/revisions",
                        json={"base_version": 1, "notes": "过期版本"}, headers=owner["headers"])
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"


# ---------- 拼合候选、互斥确认与撤销 ----------

def _links_by_pair(client, owner, pid):
    links = client.get(f"/api/zooarch/projects/{pid}/refits", headers=owner["headers"]).json()["data"]
    return {(link["record_a"], link["record_b"]): link for link in links}


def test_refit_candidates_mutual_exclusion_and_revoke(client, owner, project, ruleset):
    pid = project["id"]
    r1 = add_record(client, owner, pid, "f1", portion=0.4, bag="b1")
    r2 = add_record(client, owner, pid, "f2", portion=0.5, bag="b2")
    r3 = add_record(client, owner, pid, "f3", portion=0.3, bag="b3")

    scan = client.post(f"/api/zooarch/projects/{pid}/refits/scan", headers=owner["headers"])
    assert scan.status_code == 200, scan.text
    assert scan.json()["created"] == 3
    rescan = client.post(f"/api/zooarch/projects/{pid}/refits/scan", headers=owner["headers"]).json()
    assert rescan["created"] == 0 and rescan["candidates"] == 3

    pairs = _links_by_pair(client, owner, pid)
    link12 = pairs[(r1["id"], r2["id"])]
    link13 = pairs[(r1["id"], r3["id"])]
    link23 = pairs[(r2["id"], r3["id"])]
    assert {link["status"] for link in pairs.values()} == {"suggested"}

    before = quantify(client, owner, pid, ["layer"])
    assert before["totals"]["mne"] == 3
    assert before["totals"]["mni"] == 3

    confirmed = client.post(f"/api/zooarch/projects/{pid}/refits/{link12['id']}/confirm",
                            headers=owner["headers"])
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"

    pairs = _links_by_pair(client, owner, pid)
    assert pairs[(r1["id"], r3["id"])]["status"] == "excluded"
    assert pairs[(r2["id"], r3["id"])]["status"] == "excluded"
    assert pairs[(r1["id"], r3["id"])]["excluded_by"] == link12["id"]

    merged = quantify(client, owner, pid, ["layer"])
    assert merged["totals"]["mne"] == 2
    assert merged["totals"]["mni"] == 2
    assert merged["totals"]["nisp"] == 3

    blocked = client.post(f"/api/zooarch/projects/{pid}/refits/{link13['id']}/confirm",
                          headers=owner["headers"])
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "refit_excluded"

    revoked = client.post(f"/api/zooarch/projects/{pid}/refits/{link12['id']}/revoke",
                          headers=owner["headers"])
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    pairs = _links_by_pair(client, owner, pid)
    assert pairs[(r1["id"], r3["id"])]["status"] == "suggested"
    assert pairs[(r2["id"], r3["id"])]["status"] == "suggested"

    restored = quantify(client, owner, pid, ["layer"])
    assert restored["totals"]["mne"] == 3

    again = client.post(f"/api/zooarch/projects/{pid}/refits/{link23['id']}/confirm",
                        headers=owner["headers"])
    assert again.status_code == 200
    repeat = client.post(f"/api/zooarch/projects/{pid}/refits/{link23['id']}/confirm",
                         headers=owner["headers"])
    assert repeat.status_code == 200
    assert repeat.json()["status"] == "confirmed"


def test_refit_scan_rules(client, owner, project, ruleset):
    pid = project["id"]
    add_record(client, owner, pid, "a1", portion=0.4, bag="same")
    add_record(client, owner, pid, "a2", portion=0.4, bag="same")  # 同袋不候选
    add_record(client, owner, pid, "a3", portion=0.4, side="right", bag="b3")  # 左右不候选
    add_record(client, owner, pid, "a4", portion=0.8, bag="b4")  # 比例和 >1 不候选
    add_record(client, owner, pid, "a5", portion=0.4, age_stage="juvenile", bag="b5")  # 年龄不相容
    scan = client.post(f"/api/zooarch/projects/{pid}/refits/scan", headers=owner["headers"]).json()
    assert scan["candidates"] == 0

    add_record(client, owner, pid, "a6", portion=0.5, bag="b6")
    scan = client.post(f"/api/zooarch/projects/{pid}/refits/scan", headers=owner["headers"]).json()
    assert scan["candidates"] == 2  # a6 仅与 a1、a2 互补


def test_confirm_rejects_invalidated_pair(client, owner, project, ruleset):
    pid = project["id"]
    r1 = add_record(client, owner, pid, "x1", portion=0.4, bag="b1")
    r2 = add_record(client, owner, pid, "x2", portion=0.5, bag="b2")
    client.post(f"/api/zooarch/projects/{pid}/refits/scan", headers=owner["headers"])
    link = _links_by_pair(client, owner, pid)[(r1["id"], r2["id"])]
    client.post(f"/api/zooarch/projects/{pid}/records/{r2['id']}/revisions",
                json={"base_version": 1, "element": "femur"}, headers=owner["headers"])
    resp = client.post(f"/api/zooarch/projects/{pid}/refits/{link['id']}/confirm", headers=owner["headers"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "refit_invalid"


def test_report_pins_refit_state(client, owner, project, ruleset):
    pid = project["id"]
    r1 = add_record(client, owner, pid, "p1", portion=0.4, bag="b1")
    r2 = add_record(client, owner, pid, "p2", portion=0.5, bag="b2")
    client.post(f"/api/zooarch/projects/{pid}/refits/scan", headers=owner["headers"])
    link = _links_by_pair(client, owner, pid)[(r1["id"], r2["id"])]
    client.post(f"/api/zooarch/projects/{pid}/refits/{link['id']}/confirm", headers=owner["headers"])

    report = client.post(f"/api/zooarch/projects/{pid}/reports",
                         json={"name": "拼合后", "group_by": ["layer"], "ruleset_version": 1},
                         headers=owner["headers"]).json()
    assert report["result"]["totals"]["mne"] == 1
    assert report["pinned"]["refits"] == [[r1["id"], r2["id"]]]

    client.post(f"/api/zooarch/projects/{pid}/refits/{link['id']}/revoke", headers=owner["headers"])
    assert quantify(client, owner, pid, ["layer"])["totals"]["mne"] == 2

    outcome = client.post(f"/api/zooarch/projects/{pid}/reports/{report['id']}/recompute",
                          headers=owner["headers"]).json()
    assert outcome["matches"] is True
    assert outcome["result"]["totals"]["mne"] == 1


# ---------- 并发审核 ----------

def test_concurrent_revisions_single_winner(client, owner, project, ruleset):
    from app.database import close_connection
    from app.service import ServiceError
    from app.zooarch.service import ZooarchService

    pid = project["id"]
    record = add_record(client, owner, pid, "cc1")
    worker_count = 6
    barrier = threading.Barrier(worker_count)
    outcomes = []
    lock = threading.Lock()

    def worker(index):
        service = ZooarchService()
        try:
            barrier.wait(timeout=10)
            service.revise_record(record["id"], 1, {"notes": f"并发修订{index}"}, "并发", owner["user"]["id"])
            with lock:
                outcomes.append("ok")
        except ServiceError as exc:
            with lock:
                outcomes.append(exc.code)
        finally:
            close_connection()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert outcomes.count("ok") == 1
    assert outcomes.count("version_conflict") == worker_count - 1

    after = client.get(f"/api/zooarch/projects/{pid}/records/{record['id']}", headers=owner["headers"])
    assert after.json()["version"] == 2
    events = client.get(f"/api/zooarch/projects/{pid}/records/{record['id']}/events",
                        headers=owner["headers"]).json()["data"]
    assert [e["action"] for e in events] == ["create", "revise"]


# ---------- SQLite 重启前后一致性 ----------

def test_sqlite_restart_persistence(tmp_path):
    os.environ["ARCHAEOLOGY_DATABASE_PATH"] = str(tmp_path / "restart.db")
    from app.database import close_connection
    close_connection()
    from app.main import app

    with TestClient(app) as client:
        assert client.post("/api/users", json={"username": "keeper", "display_name": "管理员",
                                               "password": "KeeperPass!234"}).status_code == 201
        token = client.post("/api/sessions", json={"username": "keeper", "password": "KeeperPass!234"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        project = client.post("/api/projects", json={"code": "RST", "name": "重启测试", "site_name": "遗址"},
                              headers=headers).json()
        pid = project["id"]
        client.post(f"/api/zooarch/projects/{pid}/rulesets", json={"name": "默认", "rules": {}}, headers=headers)
        add_record(client, {"headers": headers}, pid, "k1", side="left")
        add_record(client, {"headers": headers}, pid, "k2", side="right")
        add_record(client, {"headers": headers}, pid, "k3", layer="")
        before = client.post(f"/api/zooarch/projects/{pid}/quantify",
                             json={"group_by": ["layer"]}, headers=headers).json()
        report = client.post(f"/api/zooarch/projects/{pid}/reports",
                             json={"name": "重启前", "group_by": ["layer"], "ruleset_version": 1},
                             headers=headers).json()
    close_connection()

    with TestClient(app) as client:
        token = client.post("/api/sessions", json={"username": "keeper", "password": "KeeperPass!234"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        after = client.post(f"/api/zooarch/projects/{pid}/quantify",
                            json={"group_by": ["layer"]}, headers=headers).json()
        assert after == before
        assert after["totals"]["mni"] == 1
        assert after["totals"]["excluded"]["staged"] == 1

        fetched = client.get(f"/api/zooarch/projects/{pid}/reports/{report['id']}", headers=headers).json()
        assert fetched["result_hash"] == report["result_hash"]
        outcome = client.post(f"/api/zooarch/projects/{pid}/reports/{report['id']}/recompute",
                              headers=headers).json()
        assert outcome["matches"] is True
    close_connection()


# ---------- 命令行固定输入复现 ----------

def test_cli_quantify_reproduces_http_result(client, owner, project, ruleset, tmp_path):
    pid = project["id"]
    add_record(client, owner, pid, "g1", side="left", layer="L1")
    add_record(client, owner, pid, "g2", side="right", layer="L1")
    add_record(client, owner, pid, "g3", side="left", age_stage="juvenile", layer="L2")

    http_result = quantify(client, owner, pid, ["layer"])
    records = client.get(f"/api/zooarch/projects/{pid}/records", headers=owner["headers"]).json()["data"]
    rules = client.get(f"/api/zooarch/projects/{pid}/rulesets/1", headers=owner["headers"]).json()["rules"]
    payload = {
        "records": [{key: record[key] for key in RECORD_FIELDS} for record in records],
        "ruleset": rules,
        "group_by": ["layer"],
        "refit_groups": [],
    }
    input_path = tmp_path / "quantify-input.json"
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "app.zooarch.cli", "quantify", "--input", str(input_path)],
        capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert proc.returncode == 0, proc.stderr
    cli_result = json.loads(proc.stdout)
    assert cli_result["result_hash"] == http_result["result_hash"]
    assert cli_result == {k: v for k, v in http_result.items() if k != "ruleset_version"}


# ---------- 权限与幂等 ----------

def test_record_key_idempotency(client, owner, project, ruleset):
    pid = project["id"]
    first = add_record(client, owner, pid, "dup")
    payload = {"record_key": "dup", "taxon_path": TAXON, "taxon_rank": "species", "element": "humerus",
               "side": "left", "age_stage": "adult", "portion": 1.0, "fragment_count": 1,
               "unit": "U1", "layer": "L1", "square": "", "bag": "bag-dup", "confidence": 1.0}
    second = client.post(f"/api/zooarch/projects/{pid}/records", json=payload, headers=owner["headers"])
    assert second.status_code == 201
    assert second.json()["id"] == first["id"]
    conflict = client.post(f"/api/zooarch/projects/{pid}/records",
                           json={**payload, "portion": 0.5}, headers=owner["headers"])
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "record_key_exists"


def test_cross_project_record_access_denied(client, owner, project, ruleset):
    pid = project["id"]
    record = add_record(client, owner, pid, "mine")
    other = client.post("/api/projects", json={"code": "ZOO2", "name": "另一项目", "site_name": "遗址"},
                        headers=owner["headers"]).json()
    assert client.get(f"/api/zooarch/projects/{other['id']}/records/{record['id']}",
                      headers=owner["headers"]).status_code == 404
    denied = client.post(f"/api/zooarch/projects/{other['id']}/records/{record['id']}/revisions",
                         json={"base_version": 1, "notes": "越权"}, headers=owner["headers"])
    assert denied.status_code == 404


def test_viewer_cannot_write(client, owner, project, ruleset):
    pid = project["id"]
    user = client.post("/api/users", json={"username": "viewer", "display_name": "观察员",
                                           "password": "ViewerPass!234"}).json()
    client.post(f"/api/projects/{pid}/members", json={"user_id": user["id"], "role": "viewer"},
                headers=owner["headers"])
    token = client.post("/api/sessions", json={"username": "viewer", "password": "ViewerPass!234"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get(f"/api/zooarch/projects/{pid}/records", headers=headers).status_code == 200
    denied = client.post(f"/api/zooarch/projects/{pid}/records",
                         json={"record_key": "v1", "taxon_path": TAXON, "element": "humerus",
                               "portion": 1.0, "unit": "U1", "layer": "L1"}, headers=headers)
    assert denied.status_code == 403

    outsider = client.post("/api/users", json={"username": "outsider", "display_name": "局外人",
                                               "password": "OutsiderPass!234"}).json()
    token = client.post("/api/sessions", json={"username": "outsider", "password": "OutsiderPass!234"}).json()["token"]
    forbidden = client.get(f"/api/zooarch/projects/{pid}/records",
                           headers={"Authorization": f"Bearer {token}"})
    assert forbidden.status_code == 403
