"""动物遗存鉴定与量化服务的 HTTP 路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.deps import current_user
from app.service import ResearchService
from app.zooarch.models import QuantifyRequest, RecordCreate, RecordRevise, ReportCreate, RulesetCreate
from app.zooarch.service import ZooarchService

router = APIRouter(prefix="/api/zooarch/projects/{project_id}", tags=["zooarch"])

RECORD_ROLES = {"owner", "researcher", "recorder"}
REVIEW_ROLES = {"owner", "researcher", "reviewer"}
READ_ROLES = {"owner", "researcher", "recorder", "reviewer", "viewer"}


def _check(project_id: int, user: dict, roles: set[str]) -> None:
    ResearchService().require_role(project_id, user["id"], roles)


@router.post("/records", status_code=201)
def create_record(project_id: int, payload: RecordCreate, user=Depends(current_user)):
    _check(project_id, user, RECORD_ROLES)
    return ZooarchService().create_record(project_id, payload.model_dump(), user["id"])


@router.get("/records")
def list_records(project_id: int, user=Depends(current_user),
                 status: str | None = Query(default=None), element: str | None = Query(default=None),
                 side: str | None = Query(default=None), unit: str | None = Query(default=None),
                 layer: str | None = Query(default=None), taxon_path: str | None = Query(default=None)):
    _check(project_id, user, READ_ROLES)
    filters = {k: v for k, v in {"status": status, "element": element, "side": side,
                                 "unit": unit, "layer": layer, "taxon_path": taxon_path}.items() if v}
    return {"data": ZooarchService().list_records(project_id, filters)}


@router.get("/records/{record_id}")
def get_record(project_id: int, record_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return ZooarchService().get_record(record_id, project_id)


@router.get("/records/{record_id}/events")
def record_events(project_id: int, record_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return {"data": ZooarchService().record_events(record_id, project_id)}


@router.get("/records/{record_id}/versions")
def record_versions(project_id: int, record_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return {"data": ZooarchService().record_versions(record_id, project_id)}


@router.post("/records/{record_id}/revisions")
def revise_record(project_id: int, record_id: int, payload: RecordRevise, user=Depends(current_user)):
    _check(project_id, user, REVIEW_ROLES)
    return ZooarchService().revise_record(record_id, payload.base_version, payload.changes(), payload.note, user["id"], project_id)


@router.post("/rulesets", status_code=201)
def create_ruleset(project_id: int, payload: RulesetCreate, user=Depends(current_user)):
    _check(project_id, user, {"owner", "researcher"})
    return ZooarchService().create_ruleset(project_id, payload.name, payload.rules.model_dump(), user["id"])


@router.get("/rulesets")
def list_rulesets(project_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return {"data": ZooarchService().list_rulesets(project_id)}


@router.get("/rulesets/{version}")
def get_ruleset(project_id: int, version: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return ZooarchService().get_ruleset(project_id, version)


@router.post("/quantify")
def quantify(project_id: int, payload: QuantifyRequest, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return ZooarchService().quantify(project_id, payload.group_by, payload.ruleset_version)


@router.post("/reports", status_code=201)
def create_report(project_id: int, payload: ReportCreate, user=Depends(current_user)):
    _check(project_id, user, REVIEW_ROLES)
    return ZooarchService().create_report(project_id, payload.name, payload.group_by,
                                          payload.ruleset_version, user["id"])


@router.get("/reports")
def list_reports(project_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return {"data": ZooarchService().list_reports(project_id)}


@router.get("/reports/{report_id}")
def get_report(project_id: int, report_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return ZooarchService().get_report(report_id, project_id)


@router.post("/reports/{report_id}/recompute")
def recompute_report(project_id: int, report_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return ZooarchService().recompute_report(report_id, project_id)


@router.post("/refits/scan")
def scan_refits(project_id: int, user=Depends(current_user)):
    _check(project_id, user, RECORD_ROLES)
    return ZooarchService().scan_refits(project_id, user["id"])


@router.get("/refits")
def list_refits(project_id: int, status: str | None = Query(default=None), user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return {"data": ZooarchService().list_refits(project_id, status)}


@router.post("/refits/{link_id}/confirm")
def confirm_refit(project_id: int, link_id: int, user=Depends(current_user)):
    _check(project_id, user, REVIEW_ROLES)
    return ZooarchService().confirm_refit(link_id, user["id"], project_id)


@router.post("/refits/{link_id}/revoke")
def revoke_refit(project_id: int, link_id: int, user=Depends(current_user)):
    _check(project_id, user, REVIEW_ROLES)
    return ZooarchService().revoke_refit(link_id, user["id"], project_id)
