"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.errors import install_exception_handlers
from app.runtime_logging import configure_logging, get_runtime_log_service
from app.services.account_state import get_account_state_service
from app.services.ocr_service import get_ocr_service
from app.services.scheduler_service import get_scheduler_service
from app.web.routes import router

configure_logging()
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build the FastAPI app."""
    app = FastAPI(
        title="GLM Desk",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )
    install_exception_handlers(app)
    dist_assets = Path(__file__).resolve().parents[1] / "web" / "dist" / "assets"
    if dist_assets.exists():
        app.mount("/assets", StaticFiles(directory=str(dist_assets)), name="spa-assets")
    app.include_router(router)

    @app.on_event("startup")
    def start_scheduler() -> None:
        runtime_logs = get_runtime_log_service()
        runtime_logs.log_system_event(
            stage="startup",
            status="started",
            message="服务启动，开始执行缓存清理、OCR 预热和调度器初始化",
        )
        get_account_state_service().clear_payment_cache()
        try:
            get_ocr_service().warmup()
            runtime_logs.log_system_event(
                stage="ocr_warmup",
                status="success",
                message="OCR 预热完成",
                details=get_ocr_service().status_payload(),
            )
        except Exception as exc:  # pragma: no cover - startup best effort
            logger.warning("OCR warmup failed: %s", exc)
            runtime_logs.log_system_event(
                stage="ocr_warmup",
                status="failed",
                message=f"OCR 预热失败：{exc}",
                details={"error": exc.__class__.__name__},
                level=logging.WARNING,
            )
        get_scheduler_service().start()
        runtime_logs.log_system_event(
            stage="startup",
            status="success",
            message="服务启动完成",
        )

    @app.on_event("shutdown")
    def stop_scheduler() -> None:
        get_scheduler_service().stop()
        get_ocr_service().shutdown()
        get_runtime_log_service().log_system_event(
            stage="shutdown",
            status="success",
            message="服务已停止",
        )

    return app


app = create_app()
