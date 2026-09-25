"""量化报告:贡献明细、历史版本引用、规则版本化与可复现校验。"""
from __future__ import annotations


def _revise(client, headers, project_id, record, **changes):
    current = record["current"]
    payload = {k: current[k] for k in (
        "taxon_path", "element", "side", "age_stage", "portion", "burning", "cut_marks",
        "context_unit", "layer", "grid", "bag", "confidence", "fragment_count", "note",
    )}
    payload.update(changes)
    payload["base_version_no"] = current["version_no"]
    return client.post(
        f"/api/projects/{project_id}/zoo/records/{record['id']}/revisions",
        json=payload,
        headers=headers,
    )


def test_report_returns_nisp_mne_mni_with_contributions(client, owner, project, zoo):
    zoo.create_record(record_no="L1", side="left")
    zoo.create_record(record_no="L2", side="left")
    zoo.create_record(record_no="R1", side="right")
    zoo.create_record(record_no="S1", element="sacrum", side="axial", fragment_count=3)
    rule = zoo.create_rule(params={"unique_elements": ["sacrum"]}).json()
    report = zoo.create_report(rule["id"]).json()

    result = report["result"]
    assert result["totals"]["nisp"] == 6  # 3 条肱骨 + 骶骨记录的 3 块碎片
    assert result["totals"]["mne"] == 4
    assert result["totals"]["mni"] == 2  # 肱骨左右配对得 2,骶骨 1
    taxon = result["groups"][0]["taxa"][0]
    assert taxon["deciding_elements"] == ["humerus"]
    humerus = next(e for e in taxon["elements"] if e["element"] == "humerus")
    assert humerus["mni"] == 2
    assert humerus["sides"]["left"]["individual_bound"] == 2
    assert humerus["sides"]["right"]["units"][0]["members"] == ["R1"]


def test_report_grouping_scope(client, owner, project, zoo):
    zoo.create_record(record_no="A1", side="left", layer="L1")
    zoo.create_record(record_no="A2", side="right", layer="L2")
    rule = zoo.create_rule(params={}).json()
    grouped = zoo.create_report(rule["id"], grouping=["layer"]).json()
    assert grouped["result"]["totals"]["mni"] == 2  # 跨层不配对
    assert [g["key"] for g in grouped["result"]["groups"]] == [{"layer": "L1"}, {"layer": "L2"}]
    ungrouped = zoo.create_report(rule["id"]).json()
    assert ungrouped["result"]["totals"]["mni"] == 1


def test_historical_report_keeps_original_versions(client, owner, project, zoo):
    record = zoo.create_record(record_no="H1", side="left").json()
    zoo.create_record(record_no="H2", side="left")
    rule = zoo.create_rule(params={}).json()
    report = zoo.create_report(rule["id"]).json()
    assert report["result"]["totals"]["mni"] == 2  # 两件左侧肱骨

    # 修订:H2 实为右侧 —— 新报告应变为 MNI=1,旧报告重算仍须一致
    other = client.get(f"/api/projects/{project['id']}/zoo/records", headers=owner["headers"]).json()["data"]
    target = next(row for row in other if row["record_no"] == "H2")
    detail = client.get(f"/api/projects/{project['id']}/zoo/records/{target['id']}", headers=owner["headers"]).json()
    assert _revise(client, owner["headers"], project["id"], detail, side="right", reason="复核改侧").status_code == 201

    verify = client.post(
        f"/api/projects/{project['id']}/zoo/reports/{report['id']}/verify", headers=owner["headers"]
    ).json()
    assert verify["consistent"] is True
    assert verify["recomputed_result_hash"] == report["result_hash"]

    fresh = zoo.create_report(rule["id"]).json()
    assert fresh["result"]["totals"]["mni"] == 1
    assert fresh["id"] != report["id"]


def test_rule_versions_are_frozen_per_report(client, owner, project, zoo):
    zoo.create_record(record_no="C1", confidence=0.9)
    zoo.create_record(record_no="C2", side="right", confidence=0.3)
    strict = zoo.create_rule(params={"min_confidence": 0.5}).json()
    strict_report = zoo.create_report(strict["id"]).json()
    assert strict_report["result"]["totals"]["nisp"] == 1  # 低置信被排除

    relaxed = zoo.create_rule(params={"min_confidence": 0.2}).json()
    assert relaxed["version_no"] == 2
    relaxed_report = zoo.create_report(relaxed["id"]).json()
    assert relaxed_report["result"]["totals"]["nisp"] == 2

    # 旧报告仍引用规则版本 1,重算一致
    verify = client.post(
        f"/api/projects/{project['id']}/zoo/reports/{strict_report['id']}/verify", headers=owner["headers"]
    ).json()
    assert verify["consistent"] is True
    rules = client.get(f"/api/projects/{project['id']}/zoo/rules", headers=owner["headers"]).json()["data"]
    statuses = {row["version_no"]: row["status"] for row in rules}
    assert statuses == {1: "superseded", 2: "active"}


def test_report_filters_and_input_freeze(client, owner, project, zoo):
    zoo.create_record(record_no="F1", context_unit="H1")
    zoo.create_record(record_no="F2", context_unit="H2", side="right")
    rule = zoo.create_rule(params={}).json()
    report = zoo.create_report(rule["id"], filters={"context_unit": ["H1"]}).json()
    assert report["result"]["totals"]["nisp"] == 1
    assert len(report["inputs"]) == 1
    # 输入快照哈希可用于复现比对
    assert len(report["input_hash"]) == 64
    verify = client.post(
        f"/api/projects/{project['id']}/zoo/reports/{report['id']}/verify", headers=owner["headers"]
    ).json()
    assert verify["consistent"] is True


def test_invalid_rule_params_rejected(client, owner, project, zoo):
    bad = zoo.create_rule(params={"no_such_param": 1})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_rule"


def test_invalid_grouping_field_rejected(client, owner, project, zoo):
    zoo.create_record(record_no="G1")
    rule = zoo.create_rule(params={}).json()
    bad = zoo.create_report(rule["id"], grouping=["not_a_field"])
    assert bad.status_code == 400
