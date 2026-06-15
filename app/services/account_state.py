"""Account/session/task persistence and helpers."""

from __future__ import annotations

import secrets
import shutil
import logging
from datetime import datetime, timezone
from functools import lru_cache
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.browser_profiles import random_browser_impersonate, resolve_browser_impersonate
from app.config import get_settings
from app.errors import BadRequestError, NotFoundError
from app.models import (
    AccountPreferencesRequest,
    AccountDetailResponse,
    AccountImportRequest,
    AccountRecord,
    AccountSessionState,
    PaymentTaskRecord,
    PublicAccountRecord,
)
from app.runtime_logging import get_runtime_log_service
from app.storage.json_store import JsonFileStore

TOKEN_COOKIE_KEY = "bigmodel_token_production"
DEFAULT_INVITATION_CODE = "XOJGYOGNLN"
DEFAULT_SCHEDULED_START_TIME = "09:59:58"
DEFAULT_PREVIEW_CONCURRENCY = 1
MAX_PREVIEW_CONCURRENCY = 4

try:
    SCHEDULE_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # pragma: no cover - Windows without tzdata
    SCHEDULE_TZ = None

logger = logging.getLogger(__name__)


class AccountStateService:
    """Manage local JSON-backed account state."""

    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings
        self.accounts_store = JsonFileStore(settings.accounts_path, default_factory=list)
        self.tasks_store = JsonFileStore(settings.tasks_path, default_factory=list)

    def list_accounts(self) -> list[PublicAccountRecord]:
        accounts = [AccountRecord.model_validate(item) for item in self.accounts_store.read()]
        accounts.sort(key=lambda item: item.updated_at, reverse=True)
        return [self.to_public_account(account) for account in accounts]

    def get_account(self, account_id: str) -> AccountRecord:
        for item in self.accounts_store.read():
            account = AccountRecord.model_validate(item)
            if account.id == account_id:
                return account
        raise NotFoundError("账号不存在", details={"account_id": account_id})

    def import_account(self, request: AccountImportRequest) -> PublicAccountRecord:
        cookies = self._merge_cookies(request.token, request.cookie_header, request.cookies)
        token = (request.token or "").strip() or cookies.get(TOKEN_COOKIE_KEY, "")
        if not token:
            raise BadRequestError(
                f"凭据里缺少 `{TOKEN_COOKIE_KEY}`，后续请求没法发，别整这没头没尾的半截登录态。",
            )

        cookie_header = (request.cookie_header or "").strip() or self._cookies_to_header(cookies)
        now = utc_now_iso()
        resolved_account_id = {"value": (request.id or "").strip()}

        def updater(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
            account_id = resolved_account_id["value"]
            index = -1
            if account_id:
                for idx, item in enumerate(records):
                    if item.get("id") == account_id:
                        index = idx
                        break
            else:
                for idx, item in enumerate(records):
                    if item.get("label") == request.label:
                        index = idx
                        account_id = str(item.get("id") or "")
                        break
            if not account_id:
                account_id = make_id("acct")
            resolved_account_id["value"] = account_id

            existing = records[index] if index >= 0 else None
            created_at = str(existing.get("created_at")) if existing else now
            last_bootstrap_at = existing.get("last_bootstrap_at") if existing else None
            existing_impersonate = str(existing.get("browser_impersonate") or "") if existing else ""
            requested_impersonate = (request.browser_impersonate or "").strip()
            if requested_impersonate:
                browser_impersonate = resolve_browser_impersonate(requested_impersonate)
            elif existing_impersonate:
                browser_impersonate = resolve_browser_impersonate(existing_impersonate)
            else:
                browser_impersonate = random_browser_impersonate()
            record = AccountRecord(
                id=account_id,
                label=request.label,
                token=token,
                cookie_header=cookie_header,
                cookies=cookies,
                org_id=request.org_id.strip(),
                project_id=request.project_id.strip(),
                invitation_code=request.invitation_code.strip() or DEFAULT_INVITATION_CODE,
                proxy_url=request.proxy_url.strip(),
                user_agent=request.user_agent.strip(),
                browser_impersonate=browser_impersonate,
                preview_concurrency=_clamp_preview_concurrency(existing.get("preview_concurrency") if existing else 1),
                schedule_enabled=bool(existing.get("schedule_enabled")) if existing else False,
                scheduled_start_time=str(existing.get("scheduled_start_time") or DEFAULT_SCHEDULED_START_TIME) if existing else DEFAULT_SCHEDULED_START_TIME,
                last_scheduled_run_at=existing.get("last_scheduled_run_at") if existing else None,
                last_scheduled_run_key=str(existing.get("last_scheduled_run_key") or "") if existing else "",
                last_manual_run_at=existing.get("last_manual_run_at") if existing else None,
                last_schedule_status=str(existing.get("last_schedule_status") or "") if existing else "",
                last_schedule_message=str(existing.get("last_schedule_message") or "") if existing else "",
                account_status=str(existing.get("account_status") or "unchecked") if existing else "unchecked",
                account_status_message=str(existing.get("account_status_message") or "") if existing else "",
                account_checked_at=existing.get("account_checked_at") if existing else None,
                created_at=created_at,
                updated_at=now,
                last_bootstrap_at=last_bootstrap_at,
            ).model_dump()
            if index >= 0:
                records[index] = record
            else:
                records.append(record)
            return records

        updated_records = self.accounts_store.update(updater)
        account = next(
            AccountRecord.model_validate(item)
            for item in updated_records
            if item.get("id") == resolved_account_id["value"]
        )

        session = self.load_session(account.id)
        session.org_id = account.org_id or session.org_id
        session.project_id = account.project_id or session.project_id
        session.updated_at = now
        self.save_session(session)
        get_runtime_log_service().log_account_event(
            account_id=account.id,
            action="account_import",
            stage="account",
            status="success",
            message="账号导入成功",
            details={
                "label": account.label,
                "browser_impersonate": account.browser_impersonate,
                "has_cookie_header": bool(account.cookie_header),
                "schedule_enabled": account.schedule_enabled,
                "scheduled_start_time": account.scheduled_start_time,
            },
        )
        return self.to_public_account(account)

    def update_account(self, account: AccountRecord, *, touch_updated_at: bool = False) -> AccountRecord:
        if touch_updated_at:
            account.updated_at = utc_now_iso()

        def updater(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
            for index, item in enumerate(records):
                if item.get("id") == account.id:
                    records[index] = account.model_dump()
                    return records
            raise NotFoundError("账号不存在", details={"account_id": account.id})

        self.accounts_store.update(updater)
        return account

    def touch_account_updated_at(self, account_id: str) -> AccountRecord:
        updated_account: AccountRecord | None = None
        now = utc_now_iso()

        def updater(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
            nonlocal updated_account
            for index, item in enumerate(records):
                if item.get("id") != account_id:
                    continue
                account = AccountRecord.model_validate(item)
                account.updated_at = now
                records[index] = account.model_dump()
                updated_account = account
                return records
            raise NotFoundError("账号不存在", details={"account_id": account_id})

        self.accounts_store.update(updater)
        return updated_account or self.get_account(account_id)

    def update_runtime_progress(
        self,
        account_id: str,
        *,
        schedule_status: str | None = None,
        schedule_message: str | None = None,
        account_status_message: str | None = None,
    ) -> AccountRecord:
        updated_account: AccountRecord | None = None

        def updater(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
            nonlocal updated_account
            for index, item in enumerate(records):
                if item.get("id") != account_id:
                    continue
                account = AccountRecord.model_validate(item)
                if schedule_status is not None:
                    account.last_schedule_status = schedule_status.strip() or account.last_schedule_status
                if schedule_message is not None:
                    account.last_schedule_message = schedule_message.strip()
                if account_status_message is not None:
                    account.account_status_message = account_status_message.strip()
                records[index] = account.model_dump()
                updated_account = account
                return records
            raise NotFoundError("账号不存在", details={"account_id": account_id})

        self.accounts_store.update(updater)
        return updated_account or self.get_account(account_id)

    def rotate_browser_impersonate(self, account_id: str) -> AccountRecord:
        account = self.get_account(account_id)
        current = resolve_browser_impersonate(account.browser_impersonate)
        browser_impersonate = current
        for _ in range(8):
            candidate = random_browser_impersonate()
            if candidate != current:
                browser_impersonate = candidate
                break
        account.browser_impersonate = browser_impersonate
        return self.update_account(account)

    def update_preferences(self, account_id: str, request: AccountPreferencesRequest) -> AccountDetailResponse:
        account = self.get_account(account_id)
        session = self.load_session(account_id)
        previous_schedule_enabled = account.schedule_enabled
        previous_scheduled_start_time = account.scheduled_start_time

        if request.invitation_code is not None:
            account.invitation_code = request.invitation_code.strip() or DEFAULT_INVITATION_CODE
        if request.schedule_enabled is not None:
            account.schedule_enabled = bool(request.schedule_enabled)
        if request.scheduled_start_time is not None:
            account.scheduled_start_time = request.scheduled_start_time.strip() or DEFAULT_SCHEDULED_START_TIME
        if request.selected_product_id is not None:
            session.selected_product_id = request.selected_product_id.strip()
        if request.preview_concurrency is not None:
            account.preview_concurrency = _clamp_preview_concurrency(request.preview_concurrency)
        if self._should_skip_today_after_schedule_update(
            account=account,
            previous_schedule_enabled=previous_schedule_enabled,
            previous_scheduled_start_time=previous_scheduled_start_time,
            request=request,
        ):
            current_date, _ = self._current_schedule_date_time()
            account.last_scheduled_run_key = self._scheduled_run_key(current_date, account.scheduled_start_time)

        self.update_account(account, touch_updated_at=False)
        self.save_session(session)
        return self.get_account_detail(account_id)

    def _should_skip_today_after_schedule_update(
        self,
        *,
        account: AccountRecord,
        previous_schedule_enabled: bool,
        previous_scheduled_start_time: str,
        request: AccountPreferencesRequest,
    ) -> bool:
        if not account.schedule_enabled or not account.scheduled_start_time:
            return False
        schedule_touched = request.schedule_enabled is not None or request.scheduled_start_time is not None
        if not schedule_touched:
            return False
        enabled_now = request.schedule_enabled is True and not previous_schedule_enabled
        time_changed = (
            request.scheduled_start_time is not None
            and account.scheduled_start_time != (previous_scheduled_start_time or "")
        )
        if not enabled_now and not time_changed:
            return False
        _, current_hms = self._current_schedule_date_time()
        return account.scheduled_start_time <= current_hms

    def _current_schedule_date_time(self) -> tuple[str, str]:
        now = datetime.now(SCHEDULE_TZ) if SCHEDULE_TZ is not None else datetime.now().astimezone()
        return now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")

    def _scheduled_run_key(self, current_date: str, scheduled_start_time: str) -> str:
        return f"{current_date}|{(scheduled_start_time or '').strip()}"

    def load_session(self, account_id: str) -> AccountSessionState:
        path = self._session_path(account_id)
        store = JsonFileStore(
            path,
            default_factory=lambda: AccountSessionState(
                account_id=account_id,
                updated_at=utc_now_iso(),
            ).model_dump(),
        )
        return AccountSessionState.model_validate(store.read())

    def save_session(self, session: AccountSessionState) -> AccountSessionState:
        session.updated_at = utc_now_iso()
        store = JsonFileStore(
            self._session_path(session.account_id),
            default_factory=dict,
        )
        store.write(session.model_dump())
        return session

    def list_tasks(self, account_id: str | None = None) -> list[PaymentTaskRecord]:
        tasks = [PaymentTaskRecord.model_validate(item) for item in self.tasks_store.read()]
        if account_id:
            tasks = [task for task in tasks if task.account_id == account_id]
        tasks.sort(key=lambda item: item.updated_at, reverse=True)
        return tasks

    def save_task(self, task: PaymentTaskRecord) -> PaymentTaskRecord:
        def updater(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
            for index, item in enumerate(records):
                if item.get("id") == task.id:
                    records[index] = task.model_dump()
                    return records
            records.append(task.model_dump())
            return records

        self.tasks_store.update(updater)
        return task

    def clear_payment_cache(self) -> None:
        """Discard QR/payment task cache that should not survive service restarts."""
        self.tasks_store.write([])
        for account in self.list_accounts():
            session = self.load_session(account.id)
            session.last_sign = ""
            session.last_order_id = ""
            session.preview = None
            self.save_session(session)
        logger.info("payment cache cleared on startup")

    def delete_account(self, account_id: str) -> None:
        account = self.get_account(account_id)

        def update_accounts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [item for item in records if item.get("id") != account_id]

        def update_tasks(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [item for item in records if item.get("account_id") != account_id]

        self.accounts_store.update(update_accounts)
        self.tasks_store.update(update_tasks)
        session_path = self._session_path(account_id)
        if session_path.exists():
            session_path.unlink()
        self._remove_account_artifacts(account_id)
        get_runtime_log_service().log_account_event(
            account_id=account_id,
            action="account_delete",
            stage="account",
            status="success",
            message="账号及本地缓存已删除",
            details={"label": account.label},
        )

    def get_task_by_biz_id(self, account_id: str, biz_id: str) -> PaymentTaskRecord | None:
        for task in self.list_tasks(account_id):
            if task.biz_id == biz_id:
                return task
        return None

    def get_account_detail(self, account_id: str) -> AccountDetailResponse:
        account = self.get_account(account_id)
        session = self.load_session(account_id)
        tasks = self.list_tasks(account_id)
        return AccountDetailResponse(
            account=self.to_public_account(account),
            session=session,
            tasks=tasks,
        )

    def to_public_account(self, account: AccountRecord) -> PublicAccountRecord:
        return PublicAccountRecord(
            id=account.id,
            label=account.label,
            org_id=account.org_id,
            project_id=account.project_id,
            invitation_code=account.invitation_code,
            proxy_url=account.proxy_url,
            user_agent=account.user_agent,
            browser_impersonate=resolve_browser_impersonate(account.browser_impersonate),
            preview_concurrency=account.preview_concurrency,
            schedule_enabled=account.schedule_enabled,
            scheduled_start_time=account.scheduled_start_time,
            last_scheduled_run_at=account.last_scheduled_run_at,
            last_scheduled_run_key=account.last_scheduled_run_key,
            last_manual_run_at=account.last_manual_run_at,
            last_schedule_status=account.last_schedule_status,
            last_schedule_message=account.last_schedule_message,
            account_status=account.account_status,
            account_status_message=account.account_status_message,
            account_checked_at=account.account_checked_at,
            has_token=bool(account.token),
            token_preview=mask_secret(account.token),
            has_cookie_header=bool(account.cookie_header),
            last_bootstrap_at=account.last_bootstrap_at,
            created_at=account.created_at,
            updated_at=account.updated_at,
        )

    def set_account_status(
        self,
        account_id: str,
        *,
        status: str,
        message: str = "",
    ) -> AccountRecord:
        account = self.get_account(account_id)
        account.account_status = status.strip() or "unchecked"
        account.account_status_message = message.strip()
        account.account_checked_at = utc_now_iso()
        return self.update_account(account)

    def _session_path(self, account_id: str):
        return self.settings.sessions_dir / f"{account_id}.json"

    def _remove_account_artifacts(self, account_id: str) -> None:
        for root in (self.settings.data_dir / "logs", self.settings.data_dir / "test_runs"):
            if not root.exists():
                continue
            for path in list(root.iterdir()):
                try:
                    if self._path_matches_account(path, account_id):
                        if path.is_dir():
                            shutil.rmtree(path, ignore_errors=True)
                        else:
                            path.unlink(missing_ok=True)
                except Exception:
                    # best-effort cleanup only; primary account deletion must still succeed
                    continue

    def _path_matches_account(self, path: Path, account_id: str) -> bool:
        if account_id in path.name:
            return True
        if path.is_file():
            return self._file_contains_account(path, account_id)
        for child in path.rglob("*"):
            if account_id in child.name:
                return True
            if child.is_file() and self._file_contains_account(child, account_id):
                return True
        return False

    def _file_contains_account(self, path: Path, account_id: str) -> bool:
        try:
            return account_id.encode("utf-8") in path.read_bytes()
        except Exception:
            return False

    def _merge_cookies(
        self,
        token: str | None,
        cookie_header: str | None,
        cookies: dict[str, str],
    ) -> dict[str, str]:
        merged: dict[str, str] = {}
        merged.update(self._parse_cookie_header(cookie_header))
        merged.update({str(key): str(value) for key, value in (cookies or {}).items()})
        if token and token.strip():
            merged[TOKEN_COOKIE_KEY] = token.strip()
        return merged

    def _parse_cookie_header(self, cookie_header: str | None) -> dict[str, str]:
        header = (cookie_header or "").strip()
        if not header:
            return {}
        cookie = SimpleCookie()
        try:
            cookie.load(header)
            if cookie:
                return {key: morsel.value for key, morsel in cookie.items()}
        except Exception:
            pass

        parsed: dict[str, str] = {}
        for segment in header.split(";"):
            if "=" not in segment:
                continue
            key, value = segment.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key:
                parsed[key] = value
        return parsed

    def _cookies_to_header(self, cookies: dict[str, str]) -> str:
        return "; ".join(f"{key}={value}" for key, value in cookies.items())


@lru_cache(maxsize=1)
def get_account_state_service() -> AccountStateService:
    """Get the shared state service."""
    return AccountStateService()


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def make_id(prefix: str) -> str:
    """Create a short local identifier."""
    return f"{prefix}-{secrets.token_hex(4)}"


def mask_secret(value: str) -> str:
    """Mask a secret for UI display."""
    normalized = (value or "").strip()
    if not normalized:
        return ""
    if len(normalized) <= 8:
        return f"{normalized[:2]}***{normalized[-2:]}"
    return f"{normalized[:4]}***{normalized[-4:]}"


def _clamp_preview_concurrency(value: Any) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = DEFAULT_PREVIEW_CONCURRENCY
    return max(1, min(MAX_PREVIEW_CONCURRENCY, normalized))
