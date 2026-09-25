"""鉴定记录的创建、暂存/发布门禁、版本化修订与审核事件测试。"""
from __future__ import annotations


def test_complete_record_auto_publishes(client, owner, project, zoo):
    response = zoo.create_record(record_no="R-1")
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "published"
    assert body["current"]["version_no"] == 1
    assert body["current"]["taxon_path"].endswith("Ovis aries")


def test_incomplete_provenance_is_staged(client, owner, project, zoo):
    response = zoo.create_record(record_no="R-2", layer="")
    assert response.status_code == 201
    assert response.json()["status"] == "staged"


def test_incomplete_provenance_cannot_publish(client, owner, project, zoo):
    forced = zoo.create_record(record_no="R-3", layer="", status="published")
    assert forced.status_code == 400
    assert forced.json()["error"]["code"] == "provenance_incomplete"
    staged = zoo.create_record(record_no="R-4", context_unit="").json()
    blocked = zoo.publish(staged["id"])
    assert blocked.status_code == 400
    # 补全来源并发布后进入已发布统计
    current = staged["current"]
    revised = client.post(
        f"/api/projects/{project['id']}/zoo/records/{staged['id']}/revisions",
        json={**{k: current[k] for k in (
            "taxon_path", "element", "side", "age_stage", "portion", "burning", "cut_marks",
            "layer", "grid", "bag", "confidence", "fragment_count", "note",
        )}, "context_unit": "H9", "base_version_no": 1, "reason": "补录遗迹单位"},
        headers=owner["headers"],
    )
    assert revised.status_code == 201
    assert zoo.publish(staged["id"]).status_code == 200


def test_staged_record_excluded_from_report(client, owner, project, zoo):
    published = zoo.create_record(record_no="P-1").json()
    staged = zoo.create_record(record_no="S-1", layer="", side="right").json()
    assert published["status"] == "published" and staged["status"] == "staged"
    rule = zoo.create_rule(params={}).json()
    report = zoo.create_report(rule["id"]).json()
    assert report["result"]["totals"]["nisp"] == 1
    assert report["result"]["totals"]["included_records"] == 1


def test_revision_appends_review_events(client, owner, project, zoo):
    record = zoo.create_record(record_no="R-10").json()
    current = record["current"]
    payload = {k: current[k] for k in (
        "taxon_path", "element", "side", "age_stage", "portion", "burning", "cut_marks",
        "context_unit", "layer", "grid", "bag", "confidence", "fragment_count", "note",
    )}
    payload.update(side="right", base_version_no=1, reason="复核改为右侧")
    revised = client.post(
        f"/api/projects/{project['id']}/zoo/records/{record['id']}/revisions",
        json=payload,
        headers=owner["headers"],
    )
    assert revised.status_code == 201
    assert revised.json()["current"]["version_no"] == 2
    assert revised.json()["current"]["side"] == "right"

    history = client.get(
        f"/api/projects/{project['id']}/zoo/records/{record['id']}?history=true",
        headers=owner["headers"],
    ).json()
    assert [event["event_type"] for event in history["events"]] == ["create", "revise"]
    assert [version["version_no"] for version in history["versions"]] == [1, 2]
    assert history["versions"][0]["side"] == "left"  # 历史版本保持原样


def test_stale_base_version_rejected(client, owner, project, zoo):
    record = zoo.create_record(record_no="R-11").json()
    current = record["current"]
    payload = {k: current[k] for k in (
        "taxon_path", "element", "side", "age_stage", "portion", "burning", "cut_marks",
        "context_unit", "layer", "grid", "bag", "confidence", "fragment_count", "note",
    )}
    payload["base_version_no"] = 99
    conflict = client.post(
        f"/api/projects/{project['id']}/zoo/records/{record['id']}/revisions",
        json=payload,
        headers=owner["headers"],
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "version_conflict"


def test_duplicate_record_no_rejected(client, owner, project, zoo):
    assert zoo.create_record(record_no="DUP").status_code == 201
    assert zoo.create_record(record_no="DUP").status_code == 409


def test_viewer_cannot_create_record(client, owner, project, zoo):
    user = client.post("/api/users", json={"username": "viewer1", "display_name": "观察员", "password": "ViewerPass!234"}).json()
    client.post(f"/api/projects/{project['id']}/members", json={"user_id": user["id"], "role": "viewer"}, headers=owner["headers"])
    login = client.post("/api/sessions", json={"username": "viewer1", "password": "ViewerPass!234"}).json()
    headers = {"Authorization": f"Bearer {login['token']}"}
    denied = client.post(
        f"/api/projects/{project['id']}/zoo/records",
        json={"record_no": "V-1", "taxon_path": "Mammalia", "element": "femur", "side": "left", "portion": 1.0},
        headers=headers,
    )
    assert denied.status_code == 403
    listed = client.get(f"/api/projects/{project['id']}/zoo/records", headers=headers)
    assert listed.status_code == 200


def test_unpublish_removes_from_statistics(client, owner, project, zoo):
    record = zoo.create_record(record_no="U-1").json()
    rule = zoo.create_rule(params={}).json()
    first = zoo.create_report(rule["id"]).json()
    assert first["result"]["totals"]["nisp"] == 1
    client.post(
        f"/api/projects/{project['id']}/zoo/records/{record['id']}/status",
        json={"action": "unpublish", "reason": "标本重新核对"},
        headers=owner["headers"],
    )
    second = zoo.create_report(rule["id"]).json()
    assert second["result"]["totals"]["nisp"] == 0
