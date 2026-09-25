"""动物遗存模块的请求模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RecordBase(BaseModel):
    taxon_path: str = Field(..., min_length=1, max_length=300)
    element: str = Field(..., min_length=1, max_length=80)
    side: Literal["left", "right", "axial", "unknown"]
    age_stage: str = Field(default="unknown", max_length=40)
    portion: float = Field(..., gt=0, le=1)
    burning: str = Field(default="none", max_length=40)
    cut_marks: bool = False
    context_unit: str = Field(default="", max_length=80)
    layer: str = Field(default="", max_length=80)
    grid: str = Field(default="", max_length=80)
    bag: str = Field(default="", max_length=80)
    confidence: float = Field(default=1.0, ge=0, le=1)
    fragment_count: int = Field(default=1, ge=1)
    note: str = Field(default="", max_length=500)


class RecordCreate(RecordBase):
    record_no: str = Field(..., min_length=1, max_length=80)
    status: Literal["auto", "staged", "published"] = "auto"
    reason: str = Field(default="", max_length=300)


class RecordRevise(RecordBase):
    base_version_no: int = Field(..., ge=1)
    reason: str = Field(default="", max_length=300)


class StatusChange(BaseModel):
    action: Literal["publish", "unpublish"]
    reason: str = Field(default="", max_length=300)


class RuleCreate(BaseModel):
    rule_code: str = Field(..., min_length=1, max_length=60)
    params: dict = Field(default_factory=dict)


class ReportCreate(BaseModel):
    rule_id: int
    grouping: list[str] = Field(default_factory=list)
    filters: dict = Field(default_factory=dict)


class RefitDetect(BaseModel):
    rule_id: int | None = None


class RefitDecision(BaseModel):
    action: Literal["confirm", "reject", "undo"]
    reason: str = Field(default="", max_length=300)
