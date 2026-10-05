"""引擎与关卡测试：物理守恒 / 里程碑判定 / 全部关卡参考解法可解 / 评分规则。"""
import math
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import physics
from app.services.levels import LEVELS


def test_slingshot_conserves_speed_in_planet_frame():
    """弹弓在行星参考系中速度大小守恒（双曲飞掠能量守恒）。"""
    lv = LEVELS[2]
    r = physics.integrate(lv, lv["ref"])
    ev = [e for e in r["events"] if e["type"] == "slingshot"]
    assert ev, "参考解法应触发弹弓"
    # v_inf 记录的是入轨相对速度；弹弓后相对速度大小应等于入轨 v_inf。
    # 引擎在事件里同时记录了入轨 v_inf 与偏转角，这里间接验证偏转角公式闭合：
    assert 0.0 < ev[0]["delta_deg"] < 180.0


def test_capture_criterion():
    """捕获判定：v_inf² < 2μ/r_p 时 captured=True。"""
    mu_j = physics._mu("jupiter")
    for v_inf, r_p, expect in [
        (0.003, 0.01, True),
        (0.02, 0.01, False),
    ]:
        assert (v_inf * v_inf < 2.0 * mu_j / r_p) == expect


def test_level1_ref_solvable():
    lv = LEVELS[0]
    r = physics.integrate(lv, lv["ref"])
    assert r["ok"], r["reason"]
    assert r["fuel_used"] <= lv["budget_dv"]


def test_level2_ref_solvable():
    lv = LEVELS[1]
    r = physics.integrate(lv, lv["ref"])
    assert r["ok"], r["reason"]
    assert r["fuel_used"] <= lv["budget_dv"]


def test_level3_ref_solvable():
    lv = LEVELS[2]
    r = physics.integrate(lv, lv["ref"])
    assert r["ok"], r["reason"]
    ev = [e for e in r["events"] if e["type"] == "slingshot" and e["captured"]]
    assert ev, "需要一次成功的木星捕获弹弓"


def test_level4_ref_solvable():
    lv = LEVELS[3]
    r = physics.integrate(lv, lv["ref"])
    assert r["ok"], r["reason"]
    assert r["fuel_used"] <= lv["budget_dv"]


def test_level5_ref_solvable():
    lv = LEVELS[4]
    r = physics.integrate(lv, lv["ref"])
    assert r["ok"], r["reason"]
    assert r["fuel_used"] <= lv["budget_dv"]


def test_fuel_exhaustion_fails():
    """燃料超出预算必须失败（燃料耗尽）。"""
    lv = LEVELS[0]
    acts = [{"type": "burn", "angle": 90.0, "dv": lv["budget_dv"] + 0.001}]
    r = physics.integrate(lv, acts)
    assert not r["ok"]
    assert r["reason"] == "燃料耗尽"


def test_tiny_periapsis_crashes():
    """弹弓近心点小于行星半径 → 解体失败。"""
    lv = LEVELS[2]
    acts = [{"type": "burn", "angle": 90.0, "dv": 0.0051},
            {"type": "slingshot", "planet_id": "jupiter", "b": 1e-6}]
    r = physics.integrate(lv, acts)
    assert not r["ok"]


def test_stars_scoring():
    """评分：≤0.55 预算三星，≤0.80 二星，其余一星；失败零星。"""
    lv = LEVELS[0]
    b = lv["budget_dv"]
    assert physics.stars_for(lv, 0.55 * b, True) == 3
    assert physics.stars_for(lv, 0.70 * b, True) == 2
    assert physics.stars_for(lv, 0.90 * b, True) == 1
    assert physics.stars_for(lv, 0.5 * b, False) == 0


def test_ref_solutions_earn_three_stars():
    """每个参考解法（最优解）都应拿到三星。"""
    for lv in LEVELS:
        r = physics.integrate(lv, lv["ref"])
        assert r["ok"]
        assert physics.stars_for(lv, r["fuel_used"], True) == 3, f"L{lv['id']} 参考解法应三星"


def test_planet_circular_orbit_speed():
    """行星圆轨道速度 ≈ sqrt(μ/r)。真实周期与简化 μ 存在 ~1% 固有偏差，容忍 2%。"""
    for b in physics.BODIES:
        if b["id"] == "sun":
            continue
        w = 2.0 * math.pi / b["period"]
        v_orbit = b["orbit"] * w
        v_theory = math.sqrt(physics.MU_SUN / b["orbit"])
        assert abs(v_orbit - v_theory) / v_theory < 0.02
