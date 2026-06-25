"""Structured runtime logging for formal production operations."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings

SENSITIVE_KEYS = {
    "authorization",
    "bigmodel_token_production",
    "collect",
    "cookie",
    "cookie_header",
    "cookies",
    "debug_image_base64",
    "eks",
    "image_base64",
    "pow_answer",
    "qr_base64",
    "randstr",
    "sign",
    "ticket",
    "ticket_value",
    "token",
    "randstr_value",
}


@dataclass(frozen=True)
class FlowRun:
    """Single runtime flow context."""

    run_id: str
    account_id: str
    action: str
    source: str = ""
    product_id: str = ""
    pay_type: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))


def configure_logging(settings: Settings | None = None) -> Path:
    """Attach a file handler for formal runtime logs."""

    settings = settings or get_settings()
    log_dir = settings.runtime_logs_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if getattr(handler, "_aegisflow_runtime_handler", False):
            return log_dir

    handler = TimedRotatingFileHandler(
        filename=str(log_dir / "app.log"),
        when="midnight",
        interval=1,
        backupCount=max(settings.runtime_log_retention_days, 3),
        encoding="utf-8",
    )
    handler._aegisflow_runtime_handler = True  # type: ignore[attr-defined]
    handler.setLevel(getattr(logging, settings.runtime_log_level.upper(), logging.INFO))
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(threadName)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root_logger.addHandler(handler)
    desired_level = getattr(logging, settings.runtime_log_level.upper(), logging.INFO)
    if root_logger.level == logging.NOTSET or root_logger.level > desired_level:
        root_logger.setLevel(desired_level)
    logging.captureWarnings(True)
    return log_dir


class RuntimeLogService:
    """Persist structured JSONL flow events alongside standard logs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = threading.Lock()
        self.logger = logging.getLogger("aegisflow.runtime")
        configure_logging(self.settings)
        (self.settings.runtime_logs_dir / "accounts").mkdir(parents=True, exist_ok=True)

    def start_run(
        self,
        *,
        account_id: str,
        action: str,
        source: str = "",
        product_id: str = "",
        pay_type: str = "",
        details: dict[str, Any] | None = None,
    ) -> FlowRun:
        run = FlowRun(
            run_id=self._make_run_id(),
            account_id=account_id,
            action=action.strip() or "flow",
            source=source.strip(),
            product_id=product_id.strip(),
            pay_type=pay_type.strip(),
        )
        self.log_event(
            run,
            stage="run",
            status="started",
            message=f"{run.action} started",
            details=details,
        )
        return run

    def finish_run(
        self,
        run: FlowRun,
        *,
        status: str,
        message: str,
        details: dict[str, Any] | None = None,
        level: int | None = None,
    ) -> None:
        resolved_level = level if level is not None else (logging.ERROR if status == "failed" else logging.INFO)
        self.log_event(
            run,
            stage="run",
            status=status,
            message=message,
            details=details,
            level=resolved_level,
        )

    def log_event(
        self,
        run: FlowRun,
        *,
        stage: str,
        status: str,
        message: str,
        details: dict[str, Any] | None = None,
        level: int = logging.INFO,
    ) -> None:
        entry = self._base_entry(
            stage=stage,
            status=status,
            message=message,
            account_id=run.account_id,
            run_id=run.run_id,
            action=run.action,
            source=run.source,
            product_id=run.product_id,
            pay_type=run.pay_type,
            details=details,
        )
        self._write_entry(entry)
        self.logger.log(
            level,
            "[%s][%s][%s/%s] %s",
            run.account_id,
            run.run_id,
            run.action,
            stage,
            message,
        )

    def log_account_event(
        self,
        *,
        account_id: str,
        action: str,
        stage: str,
        status: str,
        message: str,
        details: dict[str, Any] | None = None,
        source: str = "",
        product_id: str = "",
        pay_type: str = "",
        level: int = logging.INFO,
    ) -> None:
        run = FlowRun(
            run_id=self._make_run_id(),
            account_id=account_id,
            action=action.strip() or "event",
            source=source.strip(),
            product_id=product_id.strip(),
            pay_type=pay_type.strip(),
        )
        self.log_event(
            run,
            stage=stage,
            status=status,
            message=message,
            details=details,
            level=level,
        )

    def log_system_event(
        self,
        *,
        stage: str,
        status: str,
        message: str,
        details: dict[str, Any] | None = None,
        level: int = logging.INFO,
    ) -> None:
        entry = self._base_entry(
            stage=stage,
            status=status,
            message=message,
            account_id="system",
            run_id=self._make_run_id(prefix="sys"),
            action="system",
            source="server",
            product_id="",
            pay_type="",
            details=details,
        )
        self._write_entry(entry)
        self.logger.log(level, "[system][%s] %s", stage, message)

    def _base_entry(
        self,
        *,
        stage: str,
        status: str,
        message: str,
        account_id: str,
        run_id: str,
        action: str,
        source: str,
        product_id: str,
        pay_type: str,
        details: dict[str, Any] | None,
    ) -> dict[str, Any]:
        now = datetime.now().astimezone()
        return {
            "timestamp": now.isoformat(timespec="seconds"),
            "date": now.strftime("%Y-%m-%d"),
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
            "account_id": account_id,
            "run_id": run_id,
            "action": action,
            "source": source,
            "stage": stage,
            "status": status,
            "product_id": product_id,
            "pay_type": pay_type,
            "message": message,
            "details": self._sanitize_value(details or {}),
        }

    def _write_entry(self, entry: dict[str, Any]) -> None:
        date_part = str(entry.get("date") or datetime.now().strftime("%Y-%m-%d"))
        payload = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._append_line(self.settings.runtime_logs_dir / f"events-{date_part}.jsonl", payload)
            account_id = str(entry.get("account_id") or "").strip()
            if account_id and account_id != "system":
                account_dir = self.settings.runtime_logs_dir / "accounts" / account_id
                account_dir.mkdir(parents=True, exist_ok=True)
                self._append_line(account_dir / f"{date_part}.jsonl", payload)

    def _append_line(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")

    def _make_run_id(self, *, prefix: str = "run") -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"{prefix}-{stamp}-{secrets.token_hex(3)}"

    def _sanitize_value(self, value: Any, *, key: str = "") -> Any:
        key_lower = key.lower()
        if key_lower in SENSITIVE_KEYS:
            return self._mask_string(value)
        if isinstance(value, dict):
            return {str(item_key): self._sanitize_value(item_val, key=str(item_key)) for item_key, item_val in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._sanitize_value(item, key=key) for item in value]
        if isinstance(value, str):
            if len(value) > 600:
                return value[:280] + "...<trimmed>"
            return value
        if isinstance(value, bytes):
            return f"<bytes:{len(value)}>"
        if isinstance(value, Path):
            return str(value)
        if hasattr(value, "item"):
            return value.item()
        return value

    def _mask_string(self, value: Any) -> str:
        raw = str(value or "")
        if not raw:
            return ""
        if len(raw) <= 12:
            return raw[:2] + "***"
        return raw[:6] + "***" + raw[-4:]


@lru_cache(maxsize=1)
def get_runtime_log_service() -> RuntimeLogService:
    """Get the shared runtime log service."""

    return RuntimeLogService()
