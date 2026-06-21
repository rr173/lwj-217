from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import date
from enum import Enum


class VarietyCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=20)
    name: str = Field(..., min_length=1, max_length=100)


class SiteCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=20)
    name: str = Field(..., min_length=1, max_length=100)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    altitude: float = Field(..., ge=0)
    soil_type: str = Field(..., min_length=1, max_length=50)


class TrialCreate(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    crop_name: str = Field(..., min_length=1, max_length=100)
    varieties: List[VarietyCreate] = Field(..., min_length=3, max_length=20)
    control_variety_code: str = Field(..., description="对照品种编号，必须在参试品种列表中")
    sites: List[SiteCreate] = Field(..., min_length=3, max_length=15)

    @field_validator("control_variety_code")
    @classmethod
    def control_must_be_in_varieties(cls, v, info):
        if info.data.get("varieties"):
            codes = [var.code for var in info.data["varieties"]]
            if v not in codes:
                raise ValueError(f"对照品种编号 {v} 不在参试品种列表中")
        return v


class VarietyOut(BaseModel):
    id: int
    code: str
    name: str
    is_control: bool

    model_config = {"from_attributes": True}


class SiteOut(BaseModel):
    id: int
    code: str
    name: str
    latitude: float
    longitude: float
    altitude: float
    soil_type: str

    model_config = {"from_attributes": True}


class TrialOut(BaseModel):
    id: int
    year: int
    crop_name: str
    status: str
    created_at: str
    varieties: List[VarietyOut]
    sites: List[SiteOut]

    model_config = {"from_attributes": True}


class PhenologyInput(BaseModel):
    sowing_date: Optional[date] = None
    emergence_date: Optional[date] = None
    heading_date: Optional[date] = None
    maturity_date: Optional[date] = None

    @field_validator("emergence_date")
    @classmethod
    def emergence_after_sowing(cls, v, info):
        if v and info.data.get("sowing_date") and v < info.data["sowing_date"]:
            raise ValueError("出苗日期不能早于播种日期")
        return v

    @field_validator("heading_date")
    @classmethod
    def heading_after_emergence(cls, v, info):
        if v and info.data.get("emergence_date") and v < info.data["emergence_date"]:
            raise ValueError("抽穗日期不能早于出苗日期")
        return v

    @field_validator("maturity_date")
    @classmethod
    def maturity_after_heading(cls, v, info):
        if v and info.data.get("heading_date") and v < info.data["heading_date"]:
            raise ValueError("成熟日期不能早于抽穗日期")
        return v


class YieldInput(BaseModel):
    plant_height: Optional[float] = Field(None, ge=30, le=300, description="株高(cm)")
    grains_per_spike: Optional[int] = Field(None, ge=10, le=500, description="穗粒数")
    thousand_grain_weight: Optional[float] = Field(None, ge=10, le=80, description="千粒重(g)")
    plot_yield: Optional[float] = Field(None, ge=0.5, le=50, description="小区产量(kg)")


class PhenologyOut(BaseModel):
    plot_code: str
    sowing_date: Optional[date] = None
    emergence_date: Optional[date] = None
    heading_date: Optional[date] = None
    maturity_date: Optional[date] = None


class YieldOut(BaseModel):
    plot_code: str
    plant_height: Optional[float] = None
    grains_per_spike: Optional[int] = None
    thousand_grain_weight: Optional[float] = None
    plot_yield: Optional[float] = None


class YieldAnalysisResult(BaseModel):
    variety_code: str
    variety_name: str
    is_control: bool
    mean_yield: float
    yield_increase_pct: Optional[float] = None
    cv: Optional[float] = None


class AdaptabilityResult(BaseModel):
    variety_code: str
    variety_name: str
    is_control: bool
    adapt_sites: int
    adapt_rate: float
    bi: Optional[float] = None
    site_details: Optional[dict] = None


class EvaluationResult(BaseModel):
    variety_code: str
    variety_name: str
    is_control: bool
    yield_score: float
    stability_score: float
    adaptability_score: float
    total_score: float
    recommendation: str
    mean_yield: float
    yield_increase_pct: Optional[float] = None
    cv: Optional[float] = None
    adapt_rate: Optional[float] = None
