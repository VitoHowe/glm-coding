"""Background scheduler for timed payment flow execution."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.errors import AegisFlowError
from app.runtime_logging import get_runtime_log_service
from app.services.account_state import get_account_state_service, utc_now_iso
from app.services.payment_service import RunPausedError, get_payment_service

logger = logging.getLogger(__name__)
try:
    SCHEDULER_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # pragma: no cover - Windows without tzdata
    SCHEDULER_TZ = None


class SchedulerService:
    """Run account payment flows at configured local times."""

    def __init__(self) -> None:
        self.state_service = get_account_state_service()
        self.payment_service = get_payment_service()
        self.runtime_logs = get_runtime_log_service()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running_accounts: set[str] = set()
        self._flow_accounts: set[str] = set()
        self._pause_requested: set[str] = set()
        self._pending_scheduled_runs: dict[str, str] = {}
        self._stock_monitor_threads: dict[str, threading.Thread] = {}
        self._stock_monitor_stops: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._clear_stale_schedule_statuses()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="aegisflow-scheduler", daemon=True)
        self._thread.start()
        self._start_enabled_stock_monitors()
        threading.Thread(target=self.check_cached_accounts_once, name="aegisflow-account-check", daemon=True).start()
        self.runtime_logs.log_system_event(
            stage="scheduler",
            status="started",
            message="调度器已启动",
        )

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            monitor_stops = list(self._stock_monitor_stops.values())
        for stop_event in monitor_stops:
            stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self.runtime_logs.log_system_event(
            stage="scheduler",
            status="stopped",
            message="调度器已停止",
        )

    def _clear_stale_schedule_statuses(self) -> None:
        stale_statuses = {"running", "pause_requested"}
        with self._lock:
            active_flow_accounts = set(self._flow_accounts)
        for public_account in self.state_service.list_accounts():
            if public_account.id in active_flow_accounts:
                continue
            account = self.state_service.get_account(public_account.id)
            if str(account.last_schedule_status or "").lower() not in stale_statuses:
                continue
            account.last_schedule_status = "paused"
            account.last_schedule_message = "服务重启后任务已停止，请重新启动"
            self.state_service.update_account(account)
            self.runtime_logs.log_account_event(
                account_id=account.id,
                action="run_payment_flow",
                stage="scheduler",
                status="stale_cleared",
                message="已清理服务重启遗留的运行状态",
            )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # pragma: no cover - defensive scheduler guard
                logger.exception("scheduler poll failed: %s", exc)
                self.runtime_logs.log_system_event(
                    stage="scheduler_poll",
                    status="failed",
                    message=f"调度器轮询失败：{exc}",
                    details={"error": exc.__class__.__name__},
                    level=logging.ERROR,
                )
            self._stop_event.wait(1)

    def poll_once(self) -> None:
        now = datetime.now(SCHEDULER_TZ) if SCHEDULER_TZ is not None else datetime.now().astimezone()
        current_hms = now.strftime("%H:%M:%S")
        current_date = now.strftime("%Y-%m-%d")
        for public_account in self.state_service.list_accounts():
            if not public_account.schedule_enabled or not public_account.scheduled_start_time:
                continue
            effective_start = self._effective_trigger_hms(
                public_account.scheduled_start_time,
                getattr(public_account, "preview_warmup_lead_seconds", 0) or 0,
            )
            if effective_start > current_hms:
                continue
            account = self.state_service.get_account(public_account.id)
            # run_key is keyed on the user-facing scheduled_start_time so warmup-lead
            # changes don't accidentally re-fire an already-ran schedule for today.
            run_key = self._scheduled_run_key(current_date, account.scheduled_start_time)
            if self._already_ran_schedule(account, run_key):
                continue
            if self._queue_scheduled_run_if_busy(account.id, run_key):
                continue
            self.start_account_flow(account.id, source="scheduled", scheduled_run_key=run_key)

    @staticmethod
    def _effective_trigger_hms(scheduled_start_time: str, lead_seconds: int) -> str:
        if lead_seconds <= 0:
            return scheduled_start_time
        try:
            hh, mm, ss = (int(p) for p in scheduled_start_time.split(":"))
        except ValueError:
            return scheduled_start_time
        total = hh * 3600 + mm * 60 + ss - max(0, int(lead_seconds))
        if total < 0:
            total = 0
        return f"{total // 3600:02d}:{(total // 60) % 60:02d}:{total % 60:02d}"

    def check_cached_accounts_once(self) -> None:
        for public_account in self.state_service.list_accounts():
            account_id = public_account.id
            with self._lock:
                if account_id in self._running_accounts:
                    continue
                self._running_accounts.add(account_id)
            try:
                self.payment_service.bootstrap_account(account_id)
                self.state_service.set_account_status(
                    account_id,
                    status="valid",
                    message="启动检查通过",
                )
                self.runtime_logs.log_account_event(
                    account_id=account_id,
                    action="startup_check",
                    stage="bootstrap",
                    status="success",
                    message="启动检查通过",
                )
            except AegisFlowError as exc:
                self.state_service.set_account_status(
                    account_id,
                    status="expired",
                    message=exc.message,
                )
                self.runtime_logs.log_account_event(
                    account_id=account_id,
                    action="startup_check",
                    stage="bootstrap",
                    status="failed",
                    message=exc.message,
                    details=exc.details,
                    level=logging.WARNING,
                )
            except Exception as exc:  # pragma: no cover - defensive startup check
                self.state_service.set_account_status(
                    account_id,
                    status="error",
                    message=str(exc),
                )
                logger.exception("account startup check failed for %s: %s", account_id, exc)
                self.runtime_logs.log_account_event(
                    account_id=account_id,
                    action="startup_check",
                    stage="bootstrap",
                    status="failed",
                    message=str(exc),
                    details={"error": exc.__class__.__name__},
                    level=logging.ERROR,
                )
            finally:
                with self._lock:
                    self._running_accounts.discard(account_id)

    def start_account_flow(
        self,
        account_id: str,
        *,
        source: str = "manual",
        scheduled_run_key: str = "",
    ) -> dict[str, object]:
        with self._lock:
            if account_id in self._running_accounts:
                if source == "scheduled" and scheduled_run_key:
                    already_pending = self._pending_scheduled_runs.get(account_id) == scheduled_run_key
                    self._pending_scheduled_runs[account_id] = scheduled_run_key
                    if not already_pending:
                        self.runtime_logs.log_account_event(
                            account_id=account_id,
                            action="run_payment_flow",
                            stage="scheduler",
                            status="pending",
                            message="定时任务到点时账号正在运行，已挂起等待当前任务结束",
                            details={"scheduled_run_key": scheduled_run_key},
                        )
                    return {"started": False, "status": "pending"}
                self.runtime_logs.log_account_event(
                    account_id=account_id,
                    action="run_payment_flow",
                    stage="scheduler",
                    status="ignored",
                    message="账号任务已在运行，忽略重复启动",
                    details={"source": source},
                    level=logging.WARNING,
                )
                return {"started": False, "status": "running"}
            self._pause_requested.discard(account_id)
            if source == "scheduled":
                self._pending_scheduled_runs.pop(account_id, None)
            self._running_accounts.add(account_id)
            self._flow_accounts.add(account_id)
        account = self.state_service.get_account(account_id)
        if source == "scheduled":
            account.last_scheduled_run_at = utc_now_iso()
            account.last_scheduled_run_key = scheduled_run_key or self._scheduled_run_key(
                datetime.now(SCHEDULER_TZ).strftime("%Y-%m-%d") if SCHEDULER_TZ is not None else datetime.now().astimezone().strftime("%Y-%m-%d"),
                account.scheduled_start_time,
            )
        else:
            account.last_manual_run_at = utc_now_iso()
        account.last_schedule_status = "running"
        if source == "scheduled":
            account.last_schedule_message = "定时任务运行中"
        elif source == "probe":
            account.last_schedule_message = "测活任务运行中"
        elif source == "stock_monitor":
            account.last_schedule_message = "库存有货，自动支付链路运行中"
        else:
            account.last_schedule_message = "手动任务运行中"
        self.state_service.update_account(account)
        self.runtime_logs.log_account_event(
            account_id=account_id,
            action="run_payment_flow",
            stage="scheduler",
            status="started",
            message="已提交账号运行任务",
            details={"source": source, "scheduled_start_time": account.scheduled_start_time},
        )
        threading.Thread(
            target=self._run_account_flow,
            args=(account_id, source),
            name=f"aegisflow-run-{account_id}",
            daemon=True,
        ).start()
        return {"started": True, "status": "running"}

    def start_stock_monitor(self, account_id: str) -> dict[str, object]:
        account = self.state_service.get_account(account_id)
        session = self.state_service.load_session(account_id)
        product_id = (session.selected_product_id or "").strip()
        if not product_id:
            product_id = "product-5643e6"
            session.selected_product_id = product_id
            self.state_service.save_session(session)

        account.stock_monitor_enabled = True
        account.stock_monitor_last_checked_at = None
        account.stock_monitor_last_message = "库存监控已启动，等待首次检查"
        account.last_schedule_status = "stock_monitoring"
        account.last_schedule_message = "库存监控中"
        self.state_service.update_account(account)
        self._ensure_stock_monitor_thread(account_id)
        self.runtime_logs.log_account_event(
            account_id=account_id,
            action="stock_monitor",
            stage="monitor",
            status="started",
            message="库存监控已启动",
            details={"product_id": product_id, "interval_seconds": 1},
        )
        return {"started": True, "status": "stock_monitoring", "product_id": product_id}

    def stop_stock_monitor(self, account_id: str) -> dict[str, object]:
        account = self.state_service.get_account(account_id)
        account.stock_monitor_enabled = False
        account.stock_monitor_last_message = "库存监控已停止"
        if str(account.last_schedule_status or "").lower() == "stock_monitoring":
            account.last_schedule_status = "idle"
            account.last_schedule_message = "库存监控已停止"
        self.state_service.update_account(account)
        with self._lock:
            stop_event = self._stock_monitor_stops.pop(account_id, None)
        if stop_event:
            stop_event.set()
        self.runtime_logs.log_account_event(
            account_id=account_id,
            action="stock_monitor",
            stage="monitor",
            status="stopped",
            message="库存监控已停止",
        )
        return {"stopped": True, "status": "idle"}

    def request_pause(self, account_id: str) -> dict[str, object]:
        with self._lock:
            is_flow_running = account_id in self._flow_accounts
            if is_flow_running:
                self._pause_requested.add(account_id)
        if not is_flow_running:
            account = self.state_service.get_account(account_id)
            stale_statuses = {"running", "pause_requested"}
            if str(account.last_schedule_status or "").lower() in stale_statuses:
                latest_task = next(iter(self.state_service.list_tasks(account_id)), None)
                if latest_task and latest_task.qr_base64:
                    account.last_schedule_status = "success"
                    account.last_schedule_message = f"生成二维码成功：{latest_task.biz_id}"
                    self.state_service.update_account(account)
                    return {"paused": False, "status": "success", "stale_cleared": True}
                account.last_schedule_status = "paused"
                account.last_schedule_message = "任务不在当前进程运行，已清理陈旧运行状态"
                self.state_service.update_account(account)
                self.runtime_logs.log_account_event(
                    account_id=account_id,
                    action="run_payment_flow",
                    stage="pause",
                    status="stale_cleared",
                    message="暂停时发现任务不在当前进程运行，已清理陈旧状态",
                )
                return {"paused": False, "status": "paused", "stale_cleared": True}
            return {"paused": False, "status": "idle"}
        account = self.state_service.get_account(account_id)
        account.last_schedule_status = "pause_requested"
        account.last_schedule_message = "暂停请求已提交"
        self.state_service.update_account(account)
        self.runtime_logs.log_account_event(
            account_id=account_id,
            action="run_payment_flow",
            stage="pause",
            status="requested",
            message="暂停请求已提交",
        )
        return {"paused": True, "status": "pause_requested"}

    def is_pause_requested(self, account_id: str) -> bool:
        with self._lock:
            return account_id in self._pause_requested

    def _start_enabled_stock_monitors(self) -> None:
        for public_account in self.state_service.list_accounts():
            if public_account.stock_monitor_enabled:
                self._ensure_stock_monitor_thread(public_account.id)

    def _ensure_stock_monitor_thread(self, account_id: str) -> None:
        with self._lock:
            existing = self._stock_monitor_threads.get(account_id)
            if existing and existing.is_alive():
                return
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run_stock_monitor,
                args=(account_id, stop_event),
                name=f"aegisflow-stock-{account_id}",
                daemon=True,
            )
            self._stock_monitor_stops[account_id] = stop_event
            self._stock_monitor_threads[account_id] = thread
            thread.start()

    def _run_stock_monitor(self, account_id: str, stop_event: threading.Event) -> None:
        while not self._stop_event.is_set() and not stop_event.is_set():
            should_continue = self._check_stock_once(account_id, stop_event)
            if not should_continue:
                break
            stop_event.wait(1)
        with self._lock:
            current_stop = self._stock_monitor_stops.get(account_id)
            if current_stop is stop_event:
                self._stock_monitor_stops.pop(account_id, None)
            current_thread = self._stock_monitor_threads.get(account_id)
            if current_thread is threading.current_thread():
                self._stock_monitor_threads.pop(account_id, None)

    def _check_stock_once(self, account_id: str, stop_event: threading.Event) -> bool:
        try:
            account = self.state_service.get_account(account_id)
        except Exception:
            return False
        if not account.stock_monitor_enabled:
            return False
        with self._lock:
            if account_id in self._running_accounts:
                return True

        session = self.state_service.load_session(account_id)
        product_id = (session.selected_product_id or "").strip()
        if not product_id:
            account.stock_monitor_last_checked_at = utc_now_iso()
            account.stock_monitor_last_message = "库存监控等待套餐选择"
            account.last_schedule_status = "stock_monitoring"
            account.last_schedule_message = account.stock_monitor_last_message
            self.state_service.update_account(account)
            return True

        try:
            products = self.payment_service.load_products(account_id)
            product = next((item for item in products if item.product_id == product_id), None)
            raw = product.raw if product else {}
            raw_sold_out = bool(raw.get("soldOut") or raw.get("sold_out"))
            forbidden = bool(raw.get("forbidden") or (product.forbidden if product else False))
            has_upstream_product = bool(raw.get("productId") or raw.get("product_id"))
            available = bool(product and has_upstream_product and not raw_sold_out and not forbidden)
            account = self.state_service.get_account(account_id)
            account.stock_monitor_last_checked_at = utc_now_iso()
            if available:
                account.stock_monitor_enabled = False
                account.stock_monitor_last_message = "库存有货，已自动启动支付链路"
                account.last_schedule_status = "stock_available"
                account.last_schedule_message = account.stock_monitor_last_message
                self.state_service.update_account(account)
                stop_event.set()
                self.runtime_logs.log_account_event(
                    account_id=account_id,
                    action="stock_monitor",
                    stage="stock_check",
                    status="available",
                    message="监控到套餐有货，自动启动支付链路",
                    details={
                        "product_id": product_id,
                        "product_name": product.product_name if product else "",
                        "unit": product.unit if product else "",
                        "has_upstream_product": has_upstream_product,
                        "raw_sold_out": raw_sold_out,
                        "forbidden": forbidden,
                    },
                )
                self.start_account_flow(account_id, source="stock_monitor")
                return False

            account.stock_monitor_last_message = "库存监控中：目标套餐暂无库存"
            account.last_schedule_status = "stock_monitoring"
            account.last_schedule_message = account.stock_monitor_last_message
            self.state_service.update_account(account)
            self.runtime_logs.log_account_event(
                account_id=account_id,
                action="stock_monitor",
                stage="stock_check",
                status="sold_out",
                message="目标套餐暂无库存，继续监控",
                details={
                    "product_id": product_id,
                    "product_found": bool(product),
                    "has_upstream_product": has_upstream_product,
                    "raw_sold_out": raw_sold_out,
                    "forbidden": forbidden,
                },
            )
            return True
        except Exception as exc:
            account = self.state_service.get_account(account_id)
            account.stock_monitor_last_checked_at = utc_now_iso()
            account.stock_monitor_last_message = f"库存检查失败：{exc}"
            account.last_schedule_status = "stock_monitoring"
            account.last_schedule_message = account.stock_monitor_last_message
            self.state_service.update_account(account)
            logger.warning("stock monitor check failed for %s: %s", account_id, exc)
            self.runtime_logs.log_account_event(
                account_id=account_id,
                action="stock_monitor",
                stage="stock_check",
                status="failed",
                message="库存检查失败，继续监控",
                details={"error": exc.__class__.__name__, "message": str(exc)},
                level=logging.WARNING,
            )
            return True

    def _scheduled_run_key(self, current_date: str, scheduled_start_time: str) -> str:
        return f"{current_date}|{(scheduled_start_time or '').strip()}"

    def _already_ran_schedule(self, account, run_key: str) -> bool:
        return bool(run_key) and (account.last_scheduled_run_key or "").strip() == run_key

    def _queue_scheduled_run_if_busy(self, account_id: str, run_key: str) -> bool:
        with self._lock:
            if account_id not in self._running_accounts:
                return False
            if self._pending_scheduled_runs.get(account_id) == run_key:
                return True
            self._pending_scheduled_runs[account_id] = run_key
        self.runtime_logs.log_account_event(
            account_id=account_id,
            action="run_payment_flow",
            stage="scheduler",
            status="pending",
            message="定时任务到点时账号正在运行，已挂起等待当前任务结束",
            details={"scheduled_run_key": run_key},
        )
        return True

    def _pop_pending_scheduled_run(self, account_id: str) -> str:
        with self._lock:
            return self._pending_scheduled_runs.pop(account_id, "")

    def _start_pending_scheduled_run(self, account_id: str, run_key: str) -> None:
        if not run_key:
            return
        account = self.state_service.get_account(account_id)
        if not account.schedule_enabled:
            return
        scheduled_date, _, _ = run_key.partition("|")
        if run_key != self._scheduled_run_key(scheduled_date, account.scheduled_start_time):
            return
        if self._already_ran_schedule(account, run_key):
            return
        self.start_account_flow(account_id, source="scheduled", scheduled_run_key=run_key)

    def _run_account_flow(self, account_id: str, source: str) -> None:
        try:
            task = self.payment_service.run_payment_flow(account_id, source=source)
            account = self.state_service.get_account(account_id)
            account.last_schedule_status = "success"
            if source == "probe":
                account.last_schedule_message = f"账号链路正常：{task.biz_id}"
            elif source == "stock_monitor":
                account.last_schedule_message = f"库存有货自动生成二维码成功：{task.biz_id}"
            else:
                account.last_schedule_message = f"生成二维码成功：{task.biz_id}"
            account.account_status = "valid"
            account.account_status_message = "账号链路正常" if source == "probe" else "最近一次执行成功"
            account.account_checked_at = utc_now_iso()
            self.state_service.update_account(account)
        except RunPausedError as exc:
            account = self.state_service.get_account(account_id)
            account.last_schedule_status = "paused"
            account.last_schedule_message = str(exc)
            self.state_service.update_account(account)
            self.runtime_logs.log_account_event(
                account_id=account_id,
                action="run_payment_flow",
                stage="scheduler",
                status="paused",
                message=str(exc),
            )
        except Exception as exc:
            account = self.state_service.get_account(account_id)
            account.last_schedule_status = "failed"
            account.last_schedule_message = f"账号链路异常：{exc}" if source == "probe" else str(exc)
            account.account_status = "error"
            account.account_status_message = f"账号链路异常：{exc}" if source == "probe" else str(exc)
            account.account_checked_at = utc_now_iso()
            self.state_service.update_account(account)
            logger.exception("scheduled account flow failed for %s: %s", account_id, exc)
            self.runtime_logs.log_account_event(
                account_id=account_id,
                action="run_payment_flow",
                stage="scheduler",
                status="failed",
                message=str(exc),
                details={"source": source, "error": exc.__class__.__name__},
                level=logging.ERROR,
            )
        finally:
            with self._lock:
                self._running_accounts.discard(account_id)
                self._flow_accounts.discard(account_id)
                self._pause_requested.discard(account_id)
            self._start_pending_scheduled_run(account_id, self._pop_pending_scheduled_run(account_id))


_scheduler_service: SchedulerService | None = None


def get_scheduler_service() -> SchedulerService:
    global _scheduler_service
    if _scheduler_service is None:
        _scheduler_service = SchedulerService()
    return _scheduler_service
