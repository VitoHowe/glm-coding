"""Background scheduler for timed payment flow execution."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.errors import GlmDeskError
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
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._clear_stale_schedule_statuses()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="glm-desk-scheduler", daemon=True)
        self._thread.start()
        threading.Thread(target=self.check_cached_accounts_once, name="glm-desk-account-check", daemon=True).start()
        self.runtime_logs.log_system_event(
            stage="scheduler",
            status="started",
            message="调度器已启动",
        )

    def stop(self) -> None:
        self._stop_event.set()
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
            if public_account.scheduled_start_time > current_hms:
                continue
            account = self.state_service.get_account(public_account.id)
            run_key = self._scheduled_run_key(current_date, account.scheduled_start_time)
            if self._already_ran_schedule(account, run_key):
                continue
            if self._queue_scheduled_run_if_busy(account.id, run_key):
                continue
            self.start_account_flow(account.id, source="scheduled", scheduled_run_key=run_key)

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
            except GlmDeskError as exc:
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
            name=f"glm-desk-run-{account_id}",
            daemon=True,
        ).start()
        return {"started": True, "status": "running"}

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
            account.last_schedule_message = f"账号链路正常：{task.biz_id}" if source == "probe" else f"生成二维码成功：{task.biz_id}"
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
