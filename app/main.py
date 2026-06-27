"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.errors import install_exception_handlers
from app.proxy_pool.service import get_builtin_proxy_pool_service
from app.runtime_logging import configure_logging, get_runtime_log_service
from app.services.network_mode_service import get_network_mode_service
from app.services.account_state import get_account_state_service
from app.services.ocr_service import get_ocr_service
from app.services.scheduler_service import get_scheduler_service
from app.web.routes import router

configure_logging()
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build the FastAPI app."""
    app = FastAPI(
        title="AegisFlow",
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
        if get_network_mode_service().get_mode() == "proxy_pool":
            # 上次为代理池模式：后台静默拉起内置代理池服务，不阻塞启动、不做连通性检验
            def _bootstrap_proxy_pool() -> None:
                try:
                    get_builtin_proxy_pool_service().start()
                    runtime_logs.log_system_event(
                        stage="proxy_pool",
                        status="started",
                        message="上次为代理池模式，后台拉起内置 Python 代理池服务",
                        details=get_builtin_proxy_pool_service().status_payload(),
                    )
                except Exception as exc:  # pragma: no cover - startup best effort
                    logger.warning("Built-in proxy pool startup failed: %s", exc)
                    runtime_logs.log_system_event(
                        stage="proxy_pool",
                        status="failed",
                        message=f"内置 Python 代理池服务启动失败：{exc}",
                        details={"error": exc.__class__.__name__},
                        level=logging.WARNING,
                    )

            threading.Thread(target=_bootstrap_proxy_pool, name="proxy-pool-bootstrap", daemon=True).start()
        ocr_service = get_ocr_service()
        if ocr_service.warmup_in_background():
            runtime_logs.log_system_event(
                stage="ocr_warmup",
                status="started",
                message="OCR 预热已转入后台执行，服务启动不会等待模型下载",
                details=ocr_service.status_payload(),
            )
        else:
            runtime_logs.log_system_event(
                stage="ocr_warmup",
                status="skipped",
                message="OCR 预热未启动或已有预热任务在运行",
                details=ocr_service.status_payload(),
            )
        health = get_scheduler_service().payment_service.health_payload()
        if health.get("status") != "ok":
            runtime_logs.log_system_event(
                stage="preflight",
                status="failed",
                message="运行前置依赖检查存在问题，请先处理后再启动任务",
                details={"problems": health.get("problems") or [], "tdc": health.get("tdc"), "ocr": health.get("ocr"), "proxy": health.get("proxy")},
                level=logging.ERROR,
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
        if get_builtin_proxy_pool_service().is_started:
            get_builtin_proxy_pool_service().stop()
        get_ocr_service().shutdown()
        get_runtime_log_service().log_system_event(
            stage="shutdown",
            status="success",
            message="服务已停止",
        )

    return app


app = create_app()
