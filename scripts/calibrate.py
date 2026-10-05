"""开发校准脚本：验证参考解法可解性，并用“相位对准”自动确定行星初始相位。

用法：python scripts/calibrate.py
输出：各关卡参考解法结果；L4/L5 可解相位集合及交集（二者共用同一土星相位）。
"""
import sys
import os
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import physics
from app.services.levels import LEVELS

PERIOD_SATURN = 10759.2


def report(tag, r):
    if r["ok"]:
        print(f"  {tag}: OK  燃料 {r['fuel_used']:.5f}  用时 {r['elapsed_days']:.0f} 天")
    else:
        print(f"  {tag}: 失败 ({r['reason']}) 燃料 {r['fuel_used']:.5f}")


def crossings_of(pts, r_target, after_t=0.0):
    out = []
    for i in range(1, len(pts)):
        x0, y0, t0 = pts[i - 1]
        x1, y1, t1 = pts[i]
        r0 = math.hypot(x0, y0)
        r1 = math.hypot(x1, y1)
        if t1 <= after_t or (r0 - r_target) * (r1 - r_target) > 0:
            continue
        tc = (t0 + t1) / 2.0
        phi = math.atan2((y0 + y1) / 2.0, (x0 + x1) / 2.0) % (2 * math.pi)
        req = (phi - 2.0 * math.pi * tc / PERIOD_SATURN) % (2 * math.pi)
        out.append({"t": tc, "theta0": req})
    return out


def target_phase(lv, acts):
    physics.BODY_BY_ID["saturn"]["theta0"] = 0.0
    r0 = physics.integrate(lv, acts)
    return crossings_of(r0["trajectory"], 9.582, 0.0)


def vel_dir(pts, t_target):
    best = min(range(len(pts)), key=lambda i: abs(pts[i][2] - t_target))
    i = max(1, best)
    x0, y0 = pts[i - 1][0], pts[i - 1][1]
    x1, y1 = pts[i][0], pts[i][1]
    return math.atan2(y1 - y0, x1 - x0)


def search(lv, burn1, with_burn2):
    """返回可行解列表：[(theta0, bj, bs, fuel, acts), ...]"""
    results = []
    b_vals = [-0.010, -0.008, -0.006, 0.006, 0.008, 0.010]
    for bj in b_vals:
        for bs in b_vals:
            base = [{"type": "burn", "angle": 90.0, "dv": burn1},
                    {"type": "slingshot", "planet_id": "jupiter", "b": bj},
                    {"type": "slingshot", "planet_id": "saturn", "b": bs}]
            for cand in target_phase(lv, base):
                physics.BODY_BY_ID["saturn"]["theta0"] = cand["theta0"]
                r1 = physics.integrate(lv, base)
                if r1["ok"]:
                    results.append((cand["theta0"], bj, bs, r1["fuel_used"], list(base)))
                    continue
                if not with_burn2:
                    continue
                ev = [e for e in r1["events"] if e["type"] == "slingshot"
                      and e["planet_id"] == "saturn"]
                if not ev:
                    continue
                t_assist = ev[-1]["t"]
                d0 = math.degrees(vel_dir(r1["trajectory"], t_assist))
                for dv2, da in [(0.0006, 0), (0.0009, 0), (0.0012, 0),
                                (0.0009, 15), (0.0009, -15)]:
                    acts = base + [{"type": "burn", "angle": d0 + da, "dv": dv2}]
                    r2 = physics.integrate(lv, acts)
                    if r2["ok"]:
                        results.append((cand["theta0"], bj, bs, r2["fuel_used"], list(acts)))
                        break
    return results


def main():
    print("== 关卡 1 火星飞掠 ==")
    r = physics.integrate(LEVELS[0], LEVELS[0]["ref"])
    report("参考解法", r)

    print("\n== 关卡 2 小行星带穿越 ==")
    r = physics.integrate(LEVELS[1], LEVELS[1]["ref"])
    report("参考解法", r)

    print("\n== 关卡 3 木星捕获 ==")
    r = physics.integrate(LEVELS[2], LEVELS[2]["ref"])
    report("参考解法", r)
    ev = [e for e in r["events"] if e["type"] == "slingshot"]
    if ev:
        print(f"  弹弓事件: v_inf={ev[0]['v_inf']:.5f} 偏转={ev[0]['delta_deg']}° 捕获={ev[0]['captured']}")

    print("\n== 关卡 4 土星远航 ==")
    res4 = search(LEVELS[3], 0.0050, False)
    for th, bj, bs, fuel, acts in res4:
        print(f"  θ0={th:.3f} 木星b={bj:+.3f} 土星b={bs:+.3f} 燃料={fuel:.5f}")

    print("\n== 关卡 5 星际逃逸 ==")
    res5 = search(LEVELS[4], 0.0050, True)
    for th, bj, bs, fuel, acts in res5:
        print(f"  θ0={th:.3f} 木星b={bj:+.3f} 土星b={bs:+.3f} 燃料={fuel:.5f}")

    th4 = {round(x[0], 2) for x in res4}
    th5 = {round(x[0], 2) for x in res5}
    common = sorted(th4 & th5)
    print("\n== 两关共用土星相位候选 ==")
    if common:
        for th in common:
            r4 = [x for x in res4 if round(x[0], 2) == th]
            r5 = [x for x in res5 if round(x[0], 2) == th]
            print(f"  θ0={th:.3f}: L4 {len(r4)} 组 / L5 {len(r5)} 组")
    else:
        print("  无交集 —— 需要调整弹弓参数或补火")


if __name__ == "__main__":
    main()
