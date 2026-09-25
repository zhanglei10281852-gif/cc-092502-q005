from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path):
    os.environ["ARCHAEOLOGY_DATABASE_PATH"] = str(tmp_path / "test.db")
    from app.database import close_connection
    close_connection()
    from app.main import app
    with TestClient(app) as value:
        yield value
    close_connection()


@pytest.fixture()
def owner(client):
    user = client.post("/api/users", json={"username": "owner", "display_name": "项目负责人", "password": "OwnerPass!234"})
    assert user.status_code == 201
    login = client.post("/api/sessions", json={"username": "owner", "password": "OwnerPass!234"})
    return {"user": user.json(), "headers": {"Authorization": f"Bearer {login.json()['token']}"}}


@pytest.fixture()
def project(client, owner):
    response = client.post(
        "/api/projects",
        json={"code": "ZOO1", "name": "动物考古研究", "site_name": "测试遗址"},
        headers=owner["headers"],
    )
    assert response.status_code == 201
    return response.json()


@pytest.fixture()
def make_record():
    """构造鉴定记录请求体,默认来源完整(自动发布)。"""
    def _make(**overrides):
        payload = {
            "record_no": "R-1",
            "taxon_path": "Mammalia/Artiodactyla/Bovidae/Ovis aries",
            "element": "humerus",
            "side": "left",
            "age_stage": "adult",
            "portion": 1.0,
            "burning": "none",
            "cut_marks": False,
            "context_unit": "H1",
            "layer": "L1",
            "grid": "T1",
            "bag": "B1",
            "confidence": 1.0,
            "fragment_count": 1,
            "note": "",
        }
        payload.update(overrides)
        return payload
    return _make


@pytest.fixture()
def zoo(client, owner, project, make_record):
    """面向 HTTP 层的快捷操作集合。"""
    headers = owner["headers"]
    base = f"/api/projects/{project['id']}/zoo"

    class Zoo:
        def create_record(self, **overrides):
            return client.post(f"{base}/records", json=make_record(**overrides), headers=headers)

        def publish(self, record_id):
            return client.post(f"{base}/records/{record_id}/status", json={"action": "publish"}, headers=headers)

        def create_rule(self, rule_code="mni-standard", params=None):
            return client.post(f"{base}/rules", json={"rule_code": rule_code, "params": params or {}}, headers=headers)

        def create_report(self, rule_id, grouping=None, filters=None):
            return client.post(
                f"{base}/reports",
                json={"rule_id": rule_id, "grouping": grouping or [], "filters": filters or {}},
                headers=headers,
            )

    return Zoo()
