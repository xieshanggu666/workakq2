"""引力跳板核心引擎：简化 N 体轨道积分 + 引力弹弓（双曲飞掠）+ 里程碑判定。

单位约定：距离 AU，时间 天，速度 AU/天，标准引力参数 μ 单位 AU³/天²。
弹弓事件在探测器进入行星影响球（SOI）的瞬间按双曲飞掠公式瞬时结算：
  入轨相对速度 v_inf，近心点 r_p，偏转角 δ = 2·asin(1 / (1 + r_p·v_inf²/μ))。
行星之间互不摄动（圆轨道），探测器在弹弓事件之间仅受太阳引力（RK4 积分）。
"""
from __future__ import annotations

import math
from typing import Dict, List, Sequence

MU_SUN = 4.0 * math.pi * math.pi / (365.25 * 365.25)   # ≈ 2.9591e-4 AU³/天²
AU_PER_KM_S = 86400.0 / 1.496e8                        # 1 km/s = 5.775e-4 AU/天

# 行星数据：轨道半径/周期/质量比/半径（AU）均取真实太阳系参数
BODIES = [
    {"id": "sun",    "name": "太阳", "color": "#ffd166", "radius": 0.00465,
     "orbit": 0.0, "period": 0.0, "mass_ratio": 1.0, "theta0": 0.0},
    {"id": "venus",  "name": "金星", "color": "#f4a261", "radius": 4.05e-5,
     "orbit": 0.723, "period": 224.70, "mass_ratio": 2.447e-6, "theta0": 2.40},
    {"id": "earth",  "name": "地球", "color": "#4cc9f0", "radius": 4.26e-5,
     "orbit": 1.0, "period": 365.25, "mass_ratio": 3.003e-6, "theta0": 0.0},
    {"id": "mars",   "name": "火星", "color": "#ef8354", "radius": 2.27e-5,
     "orbit": 1.524, "period": 686.98, "mass_ratio": 3.227e-7, "theta0": 0.75},
    {"id": "jupiter", "name": "木星", "color": "#e8b4b8", "radius": 4.78e-4,
     "orbit": 5.204, "period": 4332.59, "mass_ratio": 9.545e-4, "theta0": 1.70},
    {"id": "saturn", "name": "土星", "color": "#f7d9a8", "radius": 4.03e-4,
     "orbit": 9.582, "period": 10759.2, "mass_ratio": 2.857e-4, "theta0": 4.18},
]

BODY_BY_ID = {b["id"]: b for b in BODIES}
_PLANETS = [b for b in BODIES if b["id"] != "sun"]


def _mu(body_id: str) -> float:
    return MU_SUN * BODY_BY_ID[body_id]["mass_ratio"]


def _soi_radius(body_id: str) -> float:
    b = BODY_BY_ID[body_id]
    return b["orbit"] * b["mass_ratio"] ** 0.4


def planet_position(body_id: str, t: float) -> tuple[float, float]:
    b = BODY_BY_ID[body_id]
    ang = b["theta0"] + 2.0 * math.pi * t / b["period"]
    return (b["orbit"] * math.cos(ang), b["orbit"] * math.sin(ang))


def planet_velocity(body_id: str, t: float) -> tuple[float, float]:
    b = BODY_BY_ID[body_id]
    ang = b["theta0"] + 2.0 * math.pi * t / b["period"]
    w = 2.0 * math.pi / b["period"]
    # d/dt [a·cos, a·sin] = [-a·w·sin, a·w·cos]
    return (-b["orbit"] * w * math.sin(ang), b["orbit"] * w * math.cos(ang))


def _acc(x: float, y: float) -> tuple[float, float]:
    r2 = x * x + y * y
    r = math.sqrt(r2)
    f = -MU_SUN / (r2 * r)
    return (x * f, y * f)


def _rk4_step(x, y, vx, vy, dt: float):
    ax1, ay1 = _acc(x, y)
    kx2 = vx + 0.5 * dt * ax1
    ky2 = vy + 0.5 * dt * ay1
    ax2, ay2 = _acc(x + 0.5 * dt * kx2, y + 0.5 * dt * ky2)
    kx3 = vx + 0.5 * dt * ax2
    ky3 = vy + 0.5 * dt * ay2
    ax3, ay3 = _acc(x + 0.5 * dt * kx3, y + 0.5 * dt * ky3)
    kx4 = vx + dt * ax3
    ky4 = vy + dt * ay3
    ax4, ay4 = _acc(x + dt * kx4, y + dt * ky4)
    return (
        x + dt / 6.0 * (kx2 * 2 + kx3 * 2 + kx4 + vx),
        y + dt / 6.0 * (ky2 * 2 + ky3 * 2 + ky4 + vy),
        vx + dt / 6.0 * (ax1 + ax2 * 2 + ax3 * 2 + ax4),
        vy + dt / 6.0 * (ay1 + ay2 * 2 + ay3 * 2 + ay4),
    )


def _slingshot_delta(v_inf: float, r_p: float, mu: float) -> float:
    """双曲飞掠偏转角（弧度）。"""
    e = 1.0 + r_p * v_inf * v_inf / mu
    return 2.0 * math.asin(min(1.0, 1.0 / e))


def integrate(level: dict, actions: Sequence[dict],
              dt: float = 0.5, max_points: int = 800) -> dict:
    """按动作序列积分整条任务，返回完整结算结果。

    动作顺序执行：coast 让后续动作最早在 N 天后可执行；burn 到点立即点火；
    slingshot 保持挂起直到进入目标行星影响球（SOI）才瞬间结算双曲飞掠。
    """
    t = 0.0
    start_pos, start_vel = _level_start(level)
    x, y = start_pos
    vx, vy = start_vel
    fuel_used = 0.0
    budget = level["budget_dv"]
    t_max = level["t_max"]

    traj = [(x, y, t)]
    events: List[dict] = []
    milestones: List[dict] = []
    outcome = None

    pending = 0                     # 下一个待执行动作下标
    arm_time = 0.0                  # 当前动作最早可执行时刻
    last_trigger = {}               # planet_id -> 最近弹弓时刻（避免同行星连发）
    step = 0
    while t < t_max and outcome is None:
        # ---- 顺序执行动作 ----
        if pending < len(actions):
            a = actions[pending]
            if a["type"] == "coast":
                arm_time = max(arm_time, t) + max(0.0, a.get("days", 0.0))
                pending += 1
            elif a["type"] == "burn" and t >= arm_time - 1e-9:
                dv = max(0.0, a.get("dv", 0.0))
                if dv > 0:
                    if fuel_used + dv > budget + 1e-12:
                        outcome = {"ok": False, "reason": "燃料耗尽"}
                        break
                    fuel_used += dv
                    ang = math.radians(a.get("angle", 0.0))
                    vx += dv * math.cos(ang)
                    vy += dv * math.sin(ang)
                    events.append({"type": "burn", "t": t, "dv": dv,
                                   "angle": a.get("angle", 0.0)})
                pending += 1
            elif a["type"] == "slingshot" and t >= arm_time - 1e-9:
                pid = a.get("planet_id")
                r_p = abs(a.get("b", 0.01))
                if r_p < BODY_BY_ID[pid]["radius"] * 1.1:
                    outcome = {"ok": False, "reason": "弹弓近心点过小，探测器解体"}
                    break
                px, py = planet_position(pid, t)
                d = math.hypot(x - px, y - py)
                if d <= _soi_radius(pid) and t - last_trigger.get(pid, -1e9) > 10.0:
                    pvx, pvy = planet_velocity(pid, t)
                    vxr, vyr = vx - pvx, vy - pvy
                    v_inf = math.hypot(vxr, vyr)
                    mu = _mu(pid)
                    delta = _slingshot_delta(v_inf, r_p, mu)
                    s = 1.0 if a.get("b", 0.0) >= 0 else -1.0
                    c, sn = math.cos(s * delta), math.sin(s * delta)
                    nvx, nvy = c * vxr - sn * vyr, sn * vxr + c * vyr
                    vx, vy = pvx + nvx, pvy + nvy
                    events.append({
                        "type": "slingshot", "t": t, "planet_id": pid,
                        "r_p": r_p, "v_inf": v_inf,
                        "delta_deg": round(math.degrees(delta), 1),
                        "captured": v_inf * v_inf < 2.0 * mu / r_p,
                    })
                    last_trigger[pid] = t
                    pending += 1
            # 未到执行时刻：继续积分

        if outcome is not None:
            break

        # ---- RK4 积分一步 ----
        x, y, vx, vy = _rk4_step(x, y, vx, vy, dt)
        t += dt
        step += 1
        if step % 2 == 0:
            traj.append((x, y, t))

        # ---- 撞毁检查 ----
        r_sun = math.hypot(x, y)
        if r_sun < BODY_BY_ID["sun"]["radius"]:
            outcome = {"ok": False, "reason": "坠入太阳"}
            break
        for b in _PLANETS:
            px, py = planet_position(b["id"], t)
            if math.hypot(x - px, y - py) < b["radius"]:
                outcome = {"ok": False, "reason": f"撞毁于{b['name']}"}
                break
        if outcome is not None:
            break

        # ---- 里程碑 ----
        for ms in level["milestones"]:
            if ms["id"] not in [m["id"] for m in milestones] and _check_milestone(ms, x, y, vx, vy, t, events):
                milestones.append({"id": ms["id"], "name": ms["name"], "t": t})
                if len(milestones) == len(level["milestones"]):
                    outcome = {"ok": True, "reason": "任务达成"}
                    break

    if outcome is None:
        outcome = {"ok": False, "reason": "时间耗尽"}

    # ---- 缩放轨迹 ----
    downsample = max(1, math.ceil(len(traj) / max_points))
    pts = traj[::downsample]

    return {
        "ok": outcome["ok"],
        "reason": outcome["reason"],
        "elapsed_days": round(min(t, t_max), 1),
        "fuel_used": round(fuel_used, 6),
        "budget_dv": budget,
        "milestones": milestones,
        "events": events,
        "trajectory": [[round(p[0], 5), round(p[1], 5), round(p[2], 1)] for p in pts],
    }


def _level_start(level: dict) -> tuple[tuple[float, float], tuple[float, float]]:
    """起点：地球轨道上落后 0.05 AU 处，速度与地球同步。"""
    b = BODY_BY_ID["earth"]
    ang = b["theta0"] - 0.05 / b["orbit"]
    x = b["orbit"] * math.cos(ang)
    y = b["orbit"] * math.sin(ang)
    w = 2.0 * math.pi / b["period"]
    vx = -b["orbit"] * w * math.sin(ang)
    vy = b["orbit"] * w * math.cos(ang)
    return (x, y), (vx, vy)


def _check_milestone(ms: dict, x: float, y: float, vx: float, vy: float,
                     t: float, events: Sequence[dict]) -> bool:
    r = math.hypot(x, y)
    v = math.hypot(vx, vy)
    kind = ms["kind"]
    if kind == "proximity":
        px, py = planet_position(ms["planet_id"], t)
        return math.hypot(x - px, y - py) < ms["dist"]
    if kind == "radius":
        return r >= ms["r"]
    if kind == "assist_capture":
        ev = [e for e in events if e["type"] == "slingshot" and e["planet_id"] == ms["planet_id"]]
        return bool(ev and ev[-1]["captured"])
    if kind == "assist_then_radius":
        ev = [e for e in events if e["type"] == "slingshot" and e["planet_id"] == ms["planet_id"]]
        return bool(ev and t > ev[-1]["t"] and r >= ms["r"])
    if kind == "escape":
        return r >= ms["r"] and v * v > 2.0 * MU_SUN / r
    return False


def stars_for(level: dict, fuel_used: float, ok: bool) -> int:
    if not ok:
        return 0
    ratio = fuel_used / level["budget_dv"]
    if ratio <= 0.55:
        return 3
    if ratio <= 0.80:
        return 2
    return 1
