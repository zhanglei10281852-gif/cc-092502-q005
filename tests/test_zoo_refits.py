"""跨袋拼合候选:识别不合并、互斥确认、撤销与幂等检测。"""
from __future__ import annotations


def _detect(client, headers, project_id, rule_id=None):
    return client.post(
        f"/api/projects/{project_id}/zoo/refits/detect",
        json={"rule_id": rule_id},
        headers=headers,
    )


def _decide(client, headers, project_id, candidate_id, action):
    return client.post(
        f"/api/projects/{project_id}/zoo/refits/{candidate_id}/decisions",
        json={"action": action},
        headers=headers,
    )


def _list(client, headers, project_id):
    return client.get(f"/api/projects/{project_id}/zoo/refits", headers=headers).json()["data"]


def test_detect_proposes_cross_bag_candidates_without_merging(client, owner, project, zoo):
    zoo.create_record(record_no="BAG1", bag="B1", portion=0.4)
    zoo.create_record(record_no="BAG2", bag="B2", portion=0.5)
    zoo.create_record(record_no="BAG3", bag="B3", portion=0.9)  # 与谁都不互补
    outcome = _detect(client, owner["headers"], project["id"]).json()
    assert len(outcome["created"]) == 1
    assert outcome["skipped_existing"] == 0

    candidates = _list(client, owner["headers"], project["id"])
    assert candidates[0]["status"] == "proposed"
    assert {candidates[0]["record_no_a"], candidates[0]["record_no_b"]} == {"BAG1", "BAG2"}

    # 候选不自动合并:记录仍为两条已发布记录
    records = client.get(f"/api/projects/{project['id']}/zoo/records", headers=owner["headers"]).json()["data"]
    assert len(records) == 3
    rule = zoo.create_rule(params={}).json()
    report = zoo.create_report(rule["id"]).json()
    assert report["result"]["totals"]["nisp"] == 3


def test_detect_is_idempotent(client, owner, project, zoo):
    zoo.create_record(record_no="I1", bag="B1", portion=0.4)
    zoo.create_record(record_no="I2", bag="B2", portion=0.5)
    first = _detect(client, owner["headers"], project["id"]).json()
    second = _detect(client, owner["headers"], project["id"]).json()
    assert len(first["created"]) == 1
    assert second["created"] == [] and second["skipped_existing"] == 1


def test_same_bag_not_proposed(client, owner, project, zoo):
    zoo.create_record(record_no="SAME1", bag="B1", portion=0.4)
    zoo.create_record(record_no="SAME2", bag="B1", portion=0.5)
    outcome = _detect(client, owner["headers"], project["id"]).json()
    assert outcome["created"] == []


def test_mutual_exclusion_and_undo(client, owner, project, zoo):
    # A(bag1) 与 B(bag2)、C(bag3) 均互补 → 两个互斥候选;B+C 比例超 1 不构成候选
    zoo.create_record(record_no="A", bag="B1", portion=0.4)
    zoo.create_record(record_no="B", bag="B2", portion=0.5)
    zoo.create_record(record_no="C", bag="B3", portion=0.6)
    _detect(client, owner["headers"], project["id"])
    candidates = _list(client, owner["headers"], project["id"])
    assert len(candidates) == 2
    by_pair = {tuple(sorted((c["record_no_a"], c["record_no_b"]))): c for c in candidates}
    ab = by_pair[("A", "B")]
    ac = by_pair[("A", "C")]

    # 确认 AB 后,AC 被互斥阻塞,不能再确认
    confirmed = _decide(client, owner["headers"], project["id"], ab["id"], "confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    blocked = _decide(client, owner["headers"], project["id"], ac["id"], "confirm")
    assert blocked.status_code == 409
    states = {c["id"]: c["status"] for c in _list(client, owner["headers"], project["id"])}
    assert states[ac["id"]] == "blocked"

    # 撤销 AB 的确认后,AC 恢复为提议状态,可以确认
    undone = _decide(client, owner["headers"], project["id"], ab["id"], "undo")
    assert undone.status_code == 200
    assert undone.json()["status"] == "proposed"
    states = {c["id"]: c["status"] for c in _list(client, owner["headers"], project["id"])}
    assert states[ac["id"]] == "proposed"
    assert _decide(client, owner["headers"], project["id"], ac["id"], "confirm").status_code == 200


def test_reject_and_undo_reject(client, owner, project, zoo):
    zoo.create_record(record_no="RJ1", bag="B1", portion=0.4)
    zoo.create_record(record_no="RJ2", bag="B2", portion=0.5)
    _detect(client, owner["headers"], project["id"])
    candidate = _list(client, owner["headers"], project["id"])[0]

    rejected = _decide(client, owner["headers"], project["id"], candidate["id"], "reject")
    assert rejected.json()["status"] == "rejected"
    # 否决后可撤销,恢复为提议
    restored = _decide(client, owner["headers"], project["id"], candidate["id"], "undo")
    assert restored.json()["status"] == "proposed"
    # 再次否决后不可重复撤销同一决策链末端两次
    _decide(client, owner["headers"], project["id"], candidate["id"], "reject")
    assert _decide(client, owner["headers"], project["id"], candidate["id"], "undo").status_code == 200
    nothing = _decide(client, owner["headers"], project["id"], candidate["id"], "undo")
    assert nothing.status_code == 409
    assert nothing.json()["error"]["code"] == "nothing_to_undo"


def test_low_confidence_records_not_proposed(client, owner, project, zoo):
    zoo.create_record(record_no="LC1", bag="B1", portion=0.4, confidence=0.9)
    zoo.create_record(record_no="LC2", bag="B2", portion=0.5, confidence=0.3)
    rule = zoo.create_rule(params={"min_confidence": 0.5}).json()
    outcome = _detect(client, owner["headers"], project["id"], rule["id"]).json()
    assert outcome["created"] == []
