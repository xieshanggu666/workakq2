import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.api.router import router
from app.api.challenges import router as challenges_router
from app.core.config import PORT
from app.core.database import Base, SessionLocal, engine
from app.services import challenges as ch_svc

Base.metadata.create_all(bind=engine)

app = FastAPI(title="引力跳板：星际弹弓轨道规划游戏")
app.include_router(router)
app.include_router(challenges_router)


@app.on_event("startup")
def _startup_review_chain():
    """审核/申诉链路启动引导：旧库迁移 + 内置审核账号 + 历史成绩事件补登。"""
    with SessionLocal() as db:
        ch_svc.bootstrap(db)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
STATIC_DIR = os.path.abspath(STATIC_DIR)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
