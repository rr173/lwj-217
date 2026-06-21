import math
import csv
import io
from datetime import date, datetime
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload

from app.database import engine, Base, get_db
from app.models import Trial, Variety, Site, Plot, PhenologyData, YieldData
from app.schemas import (
    TrialCreate, TrialOut, VarietyOut, SiteOut,
    PhenologyInput, PhenologyOut, YieldInput, YieldOut,
    YieldAnalysisResult, AdaptabilityResult, EvaluationResult,
)

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="种业区域试验数据采集与品种评估服务",
    description="提供试验方案管理、数据采集、产量分析、适应性分析、综合评估和数据导出功能",
    version="1.0.0",
)


def _trial_to_out(trial: Trial) -> TrialOut:
    return TrialOut(
        id=trial.id,
        year=trial.year,
        crop_name=trial.crop_name,
        status=trial.status,
        created_at=trial.created_at.isoformat() if trial.created_at else "",
        varieties=[VarietyOut.model_validate(v) for v in trial.varieties],
        sites=[SiteOut.model_validate(s) for s in trial.sites],
    )


# ===================== 内部分析函数 =====================

def _do_yield_analysis(trial_id: int, db: Session) -> List[YieldAnalysisResult]:
    trial = db.query(Trial).filter(Trial.id == trial_id).first()
    if not trial:
        raise HTTPException(404, "试验方案不存在")

    plots = db.query(Plot).options(
        joinedload(Plot.yield_data), joinedload(Plot.variety), joinedload(Plot.site)
    ).filter(Plot.trial_id == trial_id).all()

    missing = [p.plot_code for p in plots if not p.yield_data or p.yield_data.plot_yield is None]
    if missing:
        raise HTTPException(400, f"以下小区缺少产量数据，无法进行分析: {', '.join(missing)}")

    variety_yields = {}
    for p in plots:
        vcode = p.variety.code
        if vcode not in variety_yields:
            variety_yields[vcode] = {
                "code": vcode,
                "name": p.variety.name,
                "is_control": p.variety.is_control,
                "yields": [],
            }
        variety_yields[vcode]["yields"].append(p.yield_data.plot_yield)

    control_code = None
    control_mean = None
    for vcode, vdata in variety_yields.items():
        if vdata["is_control"]:
            control_code = vcode
            control_mean = sum(vdata["yields"]) / len(vdata["yields"])
            break

    results = []
    for vcode, vdata in variety_yields.items():
        yields = vdata["yields"]
        mean_yield = sum(yields) / len(yields)
        std_dev = math.sqrt(sum((y - mean_yield) ** 2 for y in yields) / len(yields))
        cv = (std_dev / mean_yield) * 100 if mean_yield > 0 else 0

        increase_pct = None
        if control_mean and control_mean > 0 and vcode != control_code:
            increase_pct = round(((mean_yield - control_mean) / control_mean) * 100, 2)

        results.append(YieldAnalysisResult(
            variety_code=vcode,
            variety_name=vdata["name"],
            is_control=vdata["is_control"],
            mean_yield=round(mean_yield, 2),
            yield_increase_pct=increase_pct,
            cv=round(cv, 2),
        ))

    results.sort(key=lambda x: x.mean_yield, reverse=True)
    return results


def _do_adaptability_analysis(trial_id: int, db: Session) -> List[AdaptabilityResult]:
    trial = db.query(Trial).filter(Trial.id == trial_id).first()
    if not trial:
        raise HTTPException(404, "试验方案不存在")

    plots = db.query(Plot).options(
        joinedload(Plot.yield_data), joinedload(Plot.variety), joinedload(Plot.site)
    ).filter(Plot.trial_id == trial_id).all()

    missing = [p.plot_code for p in plots if not p.yield_data or p.yield_data.plot_yield is None]
    if missing:
        raise HTTPException(400, f"以下小区缺少产量数据，无法进行分析: {', '.join(missing)}")

    sites = db.query(Site).filter(Site.trial_id == trial_id).all()
    varieties = db.query(Variety).filter(Variety.trial_id == trial_id).all()

    variety_site_yields = {}
    for v in varieties:
        variety_site_yields[v.code] = {
            "name": v.name,
            "is_control": v.is_control,
            "sites": {},
        }

    site_all_means = {}
    for site in sites:
        site_plots = [p for p in plots if p.site_id == site.id]
        site_variety_means = {}
        for v in varieties:
            v_plots = [p for p in site_plots if p.variety_id == v.id]
            if v_plots:
                mean_y = sum(p.yield_data.plot_yield for p in v_plots) / len(v_plots)
                site_variety_means[v.code] = mean_y
                variety_site_yields[v.code]["sites"][site.code] = mean_y

        all_vals = list(site_variety_means.values())
        site_all_means[site.code] = sum(all_vals) / len(all_vals) if all_vals else 0

    total_sites = len(sites)
    results = []

    for vcode, vdata in variety_site_yields.items():
        adapt_count = 0
        for site_code, vmean in vdata["sites"].items():
            if vmean > site_all_means[site_code]:
                adapt_count += 1

        adapt_rate = (adapt_count / total_sites * 100) if total_sites > 0 else 0

        site_means = [vdata["sites"][s.code] for s in sites if s.code in vdata["sites"]]
        env_indices = [site_all_means[s.code] for s in sites if s.code in vdata["sites"]]

        bi = None
        if len(site_means) >= 2:
            n = len(site_means)
            sum_x = sum(env_indices)
            sum_y = sum(site_means)
            sum_xy = sum(x * y for x, y in zip(env_indices, site_means))
            sum_x2 = sum(x * x for x in env_indices)
            denom = n * sum_x2 - sum_x * sum_x
            if denom != 0:
                bi = (n * sum_xy - sum_x * sum_y) / denom
                bi = round(bi, 4)

        results.append(AdaptabilityResult(
            variety_code=vcode,
            variety_name=vdata["name"],
            is_control=vdata["is_control"],
            adapt_sites=adapt_count,
            adapt_rate=round(adapt_rate, 2),
            bi=bi,
        ))

    return results


def _do_comprehensive_evaluation(
    trial_id: int, db: Session,
    yield_weight: float = 0.4,
    stability_weight: float = 0.3,
    adapt_weight: float = 0.3,
) -> List[EvaluationResult]:
    trial = db.query(Trial).filter(Trial.id == trial_id).first()
    if not trial:
        raise HTTPException(404, "试验方案不存在")

    plots = db.query(Plot).options(
        joinedload(Plot.yield_data), joinedload(Plot.variety), joinedload(Plot.site)
    ).filter(Plot.trial_id == trial_id).all()

    missing = [p.plot_code for p in plots if not p.yield_data or p.yield_data.plot_yield is None]
    if missing:
        raise HTTPException(400, f"以下小区缺少产量数据，无法进行评估: {', '.join(missing)}")

    sites = db.query(Site).filter(Site.trial_id == trial_id).all()
    varieties = db.query(Variety).filter(Variety.trial_id == trial_id).all()

    variety_yields = {}
    for v in varieties:
        variety_yields[v.code] = {
            "name": v.name,
            "is_control": v.is_control,
            "all_yields": [],
            "site_yields": {},
        }

    for p in plots:
        vcode = p.variety.code
        variety_yields[vcode]["all_yields"].append(p.yield_data.plot_yield)
        if p.site.code not in variety_yields[vcode]["site_yields"]:
            variety_yields[vcode]["site_yields"][p.site.code] = []
        variety_yields[vcode]["site_yields"][p.site.code].append(p.yield_data.plot_yield)

    site_all_means = {}
    for site in sites:
        site_means = []
        for v in varieties:
            if site.code in variety_yields[v.code]["site_yields"]:
                ylist = variety_yields[v.code]["site_yields"][site.code]
                site_means.append(sum(ylist) / len(ylist))
        site_all_means[site.code] = sum(site_means) / len(site_means) if site_means else 0

    control_code = None
    control_mean_yield = None
    control_cv = None
    for vcode, vdata in variety_yields.items():
        if vdata["is_control"]:
            control_code = vcode
            yields = vdata["all_yields"]
            control_mean_yield = sum(yields) / len(yields)
            std = math.sqrt(sum((y - control_mean_yield) ** 2 for y in yields) / len(yields))
            control_cv = (std / control_mean_yield) * 100 if control_mean_yield > 0 else 0
            break

    results = []
    total_sites = len(sites)

    for vcode, vdata in variety_yields.items():
        yields = vdata["all_yields"]
        mean_yield = sum(yields) / len(yields)
        std = math.sqrt(sum((y - mean_yield) ** 2 for y in yields) / len(yields))
        cv = (std / mean_yield) * 100 if mean_yield > 0 else 0

        increase_pct = 0
        if control_mean_yield and control_mean_yield > 0 and vcode != control_code:
            increase_pct = ((mean_yield - control_mean_yield) / control_mean_yield) * 100

        yield_score = increase_pct if vcode != control_code else 0

        stability_score = 0
        if control_cv is not None and vcode != control_code:
            stability_score = control_cv - cv

        adapt_count = 0
        for site in sites:
            if site.code in vdata["site_yields"]:
                v_site_mean = sum(vdata["site_yields"][site.code]) / len(vdata["site_yields"][site.code])
                if v_site_mean > site_all_means[site.code]:
                    adapt_count += 1

        adapt_rate = (adapt_count / total_sites * 100) if total_sites > 0 else 0
        adapt_score = adapt_rate / 10

        total_score = yield_score * yield_weight + stability_score * stability_weight + adapt_score * adapt_weight

        recommendation = ""
        if control_mean_yield and mean_yield < control_mean_yield:
            recommendation = "淘汰"

        results.append(EvaluationResult(
            variety_code=vcode,
            variety_name=vdata["name"],
            is_control=vdata["is_control"],
            yield_score=round(yield_score, 2),
            stability_score=round(stability_score, 2),
            adaptability_score=round(adapt_score, 2),
            total_score=round(total_score, 2),
            recommendation=recommendation,
            mean_yield=round(mean_yield, 2),
            yield_increase_pct=round(increase_pct, 2) if vcode != control_code else None,
            cv=round(cv, 2),
            adapt_rate=round(adapt_rate, 2),
        ))

    results.sort(key=lambda x: x.total_score, reverse=True)

    if results and control_mean_yield:
        if not results[0].is_control and results[0].mean_yield >= control_mean_yield:
            results[0].recommendation = "强烈推荐"

        top3_count = 0
        for r in results:
            if r.recommendation == "强烈推荐":
                continue
            if r.mean_yield < control_mean_yield:
                r.recommendation = "淘汰"
            else:
                top3_count += 1
                if top3_count <= 3:
                    r.recommendation = "推荐"
                else:
                    r.recommendation = ""

    return results


# ===================== 试验管理 =====================

@app.post("/api/trials", response_model=TrialOut, summary="创建试验方案")
def create_trial(data: TrialCreate, db: Session = Depends(get_db)):
    control_codes = [v.code for v in data.varieties if v.code == data.control_variety_code]
    if not control_codes:
        raise HTTPException(400, f"对照品种编号 {data.control_variety_code} 不在参试品种列表中")

    trial = Trial(year=data.year, crop_name=data.crop_name, status="进行中")
    db.add(trial)
    db.flush()

    for v in data.varieties:
        variety = Variety(
            trial_id=trial.id,
            code=v.code,
            name=v.name,
            is_control=(v.code == data.control_variety_code),
        )
        db.add(variety)
    db.flush()

    for s in data.sites:
        site = Site(
            trial_id=trial.id,
            code=s.code,
            name=s.name,
            latitude=s.latitude,
            longitude=s.longitude,
            altitude=s.altitude,
            soil_type=s.soil_type,
        )
        db.add(site)
    db.flush()

    varieties = db.query(Variety).filter(Variety.trial_id == trial.id).all()
    sites = db.query(Site).filter(Site.trial_id == trial.id).all()

    for site in sites:
        for variety in varieties:
            for rep in range(1, 4):
                plot_code = f"{site.code}-{variety.code}-{rep}"
                plot = Plot(
                    trial_id=trial.id,
                    site_id=site.id,
                    variety_id=variety.id,
                    plot_code=plot_code,
                    replication=rep,
                )
                db.add(plot)

    db.commit()
    db.refresh(trial)
    return _trial_to_out(trial)


@app.get("/api/trials", response_model=List[TrialOut], summary="获取试验方案列表")
def list_trials(db: Session = Depends(get_db)):
    trials = db.query(Trial).options(
        joinedload(Trial.varieties), joinedload(Trial.sites)
    ).all()
    return [_trial_to_out(t) for t in trials]


@app.get("/api/trials/{trial_id}", response_model=TrialOut, summary="获取试验方案详情")
def get_trial(trial_id: int, db: Session = Depends(get_db)):
    trial = db.query(Trial).options(
        joinedload(Trial.varieties), joinedload(Trial.sites)
    ).filter(Trial.id == trial_id).first()
    if not trial:
        raise HTTPException(404, "试验方案不存在")
    return _trial_to_out(trial)


@app.put("/api/trials/{trial_id}/close", response_model=TrialOut, summary="关闭试验方案")
def close_trial(trial_id: int, db: Session = Depends(get_db)):
    trial = db.query(Trial).filter(Trial.id == trial_id).first()
    if not trial:
        raise HTTPException(404, "试验方案不存在")
    if trial.status == "已完成":
        raise HTTPException(400, "试验方案已经处于已完成状态")
    trial.status = "已完成"
    db.commit()
    db.refresh(trial)
    trial.varieties
    trial.sites
    return _trial_to_out(trial)


# ===================== 数据采集 =====================

@app.post("/api/plots/{plot_code}/phenology", response_model=PhenologyOut, summary="录入物候期数据")
def upsert_phenology(plot_code: str, data: PhenologyInput, db: Session = Depends(get_db)):
    plot = db.query(Plot).filter(Plot.plot_code == plot_code).first()
    if not plot:
        raise HTTPException(404, f"小区 {plot_code} 不存在")

    trial = db.query(Trial).filter(Trial.id == plot.trial_id).first()
    if trial.status == "已完成":
        raise HTTPException(400, "已完成的试验方案不允许修改数据")

    existing = db.query(PhenologyData).filter(PhenologyData.plot_id == plot.id).first()

    if existing:
        if data.sowing_date is not None:
            existing.sowing_date = data.sowing_date
        if data.emergence_date is not None:
            existing.emergence_date = data.emergence_date
        if data.heading_date is not None:
            existing.heading_date = data.heading_date
        if data.maturity_date is not None:
            existing.maturity_date = data.maturity_date

        if existing.sowing_date and existing.emergence_date and existing.emergence_date < existing.sowing_date:
            raise HTTPException(400, "出苗日期不能早于播种日期")
        if existing.emergence_date and existing.heading_date and existing.heading_date < existing.emergence_date:
            raise HTTPException(400, "抽穗日期不能早于出苗日期")
        if existing.heading_date and existing.maturity_date and existing.maturity_date < existing.heading_date:
            raise HTTPException(400, "成熟日期不能早于抽穗日期")
    else:
        if data.sowing_date and data.emergence_date and data.emergence_date < data.sowing_date:
            raise HTTPException(400, "出苗日期不能早于播种日期")
        if data.emergence_date and data.heading_date and data.heading_date < data.emergence_date:
            raise HTTPException(400, "抽穗日期不能早于出苗日期")
        if data.heading_date and data.maturity_date and data.maturity_date < data.heading_date:
            raise HTTPException(400, "成熟日期不能早于抽穗日期")

        existing = PhenologyData(
            plot_id=plot.id,
            sowing_date=data.sowing_date,
            emergence_date=data.emergence_date,
            heading_date=data.heading_date,
            maturity_date=data.maturity_date,
        )
        db.add(existing)

    db.commit()
    return PhenologyOut(plot_code=plot_code, sowing_date=existing.sowing_date,
                        emergence_date=existing.emergence_date,
                        heading_date=existing.heading_date,
                        maturity_date=existing.maturity_date)


@app.post("/api/plots/{plot_code}/yield", response_model=YieldOut, summary="录入产量性状数据")
def upsert_yield(plot_code: str, data: YieldInput, db: Session = Depends(get_db)):
    plot = db.query(Plot).filter(Plot.plot_code == plot_code).first()
    if not plot:
        raise HTTPException(404, f"小区 {plot_code} 不存在")

    trial = db.query(Trial).filter(Trial.id == plot.trial_id).first()
    if trial.status == "已完成":
        raise HTTPException(400, "已完成的试验方案不允许修改数据")

    existing = db.query(YieldData).filter(YieldData.plot_id == plot.id).first()

    if existing:
        if data.plant_height is not None:
            existing.plant_height = round(data.plant_height, 1)
        if data.grains_per_spike is not None:
            existing.grains_per_spike = data.grains_per_spike
        if data.thousand_grain_weight is not None:
            existing.thousand_grain_weight = round(data.thousand_grain_weight, 1)
        if data.plot_yield is not None:
            existing.plot_yield = round(data.plot_yield, 2)
    else:
        existing = YieldData(
            plot_id=plot.id,
            plant_height=round(data.plant_height, 1) if data.plant_height is not None else None,
            grains_per_spike=data.grains_per_spike,
            thousand_grain_weight=round(data.thousand_grain_weight, 1) if data.thousand_grain_weight is not None else None,
            plot_yield=round(data.plot_yield, 2) if data.plot_yield is not None else None,
        )
        db.add(existing)

    db.commit()
    return YieldOut(plot_code=plot_code, plant_height=existing.plant_height,
                    grains_per_spike=existing.grains_per_spike,
                    thousand_grain_weight=existing.thousand_grain_weight,
                    plot_yield=existing.plot_yield)


@app.get("/api/trials/{trial_id}/plots", summary="获取试验方案的所有小区数据")
def get_trial_plots(trial_id: int, db: Session = Depends(get_db)):
    trial = db.query(Trial).filter(Trial.id == trial_id).first()
    if not trial:
        raise HTTPException(404, "试验方案不存在")

    plots = db.query(Plot).options(
        joinedload(Plot.phenology), joinedload(Plot.yield_data),
        joinedload(Plot.variety), joinedload(Plot.site)
    ).filter(Plot.trial_id == trial_id).all()

    result = []
    for p in plots:
        item = {
            "plot_code": p.plot_code,
            "site_code": p.site.code,
            "site_name": p.site.name,
            "variety_code": p.variety.code,
            "variety_name": p.variety.name,
            "is_control": p.variety.is_control,
            "replication": p.replication,
        }
        if p.phenology:
            item["phenology"] = {
                "sowing_date": str(p.phenology.sowing_date) if p.phenology.sowing_date else None,
                "emergence_date": str(p.phenology.emergence_date) if p.phenology.emergence_date else None,
                "heading_date": str(p.phenology.heading_date) if p.phenology.heading_date else None,
                "maturity_date": str(p.phenology.maturity_date) if p.phenology.maturity_date else None,
            }
        else:
            item["phenology"] = None

        if p.yield_data:
            item["yield"] = {
                "plant_height": p.yield_data.plant_height,
                "grains_per_spike": p.yield_data.grains_per_spike,
                "thousand_grain_weight": p.yield_data.thousand_grain_weight,
                "plot_yield": p.yield_data.plot_yield,
            }
        else:
            item["yield"] = None

        result.append(item)
    return result


# ===================== 产量分析 =====================

@app.get("/api/trials/{trial_id}/analysis/yield", response_model=List[YieldAnalysisResult], summary="产量分析")
def yield_analysis(trial_id: int, db: Session = Depends(get_db)):
    return _do_yield_analysis(trial_id, db)


# ===================== 适应性分析 =====================

@app.get("/api/trials/{trial_id}/analysis/adaptability", response_model=List[AdaptabilityResult], summary="适应性分析")
def adaptability_analysis(trial_id: int, db: Session = Depends(get_db)):
    return _do_adaptability_analysis(trial_id, db)


# ===================== 综合评估 =====================

@app.get("/api/trials/{trial_id}/evaluation", response_model=List[EvaluationResult], summary="综合评估")
def comprehensive_evaluation(
    trial_id: int,
    yield_weight: float = Query(0.4, ge=0, le=1, description="丰产性权重"),
    stability_weight: float = Query(0.3, ge=0, le=1, description="稳定性权重"),
    adapt_weight: float = Query(0.3, ge=0, le=1, description="适应性权重"),
    db: Session = Depends(get_db),
):
    return _do_comprehensive_evaluation(trial_id, db, yield_weight, stability_weight, adapt_weight)


# ===================== 数据导出 =====================

@app.get("/api/trials/{trial_id}/export/json", summary="导出完整数据为JSON")
def export_json(trial_id: int, db: Session = Depends(get_db)):
    trial = db.query(Trial).filter(Trial.id == trial_id).first()
    if not trial:
        raise HTTPException(404, "试验方案不存在")

    varieties = db.query(Variety).filter(Variety.trial_id == trial_id).all()
    sites = db.query(Site).filter(Site.trial_id == trial_id).all()
    plots = db.query(Plot).options(
        joinedload(Plot.phenology), joinedload(Plot.yield_data),
        joinedload(Plot.variety), joinedload(Plot.site)
    ).filter(Plot.trial_id == trial_id).all()

    result = {
        "trial": {
            "id": trial.id,
            "year": trial.year,
            "crop_name": trial.crop_name,
            "status": trial.status,
            "created_at": trial.created_at.isoformat() if trial.created_at else None,
        },
        "varieties": [
            {"id": v.id, "code": v.code, "name": v.name, "is_control": v.is_control}
            for v in varieties
        ],
        "sites": [
            {"id": s.id, "code": s.code, "name": s.name, "latitude": s.latitude,
             "longitude": s.longitude, "altitude": s.altitude, "soil_type": s.soil_type}
            for s in sites
        ],
        "plots": [],
    }

    for p in plots:
        plot_data = {
            "plot_code": p.plot_code,
            "site_code": p.site.code,
            "variety_code": p.variety.code,
            "replication": p.replication,
        }
        if p.phenology:
            plot_data["phenology"] = {
                "sowing_date": str(p.phenology.sowing_date) if p.phenology.sowing_date else None,
                "emergence_date": str(p.phenology.emergence_date) if p.phenology.emergence_date else None,
                "heading_date": str(p.phenology.heading_date) if p.phenology.heading_date else None,
                "maturity_date": str(p.phenology.maturity_date) if p.phenology.maturity_date else None,
            }
        else:
            plot_data["phenology"] = None

        if p.yield_data:
            plot_data["yield"] = {
                "plant_height": p.yield_data.plant_height,
                "grains_per_spike": p.yield_data.grains_per_spike,
                "thousand_grain_weight": p.yield_data.thousand_grain_weight,
                "plot_yield": p.yield_data.plot_yield,
            }
        else:
            plot_data["yield"] = None

        result["plots"].append(plot_data)

    try:
        yield_results = _do_yield_analysis(trial_id, db)
        adapt_results = _do_adaptability_analysis(trial_id, db)
        eval_results = _do_comprehensive_evaluation(trial_id, db)
        result["analysis"] = {
            "yield_analysis": [r.model_dump() for r in yield_results],
            "adaptability_analysis": [r.model_dump() for r in adapt_results],
            "evaluation": [r.model_dump() for r in eval_results],
        }
    except HTTPException:
        result["analysis"] = None

    return result


@app.get("/api/trials/{trial_id}/export/csv", summary="导出产量分析结果为CSV")
def export_csv(trial_id: int, db: Session = Depends(get_db)):
    trial = db.query(Trial).filter(Trial.id == trial_id).first()
    if not trial:
        raise HTTPException(404, "试验方案不存在")

    try:
        yield_results = _do_yield_analysis(trial_id, db)
        adapt_results = _do_adaptability_analysis(trial_id, db)
        eval_results = _do_comprehensive_evaluation(trial_id, db)
    except HTTPException as e:
        raise HTTPException(400, f"无法生成CSV: {e.detail}")

    sites = db.query(Site).filter(Site.trial_id == trial_id).all()
    plots = db.query(Plot).options(
        joinedload(Plot.yield_data), joinedload(Plot.variety), joinedload(Plot.site)
    ).filter(Plot.trial_id == trial_id).all()

    variety_site_means = {}
    for p in plots:
        vcode = p.variety.code
        scode = p.site.code
        if vcode not in variety_site_means:
            variety_site_means[vcode] = {}
        if scode not in variety_site_means[vcode]:
            variety_site_means[vcode][scode] = []
        if p.yield_data and p.yield_data.plot_yield is not None:
            variety_site_means[vcode][scode].append(p.yield_data.plot_yield)

    adapt_map = {r.variety_code: r for r in adapt_results}
    eval_map = {r.variety_code: r for r in eval_results}

    output = io.StringIO()
    site_codes = [s.code for s in sites]
    fieldnames = ["品种名"] + [f"{sc}均产" for sc in site_codes] + ["总均产", "增产百分比", "CV", "适应率", "综合得分", "推荐等级"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for yr in yield_results:
        row = {"品种名": yr.variety_name}
        for sc in site_codes:
            vals = variety_site_means.get(yr.variety_code, {}).get(sc, [])
            row[f"{sc}均产"] = round(sum(vals) / len(vals), 2) if vals else ""
        row["总均产"] = yr.mean_yield
        row["增产百分比"] = yr.yield_increase_pct if yr.yield_increase_pct is not None else ""
        row["CV"] = yr.cv
        adapt_r = adapt_map.get(yr.variety_code)
        row["适应率"] = adapt_r.adapt_rate if adapt_r else ""
        er = eval_map.get(yr.variety_code)
        row["综合得分"] = er.total_score if er else ""
        row["推荐等级"] = er.recommendation if er else ""
        writer.writerow(row)

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=trial_{trial_id}_analysis.csv"},
    )


# ===================== 预置数据 =====================

def seed_preset_data():
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        existing = db.query(Trial).filter(Trial.year == 2024, Trial.crop_name == "水稻").first()
        if existing:
            return

        trial = Trial(year=2024, crop_name="水稻", status="进行中")
        db.add(trial)
        db.flush()

        variety_defs = [
            ("V01", "南粳9108", False),
            ("V02", "扬粳805", False),
            ("V03", "宁粳7号", False),
            ("V04", "武运粳31", False),
            ("V05", "淮稻5号", False),
            ("V06", "镇稻18号", False),
            ("V07", "苏秀867", False),
            ("V08", "武育粳3号(CK)", True),
        ]

        variety_map = {}
        for code, name, is_ctrl in variety_defs:
            v = Variety(trial_id=trial.id, code=code, name=name, is_control=is_ctrl)
            db.add(v)
            db.flush()
            variety_map[code] = v

        import random
        random.seed(42)

        site_defs = [
            ("S01", "南京试验站", 32.06, 118.78, 20.0, "水稻土"),
            ("S02", "扬州试验站", 32.39, 119.42, 5.0, "潮土"),
            ("S03", "淮安试验站", 33.50, 119.02, 15.0, "砂姜黑土"),
            ("S04", "盐城试验站", 33.35, 120.16, 3.0, "盐渍土"),
            ("S05", "徐州试验站", 34.26, 117.18, 40.0, "黄褐土"),
        ]

        site_map = {}
        for code, name, lat, lon, alt, soil in site_defs:
            s = Site(trial_id=trial.id, code=code, name=name,
                     latitude=lat, longitude=lon, altitude=alt, soil_type=soil)
            db.add(s)
            db.flush()
            site_map[code] = s

        base_yields = {
            "V01": 8.5, "V02": 7.8, "V03": 8.2, "V04": 7.5,
            "V05": 8.0, "V06": 7.2, "V07": 8.8, "V08": 7.0,
        }

        site_effect = {
            "S01": 1.05, "S02": 1.02, "S03": 0.95, "S04": 0.90, "S05": 0.88,
        }

        all_plots = []
        for scode, site in site_map.items():
            for vcode, variety in variety_map.items():
                for rep in range(1, 4):
                    plot_code = f"{scode}-{vcode}-{rep}"
                    plot = Plot(
                        trial_id=trial.id,
                        site_id=site.id,
                        variety_id=variety.id,
                        plot_code=plot_code,
                        replication=rep,
                    )
                    db.add(plot)
                    all_plots.append((plot, scode, vcode, rep))

        db.flush()

        missing_indices = set(random.sample(range(120), 20))

        sowing = date(2024, 5, 20)
        emergence = date(2024, 6, 1)
        heading = date(2024, 8, 15)
        maturity = date(2024, 10, 10)

        for idx, (plot, scode, vcode, rep) in enumerate(all_plots):
            pheno = PhenologyData(
                plot_id=plot.id,
                sowing_date=sowing,
                emergence_date=emergence,
                heading_date=heading,
                maturity_date=maturity,
            )
            db.add(pheno)

            if idx not in missing_indices:
                base = base_yields[vcode] * site_effect[scode]
                yld = base + random.uniform(-0.8, 0.8)
                yld = round(yld, 2)
                if yld < 0.5:
                    yld = 0.5
                if yld > 50:
                    yld = 50.0

                height = round(85 + random.uniform(-15, 15), 1)
                if height < 30:
                    height = 30.0
                if height > 300:
                    height = 300.0

                grains = int(120 + random.uniform(-30, 30))
                if grains < 10:
                    grains = 10
                if grains > 500:
                    grains = 500

                tgw = round(26 + random.uniform(-4, 4), 1)
                if tgw < 10:
                    tgw = 10.0
                if tgw > 80:
                    tgw = 80.0

                yd = YieldData(
                    plot_id=plot.id,
                    plant_height=height,
                    grains_per_spike=grains,
                    thousand_grain_weight=tgw,
                    plot_yield=yld,
                )
                db.add(yd)

        db.commit()
    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    seed_preset_data()
