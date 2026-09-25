"""动物遗存模块的请求模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.zooarch.mni import DEFAULT_AGE_CLASSES, DEFAULT_UNIQUE_ELEMENTS, GROUP_FIELDS

Side = Literal["left", "right", "axial", "unknown"]


class RecordCreate(BaseModel):
    record_key: str = Field(..., min_length=1, max_length=80)
    taxon_path: str = Field(..., min_length=1, max_length=200)
    taxon_rank: str = Field(default="", max_length=40)
    element: str = Field(..., min_length=1, max_length=80)
    side: Side = "unknown"
    age_stage: str = Field(default="unknown", min_length=1, max_length=40)
    portion: float = Field(..., gt=0, le=1)
    fragment_count: int = Field(default=1, ge=1, le=100000)
    burned: bool = False
    cut_marks: bool = False
    unit: str = Field(default="", max_length=120)
    layer: str = Field(default="", max_length=120)
    square: str = Field(default="", max_length=120)
    bag: str = Field(default="", max_length=120)
    confidence: float = Field(default=1.0, ge=0, le=1)
    notes: str = Field(default="", max_length=500)


class RecordRevise(BaseModel):
    base_version: int = Field(..., ge=1)
    note: str = Field(default="", max_length=500)
    taxon_path: str | None = Field(default=None, min_length=1, max_length=200)
    taxon_rank: str | None = Field(default=None, max_length=40)
    element: str | None = Field(default=None, min_length=1, max_length=80)
    side: Side | None = None
    age_stage: str | None = Field(default=None, min_length=1, max_length=40)
    portion: float | None = Field(default=None, gt=0, le=1)
    fragment_count: int | None = Field(default=None, ge=1, le=100000)
    burned: bool | None = None
    cut_marks: bool | None = None
    unit: str | None = Field(default=None, max_length=120)
    layer: str | None = Field(default=None, max_length=120)
    square: str | None = Field(default=None, max_length=120)
    bag: str | None = Field(default=None, max_length=120)
    confidence: float | None = Field(default=None, ge=0, le=1)
    notes: str | None = Field(default=None, max_length=500)

    def changes(self) -> dict:
        payload = self.model_dump(exclude_none=True)
        payload.pop("base_version", None)
        payload.pop("note", None)
        return payload


class RulesModel(BaseModel):
    min_confidence: float = Field(default=0.4, ge=0, le=1)
    unique_elements: list[str] = Field(default_factory=lambda: list(DEFAULT_UNIQUE_ELEMENTS))
    age_classes: dict[str, list[str]] = Field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_AGE_CLASSES.items()})
    refit_same_context: bool = True

    @field_validator("age_classes")
    @classmethod
    def _classes_disjoint(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if not value:
            raise ValueError("age_classes 不能为空")
        seen: dict[str, str] = {}
        for name, stages in value.items():
            if not stages:
                raise ValueError(f"年龄类 {name!r} 不能为空")
            for stage in stages:
                if stage in seen:
                    raise ValueError(f"年龄阶段 {stage!r} 同时出现在 {seen[stage]!r} 与 {name!r}")
                seen[stage] = name
        return value


class RulesetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    rules: RulesModel = Field(default_factory=RulesModel)


class QuantifyRequest(BaseModel):
    group_by: list[str] = Field(default_factory=lambda: ["unit", "layer"])
    ruleset_version: int | None = Field(default=None, ge=1)

    @field_validator("group_by")
    @classmethod
    def _valid_group_by(cls, value: list[str]) -> list[str]:
        bad = [f for f in value if f not in GROUP_FIELDS]
        if bad:
            raise ValueError(f"分组字段非法: {bad}，可选 {list(GROUP_FIELDS)}")
        if len(set(value)) != len(value):
            raise ValueError("分组字段重复")
        return value


class ReportCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    group_by: list[str] = Field(default_factory=lambda: ["unit", "layer"])
    ruleset_version: int | None = Field(default=None, ge=1)

    @field_validator("group_by")
    @classmethod
    def _valid_group_by(cls, value: list[str]) -> list[str]:
        return QuantifyRequest._valid_group_by(value)
