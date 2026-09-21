"""Local, read-only views over experiment artifacts."""
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from .storage import RunStore, StoreError, TraceRevisionError


def create_app(root="runs"):
    store = RunStore(root)
    app = FastAPI(title="Auto_Jev 研究台", docs_url=None, redoc_url=None)

    @app.exception_handler(StoreError)
    async def invalid_artifact(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(TraceRevisionError)
    async def stale_trace(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/api/runs")
    def runs():
        return store.list_runs()

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        return store.get_run(run_id)

    @app.get("/api/runs/{run_id}/matrix")
    def matrix(run_id: str, split: str = "validation"):
        return store.matrix(run_id, split)

    @app.get("/api/runs/{run_id}/candidates")
    def candidates(run_id: str):
        return store.list_candidates(run_id)

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str):
        return store.list_events(run_id)

    @app.get("/api/runs/{run_id}/trace/{candidate}/{episode}")
    def trace(run_id: str, candidate: str, episode: str, split: str = "validation"):
        if split not in ("train", "validation"):
            raise HTTPException(400, "封存数据不进入搜索看板")
        return store.load_trace(run_id, split, candidate, episode)

    @app.get("/api/runs/{run_id}/trace/{candidate}/{episode}/summary")
    def trace_summary(run_id: str, candidate: str, episode: str, split: str = "validation"):
        if split not in ("train", "validation"):
            raise HTTPException(400, "Sealed data is not available in the search dashboard")
        return store.load_trace_summary(run_id, split, candidate, episode)

    @app.get("/api/runs/{run_id}/trace/{candidate}/{episode}/decision/{decision_index}")
    def trace_decision(run_id: str, candidate: str, episode: str, decision_index: int,
                       split: str = "validation", revision: str | None = Query(default=None)):
        if split not in ("train", "validation"):
            raise HTTPException(400, "Sealed data is not available in the search dashboard")
        return store.load_trace_decision(run_id, split, candidate, episode, decision_index, revision)

    @app.get("/api/runs/{run_id}/frozen")
    def frozen(run_id: str):
        return store.get_frozen(run_id)

    return app
