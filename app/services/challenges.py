"""社区航线挑战服务：版本化发布 / 幂等提交 / 审核 / 申诉与复核 / 排行榜 / 回放 / 解锁联动。

设计要点：
- 版本化：每次发布生成不可变的 ChallengeVersion（预算 + 里程碑 + 时间限制），
  飞行记录与成绩都锚定具体版本；排行榜按版本结算，旧版本仍可查看与回放。
- 幂等结算：/run 落库执行档案（ChallengeRun），/submit 以 submission_id 为幂等键
  关联档案落库成绩；重复提交（双击/重试/多标签页）返回首个结果，不重复计数。
- 审核联动：成绩默认 pending；审核通过（approved）后才进入排行榜、开放轨迹回放，
  并计入解锁条件。解锁状态按规则实时求值，审核通过即自动联动，无需额外迁移。
- 申诉与复核：玩家可对 rejected/revoked 成绩凭 appeal_id 幂等键发起申诉（每成绩
  至多 2 轮、同时仅 1 条），成绩回到 pending 进入复核队列；复核员（moderator）
  维持原判(uphold→回到原状态)或推翻原判(overturn→approved)。复核员还可撤销
  (revoke)/恢复(restore)已上榜成绩；排行榜/回放/解锁全部按状态实时求值，
  撤销即自动出榜、关闭回放并级联回收解锁，接口返回前后 diff 的回滚效果。
- 可追溯：每次状态迁移追加一条不可变的 ChallengeReviewEvent（提交/初审/申诉/
  复核/撤销/恢复）；历史已审核成绩在启动时补登 legacy 事件，链路不断档。
- 审核权限：Reviewer 分 reviewer（初审）/moderator（复核、撤销、注册）两级，
  token 经 X-Reviewer-Token 上送；内置本地账号开箱即用，缺省调用降级为内置初审员。
- 并发安全：模块级锁串行化提交/审核/申诉临界区（SQLite 单写者），唯一约束兜底。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import List, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from app.models import (Challenge, ChallengeAppeal, ChallengeReviewEvent,
                        ChallengeRun, ChallengeSubmission, ChallengeVersion,
                        LevelScore, Reviewer)
from app.services import physics
from app.services.levels import LEVEL_BY_ID

# SQLite 单写者：序列化"查重-插入 / 审核状态迁移 / 申诉复核"临界区。
_LOCK = threading.Lock()

_KEEP_RULE = object()  # publish_version 的"保留原解锁条件"哨兵

_PLANET_IDS = {b["id"] for b in physics.BODIES if b["id"] != "sun"}
_MILESTONE_KINDS = {"proximity", "radius", "assist_capture", "assist_then_radius", "escape"}
_REVIEW_ACTIONS = {"approve": "approved", "reject": "rejected"}
DEFAULT_PLAYER = "匿名飞行员"

# 审核状态
PENDING, APPROVED, REJECTED, REVOKED = "pending", "approved", "rejected", "revoked"
# 申诉裁决
APPEAL_PENDING, APPEAL_UPHOLD, APPEAL_OVERTURN = "pending", "uphold", "overturn"
# 事件类型
EV_SUBMIT = "submit"
EV_REVIEW = {"approved": "review_approve", "rejected": "review_reject"}
EV_APPEAL = "appeal"
EV_APPEAL_DECISION = {APPEAL_UPHOLD: "appeal_uphold", APPEAL_OVERTURN: "appeal_overturn"}
EV_REVOKE = "revoke"
EV_RESTORE = "restore"
EV_LEGACY = "legacy"

ROLE_REVIEWER = "reviewer"
ROLE_MODERATOR = "moderator"
MAX_APPEAL_ROUNDS = 2
MAX_APPEAL_REASON = 500

# 内置审核账号（本地单机开箱即用；正式部署可禁用/改 token）
BUILTIN_REVIEWERS = [
    {"name": "本机初审员", "token": "local-reviewer", "role": ROLE_REVIEWER},
    {"name": "本机复核员", "token": "local-moderator", "role": ROLE_MODERATOR},
]


# ---------- 异常 ----------

class ChallengeNotFound(KeyError):
    """挑战不存在。"""


class VersionNotFound(KeyError):
    """挑战版本不存在。"""


class SubmissionNotFound(KeyError):
    """成绩提交记录不存在。"""


class ValidationError(ValueError):
    """关卡定义 / 解锁条件 / 请求参数校验失败。"""


class RunNotFound(KeyError):
    """run_id 对应的挑战执行档案不存在。"""


class RunChallengeMismatch(ValueError):
    """执行档案与提交的挑战不一致。"""


class ChallengeLocked(PermissionError):
    """挑战未解锁，不能结算飞行记录。"""


class ReviewConflict(RuntimeError):
    """成绩已终审或申诉链路状态冲突，不能执行该动作。"""


class UnauthorizedReviewer(PermissionError):
    """缺少有效的审核凭证（401）。"""


class ForbiddenReviewer(PermissionError):
    """审核账号权限不足（403）。"""


class AppealNotFound(KeyError):
    """申诉单不存在。"""


class AppealConflict(RuntimeError):
    """已有进行中的申诉 / 申诉轮次用尽 / 当前状态不能申诉。"""


class ReviewerConflict(ValueError):
    """审核员署名或 token 冲突。"""


# ---------- 定义校验 ----------

def _clean_text(s, max_len: int, field: str, allow_empty: bool = False) -> str:
    s = (s or "").strip()
    if not s and not allow_empty:
        raise ValidationError(f"{field}不能为空")
    if len(s) > max_len:
        raise ValidationError(f"{field}过长（≤{max_len} 字）")
    return s


def _bounded(v, lo: float, hi: float, field: str) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}必须是数字")
    if not lo <= v <= hi:
        raise ValidationError(f"{field}需在 {lo}~{hi} 之间")
    return v


def _planet(pid) -> str:
    if pid not in _PLANET_IDS:
        raise ValidationError(f"未知行星: {pid}")
    return pid


def validate_milestones(ms_list) -> List[dict]:
    """校验并规范化里程碑定义（与物理引擎支持的判定类型对齐）。"""
    if not isinstance(ms_list, list) or not 1 <= len(ms_list) <= 5:
        raise ValidationError("里程碑需 1~5 个")
    out, seen = [], set()
    for i, m in enumerate(ms_list, 1):
        kind = (m or {}).get("kind")
        if kind not in _MILESTONE_KINDS:
            raise ValidationError(f"未知里程碑类型: {kind}")
        mid = str(m.get("id") or f"m{i}")
        if mid in seen:
            raise ValidationError(f"里程碑 id 重复: {mid}")
        seen.add(mid)
        item = {"id": mid, "kind": kind,
                "name": _clean_text(m.get("name"), 40, "里程碑名称")}
        if kind == "proximity":
            item["planet_id"] = _planet(m.get("planet_id"))
            item["dist"] = _bounded(m.get("dist"), 0.001, 5.0, "飞掠距离")
        elif kind == "radius":
            item["r"] = _bounded(m.get("r"), 0.2, 50.0, "目标半径")
        elif kind == "assist_capture":
            item["planet_id"] = _planet(m.get("planet_id"))
        elif kind == "assist_then_radius":
            item["planet_id"] = _planet(m.get("planet_id"))
            item["r"] = _bounded(m.get("r"), 0.2, 50.0, "目标半径")
        elif kind == "escape":
            item["r"] = _bounded(m.get("r"), 1.0, 100.0, "逃逸边界")
        out.append(item)
    return out


def validate_definition(d: dict) -> dict:
    """校验一版关卡定义：预算 / 时间限制 / 里程碑。"""
    d = d or {}
    return {
        "name": _clean_text(d.get("name"), 40, "版本名", allow_empty=True),
        "brief": _clean_text(d.get("brief"), 500, "简介", allow_empty=True),
        "hint": _clean_text(d.get("hint"), 500, "提示", allow_empty=True),
        "budget_dv": _bounded(d.get("budget_dv"), 0.0002, 0.05, "燃料预算"),
        "t_max": _bounded(d.get("t_max"), 30, 20000, "时间限制"),
        "milestones": validate_milestones(d.get("milestones") or []),
    }


def validate_unlock_rule(db, rule, self_id: Optional[int] = None) -> Optional[dict]:
    """校验解锁条件；{"type":"none"} 与 None 都表示无条件（返回 None）。"""
    if not rule or rule.get("type") in (None, "none"):
        return None
    t = rule.get("type")
    if t == "builtin_level":
        lid = rule.get("level_id")
        if lid not in LEVEL_BY_ID:
            raise ValidationError(f"未知内置关卡: {lid}")
        return {"type": t, "level_id": lid}
    if t == "challenge":
        cid = rule.get("challenge_id")
        if self_id is not None and cid == self_id:
            raise ValidationError("解锁条件不能引用挑战自身")
        if db.query(Challenge).filter(Challenge.id == cid).first() is None:
            raise ValidationError(f"前置挑战不存在: {cid}")
        return {"type": t, "challenge_id": cid}
    if t == "stars_total":
        return {"type": t, "value": int(_bounded(rule.get("value"), 1, 99, "所需星数"))}
    raise ValidationError(f"未知解锁条件类型: {t}")


# ---------- 发布（版本化） ----------

def _add_version(db, challenge_id: int, version: int, defn: dict) -> ChallengeVersion:
    ver = ChallengeVersion(
        challenge_id=challenge_id, version=version,
        name=defn["name"], brief=defn["brief"], hint=defn["hint"],
        budget_dv=defn["budget_dv"], t_max=defn["t_max"],
        milestones_json=json.dumps(defn["milestones"], ensure_ascii=False),
        created_at=time.time(),
    )
    db.add(ver)
    return ver


def create_challenge(db, *, title: str, author: str, definition: dict,
                     unlock_rule=None) -> dict:
    """发布新挑战：创建挑战主体 + 不可变的 v1 版本。"""
    title = _clean_text(title, 40, "挑战标题")
    author = _clean_text(author, 24, "设计者署名", allow_empty=True) or "匿名设计者"
    defn = validate_definition(definition)
    rule = validate_unlock_rule(db, unlock_rule)
    ch = Challenge(title=title, author=author, status="published", current_version=1,
                   unlock_rule_json=json.dumps(rule) if rule else "",
                   created_at=time.time())
    db.add(ch)
    db.flush()  # 取 challenge.id
    _add_version(db, ch.id, 1, defn)
    db.commit()
    return challenge_detail(db, ch.id)


def publish_version(db, challenge_id: int, definition: dict,
                    unlock_rule=_KEEP_RULE) -> dict:
    """发布新版本：定义校验后落库为不可变版本并设为当前版本。

    unlock_rule 缺省（未传）时保留原解锁条件；显式传 {"type":"none"} 可清除。
    """
    ch = _get_challenge(db, challenge_id)
    defn = validate_definition(definition)
    _add_version(db, ch.id, ch.current_version + 1, defn)
    ch.current_version += 1
    if unlock_rule is not _KEEP_RULE:
        rule = validate_unlock_rule(db, unlock_rule, self_id=ch.id)
        ch.unlock_rule_json = json.dumps(rule) if rule else ""
    db.commit()
    return challenge_detail(db, ch.id)


# ---------- 查询与解锁 ----------

def _get_challenge(db, challenge_id: int) -> Challenge:
    ch = db.query(Challenge).filter(Challenge.id == challenge_id).first()
    if ch is None or ch.status == "archived":
        raise ChallengeNotFound(f"挑战不存在: {challenge_id}")
    return ch


def _get_version(db, challenge_id: int, version: int) -> ChallengeVersion:
    ver = (db.query(ChallengeVersion)
             .filter(ChallengeVersion.challenge_id == challenge_id,
                     ChallengeVersion.version == version)
             .first())
    if ver is None:
        raise VersionNotFound(f"挑战 #{challenge_id} 没有版本 v{version}")
    return ver


def level_def(ver: ChallengeVersion) -> dict:
    """把版本行转成物理引擎可用的关卡定义。"""
    return {
        "id": ver.challenge_id,
        "name": ver.name,
        "milestones": json.loads(ver.milestones_json),
        "budget_dv": ver.budget_dv,
        "t_max": ver.t_max,
    }


def _builtin_stars(db) -> dict:
    return {s.level_id: s.stars for s in db.query(LevelScore).all()}


def _challenge_stars(db) -> dict:
    """每个挑战已审核通过的最佳星数（跨版本）。"""
    out = {}
    rows = (db.query(ChallengeSubmission)
              .filter(ChallengeSubmission.review_status == "approved")
              .all())
    for r in rows:
        out[r.challenge_id] = max(out.get(r.challenge_id, 0), r.stars)
    return out


def _unlock_state(rule: Optional[dict], builtin: dict, ch_stars: dict,
                  total_stars: int, titles: dict) -> Tuple[bool, Optional[str]]:
    """求值解锁条件，返回 (是否解锁, 未解锁时的说明)。"""
    if not rule:
        return True, None
    t = rule["type"]
    if t == "builtin_level":
        ok = builtin.get(rule["level_id"], 0) >= 1
        return ok, None if ok else f"通关内置第 {rule['level_id']} 关后解锁"
    if t == "challenge":
        ok = ch_stars.get(rule["challenge_id"], 0) >= 1
        dep = titles.get(rule["challenge_id"], f"#{rule['challenge_id']}")
        return ok, None if ok else f"通关挑战「{dep}」后解锁"
    if t == "stars_total":
        ok = total_stars >= rule["value"]
        return ok, None if ok else f"累计星数 ≥ {rule['value']} 后解锁"
    return True, None


def _unlock_context(db):
    builtin = _builtin_stars(db)
    ch_stars = _challenge_stars(db)
    total = sum(builtin.values()) + sum(ch_stars.values())
    titles = {c.id: c.title for c in db.query(Challenge).all()}
    return builtin, ch_stars, total, titles


def _rule_of(ch: Challenge) -> Optional[dict]:
    return json.loads(ch.unlock_rule_json) if ch.unlock_rule_json else None


def _best_approved(db) -> dict:
    """每个挑战本机最佳已审核成绩（星数 → 燃料 → 先到优先）。"""
    rows = (db.query(ChallengeSubmission)
              .filter(ChallengeSubmission.review_status == "approved")
              .order_by(ChallengeSubmission.stars.desc(),
                        ChallengeSubmission.fuel_used.asc(),
                        ChallengeSubmission.id.asc())
              .all())
    best = {}
    for r in rows:
        if r.challenge_id not in best:
            best[r.challenge_id] = {
                "stars": r.stars, "fuel_used": r.fuel_used,
                "elapsed_days": r.elapsed_days, "record_id": r.id,
            }
    return best


def list_challenges(db) -> List[dict]:
    """挑战列表：当前版本摘要 + 解锁状态 + 本机最佳（已审核）+ 待审数。"""
    rows = (db.query(Challenge)
              .filter(Challenge.status != "archived")
              .order_by(Challenge.id)
              .all())
    vers = {}
    for v in db.query(ChallengeVersion).all():
        vers.setdefault(v.challenge_id, {})[v.version] = v
    pend = {}
    for r in (db.query(ChallengeSubmission)
                .filter(ChallengeSubmission.review_status == "pending").all()):
        pend[r.challenge_id] = pend.get(r.challenge_id, 0) + 1
    builtin, ch_stars, total, titles = _unlock_context(db)
    best = _best_approved(db)
    out = []
    for ch in rows:
        v = vers.get(ch.id, {}).get(ch.current_version)
        unlocked, desc = _unlock_state(_rule_of(ch), builtin, ch_stars, total, titles)
        out.append({
            "id": ch.id, "title": ch.title, "author": ch.author,
            "current_version": ch.current_version,
            "version_count": len(vers.get(ch.id, {})),
            "created_at": ch.created_at,
            "brief": v.brief if v else "",
            "budget_dv": v.budget_dv if v else 0.0,
            "t_max": v.t_max if v else 0.0,
            "milestone_count": len(json.loads(v.milestones_json)) if v else 0,
            "unlocked": unlocked, "unlock_desc": desc,
            "best": best.get(ch.id),
            "pending_count": pend.get(ch.id, 0),
        })
    return out


def challenge_detail(db, challenge_id: int) -> dict:
    """挑战详情：全部版本列表 + 当前版本完整定义 + 解锁状态。"""
    ch = _get_challenge(db, challenge_id)
    vers = (db.query(ChallengeVersion)
              .filter(ChallengeVersion.challenge_id == ch.id)
              .order_by(ChallengeVersion.version)
              .all())
    cur = next((v for v in vers if v.version == ch.current_version), vers[-1])
    builtin, ch_stars, total, titles = _unlock_context(db)
    rule = _rule_of(ch)
    unlocked, desc = _unlock_state(rule, builtin, ch_stars, total, titles)
    return {
        "id": ch.id, "title": ch.title, "author": ch.author,
        "status": ch.status, "current_version": ch.current_version,
        "created_at": ch.created_at,
        "unlock_rule": rule, "unlocked": unlocked, "unlock_desc": desc,
        "versions": [{
            "version": v.version, "name": v.name,
            "budget_dv": v.budget_dv, "t_max": v.t_max,
            "milestone_count": len(json.loads(v.milestones_json)),
            "created_at": v.created_at,
        } for v in vers],
        "current": {
            "version": cur.version, "name": cur.name, "brief": cur.brief,
            "hint": cur.hint, "budget_dv": cur.budget_dv, "t_max": cur.t_max,
            "milestones": json.loads(cur.milestones_json),
        },
        "best": _best_approved(db).get(ch.id),
    }


def version_detail(db, challenge_id: int, version: int) -> dict:
    """指定版本的完整定义（查看旧版本 / 按旧版本游玩）。"""
    ch = _get_challenge(db, challenge_id)
    ver = _get_version(db, challenge_id, version)
    return {
        "challenge_id": ch.id, "title": ch.title, "author": ch.author,
        "version": ver.version, "name": ver.name, "brief": ver.brief,
        "hint": ver.hint, "budget_dv": ver.budget_dv, "t_max": ver.t_max,
        "milestones": json.loads(ver.milestones_json),
        "is_current": ver.version == ch.current_version,
        "created_at": ver.created_at,
    }


def get_playable(db, challenge_id: int, version: Optional[int] = None,
                 enforce_unlock: bool = False) -> Tuple[Challenge, ChallengeVersion]:
    """取挑战 + 版本行；enforce_unlock 时未解锁抛 ChallengeLocked（结算类接口用）。"""
    ch = _get_challenge(db, challenge_id)
    ver = _get_version(db, challenge_id, version or ch.current_version)
    if enforce_unlock:
        builtin, ch_stars, total, titles = _unlock_context(db)
        unlocked, desc = _unlock_state(_rule_of(ch), builtin, ch_stars, total, titles)
        if not unlocked:
            raise ChallengeLocked(desc or "挑战未解锁")
    return ch, ver


# ---------- 飞行记录：执行档案 + 幂等提交 ----------

def record_run(db, ch: Challenge, ver: ChallengeVersion, actions: List[dict],
               result: dict, stars: int) -> ChallengeRun:
    """把一次挑战飞行的完整结算落库，返回执行档案。"""
    run = ChallengeRun(
        run_uid=uuid.uuid4().hex,
        challenge_id=ch.id, version=ver.version,
        ok=1 if result["ok"] else 0,
        reason=result["reason"], stars=stars,
        fuel_used=result["fuel_used"], elapsed_days=result["elapsed_days"],
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


def _submitted_response(rec: ChallengeSubmission, duplicated: bool) -> dict:
    return {
        "saved": True,
        "duplicated": duplicated,
        "record_id": rec.id,
        "challenge_id": rec.challenge_id,
        "version": rec.version,
        "review_status": rec.review_status,
        "stars": rec.stars,
    }


def submit(db, *, challenge_id: int, run_id: str, submission_id: Optional[str],
           player: Optional[str]) -> dict:
    """落库一条挑战成绩（默认待审核）。

    幂等：submission_id 已存在时直接返回首个提交的结果（duplicated=True），
    不重复落库、不重复进入审核队列。
    """
    with _LOCK:
        # 1) 幂等重放：同一提交已受理过 → 返回首个结果
        if submission_id:
            dup = (db.query(ChallengeSubmission)
                     .filter(ChallengeSubmission.submission_id == submission_id)
                     .first())
            if dup is not None:
                return _submitted_response(dup, duplicated=True)

        # 2) 关联执行档案：成绩以服务端结算为准，且必须属于本挑战
        run = (db.query(ChallengeRun)
                 .filter(ChallengeRun.run_uid == run_id)
                 .first())
        if run is None:
            raise RunNotFound(f"执行记录不存在: {run_id}")
        if run.challenge_id != challenge_id:
            raise RunChallengeMismatch("执行记录与挑战不匹配")

        # 3) 落库成绩记录（唯一约束兜底并发重复）
        rec = ChallengeSubmission(
            submission_id=submission_id or uuid.uuid4().hex,
            run_id=run.id,
            challenge_id=run.challenge_id,
            version=run.version,
            player=(player or "").strip()[:24] or DEFAULT_PLAYER,
            stars=run.stars,
            fuel_used=run.fuel_used,
            elapsed_days=run.elapsed_days,
            review_status="pending",
            created_at=time.time(),
        )
        db.add(rec)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            dup = (db.query(ChallengeSubmission)
                     .filter(ChallengeSubmission.submission_id == rec.submission_id)
                     .first())
            return _submitted_response(dup, duplicated=True)
        _add_event(db, rec, kind=EV_SUBMIT, actor=rec.player,
                   actor_role="player",
                   detail={"submission_id": rec.submission_id,
                           "challenge_id": rec.challenge_id, "version": rec.version,
                           "stars": rec.stars, "fuel_used": rec.fuel_used})
        db.commit()
        return _submitted_response(rec, duplicated=False)


# ---------- 审核权限 / 历史兼容 ----------

def _sqlite_migrate(db) -> None:
    """对旧库做增量列迁移（SQLite ALTER TABLE ADD COLUMN，幂等）。"""
    bind = db.get_bind()
    try:
        url = bind.url
    except Exception:
        url = None
    if url is None or not str(url).startswith("sqlite"):
        return
    path = url.database
    conn = sqlite3.connect(path)
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(challenge_submission)").fetchall()}
        if cols and "reviewed_by" not in cols:
            conn.execute(
                "ALTER TABLE challenge_submission ADD COLUMN reviewed_by VARCHAR(24)")
            conn.commit()
    finally:
        conn.close()


def _seed_reviewers(db) -> None:
    """确保内置审核账号存在（同名不覆盖 token，避免破坏既有授权）。"""
    existing = {r.name: r for r in db.query(Reviewer).all()}
    added = False
    for spec in BUILTIN_REVIEWERS:
        if spec["name"] not in existing:
            db.add(Reviewer(name=spec["name"], token=spec["token"],
                            role=spec["role"], active=1))
            added = True
    if added:
        db.commit()


def _backfill_legacy_events(db) -> None:
    """兼容已审核历史成绩：为没有事件档案的已终审成绩补登 legacy 事件。"""
    fin = (db.query(ChallengeSubmission)
             .filter(ChallengeSubmission.review_status.in_((APPROVED, REJECTED)))
             .all())
    if not fin:
        return
    with_events = {
        sid for (sid,) in db.query(ChallengeReviewEvent.submission_id).distinct().all()
    }
    n = 0
    for rec in fin:
        if rec.id in with_events:
            continue
        _add_event(db, rec, kind=EV_LEGACY,
                   actor=rec.reviewed_by or "历史审核",
                   actor_role="system",
                   detail={"status": rec.review_status,
                           "note": rec.review_note or "",
                           "reviewed_at": rec.reviewed_at})
        n += 1
    if n:
        db.commit()


_BOOTSTRAP_LOCK = threading.Lock()
_BOOTSTRAPPED = False


def bootstrap(db) -> None:
    """启动引导：旧库加列迁移 + 内置审核账号 + 历史成绩事件补登（幂等）。

    不持有业务 _LOCK：迁移会打开第二个 SQLite 连接（ALTER TABLE），
    若在 _LOCK 临界区内、且主连接已开启写事务时执行会与写锁互锁。
    """
    global _BOOTSTRAPPED
    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAPPED:
            return
        _sqlite_migrate(db)
        _seed_reviewers(db)
        _backfill_legacy_events(db)
        _BOOTSTRAPPED = True


def reset_bootstrap_for_tests() -> None:
    """测试夹具用：换库后允许再次执行启动引导。"""
    global _BOOTSTRAPPED
    _BOOTSTRAPPED = False


def authenticate(db, token: Optional[str], *, required_role: Optional[str] = None):
    """校验审核凭证，返回 Reviewer；缺省 token 降级为内置初审员（本地单机开放入口）。

    required_role=moderator 时必须显式提供复核员 token：
    无 token/失效 → UnauthorizedReviewer(401)；初审员越权 → ForbiddenReviewer(403)。
    bootstrap 必须在调用方获取 _LOCK 之前完成（见 bootstrap 文档说明）。
    """
    bootstrap(db)
    if not token:
        if required_role == ROLE_MODERATOR:
            raise UnauthorizedReviewer("该操作需要复核员权限，请提供 X-Reviewer-Token")
        return db.query(Reviewer).filter(Reviewer.token == "local-reviewer").one()
    rv = (db.query(Reviewer)
            .filter(Reviewer.token == token, Reviewer.active == 1)
            .first())
    if rv is None:
        raise UnauthorizedReviewer("审核凭证无效或已停用")
    if required_role == ROLE_MODERATOR and rv.role != ROLE_MODERATOR:
        raise ForbiddenReviewer(f"「{rv.name}」是初审员，该操作需要复核员权限")
    return rv


def register_reviewer(db, *, token: str, name: str, role: str,
                      actor: Reviewer) -> dict:
    """复核员注册新的审核账号（仅 moderator 可调用）。"""
    name = _clean_text(name, 24, "审核员署名")
    token = _clean_text(token, 64, "审核令牌")
    if role not in (ROLE_REVIEWER, ROLE_MODERATOR):
        raise ValidationError(f"未知角色: {role}")
    rv = Reviewer(name=name, token=token, role=role, active=1, created_at=time.time())
    db.add(rv)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ReviewerConflict("审核员署名或令牌已存在")
    return {"id": rv.id, "name": rv.name, "role": rv.role}


# ---------- 事件档案 ----------

def _add_event(db, rec, *, kind: str, actor: str = "",
               actor_role: str = "", detail: Optional[dict] = None,
               appeal=None) -> ChallengeReviewEvent:
    ev = ChallengeReviewEvent(
        submission_id=rec.id,
        appeal_id=appeal.id if appeal is not None else None,
        kind=kind, actor=(actor or "")[:24], actor_role=actor_role,
        detail_json=json.dumps(detail or {}, ensure_ascii=False),
        created_at=time.time())
    db.add(ev)
    return ev


# ---------- 排行榜/解锁联动效果（回滚可观测） ----------

def _snapshot_effects(db) -> dict:
    """操作前快照：各挑战是否在榜（本人最佳 approved）+ 全部挑战解锁状态。"""
    best = _best_approved(db)
    builtin, ch_stars, total, titles = _unlock_context(db)
    unlocked = {}
    for ch in db.query(Challenge).filter(Challenge.status != "archived").all():
        ok, _ = _unlock_state(_rule_of(ch), builtin, ch_stars, total, titles)
        unlocked[ch.id] = ok
    return {"best": best, "unlocked": unlocked, "titles": titles}


def _effects(db, before: dict, rec: ChallengeSubmission, *,
             replayable_before: bool) -> dict:
    """汇总一条成绩操作对排行榜/回放/解锁的联动效果（供回滚审计）。"""
    best_now = _best_approved(db)
    on_board = bool(
        best_now.get(rec.challenge_id)
        and best_now[rec.challenge_id]["record_id"] == rec.id
        and rec.review_status == APPROVED)
    builtin, ch_stars, total, titles = _unlock_context(db)
    unlocked_now, desc = {}, {}
    for ch in db.query(Challenge).filter(Challenge.status != "archived").all():
        ok, why = _unlock_state(_rule_of(ch), builtin, ch_stars, total, titles)
        unlocked_now[ch.id] = ok
        desc[ch.id] = why
    changes = []
    for cid, was_unlocked in before["unlocked"].items():
        if was_unlocked != unlocked_now.get(cid):
            changes.append({
                "challenge_id": cid,
                "title": before["titles"].get(cid, f"#{cid}"),
                "unlocked": unlocked_now[cid],
                "unlock_desc": desc.get(cid),
            })
    return {
        "record_id": rec.id,
        "status": rec.review_status,
        "leaderboard": {
            "on_board": on_board,
            "best_record_id": (best_now.get(rec.challenge_id) or {}).get("record_id"),
        },
        "replayable": {"before": replayable_before,
                       "after": rec.review_status == APPROVED},
        "unlock_changes": changes,
    }


# ---------- 审核 ----------

def review(db, *, record_id: int, action: str, note: str = "",
           reviewer_token: Optional[str] = None) -> dict:
    """初审状态机：pending → approved / rejected（需要初审员权限）。

    申诉重审中的 pending 成绩不能在此直接终审（请走复核裁决接口）。
    幂等：重复同一终审动作返回当前状态（duplicated=True）；
    已终审的成绩再作相反动作 → ReviewConflict(409)。
    """
    if action not in _REVIEW_ACTIONS:
        raise ValidationError(f"未知审核动作: {action}")
    target = _REVIEW_ACTIONS[action]
    rv = authenticate(db, reviewer_token, required_role=ROLE_REVIEWER)
    with _LOCK:
        rec = (db.query(ChallengeSubmission)
                 .filter(ChallengeSubmission.id == record_id)
                 .first())
        if rec is None:
            raise SubmissionNotFound(f"成绩记录不存在: {record_id}")
        before = _snapshot_effects(db)
        replay_before = rec.review_status == APPROVED
        open_appeal = _open_appeal(db, rec.id)
        if rec.review_status == target:
            return _review_response(rec, target, duplicated=True,
                                    actor=rv.name, effects=_effects(
                                        db, before, rec, replayable_before=replay_before))
        if rec.review_status != PENDING:
            raise ReviewConflict(
                f"成绩已审核（{rec.review_status}），不能重复审核")
        if open_appeal is not None:
            raise ReviewConflict(
                "成绩正在申诉复核中，请通过复核裁决（uphold/overturn）处理")
        rec.review_status = target
        rec.review_note = (note or "").strip()[:200]
        rec.reviewed_by = rv.name
        rec.reviewed_at = time.time()
        _add_event(db, rec, kind=EV_REVIEW[target], actor=rv.name,
                   actor_role=ROLE_REVIEWER,
                   detail={"note": rec.review_note})
        db.commit()
        return _review_response(rec, target, duplicated=False,
                                actor=rv.name,
                                effects=_effects(db, before, rec,
                                                 replayable_before=replay_before))


def _review_response(rec, status, *, duplicated: bool, actor: str,
                     effects: Optional[dict] = None) -> dict:
    return {"record_id": rec.id, "challenge_id": rec.challenge_id,
            "version": rec.version, "review_status": status,
            "reviewed_by": actor, "duplicated": duplicated,
            "effects": effects}


def revoke(db, *, record_id: int, note: str = "",
           moderator_token: Optional[str] = None) -> dict:
    """复核员撤销已上榜成绩：approved → revoked。

    自动回滚：立即移出排行榜、关闭轨迹回放，并级联回收该成绩解锁的挑战
    （解锁状态实时求值，后续有其他成绩补上时会自动重新开放）。
    幂等：重复撤销返回 duplicated=True；非 approved 状态 → ReviewConflict(409)。
    """
    rv = authenticate(db, moderator_token, required_role=ROLE_MODERATOR)
    with _LOCK:
        rec = _get_submission(db, record_id)
        before = _snapshot_effects(db)
        replay_before = rec.review_status == APPROVED
        if rec.review_status == REVOKED:
            return _moderation_response(rec, EV_REVOKE, duplicated=True,
                                        actor=rv.name,
                                        effects=_effects(db, before, rec,
                                                         replayable_before=replay_before))
        if rec.review_status != APPROVED:
            raise ReviewConflict(
                f"只有已通过的成绩可以撤销（当前 {rec.review_status}）")
        if _open_appeal(db, rec.id) is not None:
            raise ReviewConflict("成绩正在申诉复核中，不能撤销")
        _reject_terminal(db, rec, status=REVOKED, note=note, reviewer=rv,
                         kind=EV_REVOKE)
        db.commit()
        return _moderation_response(rec, EV_REVOKE, duplicated=False,
                                    actor=rv.name,
                                    effects=_effects(db, before, rec,
                                                     replayable_before=replay_before))


def restore(db, *, record_id: int, note: str = "",
            moderator_token: Optional[str] = None) -> dict:
    """复核员恢复被撤销的成绩：revoked → approved（重新上榜/回放/解锁）。

    幂等：已 approved 返回 duplicated=True；其他状态 → ReviewConflict(409)。
    """
    rv = authenticate(db, moderator_token, required_role=ROLE_MODERATOR)
    with _LOCK:
        rec = _get_submission(db, record_id)
        before = _snapshot_effects(db)
        replay_before = rec.review_status == APPROVED
        if rec.review_status == APPROVED:
            return _moderation_response(rec, EV_RESTORE, duplicated=True,
                                        actor=rv.name,
                                        effects=_effects(db, before, rec,
                                                         replayable_before=replay_before))
        if rec.review_status != REVOKED:
            raise ReviewConflict(
                f"只有已撤销的成绩可以恢复（当前 {rec.review_status}）")
        rec.review_status = APPROVED
        rec.review_note = (note or "").strip()[:200]
        rec.reviewed_by = rv.name
        rec.reviewed_at = time.time()
        _add_event(db, rec, kind=EV_RESTORE, actor=rv.name,
                   actor_role=ROLE_MODERATOR,
                   detail={"note": rec.review_note})
        db.commit()
        return _moderation_response(rec, EV_RESTORE, duplicated=False,
                                    actor=rv.name,
                                    effects=_effects(db, before, rec,
                                                     replayable_before=replay_before))


def _moderation_response(rec, action_kind, *, duplicated: bool, actor: str,
                         effects: dict) -> dict:
    return {"record_id": rec.id, "challenge_id": rec.challenge_id,
            "version": rec.version, "action": action_kind,
            "review_status": rec.review_status,
            "reviewed_by": actor, "duplicated": duplicated,
            "effects": effects}


def _reject_terminal(db, rec, *, status: str, note: str, reviewer: Reviewer,
                     kind: str) -> None:
    rec.review_status = status
    rec.review_note = (note or "").strip()[:200]
    rec.reviewed_by = reviewer.name
    rec.reviewed_at = time.time()
    _add_event(db, rec, kind=kind, actor=reviewer.name,
               actor_role=ROLE_MODERATOR, detail={"note": rec.review_note})


def _get_submission(db, record_id: int) -> ChallengeSubmission:
    rec = (db.query(ChallengeSubmission)
             .filter(ChallengeSubmission.id == record_id)
             .first())
    if rec is None:
        raise SubmissionNotFound(f"成绩记录不存在: {record_id}")
    return rec


def _open_appeal(db, record_id: int) -> Optional[ChallengeAppeal]:
    return (db.query(ChallengeAppeal)
              .filter(ChallengeAppeal.submission_id == record_id,
                      ChallengeAppeal.status == APPEAL_PENDING)
              .first())


# ---------- 玩家申诉 ----------

def create_appeal(db, *, record_id: int, player: str, reason: str,
                  appeal_id: Optional[str]) -> dict:
    """玩家对驳回/撤销的成绩发起申诉（幂等：appeal_id 去重）。

    约束：署名须与提交者一致（仅本人可申诉）；仅 rejected/revoked 可申诉；
    每条成绩最多 MAX_APPEAL_ROUNDS 轮，同时仅一条待裁决申诉。
    受理后成绩回到 pending（标注申诉轮次）进入复核队列，等待复核员裁决。
    """
    player = (player or "").strip()[:24] or DEFAULT_PLAYER
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("申诉理由不能为空")
    if len(reason) > MAX_APPEAL_REASON:
        raise ValidationError(f"申诉理由过长（≤{MAX_APPEAL_REASON} 字）")
    with _LOCK:
        # 幂等重放：同一申诉已受理 → 返回首个结果
        if appeal_id:
            dup = (db.query(ChallengeAppeal)
                     .filter(ChallengeAppeal.appeal_uid == appeal_id)
                     .first())
            if dup is not None:
                return _appeal_response(dup, duplicated=True)
        rec = _get_submission(db, record_id)
        if rec.player != player:
            raise ForbiddenReviewer("只有提交该成绩的玩家本人可以申诉")
        if rec.review_status not in (REJECTED, REVOKED):
            raise AppealConflict(
                f"当前状态（{rec.review_status}）不能申诉，仅驳回/撤销的成绩可申诉")
        if _open_appeal(db, rec.id) is not None:
            raise AppealConflict("该成绩已有进行中的申诉，请等待复核裁决")
        rounds = (db.query(ChallengeAppeal)
                    .filter(ChallengeAppeal.submission_id == rec.id)
                    .count())
        if rounds >= MAX_APPEAL_ROUNDS:
            raise AppealConflict(f"每条成绩最多申诉 {MAX_APPEAL_ROUNDS} 次")
        appeal = ChallengeAppeal(
            appeal_uid=appeal_id or uuid.uuid4().hex,
            submission_id=rec.id, round=rounds + 1,
            from_status=rec.review_status, player=player,
            reason=reason, status=APPEAL_PENDING,
            created_at=time.time())
        db.add(appeal)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            dup = (db.query(ChallengeAppeal)
                     .filter(ChallengeAppeal.appeal_uid == appeal.appeal_uid)
                     .first())
            return _appeal_response(dup, duplicated=True)
        # 成绩回到待复核队列；排行榜/回放/解锁因状态变化自动回滚
        rec.review_status = PENDING
        _add_event(db, rec, kind=EV_APPEAL, actor=player, actor_role="player",
                   detail={"round": appeal.round,
                           "from_status": appeal.from_status,
                           "reason": reason}, appeal=appeal)
        db.commit()
        return _appeal_response(appeal, duplicated=False)


def decide_appeal(db, *, appeal_id_key: int, decision: str, note: str = "",
                  moderator_token: Optional[str] = None) -> dict:
    """复核员裁决申诉：uphold=维持原判（成绩回到 from_status）；
    overturn=推翻原判（改判通过 approved，重新上榜/回放/解锁）。

    幂等：对已裁决申诉重复同一裁决返回 duplicated=True；
    相反裁决 → ReviewConflict(409)。
    """
    if decision not in (APPEAL_UPHOLD, APPEAL_OVERTURN):
        raise ValidationError(f"未知复核裁决: {decision}（uphold/overturn）")
    rv = authenticate(db, moderator_token, required_role=ROLE_MODERATOR)
    with _LOCK:
        appeal = (db.query(ChallengeAppeal)
                    .filter(ChallengeAppeal.id == appeal_id_key)
                    .first())
        if appeal is None:
            raise AppealNotFound(f"申诉记录不存在: {appeal_id_key}")
        rec = _get_submission(db, appeal.submission_id)
        before = _snapshot_effects(db)
        replay_before = rec.review_status == APPROVED
        if appeal.status != APPEAL_PENDING:
            decided = "overturn" if appeal.status == APPEAL_OVERTURN else "uphold"
            if decided == decision:
                return _appeal_decision_response(
                    appeal, rec, decision, duplicated=True, actor=rv.name,
                    effects=_effects(db, before, rec,
                                     replayable_before=replay_before))
            raise ReviewConflict(
                f"申诉已裁决（{decided}），不能改判")
        appeal.status = decision
        appeal.decision_note = (note or "").strip()[:200]
        appeal.decided_by = rv.name
        appeal.decided_at = time.time()
        rec.review_status = (
            APPROVED if decision == APPEAL_OVERTURN else appeal.from_status)
        rec.review_note = appeal.decision_note
        rec.reviewed_by = rv.name
        rec.reviewed_at = time.time()
        _add_event(db, rec, kind=EV_APPEAL_DECISION[decision], actor=rv.name,
                   actor_role=ROLE_MODERATOR,
                   detail={"round": appeal.round,
                           "from_status": appeal.from_status,
                           "note": appeal.decision_note},
                   appeal=appeal)
        db.commit()
        return _appeal_decision_response(
            appeal, rec, decision, duplicated=False, actor=rv.name,
            effects=_effects(db, before, rec,
                             replayable_before=replay_before))


def _appeal_response(ap: ChallengeAppeal, *, duplicated: bool) -> dict:
    return {"saved": True, "duplicated": duplicated,
            "appeal_id": ap.id, "appeal_uid": ap.appeal_uid,
            "record_id": ap.submission_id, "round": ap.round,
            "from_status": ap.from_status, "status": ap.status,
            "review_status": PENDING}


def _appeal_decision_response(ap, rec, decision, *, duplicated: bool,
                              actor: str, effects: dict) -> dict:
    return {"appeal_id": ap.id, "record_id": rec.id,
            "challenge_id": rec.challenge_id, "version": rec.version,
            "decision": decision, "status": decision,
            "review_status": rec.review_status,
            "reviewed_by": actor, "round": ap.round,
            "duplicated": duplicated, "effects": effects}


def list_appeals(db, *, status: str = APPEAL_PENDING,
                 limit: int = 50) -> List[dict]:
    """申诉队列：默认待复核（moderator 工作流）。"""
    q = db.query(ChallengeAppeal)
    if status in (APPEAL_PENDING, APPEAL_UPHOLD, APPEAL_OVERTURN):
        q = q.filter(ChallengeAppeal.status == status)
    rows = (q.order_by(ChallengeAppeal.id.desc())
             .limit(max(1, min(200, limit))).all())
    sub_ids = {a.submission_id for a in rows}
    subs = {s.id: s for s in
            (db.query(ChallengeSubmission)
               .filter(ChallengeSubmission.id.in_(sub_ids)).all())} if sub_ids else {}
    titles = {c.id: c.title for c in db.query(Challenge).all()}
    out = []
    for a in rows:
        s = subs.get(a.submission_id)
        out.append({
            "appeal_id": a.id, "record_id": a.submission_id,
            "challenge_id": s.challenge_id if s else None,
            "challenge_title": titles.get(s.challenge_id, f"#{s.challenge_id}")
                               if s else "",
            "version": s.version if s else None,
            "player": a.player, "round": a.round,
            "from_status": a.from_status, "status": a.status,
            "reason": a.reason, "decision_note": a.decision_note,
            "decided_by": a.decided_by,
            "stars": s.stars if s else None,
            "fuel_used": s.fuel_used if s else None,
            "elapsed_days": s.elapsed_days if s else None,
            "created_at": a.created_at, "decided_at": a.decided_at,
        })
    return out


def player_submissions(db, player: str, *, challenge_id: Optional[int] = None,
                       limit: int = 50) -> List[dict]:
    """按飞行员署名查询本人提交（玩家查看审核状态/入口申诉用）。"""
    player = (player or "").strip()[:24] or DEFAULT_PLAYER
    q = db.query(ChallengeSubmission).filter(ChallengeSubmission.player == player)
    if challenge_id is not None:
        q = q.filter(ChallengeSubmission.challenge_id == challenge_id)
    rows = (q.order_by(ChallengeSubmission.id.desc())
             .limit(max(1, min(200, limit))).all())
    titles = {c.id: c.title for c in db.query(Challenge).all()}
    out = []
    for r in rows:
        ap = _open_appeal(db, r.id)
        rounds = (db.query(ChallengeAppeal)
                    .filter(ChallengeAppeal.submission_id == r.id).count())
        out.append({
            "record_id": r.id, "challenge_id": r.challenge_id,
            "challenge_title": titles.get(r.challenge_id, f"#{r.challenge_id}"),
            "version": r.version, "stars": r.stars,
            "fuel_used": r.fuel_used, "elapsed_days": r.elapsed_days,
            "review_status": r.review_status, "review_note": r.review_note,
            "reviewed_by": r.reviewed_by,
            "appeal_rounds": rounds,
            "appeal_open": ap.id if ap else None,
            "appeal_available": (
                r.review_status in (REJECTED, REVOKED)
                and ap is None and rounds < MAX_APPEAL_ROUNDS),
            "replayable": r.review_status == APPROVED,
            "created_at": r.created_at,
        })
    return out


# ---------- 可追溯时间线 ----------

def submission_timeline(db, record_id: int) -> Optional[dict]:
    """成绩全链路：提交 → 初审 → 申诉 → 复核 → 撤销/恢复（事件档案顺序回放）。"""
    rec = (db.query(ChallengeSubmission)
             .filter(ChallengeSubmission.id == record_id)
             .first())
    if rec is None:
        return None
    ch = db.query(Challenge).filter(Challenge.id == rec.challenge_id).first()
    events = (db.query(ChallengeReviewEvent)
                .filter(ChallengeReviewEvent.submission_id == rec.id)
                .order_by(ChallengeReviewEvent.id)
                .all())
    appeals = (db.query(ChallengeAppeal)
                 .filter(ChallengeAppeal.submission_id == rec.id)
                 .order_by(ChallengeAppeal.id)
                 .all())
    return {
        "record_id": rec.id,
        "challenge_id": rec.challenge_id,
        "challenge_title": ch.title if ch else f"#{rec.challenge_id}",
        "version": rec.version, "player": rec.player,
        "stars": rec.stars, "fuel_used": rec.fuel_used,
        "elapsed_days": rec.elapsed_days,
        "review_status": rec.review_status, "review_note": rec.review_note,
        "reviewed_by": rec.reviewed_by,
        "appeal_rounds": len(appeals),
        "appeal_open": _open_appeal(db, rec.id) is not None,
        "appeal_available": (
            rec.review_status in (REJECTED, REVOKED)
            and _open_appeal(db, rec.id) is None
            and len(appeals) < MAX_APPEAL_ROUNDS),
        "replayable": rec.review_status == APPROVED,
        "created_at": rec.created_at,
        "events": [{
            "seq": i + 1, "kind": e.kind, "actor": e.actor,
            "actor_role": e.actor_role, "appeal_id": e.appeal_id,
            "detail": json.loads(e.detail_json),
            "created_at": e.created_at,
        } for i, e in enumerate(events)],
        "appeals": [{
            "appeal_id": a.id, "round": a.round, "from_status": a.from_status,
            "reason": a.reason, "status": a.status,
            "decision_note": a.decision_note, "decided_by": a.decided_by,
            "created_at": a.created_at, "decided_at": a.decided_at,
        } for a in appeals],
    }


def list_submissions(db, *, challenge_id: Optional[int] = None,
                     status: str = "pending", limit: int = 50) -> List[dict]:
    """成绩提交列表：默认待审核队列（含申诉重审，附 appeal 标记），可按挑战过滤。"""
    q = db.query(ChallengeSubmission)
    if challenge_id is not None:
        q = q.filter(ChallengeSubmission.challenge_id == challenge_id)
    if status in ("pending", "approved", "rejected", "revoked"):
        q = q.filter(ChallengeSubmission.review_status == status)
    rows = (q.order_by(ChallengeSubmission.id.desc())
             .limit(max(1, min(200, limit)))
             .all())
    titles = {c.id: c.title for c in db.query(Challenge).all()}
    result = []
    for r in rows:
        ap = _open_appeal(db, r.id)
        rounds = (db.query(ChallengeAppeal)
                    .filter(ChallengeAppeal.submission_id == r.id).count())
        result.append({
            "record_id": r.id,
            "challenge_id": r.challenge_id,
            "challenge_title": titles.get(r.challenge_id, f"#{r.challenge_id}"),
            "version": r.version,
            "player": r.player,
            "stars": r.stars,
            "fuel_used": r.fuel_used,
            "elapsed_days": r.elapsed_days,
            "review_status": r.review_status,
            "review_note": r.review_note,
            "reviewed_by": r.reviewed_by,
            "appeal_open": ap.id if ap else None,
            "appeal_round": ap.round if ap else None,
            "appeal_reason": ap.reason if ap else None,
            "appeal_rounds_total": rounds,
            "created_at": r.created_at,
        })
    return result


# ---------- 排行榜与回放 ----------

def leaderboard(db, challenge_id: int, version: Optional[int] = None,
                limit: int = 50) -> dict:
    """排行榜：仅审核通过的成绩，按版本结算，每名玩家取最佳一条。

    排序：星数 → 燃料 → 用时 → 先到优先。
    """
    ch = _get_challenge(db, challenge_id)
    v_no = version or ch.current_version
    _get_version(db, challenge_id, v_no)  # 版本必须存在
    rows = (db.query(ChallengeSubmission)
              .filter(ChallengeSubmission.challenge_id == challenge_id,
                      ChallengeSubmission.version == v_no,
                      ChallengeSubmission.review_status == "approved")
              .order_by(ChallengeSubmission.stars.desc(),
                        ChallengeSubmission.fuel_used.asc(),
                        ChallengeSubmission.elapsed_days.asc(),
                        ChallengeSubmission.id.asc())
              .all())
    entries, seen = [], set()
    for r in rows:
        if r.player in seen:
            continue
        seen.add(r.player)
        entries.append({
            "rank": len(entries) + 1,
            "record_id": r.id,
            "player": r.player,
            "stars": r.stars,
            "fuel_used": r.fuel_used,
            "elapsed_days": r.elapsed_days,
            "replayable": True,
            "created_at": r.created_at,
        })
        if len(entries) >= max(1, min(100, limit)):
            break
    return {"challenge_id": challenge_id, "version": v_no, "entries": entries}


def submission_detail(db, record_id: int) -> Optional[dict]:
    """成绩详情：审核通过后附带动作方案与轨迹（供回放）；否则只回元信息。"""
    rec = (db.query(ChallengeSubmission)
             .filter(ChallengeSubmission.id == record_id)
             .first())
    if rec is None:
        return None
    open_ap = _open_appeal(db, rec.id)
    rounds = (db.query(ChallengeAppeal)
                .filter(ChallengeAppeal.submission_id == rec.id).count())
    detail = {
        "record_id": rec.id,
        "challenge_id": rec.challenge_id,
        "version": rec.version,
        "player": rec.player,
        "stars": rec.stars,
        "fuel_used": rec.fuel_used,
        "elapsed_days": rec.elapsed_days,
        "review_status": rec.review_status,
        "review_note": rec.review_note,
        "reviewed_by": rec.reviewed_by,
        "appeal_rounds": rounds,
        "appeal_open": open_ap.id if open_ap else None,
        "appeal_available": (
            rec.review_status in (REJECTED, REVOKED)
            and open_ap is None and rounds < MAX_APPEAL_ROUNDS),
        "replayable": rec.review_status == "approved",
        "created_at": rec.created_at,
    }
    if detail["replayable"]:
        run = (db.query(ChallengeRun)
                 .filter(ChallengeRun.id == rec.run_id)
                 .first())
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
