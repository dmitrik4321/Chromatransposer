"""Точка входа сервера: uvicorn + lifespan для старта/остановки Server."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from ct.core.server import Server
from ct.north import rest_api


@asynccontextmanager
async def lifespan(app: FastAPI):
    srv = Server(os.environ.get("CT_CONFIG", "configs/server.yaml"))
    await srv.start()
    rest_api.server = srv
    app.state.server = srv
    yield
    await srv.stop()


app = FastAPI(title="Chromatransposer", version="0.1.0", lifespan=lifespan)
app.router.routes.extend(rest_api.app.routes)


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("CT_PORT", "8300")))


if __name__ == "__main__":
    main()
