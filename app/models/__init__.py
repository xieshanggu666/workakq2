import time

from sqlalchemy import (Column, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint)

from app.core.database import Base


class LevelScore(Base):
    """最佳成绩汇总（旧存档表）。结构保持不变，历史存档可直接沿用。"""
    __tablename__ = "level_score"
    __table_args__ = (UniqueConstraint("level_id", name="uq_level"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    level_id = Column(Integer, nullable=False, index=True)
    stars = Column(Integer, nullable=False, default=0)
    fuel_used = Column(Float, nullable=False, default=0.0)
    elapsed_days = Column(Float, nullable=False, default=0.0)


class RunRecord(Base):
    """一次任务执行的完整档案：动作方案 + 结算结果 + 轨迹。

    /api/run 每次执行落库一条，成绩记录通过外键关联到它，
    使每条最佳成绩都能溯源到产生它的那次发射，并支持轨迹回放。
    """
    __tablename__ = "run_record"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_uid = Column(String(36), nullable=False, unique=True, index=True)
    level_id = Column(Integer, nullable=False, index=True)
    ok = Column(Integer, nullable=False, default=0)
    reason = Column(String(64), nullable=False, default="")
    stars = Column(Integer, nullable=False, default=0)
    fuel_used = Column(Float, nullable=False, default=0.0)
    elapsed_days = Column(Float, nullable=False, default=0.0)
    actions_json = Column(Text, nullable=False, default="[]")
    trajectory_json = Column(Text, nullable=False, default="[]")
    events_json = Column(Text, nullable=False, default="[]")
    milestones_json = Column(Text, nullable=False, default="[]")
    created_at = Column(Float, nullable=False, default=time.time)


class ScoreRecord(Base):
    """一条成绩提交记录。

    submission_id 是客户端生成的幂等键：同一提交（双击/重试/多标签页）
    重复到达时只落库一次，返回首个结果。
    """
    __tablename__ = "score_record"

    id = Column(Integer, primary_key=True, autoincrement=True)
    submission_id = Column(String(64), nullable=False, unique=True, index=True)
    run_id = Column(Integer, ForeignKey("run_record.id"), nullable=True, index=True)
    level_id = Column(Integer, nullable=False, index=True)
    stars = Column(Integer, nullable=False, default=0)
    fuel_used = Column(Float, nullable=False, default=0.0)
    elapsed_days = Column(Float, nullable=False, default=0.0)
    source = Column(String(16), nullable=False, default="run")  # run=关联执行 / legacy=旧版客户端
    created_at = Column(Float, nullable=False, default=time.time)


class Challenge(Base):
    """社区航线挑战：设计者发布的关卡系列（内容版本化，逐版本结算）。"""
    __tablename__ = "challenge"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(40), nullable=False)
    author = Column(String(24), nullable=False, default="匿名设计者")
    status = Column(String(16), nullable=False, default="published")  # published / archived
    current_version = Column(Integer, nullable=False, default=1)
    unlock_rule_json = Column(Text, nullable=False, default="")  # 空串 = 无解锁条件
    created_at = Column(Float, nullable=False, default=time.time)


class ChallengeVersion(Base):
    """挑战关卡的不可变版本：燃料预算 + 里程碑 + 时间限制。

    每次发布生成新版本；旧版本保留供成绩溯源与回放，不可修改。
    """
    __tablename__ = "challenge_version"
    __table_args__ = (UniqueConstraint("challenge_id", "version", name="uq_challenge_version"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    challenge_id = Column(Integer, ForeignKey("challenge.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    name = Column(String(40), nullable=False, default="")
    brief = Column(String(500), nullable=False, default="")
    hint = Column(String(500), nullable=False, default="")
    budget_dv = Column(Float, nullable=False)
    t_max = Column(Float, nullable=False)
    milestones_json = Column(Text, nullable=False, default="[]")
    created_at = Column(Float, nullable=False, default=time.time)


class ChallengeRun(Base):
    """一次挑战飞行的执行档案：动作方案 + 服务端结算 + 轨迹。

    与 RunRecord 同构，但锚定挑战版本（challenge_id + version），
    成绩提交与回放都通过它溯源。
    """
    __tablename__ = "challenge_run"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_uid = Column(String(36), nullable=False, unique=True, index=True)
    challenge_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    ok = Column(Integer, nullable=False, default=0)
    reason = Column(String(64), nullable=False, default="")
    stars = Column(Integer, nullable=False, default=0)
    fuel_used = Column(Float, nullable=False, default=0.0)
    elapsed_days = Column(Float, nullable=False, default=0.0)
    actions_json = Column(Text, nullable=False, default="[]")
    trajectory_json = Column(Text, nullable=False, default="[]")
    events_json = Column(Text, nullable=False, default="[]")
    milestones_json = Column(Text, nullable=False, default="[]")
    created_at = Column(Float, nullable=False, default=time.time)


class ChallengeSubmission(Base):
    """挑战成绩提交记录：幂等键去重 + 审核状态机。

    submission_id 是客户端生成的幂等键（双击/重试/多标签页只结算一次）。
    审核状态 pending → approved/rejected；只有 approved 的成绩才进入排行榜、
    开放轨迹回放，并计入关卡解锁条件。
    """
    __tablename__ = "challenge_submission"

    id = Column(Integer, primary_key=True, autoincrement=True)
    submission_id = Column(String(64), nullable=False, unique=True, index=True)
    run_id = Column(Integer, ForeignKey("challenge_run.id"), nullable=False, index=True)
    challenge_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    player = Column(String(24), nullable=False, default="匿名飞行员")
    stars = Column(Integer, nullable=False, default=0)
    fuel_used = Column(Float, nullable=False, default=0.0)
    elapsed_days = Column(Float, nullable=False, default=0.0)
    review_status = Column(String(16), nullable=False, default="pending", index=True)
    # pending=待审(含申诉重审) approved=已通过(上榜/回放/解锁) rejected=已驳回 revoked=已撤销上榜
    review_note = Column(String(200), nullable=False, default="")
    reviewed_by = Column(String(24), nullable=True)  # 终审审核员署名（旧存档为空=历史审核）
    reviewed_at = Column(Float, nullable=True)
    created_at = Column(Float, nullable=False, default=time.time)


class Reviewer(Base):
    """审核权限账号：reviewer=初审员（通过/驳回），moderator=复核员
    （申诉裁决、撤销/恢复上榜成绩、注册审核员）。

    本地单机内置两个账号（见 challenges.bootstrap），token 随
    X-Reviewer-Token 请求头上送；停用（active=0）后凭证立即失效。
    """
    __tablename__ = "challenge_reviewer"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(24), nullable=False, unique=True)
    token = Column(String(64), nullable=False, unique=True, index=True)
    role = Column(String(16), nullable=False, default="reviewer")  # reviewer / moderator
    active = Column(Integer, nullable=False, default=1)
    created_at = Column(Float, nullable=False, default=time.time)


class ChallengeAppeal(Base):
    """玩家申诉单：对已驳回(rejected)/已撤销(revoked)成绩发起的复核请求。

    appeal_uid 为客户端生成的幂等键（双击/重试只受理一次）；每条成绩最多
    MAX_APPEAL_ROUNDS 轮、同时仅一条待裁决申诉。申诉期间成绩回到 pending
    进入复核队列；复核维持(uphold)回到 from_status，推翻(overturn)改判通过。
    """
    __tablename__ = "challenge_appeal"

    id = Column(Integer, primary_key=True, autoincrement=True)
    appeal_uid = Column(String(64), nullable=False, unique=True, index=True)
    submission_id = Column(Integer, ForeignKey("challenge_submission.id"),
                           nullable=False, index=True)
    round = Column(Integer, nullable=False, default=1)  # 第几轮申诉（1、2）
    from_status = Column(String(16), nullable=False)    # 申诉时成绩状态（uphold 时回到它）
    player = Column(String(24), nullable=False, default="匿名飞行员")
    reason = Column(String(500), nullable=False, default="")
    # pending=待复核 approved=复核推翻原判(改判通过) rejected=复核维持原判
    status = Column(String(16), nullable=False, default="pending", index=True)
    decision_note = Column(String(200), nullable=False, default="")
    decided_by = Column(String(24), nullable=True)
    created_at = Column(Float, nullable=False, default=time.time)
    decided_at = Column(Float, nullable=True)


class ChallengeReviewEvent(Base):
    """审核链路事件档案（只追加、不可变）：提交/初审/申诉/复核/撤销/恢复
    每一次状态迁移落一条，串起玩家提交 → 审核权限 → 排行榜/解锁回滚的全链路。

    旧存档中已审核但没有事件的成绩，启动时补登一条 kind=legacy 的事件。
    """
    __tablename__ = "challenge_review_event"

    id = Column(Integer, primary_key=True, autoincrement=True)
    submission_id = Column(Integer, ForeignKey("challenge_submission.id"),
                           nullable=False, index=True)
    appeal_id = Column(Integer, ForeignKey("challenge_appeal.id"),
                       nullable=True, index=True)
    # submit/review_approve/review_reject/appeal/appeal_uphold/appeal_overturn/
    # revoke/restore/legacy
    kind = Column(String(24), nullable=False)
    actor = Column(String(24), nullable=False, default="")
    actor_role = Column(String(16), nullable=False, default="")  # player/reviewer/moderator/system
    detail_json = Column(Text, nullable=False, default="{}")
    created_at = Column(Float, nullable=False, default=time.time)
