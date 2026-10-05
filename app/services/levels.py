"""关卡定义：目标里程碑 / 燃料预算 / 时间预算 / 参考解法（供测试与提示）。

fuel_used 单位 AU/天（ΔV）。预算按“最优解燃料 / 0.52”设定，使高效玩法可拿三星
（三星 ≤ 0.55×预算，二星 ≤ 0.80×预算）。参考解法经 scripts/calibrate.py 校准。
"""
from __future__ import annotations

LEVELS = [
    {
        "id": 1,
        "name": "火星飞掠",
        "brief": "从地球出发，让探测器近距离飞掠火星（最近距离 < 0.18 AU）。"
                 "练手关：一次点火 + 耐心航行即可到达，尽量少用燃料。",
        "milestones": [
            {"id": "m1", "kind": "proximity", "planet_id": "mars", "dist": 0.18,
             "name": "近距离飞掠火星"},
        ],
        "budget_dv": 0.004,
        "t_max": 900,
        "hint": "在地球轨道切向（约 90°）点火 0.0017 AU/天，把远日点抬到火星轨道，"
                "到达远日点时正好与火星相遇；只飞掠、不刹车，燃料自然省。",
        "ref": [
            {"type": "burn", "angle": 90.0, "dv": 0.0017},
        ],
    },
    {
        "id": 2,
        "name": "小行星带穿越",
        "brief": "越过小行星带（r ≥ 4.2 AU）进入外太阳系。标准霍曼转移即可三星，"
                 "关键是别乱烧燃料。",
        "milestones": [
            {"id": "m1", "kind": "radius", "r": 4.2, "name": "穿越小行星带"},
        ],
        "budget_dv": 0.009,
        "t_max": 2500,
        "hint": "一次性切向点火（90°，~0.0047 AU/天）把远日点抬到 4.2 AU，"
                "航行约 760 天即可达标。预算 0.009 AU/天，省着用。",
        "ref": [
            {"type": "burn", "angle": 90.0, "dv": 0.0047},
        ],
    },
    {
        "id": 3,
        "name": "木星引力捕获",
        "brief": "变轨飞向木星，在木星影响球内完成一次收紧的弹弓，"
                 "使相对速度低于逃逸速度——被木星引力捕获。",
        "milestones": [
            {"id": "m1", "kind": "assist_capture", "planet_id": "jupiter",
             "name": "被木星引力捕获"},
        ],
        "budget_dv": 0.010,
        "t_max": 3000,
        "hint": "霍曼转移约 0.0051 AU/天（90°）抵达木星，弹弓近心点取 "
                "0.005~0.01 AU 即可被捕获（越紧越稳，但别小于木星半径）。预算 0.010。",
        "ref": [
            {"type": "burn", "angle": 90.0, "dv": 0.0051},
            {"type": "slingshot", "planet_id": "jupiter", "b": 0.006},
        ],
    },
    {
        "id": 4,
        "name": "土星远航",
        "brief": "借力木星弹弓冲向土星，再完成第二次弹弓，把远日点推到 15 AU 以外。"
                 "双弹弓接力的成名关。",
        "milestones": [
            {"id": "m1", "kind": "assist_then_radius", "planet_id": "saturn", "r": 15.0,
             "name": "土星弹弓后远日点 ≥ 15 AU"},
        ],
        "budget_dv": 0.0095,
        "t_max": 6000,
        "hint": "90°、0.005 AU/天 前往木星，木星弹弓取 b=-0.006 甩向外侧；"
                "土星弹弓取 b=+0.006，把远日点抬过 15 AU。预算 0.0095。",
        "ref": [
            {"type": "burn", "angle": 90.0, "dv": 0.0050},
            {"type": "slingshot", "planet_id": "jupiter", "b": -0.006},
            {"type": "slingshot", "planet_id": "saturn", "b": 0.006},
        ],
    },
    {
        "id": 5,
        "name": "星际逃逸",
        "brief": "终局挑战：连续借力木星与土星，把探测器速度抬到当地逃逸速度之上，"
                 "飞出 40 AU 的太阳系边界。",
        "milestones": [
            {"id": "m1", "kind": "escape", "r": 40.0, "name": "挣脱太阳引力，飞出太阳系"},
        ],
        "budget_dv": 0.0115,
        "t_max": 9000,
        "hint": "木星 b=-0.006 → 土星 b=+0.006 接力后，沿当前速度方向"
                "补一次 ~0.0009 AU/天 的小点火即可逃逸。预算 0.0115。",
        "ref": [
            {"type": "burn", "angle": 90.0, "dv": 0.0050},
            {"type": "slingshot", "planet_id": "jupiter", "b": -0.006},
            {"type": "slingshot", "planet_id": "saturn", "b": 0.006},
            {"type": "burn", "angle": 37.0, "dv": 0.0009},
        ],
    },
]

LEVEL_BY_ID = {lv["id"]: lv for lv in LEVELS}
