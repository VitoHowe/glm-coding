"""Local OCR wrapper for Tencent click captcha images."""

from __future__ import annotations

import base64
import importlib.util
import logging
import multiprocessing
import os
import secrets
import threading
import time
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError
from concurrent.futures.process import BrokenProcessPool
from typing import Any

from app.config import Settings, get_settings
from app.errors import BadRequestError

OCR_DEPENDENCIES = ("cv2", "numpy", "rapidocr", "onnxruntime")
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)

logger = logging.getLogger(__name__)


def _clear_proxy_env() -> None:
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)


def _load_adapter_module():
    from app.services import tenvision_adapter

    return tenvision_adapter


def _worker_initializer() -> None:
    _clear_proxy_env()
    _load_adapter_module().get_engine()


def _warmup_worker(index: int) -> dict[str, Any]:
    _clear_proxy_env()
    _load_adapter_module().get_engine()
    # Keep warmup tasks alive briefly so ProcessPoolExecutor has a reason to
    # spawn up to the requested worker count instead of reusing one hot process.
    time.sleep(1.2)
    return {"index": index, "pid": os.getpid()}


def _worker_analyze(data: bytes, prompt_text: str, include_debug: bool) -> dict[str, Any]:
    _clear_proxy_env()
    adapter = _load_adapter_module()
    started_at = time.perf_counter()
    result = adapter.analyze_image_bytes(
        data,
        prompt_text=prompt_text,
        include_debug=include_debug,
    )
    result["_worker_pid"] = os.getpid()
    result["_worker_elapsed_ms"] = round((time.perf_counter() - started_at) * 1000, 2)
    return result


class OcrService:
    """Run the vendored TenVision captcha OCR pipeline in this project."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._executor: ProcessPoolExecutor | None = None
        self._executor_lock = threading.Lock()
        self._bootstrap_lock = threading.Lock()
        self._capacity_lock = threading.RLock()
        self._active_jobs = 0
        self._idle_shrink_timer: threading.Timer | None = None
        self._engine_bootstrapped = False
        self._active_demands: dict[str, int] = {}
        self._warmed_worker_pids: set[int] = set()

    def status_payload(self) -> dict[str, Any]:
        missing = self._missing_dependencies()
        active_demand, warmed_worker_pids = self._capacity_snapshot()
        return {
            "enabled": self.settings.tencent_ocr_enabled,
            "adapter": "local-tenvision-process-pool",
            "available": not missing,
            "missing_dependencies": missing,
            "include_debug": self.settings.tencent_ocr_include_debug,
            "workers": self.settings.tencent_ocr_workers,
            "max_workers": self.settings.tencent_ocr_workers,
            "active_jobs": self._active_jobs,
            "active_demand": active_demand,
            "idle_shrink_seconds": self.settings.tencent_ocr_idle_shrink_seconds,
            "processes": self._executor_process_count(),
            "warmed_workers": len(warmed_worker_pids),
            "warmed_worker_pids": sorted(warmed_worker_pids),
            "timeout_seconds": self.settings.tencent_ocr_timeout_seconds,
            "executor_ready": self._executor is not None,
            "engine_bootstrapped": self._engine_bootstrapped,
        }

    def warmup(self, target_workers: int | None = None) -> None:
        self.ensure_capacity(target_workers or 1)

    def reserve_capacity(self, demand: int) -> dict[str, Any]:
        normalized_demand = max(1, int(demand or 1))
        lease_id = secrets.token_hex(8)
        with self._capacity_lock:
            self._active_demands[lease_id] = normalized_demand
            active_demand = sum(self._active_demands.values())
        capacity = self.ensure_capacity(active_demand)
        return {
            **capacity,
            "lease_id": lease_id,
            "reserved_demand": normalized_demand,
            "active_demand": active_demand,
        }

    def release_capacity(self, lease_id: str) -> dict[str, Any]:
        if not lease_id:
            return self.status_payload()
        with self._capacity_lock:
            self._active_demands.pop(lease_id, None)
            active_demand = sum(self._active_demands.values())
        if active_demand <= 0:
            with self._executor_lock:
                if self._active_jobs == 0:
                    self._schedule_idle_shrink_locked()
        return self.status_payload()

    def ensure_capacity(self, requested_workers: int) -> dict[str, Any]:
        if not self.settings.tencent_ocr_enabled:
            return self.status_payload()
        missing = self._missing_dependencies()
        if missing:
            raise BadRequestError(
                "本地 OCR 依赖没装全，先运行 pip install -r requirements.txt，别让发动机缺缸还硬跑。",
                details={"missing_dependencies": missing},
            )
        target_workers = max(1, min(int(requested_workers or 1), max(1, self.settings.tencent_ocr_workers)))
        self._cancel_idle_shrink_timer()
        with self._capacity_lock:
            if len(self._warmed_worker_pids) >= target_workers:
                return self.status_payload()
            self._bootstrap_engine_once()
            executor = self._ensure_executor()
            timeout = max(self.settings.tencent_ocr_timeout_seconds, 1) + 3
            try:
                # Submit a full wave to force ProcessPoolExecutor to spawn the
                # requested process count instead of reusing one hot worker.
                futures = [executor.submit(_warmup_worker, index) for index in range(target_workers)]
                for future in futures:
                    result = future.result(timeout=timeout)
                    pid = int(result.get("pid") or 0)
                    if pid:
                        self._warmed_worker_pids.add(pid)
            except BrokenProcessPool:
                self.shutdown()
                raise
        return self.status_payload()

    def shutdown(self) -> None:
        with self._executor_lock:
            self._cancel_idle_shrink_timer_locked()
            if self._executor is None:
                return
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
            self._active_jobs = 0
            self._warmed_worker_pids.clear()
        with self._capacity_lock:
            self._active_demands.clear()

    def analyze_captcha_image(self, image_bytes: bytes, *, prompt_text: str) -> dict[str, Any]:
        if not self.settings.tencent_ocr_enabled:
            raise BadRequestError("本地 OCR 已关闭，请设置 TENCENT_OCR_ENABLED=1")

        missing = self._missing_dependencies()
        if missing:
            raise BadRequestError(
                "本地 OCR 依赖没装全，先运行 pip install -r requirements.txt，别让发动机缺缸还硬跑。",
                details={"missing_dependencies": missing},
            )

        try:
            result = self._run_worker(image_bytes, prompt_text)
        except FuturesTimeoutError as exc:
            raise BadRequestError(
                "验证码 OCR 识别超时",
                details={"timeout_seconds": self.settings.tencent_ocr_timeout_seconds},
            ) from exc
        except Exception as exc:
            raise BadRequestError("验证码 OCR 识别失败", details={"reason": str(exc)}) from exc

        return self._normalize_result(result)

    def _run_worker(self, image_bytes: bytes, prompt_text: str) -> dict[str, Any]:
        timeout = max(self.settings.tencent_ocr_timeout_seconds, 1)
        payload_prompt = prompt_text or ""
        self._bootstrap_engine_once()
        for attempt in range(1, 3):
            self._begin_ocr_job()
            executor = self._ensure_executor()
            future = executor.submit(
                _worker_analyze,
                image_bytes,
                payload_prompt,
                self.settings.tencent_ocr_include_debug,
            )
            try:
                result = future.result(timeout=timeout)
                pid = int(result.get("_worker_pid") or 0)
                if pid:
                    with self._capacity_lock:
                        self._warmed_worker_pids.add(pid)
                return result
            except FuturesTimeoutError:
                future.cancel()
                raise
            except BrokenProcessPool:
                self.shutdown()
                if attempt >= 2:
                    raise
            except Exception:
                raise
            finally:
                self._finish_ocr_job()
        raise RuntimeError("OCR worker 未返回结果")

    def _bootstrap_engine_once(self) -> None:
        if self._engine_bootstrapped:
            return
        with self._bootstrap_lock:
            if self._engine_bootstrapped:
                return
            started_at = time.perf_counter()
            _clear_proxy_env()
            _load_adapter_module().get_engine()
            self._engine_bootstrapped = True
            logger.info(
                "OCR bootstrap finished in %.2f ms",
                round((time.perf_counter() - started_at) * 1000, 2),
            )

    def _ensure_executor(self) -> ProcessPoolExecutor:
        with self._executor_lock:
            if self._executor is not None:
                return self._executor
            mp_context = multiprocessing.get_context("spawn")
            self._executor = ProcessPoolExecutor(
                max_workers=max(1, self.settings.tencent_ocr_workers),
                mp_context=mp_context,
                initializer=_worker_initializer,
            )
            return self._executor

    def _begin_ocr_job(self) -> None:
        with self._executor_lock:
            self._active_jobs += 1
            self._cancel_idle_shrink_timer_locked()

    def _finish_ocr_job(self) -> None:
        with self._executor_lock:
            self._active_jobs = max(0, self._active_jobs - 1)
            with self._capacity_lock:
                active_demand = sum(self._active_demands.values())
            if self._active_jobs == 0 and active_demand <= 0:
                self._schedule_idle_shrink_locked()

    def _schedule_idle_shrink_locked(self) -> None:
        if self._executor is None or self.settings.tencent_ocr_workers <= 1:
            return
        self._cancel_idle_shrink_timer_locked()
        timer = threading.Timer(
            max(1, self.settings.tencent_ocr_idle_shrink_seconds),
            self._shrink_idle_executor,
        )
        timer.daemon = True
        self._idle_shrink_timer = timer
        timer.start()

    def _cancel_idle_shrink_timer(self) -> None:
        with self._executor_lock:
            self._cancel_idle_shrink_timer_locked()

    def _cancel_idle_shrink_timer_locked(self) -> None:
        if self._idle_shrink_timer is None:
            return
        self._idle_shrink_timer.cancel()
        self._idle_shrink_timer = None

    def _shrink_idle_executor(self) -> None:
        with self._executor_lock:
            self._idle_shrink_timer = None
            with self._capacity_lock:
                active_demand = sum(self._active_demands.values())
            if self._executor is None or self._active_jobs > 0 or active_demand > 0:
                return
            process_count = self._executor_process_count_locked()
            if process_count <= 1:
                return
            logger.info("OCR worker pool idle; shrinking from %s processes to 1 warm worker", process_count)
            old_executor = self._executor
            old_executor.shutdown(wait=False, cancel_futures=True)
            mp_context = multiprocessing.get_context("spawn")
            self._executor = ProcessPoolExecutor(
                max_workers=max(1, self.settings.tencent_ocr_workers),
                mp_context=mp_context,
                initializer=_worker_initializer,
            )
            self._warmed_worker_pids.clear()
            executor = self._executor
        try:
            timeout = max(self.settings.tencent_ocr_timeout_seconds, 1) + 3
            result = executor.submit(_warmup_worker, 0).result(timeout=timeout)
            pid = int(result.get("pid") or 0)
            if pid:
                with self._capacity_lock:
                    self._warmed_worker_pids.add(pid)
        except Exception as exc:  # pragma: no cover - best effort idle warmup
            logger.warning("OCR idle warm worker restart failed: %s", exc)

    def _executor_process_count(self) -> int:
        with self._executor_lock:
            return self._executor_process_count_locked()

    def _executor_process_count_locked(self) -> int:
        if self._executor is None:
            return 0
        processes = getattr(self._executor, "_processes", None)
        if not processes:
            return 0
        return len(processes)

    def _capacity_snapshot(self) -> tuple[int, set[int]]:
        with self._capacity_lock:
            return sum(self._active_demands.values()), set(self._warmed_worker_pids)

    def _missing_dependencies(self) -> list[str]:
        return [name for name in OCR_DEPENDENCIES if importlib.util.find_spec(name) is None]

    def _normalize_result(self, result: dict[str, Any]) -> dict[str, Any]:
        normalized = self._json_safe(dict(result))
        debug_png = normalized.pop("debug_png", b"")
        if isinstance(debug_png, bytes) and debug_png:
            normalized["debug_image_base64"] = "data:image/png;base64," + base64.b64encode(debug_png).decode("ascii")
        return normalized

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): self._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._json_safe(item) for item in value]
        if isinstance(value, bytes):
            return value
        if hasattr(value, "item"):
            return value.item()
        return value


_ocr_service: OcrService | None = None


def get_ocr_service() -> OcrService:
    """Get the shared OCR service."""
    global _ocr_service
    if _ocr_service is None:
        _ocr_service = OcrService()
    return _ocr_service
