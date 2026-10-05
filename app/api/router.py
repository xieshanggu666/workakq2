"""游戏 API：系统信息 / 动作预览 / 任务执行 / 成绩记录与回放。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional

from app.core.database import SessionLocal
from app.schemas import Action, validate_actions
from app.services import physics
from app.services import scores as scores_svc
from app.services.levels import LEVELS, LEVEL_BY_ID

router = APIRouter(prefix="/api")


class SimRequest(BaseModel):
    level_id: int
    actions: List[Action] = Field(default_factory=list)


class ScoreRequest(BaseModel):
    """成绩提交：新版携带 run_id + submission_id（幂等键）；

    旧版客户端只带 stars/fuel_used/elapsed_days，照常受理（source=legacy）。
    """
    level_id: int
    run_id: Optional[str] = None
    submission_id: Optional[str] = None
    stars: Optional[int] = Field(default=None, ge=0, le=3)
    fuel_used: float = 0.0
    elapsed_days: float = 0.0


def _sim_response(lv, actions, with_stars: bool):
    r = physics.integrate(lv, actions)
    resp = {
        "ok": r["ok"],
        "reason": r["reason"],
        "elapsed_days": r["elapsed_days"],
        "fuel_used": r["fuel_used"],
        "budget_dv": r["budget_dv"],
        "milestones": r["milestones"],
        "events": r["events"],
        "trajectory": r["trajectory"],
    }
    if with_stars:
        stars = physics.stars_for(lv, r["fuel_used"], r["ok"])
        resp["stars"] = stars
    return resp


@router.get("/system")
def system_info():
    bodies = []
    for b in physics.BODIES:
        if b["id"] == "sun":
            bodies.append({"id": b["id"], "name": b["name"], "color": b["color"],
                           "radius": b["radius"]})
        else:
            bodies.append({"id": b["id"], "name": b["name"], "color": b["color"],
                           "radius": b["radius"], "orbit": b["orbit"],
                           "period": b["period"], "theta0": b["theta0"],
                           "soi": physics._soi_radius(b["id"])})
    levels = [{"id": lv["id"], "name": lv["name"], "brief": lv["brief"],
               "milestones": lv["milestones"], "budget_dv": lv["budget_dv"],
               "t_max": lv["t_max"], "hint": lv["hint"]} for lv in LEVELS]
    with SessionLocal() as db:
        scores = scores_svc.best_summaries(db)
    return {"bodies": bodies, "levels": levels, "scores": scores}


@router.post("/preview")
def preview(req: SimRequest):
    lv = LEVEL_BY_ID.get(req.level_id)
    if lv is None:
        raise HTTPException(404, "关卡不存在")
    return _sim_response(lv, validate_actions(req.actions), with_stars=False)


@router.post("/run")
def run(req: SimRequest):
    lv = LEVEL_BY_ID.get(req.level_id)
    if lv is None:
        raise HTTPException(404, "关卡不存在")
    actions = validate_actions(req.actions)
    resp = _sim_response(lv, actions, with_stars=True)
    # 执行档案落库：成绩记录通过 run_id 关联到本次执行，实现可溯源与回放
    with SessionLocal() as db:
        rec = scores_svc.record_run(db, lv["id"], actions, resp)
        resp["run_id"] = rec.run_uid
    return resp


@router.post("/score")
def save_score(req: ScoreRequest):
    if LEVEL_BY_ID.get(req.level_id) is None:
        raise HTTPException(404, "关卡不存在")
    if req.run_id is None and req.stars is None:
        raise HTTPException(400, "缺少 run_id 或 stars")
    try:
        with SessionLocal() as db:
            return scores_svc.save_score(
                db,
                level_id=req.level_id,
                submission_id=req.submission_id,
                run_id=req.run_id,
                stars=req.stars,
                fuel_used=req.fuel_used,
                elapsed_days=req.elapsed_days,
            )
    except scores_svc.RunNotFound as e:
        raise HTTPException(404, str(e))
    except scores_svc.RunLevelMismatch as e:
        raise HTTPException(400, str(e))


@router.get("/scores/{level_id}/records")
def score_records(level_id: int, limit: int = 20):
    """某关的成绩提交记录（新→旧），用于成绩追溯。"""
    if LEVEL_BY_ID.get(level_id) is None:
        raise HTTPException(404, "关卡不存在")
    with SessionLocal() as db:
        return {"records": scores_svc.list_records(db, level_id, limit=max(1, min(100, limit)))}


@router.get("/records/{record_id}")
def record_detail(record_id: int):
    """成绩记录详情：含动作方案与轨迹（关联执行档案时），供前端回放。"""
    with SessionLocal() as db:
        detail = scores_svc.record_detail(db, record_id)
    if detail is None:
        raise HTTPException(404, "成绩记录不存在")
    return detail
