"""FastAPI app: run registry, SSE event stream, downloads, demo site."""
import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import security  # noqa: E402
from .llm import TIER_LABELS, env_flag, resolve_models  # noqa: E402
from .pipeline import run_pipeline  # noqa: E402
from .scanner import Scanner  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("accessfix")

STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_RUNS_KEPT = 50
MAX_QUEUED = 6
HEARTBEAT_S = 15

scanner = Scanner()
RUN_SLOTS = asyncio.Semaphore(2)


class Run:
    def __init__(self, url: str):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.created = time.time()
        self.events: list[dict] = []
        self.done = False
        self.result: dict | None = None
        self.cond = asyncio.Condition()

    async def emit(self, event: dict):
        async with self.cond:
            self.events.append(event)
            if event.get("type") == "result":
                self.result = event
            self.cond.notify_all()

    async def finish(self):
        async with self.cond:
            self.done = True
            self.cond.notify_all()


RUNS: dict[str, Run] = {}


def _log_models():
    if env_flag("MOCK_LLM", "1"):
        log.info("MOCK_LLM=1: Nemotron calls return canned responses")
    for tier, info in resolve_models().items():
        if info["fallback_from"]:
            log.warning("%s not configured; falling back to %s", TIER_LABELS[tier], info["model"])
        else:
            log.info("%s → %s", TIER_LABELS[tier], info["model"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log_models()
    await scanner.start()
    yield
    await scanner.stop()


app = FastAPI(title="AccessFix", lifespan=lifespan)


class RunRequest(BaseModel):
    url: str


async def _execute(run: Run):
    try:
        if RUN_SLOTS.locked():
            await run.emit({"type": "log", "message": "Waiting for a free scan slot…"})
        async with RUN_SLOTS:
            async for event in run_pipeline(run.url, scanner):
                await run.emit(event)
    except Exception as e:  # never leave a client hanging
        log.exception("run %s crashed", run.id)
        await run.emit({"type": "error", "message": str(e)[:500]})
    finally:
        await run.finish()


@app.post("/api/runs")
async def create_run(body: RunRequest):
    try:
        url = await asyncio.to_thread(security.validate_url, body.url)
    except security.UnsafeURL as e:
        raise HTTPException(400, str(e))
    active = sum(1 for r in RUNS.values() if not r.done)
    if active >= MAX_QUEUED:
        raise HTTPException(429, "AccessFix is busy right now. Try again in a minute.")
    run = Run(url)
    RUNS[run.id] = run
    for old in sorted(RUNS.values(), key=lambda r: r.created)[:-MAX_RUNS_KEPT]:
        if old.done:
            RUNS.pop(old.id, None)
    asyncio.create_task(_execute(run))
    return {"run_id": run.id, "url": url}


def _get_run(run_id: str) -> Run:
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "Run not found (runs are kept in memory and reset when the server restarts).")
    return run


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request):
    run = _get_run(run_id)
    try:
        start = int(request.headers.get("last-event-id", "-1")) + 1
    except ValueError:
        start = 0

    async def stream():
        i = start
        yield "retry: 3000\n\n"
        while True:
            async with run.cond:
                try:
                    await asyncio.wait_for(run.cond.wait_for(lambda: len(run.events) > i or run.done), HEARTBEAT_S)
                except asyncio.TimeoutError:
                    pass
                pending, finished = run.events[i:], run.done
            if not pending and not finished:
                yield ": ping\n\n"
                continue
            for event in pending:
                yield f"id: {i}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
                i += 1
            if finished and i >= len(run.events):
                yield "event: end\ndata: {}\n\n"
                return
            if await request.is_disconnected():
                return

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.get("/api/runs/{run_id}/download")
async def download(run_id: str, type: str = "patch"):
    run = _get_run(run_id)
    if not run.result:
        raise HTTPException(409, "This run hasn't finished yet.")
    r = run.result
    if type == "report":
        return Response(r["report_md"], media_type="text/markdown",
                        headers={"Content-Disposition": f'attachment; filename="accessfix-report-{run.id}.md"'})
    if type == "patch":
        keep = ("rule_id", "wcag", "selector", "action", "attributes", "remove_attributes", "style", "html",
                "explanation", "needs_review", "verified", "attempt", "before_html", "after_html", "page_url", "sources")
        body = {
            "tool": "AccessFix",
            "url": r["url"],
            "score_before": r["score_before"], "score_after": r["score_after"],
            "note": "Patches were verified with axe-core on the rendered page. Apply the after_html changes to your source code.",
            "patches": [{k: p.get(k) for k in keep} for p in r["patches"]],
            "needs_human": [{k: n.get(k) for k in ("rule_id", "wcag", "target", "html", "reason", "page_url")} for n in r["needs_human"]],
        }
        return Response(json.dumps(body, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="accessfix-patches-{run.id}.json"'})
    raise HTTPException(400, "type must be 'patch' or 'report'")


@app.get("/api/config")
async def config():
    base = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    models = resolve_models()
    return {
        "demo_url": f"{base}/demo/sunny-crumb.html",
        "self_url": f"{base}/",
        "mock_llm": env_flag("MOCK_LLM", "1"),
        "mock_tavily": env_flag("MOCK_TAVILY", "1"),
        "models": {t: m["model"] for t, m in models.items()},
    }


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True, "runs": len(RUNS)})


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/demo", StaticFiles(directory=STATIC / "demo", html=True), name="demo")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
