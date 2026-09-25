"""SQLite 重启前后的持久化:记录、版本链、报告快照与拼合决策在重连后保持一致。"""
from __future__ import annotations

from app.database import close_connection, connection, init_db
from app.zoo_schema import init_zoo_db
from app.zoo_service import ZooService


def _reopen():
    """关闭线程本地连接并重新初始化,模拟服务重启。"""
    close_connection()
    init_db()
    init_zoo_db()


def test_results_survive_reconnect(client, owner, project, zoo):
    zoo.create_record(record_no="N1", side="left")
    zoo.create_record(record_no="N2", side="right")
    zoo.create_record(record_no="N3", side="left", layer="L2")
    rule = zoo.create_rule(params={}).json()
    report = zoo.create_report(rule["id"], grouping=["layer"]).json()
    assert report["result"]["totals"] == {
        "nisp": 3, "mne": 3, "mni": 2, "included_records": 3, "excluded_records": 0,
    }

    _reopen()

    service = ZooService()
    verify = service.verify_report(project["id"], report["id"])
    assert verify["consistent"] is True
    records = service.list_records(project["id"], owner["user"]["id"])["data"]
    assert [row["record_no"] for row in records] == ["N1", "N2", "N3"]
    # 重启后版本链继续增长
    detail = service.get_record(project["id"], records[0]["id"])
    payload = {k: detail["current"][k] for k in (
        "taxon_path", "element", "side", "age_stage", "portion", "burning", "cut_marks",
        "context_unit", "layer", "grid", "bag", "confidence", "fragment_count", "note",
    )}
    payload["base_version_no"] = 1
    revised = service.revise_record(project["id"], records[0]["id"], payload, owner["user"]["id"])
    assert revised["current"]["version_no"] == 2

    _reopen()
    assert ZooService().verify_report(project["id"], report["id"])["consistent"] is True


def test_refit_decisions_survive_reconnect(client, owner, project, zoo):
    zoo.create_record(record_no="D1", bag="B1", portion=0.4)
    zoo.create_record(record_no="D2", bag="B2", portion=0.5)
    zoo.create_record(record_no="D3", bag="B3", portion=0.6)
    service = ZooService()
    service.detect_refits(project["id"], {}, owner["user"]["id"])
    candidate = service.list_refits(project["id"], owner["user"]["id"])["data"][0]
    service.decide_refit(project["id"], candidate["id"], "confirm", owner["user"]["id"])

    _reopen()

    service = ZooService()
    states = sorted(c["status"] for c in service.list_refits(project["id"], owner["user"]["id"])["data"])
    assert states == ["blocked", "confirmed"]
    # 重启后撤销仍生效
    restored = service.decide_refit(project["id"], candidate["id"], "undo", owner["user"]["id"])
    assert restored["status"] == "proposed"
    decisions = connection().execute(
        "SELECT action FROM zoo_refit_decisions WHERE candidate_id=? ORDER BY id", (candidate["id"],)
    ).fetchall()
    assert [row[0] for row in decisions] == ["confirm", "undo"]
