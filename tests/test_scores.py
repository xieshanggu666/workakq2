"""成绩记录与回放测试：执行落库 / 幂等提交 / 并发安全 / 旧存档兼容 / 记录查询。"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# 测试使用独立数据库文件（须在导入 app 模块前设置）
os.environ["SLINGSHOT_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="slingshot_test_"), "test.db")

import pytest
from fastapi import HTTPException

from app.core.database import Base, SessionLocal, engine
from app.models import LevelScore, RunRecord, ScoreRecord
from app.api import router as api


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        for t in (ScoreRecord, RunRecord, LevelScore):
            db.query(t).delete()
        db.commit()
        yield
    finally:
        db.close()


def _run_level1():
    """通过 API 执行一次第 1 关参考解法（三星）。"""
    req = api.SimRequest(level_id=1, actions=[
        api.Action(type="burn", angle=90.0, dv=0.0017)])
    return api.run(req)


def _best(level_id):
    db = SessionLocal()
    try:
        return db.query(LevelScore).filter(LevelScore.level_id == level_id).first()
    finally:
        db.close()


# ---------- 任务执行落库（可溯源） ----------

def test_run_persists_traceable_record():
    r = _run_level1()
    assert r["ok"] and r["run_id"]
    db = SessionLocal()
    run = db.query(RunRecord).filter(RunRecord.run_uid == r["run_id"]).one()
    assert run.level_id == 1 and run.stars == r["stars"] == 3
    assert run.trajectory_json.startswith("[")
    assert '"burn"' in run.actions_json
    db.close()


def test_score_linked_to_run_uses_server_values():
    """成绩关联执行档案：客户端伪造的数值被忽略，以服务端结算为准。"""
    r = _run_level1()
    resp = api.save_score(api.ScoreRequest(
        level_id=1, run_id=r["run_id"], submission_id="sub-1",
        stars=1, fuel_used=999.0, elapsed_days=0.0))
    assert resp["saved"] and not resp["duplicated"]
    assert resp["stars"] == r["stars"]
    db = SessionLocal()
    rec = db.query(ScoreRecord).one()
    assert rec.source == "run" and rec.run_id is not None
    assert rec.stars == r["stars"] and abs(rec.fuel_used - r["fuel_used"]) < 1e-9
    db.close()
    assert _best(1).stars == r["stars"]


def test_score_rejects_bad_run():
    with pytest.raises(HTTPException) as e1:
        api.save_score(api.ScoreRequest(level_id=1, run_id="nonexistent"))
    assert e1.value.status_code == 404
    r = _run_level1()
    with pytest.raises(HTTPException) as e2:  # 执行档案属于第 1 关，不能记到第 2 关
        api.save_score(api.ScoreRequest(level_id=2, run_id=r["run_id"]))
    assert e2.value.status_code == 400
    with pytest.raises(HTTPException) as e3:
        api.save_score(api.ScoreRequest(level_id=99, stars=1))
    assert e3.value.status_code == 404


# ---------- 幂等与并发 ----------

def test_duplicate_submission_idempotent():
    r = _run_level1()
    req = api.ScoreRequest(level_id=1, run_id=r["run_id"], submission_id="dup-1")
    a = api.save_score(req)
    b = api.save_score(req)
    assert a["record_id"] == b["record_id"]
    assert b["duplicated"] is True
    db = SessionLocal()
    assert db.query(ScoreRecord).count() == 1
    db.close()


def test_concurrent_duplicate_submissions():
    """同一提交并发到达（双击/重试/多标签页）：只落库一条，结果一致。"""
    r = _run_level1()
    results = []

    def worker():
        results.append(api.save_score(api.ScoreRequest(
            level_id=1, run_id=r["run_id"], submission_id="race-1")))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(x["saved"] for x in results)
    assert len({x["record_id"] for x in results}) == 1
    db = SessionLocal()
    assert db.query(ScoreRecord).count() == 1
    db.close()
    assert _best(1).stars == r["stars"]


def test_concurrent_mixed_submissions_best_monotonic():
    """并发提交不同成绩：全部落库，最佳汇总收敛到最高星且不被回退。"""

    def worker(i):
        api.save_score(api.ScoreRequest(
            level_id=2, submission_id=f"mix-{i}", stars=i % 4,
            fuel_used=0.005, elapsed_days=800.0))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db = SessionLocal()
    assert db.query(ScoreRecord).filter(ScoreRecord.level_id == 2).count() == 12
    db.close()
    assert _best(2).stars == 3


def test_same_idempotency_key_reused_another_level_is_independent():
    """同一幂等键用于不同关卡：各自独立落库，成绩不串关卡。"""
    r1 = _run_level1()
    req1 = api.ScoreRequest(level_id=1, run_id=r1["run_id"], submission_id="shared-key")
    a = api.save_score(req1)
    # 第 2 关复用同一键（旧版客户端字段）：应独立受理而非返回第 1 关记录
    b = api.save_score(api.ScoreRequest(
        level_id=2, submission_id="shared-key", stars=2,
        fuel_used=0.005, elapsed_days=800.0))
    assert a["record_id"] != b["record_id"]
    assert a["level_id"] == 1 and b["level_id"] == 2
    db = SessionLocal()
    assert db.query(ScoreRecord).count() == 2
    db.close()
    # 各关最佳互不影响，回放各自的执行档案
    assert _best(1).stars == r1["stars"] and _best(2).stars == 2
    da = api.record_detail(a["record_id"])
    db_detail = api.record_detail(b["record_id"])
    assert da["level_id"] == 1 and da["replayable"]
    assert db_detail["level_id"] == 2 and not db_detail["replayable"]
    # 同键对同关卡重放仍幂等
    again = api.save_score(req1)
    assert again["duplicated"] and again["record_id"] == a["record_id"]


def test_same_key_same_level_different_run_conflicts():
    """同关卡内同键换一条执行档案 → 409，不能并成一条成绩。"""
    r1 = _run_level1()
    r2 = _run_level1()
    api.save_score(api.ScoreRequest(level_id=1, run_id=r1["run_id"], submission_id="k-1"))
    with pytest.raises(HTTPException) as e:
        api.save_score(api.ScoreRequest(level_id=1, run_id=r2["run_id"],
                                        submission_id="k-1"))
    assert e.value.status_code == 409
    db = SessionLocal()
    assert db.query(ScoreRecord).count() == 1
    db.close()


def test_cross_level_same_key_concurrent_submissions():
    """不同关卡用同一幂等键并发提交：各落各的，互不串。"""
    results = {}

    def worker(level_id):
        results[level_id] = api.save_score(api.ScoreRequest(
            level_id=level_id, submission_id="race-shared",
            stars=2 if level_id == 2 else 1,
            fuel_used=0.005, elapsed_days=800.0))

    threads = []
    for lid in (1, 2):
        for _ in range(4):
            threads.append(threading.Thread(target=worker, args=(lid,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results[1]["level_id"] == 1 and results[2]["level_id"] == 2
    assert results[1]["record_id"] != results[2]["record_id"]
    db = SessionLocal()
    assert db.query(ScoreRecord).filter(ScoreRecord.level_id == 1).count() == 1
    assert db.query(ScoreRecord).filter(ScoreRecord.level_id == 2).count() == 1
    db.close()


# ---------- 旧存档兼容 ----------

def test_legacy_score_request_compatible():
    """旧版客户端请求格式（无 run_id / submission_id）照常受理。"""
    resp = api.save_score(api.ScoreRequest(
        level_id=1, stars=2, fuel_used=0.003, elapsed_days=500.0))
    assert resp["saved"] and resp["stars"] == 2
    db = SessionLocal()
    rec = db.query(ScoreRecord).one()
    assert rec.source == "legacy" and rec.run_id is None
    db.close()
    best = _best(1)
    assert (best.stars, best.fuel_used) == (2, 0.003)


def test_old_save_data_still_works():
    """旧存档（level_score 已有数据、无成绩记录）可读取、可单调更新。"""
    db = SessionLocal()
    db.add(LevelScore(level_id=3, stars=2, fuel_used=0.008, elapsed_days=1500.0))
    db.commit()
    db.close()
    info = api.system_info()
    s3 = info["scores"][3]
    assert s3["stars"] == 2 and s3["fuel_used"] == 0.008
    assert "record_id" not in s3  # 旧存档没有可回放记录
    # 更差的成绩不回退
    api.save_score(api.ScoreRequest(level_id=3, stars=1, fuel_used=0.009,
                                    elapsed_days=1600.0))
    best = _best(3)
    assert best.stars == 2 and best.fuel_used == 0.008
    # 更好的成绩正常更新
    api.save_score(api.ScoreRequest(level_id=3, stars=3, fuel_used=0.007,
                                    elapsed_days=1400.0))
    best = _best(3)
    assert best.stars == 3 and best.fuel_used == 0.007


def test_same_stars_lower_fuel_improves():
    api.save_score(api.ScoreRequest(level_id=4, stars=2, fuel_used=0.008,
                                    elapsed_days=900.0))
    resp = api.save_score(api.ScoreRequest(level_id=4, stars=2, fuel_used=0.006,
                                           elapsed_days=900.0))
    assert resp["improved"] is True
    assert _best(4).fuel_used == 0.006


# ---------- 记录查询与回放 ----------

def test_record_detail_replay():
    r = _run_level1()
    resp = api.save_score(api.ScoreRequest(
        level_id=1, run_id=r["run_id"], submission_id="s1"))
    detail = api.record_detail(resp["record_id"])
    assert detail["replayable"] and detail["ok"]
    assert detail["stars"] == r["stars"]
    assert detail["trajectory"] and detail["actions"][0]["type"] == "burn"
    assert detail["events"] and detail["milestones"]
    # /api/system 给该关挂上可回放记录 id（前端回放入口）
    info = api.system_info()
    assert info["scores"][1]["record_id"] == resp["record_id"]
    # 追溯列表
    recs = api.score_records(1)["records"]
    assert len(recs) == 1 and recs[0]["replayable"]


def test_record_detail_missing():
    with pytest.raises(HTTPException) as e:
        api.record_detail(9999)
    assert e.value.status_code == 404
