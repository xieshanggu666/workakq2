"""成绩存档服务：执行档案落库 / 幂等提交 / 最佳成绩汇总 / 记录查询与回放数据。

设计要点：
- 每次任务执行（/api/run）落库 RunRecord，成绩记录（ScoreRecord）通过
  run_id 关联到它 —— 分数由服务端结算得出，且每条成绩都可溯源、可回放。
- 提交幂等：客户端为每次发射生成 submission_id；重复提交（双击、网络重试、
  多标签页）命中唯一约束或查重分支，只保留首个结果，不重复刷新最佳成绩。
- 并发安全：模块级锁串行化"查重 → 插入 → 更新最佳"临界区（SQLite 单写者），
  submission_id 唯一约束作为兜底；最佳汇总只单调变优，不会被并发写回退。
- 旧存档兼容：level_score 汇总表结构不变；旧客户端不带 run_id/submission_id
  的提交照常受理（source="legacy"）。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import List, Optional

from sqlalchemy.exc import IntegrityError

from app.models import LevelScore, RunRecord, ScoreRecord

# SQLite 单写者：序列化"查重-插入-更新最佳"临界区。
_SAVE_LOCK = threading.Lock()


class RunNotFound(KeyError):
    """run_id 对应的执行档案不存在。"""


class RunLevelMismatch(ValueError):
    """执行档案与提交的关卡不一致。"""


def record_run(db, level_id: int, actions: List[dict], result: dict) -> RunRecord:
    """把一次任务执行的完整结算落库，返回执行档案。"""
    run = RunRecord(
        run_uid=uuid.uuid4().hex,
        level_id=level_id,
        ok=1 if result["ok"] else 0,
        reason=result["reason"],
        stars=result.get("stars", 0),
        fuel_used=result["fuel_used"],
        elapsed_days=result["elapsed_days"],
        actions_json=json.dumps(actions, ensure_ascii=False),
        trajectory_json=json.dumps(result["trajectory"]),
        events_json=json.dumps(result["events"], ensure_ascii=False),
        milestones_json=json.dumps(result["milestones"], ensure_ascii=False),
        created_at=time.time(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _is_better(stars: int, fuel: float, row: LevelScore) -> bool:
    """星数优先，同星比燃料（旧版只比较星数，此处向后兼容地增强）。"""
    return stars > row.stars or (stars == row.stars and fuel < row.fuel_used)


def _apply_best(db, level_id: int, stars: int, fuel: float, elapsed: float) -> bool:
    """单调更新最佳成绩汇总（level_score 旧表），返回是否刷新了纪录。"""
    row = db.query(LevelScore).filter(LevelScore.level_id == level_id).first()
    if row is None:
        db.add(LevelScore(level_id=level_id, stars=stars,
                          fuel_used=fuel, elapsed_days=elapsed))
        return True
    if _is_better(stars, fuel, row):
        row.stars = stars
        row.fuel_used = fuel
        row.elapsed_days = elapsed
        return True
    return False


def _best_stars(db, level_id: int, fallback: int) -> int:
    row = db.query(LevelScore).filter(LevelScore.level_id == level_id).first()
    return row.stars if row is not None else fallback


def _duplicated_response(db, rec: Optional[ScoreRecord], level_id: int) -> dict:
    return {
        "saved": True,
        "duplicated": True,
        "improved": False,
        "record_id": rec.id if rec else None,
        "level_id": level_id,
        "stars": _best_stars(db, level_id, rec.stars if rec else 0),
    }


def save_score(db, *, level_id: int, submission_id: Optional[str] = None,
               run_id: Optional[str] = None, stars: Optional[int] = None,
               fuel_used: float = 0.0, elapsed_days: float = 0.0) -> dict:
    """落库一条成绩记录并刷新最佳汇总。

    幂等：submission_id 已存在时直接返回首个提交的结果（duplicated=True），
    不重复落库、不重复刷新最佳成绩。
    """
    with _SAVE_LOCK:
        # 1) 幂等重放：同一提交已受理过 → 返回首个结果
        if submission_id:
            dup = (db.query(ScoreRecord)
                     .filter(ScoreRecord.submission_id == submission_id)
                     .first())
            if dup is not None:
                return _duplicated_response(db, dup, level_id)

        # 2) 取值：优先关联执行档案（服务端结算，可溯源）；否则按旧版客户端字段
        run = None
        source = "legacy"
        if run_id is not None:
            run = (db.query(RunRecord)
                     .filter(RunRecord.run_uid == run_id)
                     .first())
            if run is None:
                raise RunNotFound(f"执行记录不存在: {run_id}")
            if run.level_id != level_id:
                raise RunLevelMismatch("执行记录与关卡不匹配")
            stars, fuel_used, elapsed_days = run.stars, run.fuel_used, run.elapsed_days
            source = "run"
        stars = int(stars or 0)

        # 3) 落库成绩记录（唯一约束兜底并发重复）
        rec = ScoreRecord(
            submission_id=submission_id or uuid.uuid4().hex,
            run_id=run.id if run else None,
            level_id=level_id,
            stars=stars,
            fuel_used=fuel_used,
            elapsed_days=elapsed_days,
            source=source,
            created_at=time.time(),
        )
        db.add(rec)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            dup = (db.query(ScoreRecord)
                     .filter(ScoreRecord.submission_id == rec.submission_id)
                     .first())
            return _duplicated_response(db, dup, level_id)

        # 4) 单调刷新最佳汇总并提交
        improved = _apply_best(db, level_id, stars, fuel_used, elapsed_days)
        db.commit()
        return {
            "saved": True,
            "duplicated": False,
            "improved": improved,
            "record_id": rec.id,
            "level_id": level_id,
            "stars": _best_stars(db, level_id, stars),
        }


def best_record(db, level_id: int, replayable_only: bool = False) -> Optional[ScoreRecord]:
    """某关的最佳成绩记录（星数 → 燃料 → 先到优先）。"""
    q = db.query(ScoreRecord).filter(ScoreRecord.level_id == level_id)
    if replayable_only:
        q = q.filter(ScoreRecord.run_id.isnot(None))
    return (q.order_by(ScoreRecord.stars.desc(),
                       ScoreRecord.fuel_used.asc(),
                       ScoreRecord.id.asc())
             .first())


def best_summaries(db) -> dict:
    """/api/system 用：level_id -> 最佳成绩（旧字段不变，附加可回放记录 id）。"""
    out = {}
    for s in db.query(LevelScore).all():
        out[s.level_id] = {"stars": s.stars, "fuel_used": s.fuel_used,
                           "elapsed_days": s.elapsed_days}
    for level_id in list(out):
        rec = best_record(db, level_id, replayable_only=True)
        if rec is not None:
            out[level_id]["record_id"] = rec.id
    return out


def list_records(db, level_id: int, limit: int = 20) -> List[dict]:
    """某关最近的成绩记录（追溯列表，新→旧）。"""
    rows = (db.query(ScoreRecord)
              .filter(ScoreRecord.level_id == level_id)
              .order_by(ScoreRecord.id.desc())
              .limit(limit)
              .all())
    return [{
        "record_id": r.id,
        "level_id": r.level_id,
        "stars": r.stars,
        "fuel_used": r.fuel_used,
        "elapsed_days": r.elapsed_days,
        "source": r.source,
        "replayable": r.run_id is not None,
        "created_at": r.created_at,
    } for r in rows]


def record_detail(db, record_id: int) -> Optional[dict]:
    """成绩记录详情：关联执行档案时附带动作方案与轨迹（供前端回放）。"""
    rec = db.query(ScoreRecord).filter(ScoreRecord.id == record_id).first()
    if rec is None:
        return None
    detail = {
        "record_id": rec.id,
        "level_id": rec.level_id,
        "stars": rec.stars,
        "fuel_used": rec.fuel_used,
        "elapsed_days": rec.elapsed_days,
        "source": rec.source,
        "replayable": rec.run_id is not None,
        "created_at": rec.created_at,
    }
    if rec.run_id is not None:
        run = db.query(RunRecord).filter(RunRecord.id == rec.run_id).first()
        if run is not None:
            detail.update({
                "run_uid": run.run_uid,
                "ok": bool(run.ok),
                "reason": run.reason,
                "actions": json.loads(run.actions_json),
                "trajectory": json.loads(run.trajectory_json),
                "events": json.loads(run.events_json),
                "milestones": json.loads(run.milestones_json),
            })
    return detail
