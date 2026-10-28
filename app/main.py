import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.database import init_db
from app.services.executor import cleanup_orphaned_executions
from app.services.scheduler import start_scheduler, shutdown_scheduler
from app.web.routes import router as web_router
from app.api.routes import router as api_router

# Configuração de logging estruturado
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("pyautomation")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Inicializando PyAutomation Manager...")
    init_db()
    cleanup_orphaned_executions()
    if settings.RUN_SCHEDULER_IN_WEB:
        start_scheduler()
    yield
    if settings.RUN_SCHEDULER_IN_WEB:
        shutdown_scheduler()
    logger.info("PyAutomation Manager finalizado.")

def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    # Static files
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Routers
    app.include_router(web_router)
    app.include_router(api_router)

    return app

app = create_app()
