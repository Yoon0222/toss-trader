from contextlib import asynccontextmanager
import asyncio
from fastapi import FastAPI, Request, Query, Path as ApiPath
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from sqlalchemy import text
from app.config import Settings
from app.db import Resources
from app.market import TossMarketData
from app.paper_runner import PaperValidation
from app.us_paper_runner import USPaperValidation
from starlette.middleware.trustedhost import TrustedHostMiddleware
from typing import Literal

def create_app(settings=None):
    settings = settings or Settings()
    @asynccontextmanager
    async def lifespan(app):
        app.state.resources = Resources(settings)
        app.state.market = TossMarketData(settings)
        app.state.paper = PaperValidation(app.state.market, settings)
        await app.state.paper.start()
        app.state.us_paper = USPaperValidation(app.state.market, settings)
        await app.state.us_paper.start()
        try:
            yield
        finally:
            await app.state.us_paper.close()
            await app.state.paper.close()
            await app.state.market.close()
            await app.state.resources.close()
    api = FastAPI(title="AI 급등주 자동매매 — Phase 1", lifespan=lifespan)
    api.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
    @api.middleware("http")
    async def protect_data(request: Request, call_next):
        if request.url.path.startswith(("/api/data/", "/api/paper/")):
            if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"} or (request.headers.get("origin") and request.headers["origin"] not in {"http://127.0.0.1:8000", "http://localhost:8000"}):
                return JSONResponse({"detail":"동일 출처에서만 데이터 조회가 가능해요."}, status_code=403)
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        return await call_next(request)
    @api.get("/api/data/dashboard")
    async def market_dashboard(account: bool = Query(True)):
        return await api.state.market.dashboard(include_account=account)
    @api.get("/api/data/us/dashboard")
    async def us_market_dashboard(account: bool = Query(True)):
        return await api.state.market.dashboard(market_country="US", include_account=account)
    @api.get("/api/data/candles/{symbol}")
    async def market_candles(symbol: str = ApiPath(pattern=r"^[A-Za-z0-9]{6}$"), period: Literal["1일", "1주", "1개월"] = Query("1일")):
        return await api.state.market.section(lambda: api.state.market.candles(symbol, period))
    @api.get("/api/paper/status")
    async def paper_status():
        return api.state.paper.snapshot()
    @api.get("/api/paper/preflight")
    async def paper_preflight():
        return await api.state.paper.preflight()
    @api.post("/api/paper/pause")
    async def paper_pause():
        return api.state.paper.pause()
    @api.post("/api/paper/resume")
    async def paper_resume():
        return api.state.paper.resume()
    @api.get("/api/paper/us/status")
    async def us_paper_status():
        return api.state.us_paper.snapshot()
    @api.get("/api/paper/us/preflight")
    async def us_paper_preflight():
        return await api.state.us_paper.preflight()
    @api.post("/api/paper/us/pause")
    async def us_paper_pause():
        return api.state.us_paper.pause()
    @api.post("/api/paper/us/resume")
    async def us_paper_resume():
        return api.state.us_paper.resume()
    @api.get("/api/data/us/market")
    async def us_market_data():
        return await api.state.market.section(lambda: api.state.market.rankings(ranking_type="MARKET_TRADING_AMOUNT", duration="realtime", count=20, market_country="US"))
    @api.get("/api/data/us/candles/{symbol}")
    async def us_market_candles(symbol: str = ApiPath(pattern=r"^[A-Za-z][A-Za-z0-9.\-]{0,19}$"), period: Literal["1일", "1주", "1개월"] = Query("1일")):
        return await api.state.market.section(lambda: api.state.market.candles(symbol, period))
    static_dir = Path(__file__).parent / "static"
    api.mount("/static", StaticFiles(directory=static_dir), name="static")
    @api.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(static_dir / "index.html")
    @api.get("/health/live")
    async def live():
        return {"status": "ok"}
    @api.get("/health/ready")
    async def ready():
        async def database():
            async with api.state.resources.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        async def probe(fn):
            try:
                await asyncio.wait_for(fn(), timeout=3)
                return True
            except Exception:
                return False
        db, redis = await asyncio.gather(probe(database), probe(api.state.resources.redis.ping))
        return JSONResponse({"database": db, "redis": redis}, status_code=200 if db and redis else 503)
    @api.get("/api/status")
    async def status():
        return {"mode": settings.trading_mode, "live_orders_available": False, "phase": 1, "automation_running": bool(api.state.paper.task and not api.state.paper.task.done()), "paper_validation_enabled":settings.paper_enabled, "paper_phase":api.state.paper.phase, "us_paper_validation_enabled":settings.us_paper_enabled, "us_paper_phase":api.state.us_paper.phase, "us_automation_running": bool(api.state.us_paper.task and not api.state.us_paper.task.done())}
    return api
app = create_app()
