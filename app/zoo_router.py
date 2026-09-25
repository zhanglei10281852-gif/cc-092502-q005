"""动物遗存鉴定与量化模块的 HTTP 路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.service import ResearchService
from app.zoo_models import (
    RecordCreate,
    RecordRevise,
    RefitDecision,
    RefitDetect,
    ReportCreate,
    RuleCreate,
    StatusChange,
)
from app.zoo_service import ZooService

router = APIRouter(prefix="/api/projects/{project_id}/zoo", tags=["zooarchaeology"])


def current_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer 会话")
    return ResearchService().authenticate(authorization[7:])


@router.post("/records", status_code=201)
def create_record(project_id: int, payload: RecordCreate, user=Depends(current_user)):
    return ZooService().create_record(project_id, payload.model_dump(), user["id"])


@router.get("/records")
def list_records(project_id: int, status: str | None = Query(default=None), user=Depends(current_user)):
    return ZooService().list_records(project_id, user["id"], status)


@router.get("/records/{record_id}")
def get_record(project_id: int, record_id: int, history: bool = Query(default=False), user=Depends(current_user)):
    service = ZooService()
    service.base.require_role(project_id, user["id"], {"owner", "researcher", "recorder", "reviewer", "viewer"})
    if history:
        return service.get_record_history(project_id, record_id)
    return service.get_record(project_id, record_id)


@router.post("/records/{record_id}/revisions", status_code=201)
def revise_record(project_id: int, record_id: int, payload: RecordRevise, user=Depends(current_user)):
    return ZooService().revise_record(project_id, record_id, payload.model_dump(), user["id"])


@router.post("/records/{record_id}/status")
def change_status(project_id: int, record_id: int, payload: StatusChange, user=Depends(current_user)):
    return ZooService().set_record_status(project_id, record_id, payload.action, user["id"], payload.reason)


@router.post("/rules", status_code=201)
def create_rule(project_id: int, payload: RuleCreate, user=Depends(current_user)):
    return ZooService().create_rule(project_id, payload.model_dump(), user["id"])


@router.get("/rules")
def list_rules(project_id: int, user=Depends(current_user)):
    return ZooService().list_rules(project_id, user["id"])


@router.post("/reports", status_code=201)
def create_report(project_id: int, payload: ReportCreate, user=Depends(current_user)):
    return ZooService().create_report(project_id, payload.model_dump(), user["id"])


@router.get("/reports")
def list_reports(project_id: int, user=Depends(current_user)):
    return ZooService().list_reports(project_id, user["id"])


@router.get("/reports/{report_id}")
def get_report(project_id: int, report_id: int, user=Depends(current_user)):
    service = ZooService()
    service.base.require_role(project_id, user["id"], {"owner", "researcher", "recorder", "reviewer", "viewer"})
    return service.get_report(project_id, report_id)


@router.post("/reports/{report_id}/verify")
def verify_report(project_id: int, report_id: int, user=Depends(current_user)):
    service = ZooService()
    service.base.require_role(project_id, user["id"], {"owner", "researcher", "recorder", "reviewer", "viewer"})
    return service.verify_report(project_id, report_id)


@router.post("/refits/detect")
def detect_refits(project_id: int, payload: RefitDetect, user=Depends(current_user)):
    return ZooService().detect_refits(project_id, payload.model_dump(), user["id"])


@router.get("/refits")
def list_refits(project_id: int, status: str | None = Query(default=None), user=Depends(current_user)):
    return ZooService().list_refits(project_id, user["id"], status)


@router.post("/refits/{candidate_id}/decisions")
def decide_refit(project_id: int, candidate_id: int, payload: RefitDecision, user=Depends(current_user)):
    return ZooService().decide_refit(project_id, candidate_id, payload.action, user["id"], payload.reason)
