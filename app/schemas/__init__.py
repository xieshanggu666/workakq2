"""API 共享数据模型：动作序列结构与校验（内置关卡与社区挑战共用）。"""
from typing import List, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from app.services import physics

_PLANET_IDS = {b["id"] for b in physics.BODIES if b["id"] != "sun"}
_ACTION_TYPES = {"coast", "burn", "slingshot"}


class Action(BaseModel):
    type: str
    days: Optional[float] = None
    angle: Optional[float] = None
    dv: Optional[float] = None
    planet_id: Optional[str] = None
    b: Optional[float] = None


def validate_actions(actions: List[Action]) -> List[dict]:
    """校验并规范化动作序列（类型/参数范围/目标行星），供物理引擎结算。"""
    out = []
    for a in actions:
        if a.type not in _ACTION_TYPES:
            raise HTTPException(400, f"未知动作类型: {a.type}")
        if a.type == "coast":
            out.append({"type": "coast", "days": max(0.0, a.days or 0.0)})
        elif a.type == "burn":
            out.append({"type": "burn", "angle": float(a.angle or 0.0),
                        "dv": max(0.0, min(0.02, a.dv or 0.0))})
        elif a.type == "slingshot":
            if a.planet_id not in _PLANET_IDS:
                raise HTTPException(400, f"未知行星: {a.planet_id}")
            out.append({"type": "slingshot", "planet_id": a.planet_id,
                        "b": max(-0.2, min(0.2, a.b or 0.0))})
    return out
