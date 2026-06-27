"""Business orchestration for BigModel payment flow."""

from __future__ import annotations

import base64
import concurrent.futures
import logging
import socket
import threading
import time
from urllib.parse import urlparse
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from typing import Any

import qrcode

from app.clients.bigmodel_client import BigModelClient, get_bigmodel_client
from app.clients.tencent_captcha_client import TencentCaptchaClient, get_tencent_captcha_client
from app.config import get_settings
from app.errors import AegisFlowError, BadRequestError, UpstreamRequestError
from app.proxy_pool.service import get_builtin_proxy_pool_service, is_local_proxy_url
from app.models import (
    AccountDetailResponse,
    AccountRecord,
    AccountSessionState,
    AccountPreferencesRequest,
    CaptchaVerifyPayloadRequest,
    CreateQrRequest,
    ManualCaptchaRequest,
    PaymentCheckResult,
    PaymentTaskRecord,
    PreviewPaymentRequest,
    PreviewResult,
    PreviewSeedRequest,
    ProductOffer,
)
from app.runtime_logging import FlowRun, RuntimeLogService, get_runtime_log_service
from app.services.account_state import (
    AccountStateService,
    SCHEDULE_TZ,
    get_account_state_service,
    make_id,
    utc_now_iso,
)
from app.services.captcha_service import CaptchaService, get_captcha_service
from app.services.network_mode_service import get_network_mode_service
from app.services.ocr_service import OcrService, get_ocr_service
from app.services.tdc_service import TencentTdcService, get_tdc_service


@dataclass(frozen=True)
class StaticProduct:
    product_id: str
    product_name: str
    unit: str
    sale_price: str
    plan_type: str
    version: str = "v2"


@dataclass(frozen=True)
class PreviewRaceWinner:
    """Winning isolated preview attempt result."""

    preview: PreviewResult
    ticket: str
    randstr: str
    round: int
    lane: int


PREVIEW_RACE_MAX_ROUNDS = 999


STATIC_PRODUCTS: tuple[StaticProduct, ...] = (
    StaticProduct("product-02434c", "Lite", "month", "49", "lite"),
    StaticProduct("product-1df3e1", "Pro", "month", "149", "pro"),
    StaticProduct("product-2fc421", "Max", "month", "469", "max"),
    StaticProduct("product-b8ea38", "Lite", "quarter", "132.3", "lite"),
    StaticProduct("product-fef82f", "Pro", "quarter", "402.3", "pro"),
    StaticProduct("product-5d3a03", "Max", "quarter", "1266.3", "max"),
    StaticProduct("product-70a804", "Lite", "year", "470.4", "lite"),
    StaticProduct("product-5643e6", "Pro", "year", "1430.4", "pro"),
    StaticProduct("product-d46f8b", "Max", "year", "4502.4", "max"),
)

DEFAULT_PRODUCT_ID = "product-5643e6"
logger = logging.getLogger(__name__)


class RunPausedError(Exception):
    """Raised when an account flow is paused by the operator."""


class PaymentService:
    """Coordinate account bootstrap, preview, QR creation, and polling."""

    def __init__(
        self,
        *,
        state_service: AccountStateService | None = None,
        captcha_service: CaptchaService | None = None,
        bigmodel_client: BigModelClient | None = None,
        tencent_captcha_client: TencentCaptchaClient | None = None,
        ocr_service: OcrService | None = None,
        tdc_service: TencentTdcService | None = None,
        runtime_log_service: RuntimeLogService | None = None,
    ) -> None:
        self.state_service = state_service or get_account_state_service()
        self.captcha_service = captcha_service or get_captcha_service()
        self.bigmodel_client = bigmodel_client or get_bigmodel_client()
        self.tencent_captcha_client = tencent_captcha_client or get_tencent_captcha_client()
        self.ocr_service = ocr_service or get_ocr_service()
        self.tdc_service = tdc_service or get_tdc_service()
        self.runtime_logs = runtime_log_service or get_runtime_log_service()
        self.settings = get_settings()

    def health_payload(self) -> dict[str, Any]:
        ocr_status = self.ocr_service.status_payload()
        tdc_status = self.tdc_service.status_payload()
        proxy_status = self._fallback_proxy_health_payload()
        network_status = get_network_mode_service().status_payload()
        problems: list[str] = []
        if not bool(ocr_status.get("available")):
            missing = ocr_status.get("missing_dependencies") or []
            if not bool(ocr_status.get("enabled")):
                problems.append("OCR 已关闭：请设置 TENCENT_OCR_ENABLED=1")
            elif missing:
                problems.append(f"OCR 依赖不可用：{missing}")
            elif ocr_status.get("last_bootstrap_error"):
                problems.append(f"OCR 模型初始化失败：{ocr_status.get('last_bootstrap_error')}")
            elif ocr_status.get("last_warmup_error"):
                problems.append(f"OCR worker 预热失败：{ocr_status.get('last_warmup_error')}")
            else:
                problems.append("OCR 暂不可用：请查看运行日志")
        if not bool(tdc_status.get("available")):
            problems.extend(str(item) for item in (tdc_status.get("problems") or []))
        if network_status.get("mode") == "proxy_pool" and not bool(proxy_status.get("available")):
            problems.append(str(proxy_status.get("message") or "代理池不可用"))
        return {
            "status": "ok" if not problems else "degraded",
            "problems": problems,
            "transport": self.bigmodel_client.transport_name,
            "ocr": ocr_status,
            "tdc": tdc_status,
            "proxy": proxy_status,
            "network": network_status,
        }

    def _fallback_proxy_health_payload(self) -> dict[str, Any]:
        proxy_url = self.settings.fallback_proxy_url.strip()
        if not proxy_url:
            return {"enabled": False, "available": False, "url": "", "message": "未配置全局代理池"}

        parsed = urlparse(proxy_url)
        host = parsed.hostname or ""
        port = parsed.port
        if not host or not port:
            return {
                "enabled": True,
                "available": False,
                "url": proxy_url,
                "message": f"代理池地址格式异常：{proxy_url}",
            }

        try:
            with socket.create_connection((host, port), timeout=0.5):
                pass
        except OSError as exc:
            return {
                "enabled": True,
                "available": False,
                "url": proxy_url,
                "host": host,
                "port": port,
                "message": f"代理池不可用：{proxy_url} ({exc})",
            }

        if is_local_proxy_url(proxy_url):
            try:
                builtin_status = get_builtin_proxy_pool_service().status_payload(port=port)
            except Exception as exc:
                return {
                    "enabled": True,
                    "available": False,
                    "url": proxy_url,
                    "host": host,
                    "port": port,
                    "service": "python",
                    "message": f"Python 代理池配置不可用：{exc}",
                }
            return {
                "enabled": True,
                "available": bool(builtin_status.get("available")),
                "url": proxy_url,
                "host": host,
                "port": port,
                **builtin_status,
            }

        return {
            "enabled": True,
            "available": True,
            "url": proxy_url,
            "host": host,
            "port": port,
            "message": f"代理池已连接：{proxy_url}",
        }

    def _captcha_ticket_log_details(self, ticket: str, randstr: str) -> dict[str, Any]:
        return {
            "ticket_value": ticket,
            "randstr_value": randstr,
            "ticket_length": len(ticket or ""),
            "randstr_length": len(randstr or ""),
            "ticket_ready": bool(ticket),
            "randstr_ready": bool(randstr),
        }

    def _preview_response_log_details(
        self,
        *,
        raw: dict[str, Any],
        ticket: str,
        randstr: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        code = raw.get("code")
        msg = str(raw.get("msg") or raw.get("message") or "")
        biz_id = str(data.get("bizId") or "").strip()
        # code_desc helps diagnose ticket-expiry vs other failures at a glance
        if code == 200 and biz_id:
            code_desc = "success_with_biz_id"
        elif code == 200:
            code_desc = "success_but_no_biz_id"
        elif code == 555:
            code_desc = "system_busy_555"
        elif code == 401:
            code_desc = "unauthorized_ticket_expired_or_invalid_401"
        elif code == 400:
            code_desc = "bad_request_400"
        elif code is None:
            code_desc = "no_code_in_response"
        else:
            code_desc = f"other_code_{code}"
        return {
            **(extra or {}),
            **self._captcha_ticket_log_details(ticket, randstr),
            "code": code,
            "code_desc": code_desc,
            "msg": msg,
            "biz_id": biz_id,
            "sold_out": bool(data.get("soldOut")),
            "data": data,
            "preview_response": raw,
        }

    def _preview_concurrency_wait_seconds(self, target_time: str) -> float:
        parts = target_time.split(":")
        if len(parts) != 3:
            return 0.0
        try:
            hour = int(parts[0])
            minute = int(parts[1])
            second = int(parts[2])
        except ValueError:
            return 0.0
        now = datetime.now(SCHEDULE_TZ) if SCHEDULE_TZ is not None else datetime.now().astimezone()
        target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        return max(0.0, (target - now).total_seconds())

    def _wait_preview_concurrency_time(
        self,
        account_id: str,
        *,
        target_time: str,
        lane: int,
        round_no: int,
        stop_event: threading.Event,
        flow: FlowRun | None,
        ticket: str,
        randstr: str,
    ) -> None:
        wait_seconds = self._preview_concurrency_wait_seconds(target_time)
        details = {
            "lane": lane,
            "round": round_no,
            "race": True,
            "preview_concurrency_time": target_time,
            "wait_seconds": round(wait_seconds, 3),
            **self._captcha_ticket_log_details(ticket, randstr),
        }
        if wait_seconds <= 0:
            self.runtime_logs.log_event(
                flow,
                stage="preview_race_wait",
                status="skipped",
                message=f"preview 竞速 lane {lane} 并发时间已过，立即请求 preview",
                details=details,
            )
            return

        self.runtime_logs.log_event(
            flow,
            stage="preview_race_wait",
            status="waiting",
            message=f"preview 竞速 lane {lane} 已拿到 ticket，等待并发时间 {target_time}",
            details=details,
        )
        remaining = wait_seconds
        while remaining > 0:
            if stop_event.wait(min(0.1, remaining)):
                raise RunPausedError("preview race 已有其他任务胜出")
            self._ensure_not_paused(account_id)
            remaining = self._preview_concurrency_wait_seconds(target_time)
        self.runtime_logs.log_event(
            flow,
            stage="preview_race_wait",
            status="ready",
            message=f"preview 竞速 lane {lane} 并发时间已到，开始请求 preview",
            details={**details, "actual_wait_seconds": round(wait_seconds, 3)},
        )

    def list_accounts(self):
        return self.state_service.list_accounts()

    def get_account_detail(self, account_id: str) -> AccountDetailResponse:
        return self.state_service.get_account_detail(account_id)

    def import_account(self, request):
        record = self.state_service.import_account(request)
        # 导入后立即同步账号信息（客户信息/组织/套餐），失败不阻断导入
        try:
            self.bootstrap_account(record.id)
        except Exception as exc:
            logger.warning("导入后自动同步账号 %s 失败: %s", record.id, exc)
        return record

    def update_preferences(self, account_id: str, request: AccountPreferencesRequest) -> AccountDetailResponse:
        return self.state_service.update_preferences(account_id, request)

    def delete_account(self, account_id: str) -> dict[str, Any]:
        self.state_service.delete_account(account_id)
        return {"account_id": account_id, "deleted": True}

    def bootstrap_account(
        self,
        account_id: str,
        *,
        refresh_fingerprint: bool = False,
        flow: FlowRun | None = None,
    ) -> AccountDetailResponse:
        own_flow = flow is None
        if own_flow:
            flow = self.runtime_logs.start_run(
                account_id=account_id,
                action="bootstrap_account",
                source="refresh_fingerprint" if refresh_fingerprint else "manual",
            )
        assert flow is not None
        max_attempts = self.settings.bootstrap_fingerprint_max_retries if refresh_fingerprint else 1
        for attempt in range(1, max_attempts + 1):
            try:
                detail = self._bootstrap_account_once(
                    account_id,
                    refresh_fingerprint=refresh_fingerprint,
                    flow=flow,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                if own_flow:
                    self.runtime_logs.finish_run(
                        flow,
                        status="success",
                        message="账号同步完成",
                        details={
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "product_count": len(detail.session.products),
                            "selected_product_id": detail.session.selected_product_id,
                            "purchase_mode": detail.session.purchase_mode,
                        },
                    )
                return detail
            except Exception as exc:
                if attempt < max_attempts:
                    self.runtime_logs.log_event(
                        flow,
                        stage="bootstrap_retry",
                        status="retry",
                        message=f"账号同步失败，准备更换指纹重试（{attempt}/{max_attempts}）：{exc}",
                        details={
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "error": exc.__class__.__name__,
                        },
                        level=logging.WARNING,
                    )
                    continue
                account = self.state_service.get_account(account_id)
                account.account_status = "error"
                account.account_status_message = str(exc)
                account.account_checked_at = utc_now_iso()
                self.state_service.update_account(account)
                if own_flow:
                    self.runtime_logs.finish_run(
                        flow,
                        status="failed",
                        message=f"账号同步失败：{exc}",
                        details={
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "error": exc.__class__.__name__,
                        },
                        level=logging.ERROR,
                    )
                raise
        raise RuntimeError("bootstrap retry loop exited unexpectedly")

    def _bootstrap_account_once(
        self,
        account_id: str,
        *,
        refresh_fingerprint: bool,
        flow: FlowRun,
        attempt: int,
        max_attempts: int,
    ) -> AccountDetailResponse:
        if refresh_fingerprint:
            rotated = self.state_service.rotate_browser_impersonate(account_id)
            self.runtime_logs.log_event(
                flow,
                stage="fingerprint",
                status="rotated",
                message="账号指纹已刷新",
                details={
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "browser_impersonate": rotated.browser_impersonate,
                },
            )
        account = self.state_service.get_account(account_id)
        session = self.state_service.load_session(account_id)

        self.runtime_logs.log_event(
            flow,
            stage="get_customer_info",
            status="started",
            message="开始同步账号上下文",
            details={
                "attempt": attempt,
                "max_attempts": max_attempts,
                "browser_impersonate": account.browser_impersonate,
            },
        )
        user_info_result = self.bigmodel_client.get_customer_info(account, session)
        user_info = self._ensure_dict(user_info_result.data, label="getCustomerInfo.data")
        organizations = self._extract_organizations(user_info)
        org_id, project_id = self._choose_org_and_project(
            organizations=organizations,
            preferred_org_id=account.org_id or session.org_id,
            preferred_project_id=account.project_id or session.project_id,
        )

        session.org_id = org_id
        session.project_id = project_id
        session.customer_number = str(user_info.get("customerNumber") or "")
        session.customer_name = str(user_info.get("customerName") or "")
        session.user_info = user_info
        session.organizations = organizations

        if not session.customer_number:
            raise UpstreamRequestError(
                "getCustomerInfo 没返回 customerNumber，后面 create-sign 根本没法拼。",
                details={"payload": user_info_result.raw},
            )

        self.runtime_logs.log_event(
            flow,
            stage="get_customer_info",
            status="success",
            message="账号上下文同步成功",
            details={
                "customer_number": session.customer_number,
                "customer_name": session.customer_name,
                "org_id": org_id,
                "project_id": project_id,
                "organization_count": len(organizations),
            },
        )

        account.org_id = org_id
        account.project_id = project_id
        account.last_bootstrap_at = utc_now_iso()
        self.state_service.update_account(account)

        products = self.load_products(
            account_id,
            invitation_code=account.invitation_code,
            account=account,
            session=session,
            flow=flow,
        )
        session.products = products
        if not session.selected_product_id:
            session.selected_product_id = DEFAULT_PRODUCT_ID
        self.state_service.save_session(session)
        account = self.state_service.get_account(account_id)
        account.account_status = "valid"
        account.account_status_message = "同步上下文和套餐成功"
        account.account_checked_at = utc_now_iso()
        self.state_service.update_account(account)
        return self.state_service.get_account_detail(account_id)

    def load_products(
        self,
        account_id: str,
        *,
        invitation_code: str | None = None,
        account: AccountRecord | None = None,
        session: AccountSessionState | None = None,
        flow: FlowRun | None = None,
    ) -> list[ProductOffer]:
        own_flow = flow is None
        if own_flow:
            flow = self.runtime_logs.start_run(
                account_id=account_id,
                action="load_products",
                source="manual",
            )
        account = account or self.state_service.get_account(account_id)
        session = session or self.state_service.load_session(account_id)
        try:
            if not session.customer_number:
                detail = self.bootstrap_account(account_id, flow=flow)
                return detail.session.products

            invitation = account.invitation_code
            self.runtime_logs.log_event(
                flow,
                stage="batch_preview",
                status="started",
                message="开始同步套餐列表",
                details={"invitation_code": invitation, "customer_number": session.customer_number},
            )
            result = self.bigmodel_client.batch_preview(account, session, invitation or "")
            payload = self._ensure_dict(result.data, label="batchPreview.data")
            session.is_subscribed = bool(payload.get("isSubscribed"))
            session.purchase_mode = "upgrade" if session.is_subscribed else "new_purchase"
            products = self._merge_products(payload, purchase_mode=session.purchase_mode)
            session.products = products
            if not session.selected_product_id:
                session.selected_product_id = DEFAULT_PRODUCT_ID
            self.state_service.save_session(session)
            self.runtime_logs.log_event(
                flow,
                stage="batch_preview",
                status="success",
                message="套餐列表同步成功",
                details={
                    "purchase_mode": session.purchase_mode,
                    "is_subscribed": session.is_subscribed,
                    "product_count": len(products),
                    "selected_product_id": session.selected_product_id,
                },
            )
            if own_flow:
                self.runtime_logs.finish_run(
                    flow,
                    status="success",
                    message="套餐列表同步完成",
                    details={"product_count": len(products), "purchase_mode": session.purchase_mode},
                )
            return products
        except Exception as exc:
            if own_flow:
                self.runtime_logs.finish_run(
                    flow,
                    status="failed",
                    message=f"套餐列表同步失败：{exc}",
                    details={"error": exc.__class__.__name__},
                    level=logging.ERROR,
                )
            raise

    def save_manual_captcha(self, account_id: str, request: ManualCaptchaRequest) -> AccountSessionState:
        session = self.state_service.load_session(account_id)
        session = self.captcha_service.store_manual_ticket(session, request)
        return self.state_service.save_session(session)

    def fetch_captcha_challenge(
        self,
        account_id: str,
        *,
        analyze: bool = True,
        flow: FlowRun | None = None,
    ) -> dict[str, Any]:
        own_flow = flow is None
        if own_flow:
            flow = self.runtime_logs.start_run(account_id=account_id, action="fetch_captcha_challenge")
        try:
            account = self.state_service.get_account(account_id)
            challenge = self.tencent_captcha_client.prehandle(account)
            image_bytes = self.tencent_captcha_client.fetch_image_bytes(account, challenge)
            payload: dict[str, Any] = {
                "instruction": challenge.instruction,
                "sess": challenge.sess,
                "sid": challenge.sid,
                "image_url": challenge.image_url,
                "image_path": challenge.image_path,
                "image_size": len(image_bytes),
                "image_base64": "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii"),
                "raw": challenge.raw,
            }
            if analyze:
                payload["ocr"] = self.ocr_service.analyze_captcha_image(
                    image_bytes,
                    prompt_text=challenge.instruction,
                )
            session = self.state_service.load_session(account_id)
            self.captcha_service.store_challenge_snapshot(
                session,
                sess=challenge.sess,
                sid=challenge.sid,
                instruction=challenge.instruction,
                raw=challenge.raw,
                ocr=payload.get("ocr") if isinstance(payload.get("ocr"), dict) else None,
            )
            self.state_service.save_session(session)
            ocr = payload.get("ocr") if isinstance(payload.get("ocr"), dict) else {}
            self.runtime_logs.log_event(
                flow,
                stage="captcha_challenge",
                status="success",
                message="验证码图片获取成功",
                details={
                    "instruction": challenge.instruction,
                    "image_size": len(image_bytes),
                    "ocr_points": len(ocr.get("points") or []) if isinstance(ocr.get("points"), list) else 0,
                    "ocr_confidence": ocr.get("confidence"),
                    "worker_pid": ocr.get("_worker_pid"),
                    "worker_elapsed_ms": ocr.get("_worker_elapsed_ms"),
                },
            )
            if own_flow:
                self.runtime_logs.finish_run(
                    flow,
                    status="success",
                    message="验证码获取完成",
                )
            return payload
        except Exception as exc:
            if own_flow:
                self.runtime_logs.finish_run(
                    flow,
                    status="failed",
                    message=f"验证码获取失败：{exc}",
                    details={"error": exc.__class__.__name__},
                    level=logging.ERROR,
                )
            raise

    def _fetch_captcha_challenge_for_session(
        self,
        account: AccountRecord,
        session: AccountSessionState,
        *,
        analyze: bool = True,
        flow: FlowRun | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        challenge = self.tencent_captcha_client.prehandle(account)
        image_bytes = self.tencent_captcha_client.fetch_image_bytes(account, challenge)
        payload: dict[str, Any] = {
            "instruction": challenge.instruction,
            "sess": challenge.sess,
            "sid": challenge.sid,
            "image_url": challenge.image_url,
            "image_path": challenge.image_path,
            "image_size": len(image_bytes),
            "image_base64": "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii"),
            "raw": challenge.raw,
        }
        if analyze:
            payload["ocr"] = self.ocr_service.analyze_captcha_image(
                image_bytes,
                prompt_text=challenge.instruction,
            )
        self.captcha_service.store_challenge_snapshot(
            session,
            sess=challenge.sess,
            sid=challenge.sid,
            instruction=challenge.instruction,
            raw=challenge.raw,
            ocr=payload.get("ocr") if isinstance(payload.get("ocr"), dict) else None,
        )
        ocr = payload.get("ocr") if isinstance(payload.get("ocr"), dict) else {}
        self.runtime_logs.log_event(
            flow,
            stage="captcha_challenge",
            status="success",
            message="验证码图片获取成功",
            details={
                **(details or {}),
                "instruction": challenge.instruction,
                "image_size": len(image_bytes),
                "ocr_points": len(ocr.get("points") or []) if isinstance(ocr.get("points"), list) else 0,
                "ocr_confidence": ocr.get("confidence"),
                "worker_pid": ocr.get("_worker_pid"),
                "worker_elapsed_ms": ocr.get("_worker_elapsed_ms"),
            },
        )
        return payload

    def build_captcha_verify_payload(
        self,
        account_id: str,
        request: CaptchaVerifyPayloadRequest,
    ) -> dict[str, Any]:
        account = self.state_service.get_account(account_id)
        session = self.state_service.load_session(account_id)
        request, tdc_result = self._hydrate_tdc_if_needed(account, session, request)
        payload = self.captcha_service.build_verify_payload(session, request)
        payload["challenge"] = {
            "sess": session.captcha_challenge_sess,
            "sid": session.captcha_challenge_sid,
            "instruction": session.captcha_challenge_instruction,
            "updated_at": session.captcha_challenge_updated_at,
        }
        if tdc_result:
            payload["tdc"] = tdc_result
        self._attach_pow_if_needed(session, request, payload)
        return payload

    def submit_captcha_verify(
        self,
        account_id: str,
        request: CaptchaVerifyPayloadRequest,
        *,
        flow: FlowRun | None = None,
    ) -> dict[str, Any]:
        own_flow = flow is None
        if own_flow:
            flow = self.runtime_logs.start_run(account_id=account_id, action="captcha_verify")
        account = self.state_service.get_account(account_id)
        session = self.state_service.load_session(account_id)
        return self._submit_captcha_verify_for_session(
            account_id,
            account,
            session,
            request,
            flow=flow,
            persist_session=True,
            finish_own_flow=own_flow,
        )

    def _submit_captcha_verify_for_session(
        self,
        account_id: str,
        account: AccountRecord,
        session: AccountSessionState,
        request: CaptchaVerifyPayloadRequest,
        *,
        flow: FlowRun | None = None,
        persist_session: bool = False,
        finish_own_flow: bool = False,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            request, tdc_result = self._hydrate_tdc_if_needed(account, session, request)
            bundle = self.captcha_service.build_verify_payload(session, request)
            bundle["challenge"] = {
                "sess": session.captcha_challenge_sess,
                "sid": session.captcha_challenge_sid,
                "instruction": session.captcha_challenge_instruction,
                "updated_at": session.captcha_challenge_updated_at,
            }
            if tdc_result:
                bundle["tdc"] = tdc_result
            self._attach_pow_if_needed(session, request, bundle)
            verify_result = self.tencent_captcha_client.verify(account, bundle["payload"])

            if verify_result.sess:
                session.captcha_challenge_sess = verify_result.sess
            if verify_result.ticket and verify_result.randstr:
                session.captcha_ticket = verify_result.ticket
                session.captcha_randstr = verify_result.randstr
                session.captcha_updated_at = utc_now_iso()
            else:
                session.captcha_ticket = ""
                session.captcha_randstr = ""
                session.captcha_updated_at = None
            if persist_session:
                self.state_service.save_session(session)

            response = {
                "request": bundle,
                "response": verify_result.raw,
                "ticket": verify_result.ticket,
                "randstr": verify_result.randstr,
                "ret": verify_result.ret,
                "error_code": verify_result.error_code,
                "error_message": verify_result.error_message,
            }
            verify_ok = bool(verify_result.ticket and verify_result.randstr and str(verify_result.error_code or "0") in {"", "0"})
            self.runtime_logs.log_event(
                flow,
                stage="captcha_verify",
                status="success" if verify_ok else "failed",
                message="验证码 verify 完成" if verify_ok else "验证码 verify 未通过",
                details={
                    **(details or {}),
                    "ret": verify_result.ret,
                    "error_code": verify_result.error_code,
                    "error_message": verify_result.error_message,
                    **self._captcha_ticket_log_details(verify_result.ticket, verify_result.randstr),
                    "verify_response": verify_result.raw,
                },
                level=logging.INFO if verify_ok else logging.WARNING,
            )
            if finish_own_flow:
                self.runtime_logs.finish_run(
                    flow,
                    status="success" if verify_ok else "failed",
                    message="验证码 verify 结束",
                    details={"error_code": verify_result.error_code},
                    level=logging.INFO if verify_ok else logging.WARNING,
                )
            return response
        except Exception as exc:
            if finish_own_flow:
                self.runtime_logs.finish_run(
                    flow,
                    status="failed",
                    message=f"验证码 verify 失败：{exc}",
                    details={"error": exc.__class__.__name__},
                    level=logging.ERROR,
                )
            raise

    def solve_captcha(self, account_id: str, *, flow: FlowRun | None = None) -> dict[str, Any]:
        own_flow = flow is None
        if own_flow:
            flow = self.runtime_logs.start_run(account_id=account_id, action="solve_captcha")
        account = self.state_service.get_account(account_id)
        session = self.state_service.load_session(account_id)
        return self._solve_captcha_for_session(
            account_id,
            account,
            session,
            flow=flow,
            persist_session=True,
            finish_own_flow=own_flow,
        )

    def _solve_captcha_for_session(
        self,
        account_id: str,
        account: AccountRecord,
        session: AccountSessionState,
        *,
        flow: FlowRun | None = None,
        persist_session: bool = False,
        finish_own_flow: bool = False,
        stop_event: threading.Event | None = None,
        details: dict[str, Any] | None = None,
        push_progress: bool = True,
    ) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        last_result: dict[str, Any] = {}
        attempt = 0
        try:
            while True:
                self._ensure_not_paused(account_id)
                if stop_event is not None and stop_event.is_set():
                    raise RunPausedError("preview race 已有其他任务胜出")
                attempt += 1
                if push_progress:
                    self._push_runtime_message(
                        account_id,
                        f"正在识别验证码，第 {attempt} 轮",
                    )
                self.runtime_logs.log_event(
                    flow,
                    stage="captcha_attempt",
                    status="started",
                    message=f"开始第 {attempt} 轮验证码识别",
                    details={"attempt": attempt, **(details or {})},
                )
                try:
                    challenge = self._fetch_captcha_challenge_for_session(
                        account,
                        session,
                        analyze=True,
                        flow=flow,
                        details={"attempt": attempt, **(details or {})},
                    )
                except AegisFlowError as exc:
                    if not self._is_retryable_flow_error(exc):
                        raise
                    result = {
                        "attempt": attempt,
                        "challenge": None,
                        "verify": {
                            "skipped": True,
                            "error_code": "OCR_EXCEPTION",
                            "error_message": exc.message,
                            "details": exc.details,
                        },
                        "ticket": "",
                        "randstr": "",
                        "status": "ocr_exception",
                    }
                    attempts.append(result)
                    last_result = result
                    self.runtime_logs.log_event(
                        flow,
                        stage="captcha_attempt",
                        status="retry",
                        message="OCR 识别超时，刷新验证码重试"
                        if "超时" in exc.message
                        else "验证码 challenge/OCR 异常，刷新验证码重试",
                        details={"attempt": attempt, **(details or {}), "error": exc.message, "details": exc.details},
                        level=logging.WARNING,
                    )
                    if push_progress:
                        self._push_runtime_message(
                            account_id,
                            f"验证码 challenge/OCR 异常，第 {attempt} 轮重试中",
                        )
                    continue
                ocr_gate = self._captcha_ocr_gate(challenge)
                if not ocr_gate["usable"]:
                    result = {
                        "attempt": attempt,
                        "challenge": challenge,
                        "verify": {
                            "skipped": True,
                            "error_code": "OCR_INCOMPLETE",
                            "error_message": "OCR 点位少于 3 个或置信度不达标，跳过 verify 并刷新重试。",
                            "ocr_gate": ocr_gate,
                        },
                        "ticket": "",
                        "randstr": "",
                        "status": "ocr_rejected",
                    }
                    attempts.append(result)
                    last_result = result
                    self.runtime_logs.log_event(
                        flow,
                        stage="ocr_gate",
                        status="retry",
                        message="OCR 点位未达标，刷新验证码重试",
                        details={"attempt": attempt, **ocr_gate},
                        level=logging.WARNING,
                    )
                    if push_progress:
                        self._push_runtime_message(
                            account_id,
                            f"验证码点位不足或不匹配，第 {attempt} 轮重试中",
                        )
                    continue

                if stop_event is not None and stop_event.is_set():
                    raise RunPausedError("preview race 已有其他任务胜出")

                try:
                    verify = self._submit_captcha_verify_for_session(
                        account_id,
                        account,
                        session,
                        CaptchaVerifyPayloadRequest(),
                        flow=flow,
                        persist_session=persist_session,
                        details={"attempt": attempt, **(details or {})},
                    )
                except AegisFlowError as exc:
                    if not self._is_retryable_flow_error(exc):
                        raise
                    result = {
                        "attempt": attempt,
                        "challenge": challenge,
                        "verify": {
                            "skipped": True,
                            "error_code": "VERIFY_FLOW_EXCEPTION",
                            "error_message": exc.message,
                            "details": exc.details,
                        },
                        "ticket": "",
                        "randstr": "",
                        "status": "verify_flow_exception",
                    }
                    attempts.append(result)
                    last_result = result
                    self.runtime_logs.log_event(
                        flow,
                        stage="captcha_verify",
                        status="retry",
                        message="验证码 verify 链路异常，刷新验证码重试",
                        details={"attempt": attempt, **(details or {}), "error": exc.message, "details": exc.details},
                        level=logging.WARNING,
                    )
                    if push_progress:
                        self._push_runtime_message(
                            account_id,
                            f"验证码 verify 链路异常，第 {attempt} 轮重试中",
                        )
                    continue
                if stop_event is not None and stop_event.is_set():
                    raise RunPausedError("preview race 已有其他任务胜出")
                error_code = str(verify.get("error_code") or "")
                result = {
                    "attempt": attempt,
                    "challenge": challenge,
                    "verify": verify,
                    "ticket": verify.get("ticket") or "",
                    "randstr": verify.get("randstr") or "",
                    "status": "verified" if not error_code or error_code == "0" else "verify_failed",
                }
                if error_code == "50":
                    result["ticket"] = ""
                    result["randstr"] = ""
                    result["status"] = "recognition_failed"
                    attempts.append(result)
                    last_result = result
                    self.runtime_logs.log_event(
                        flow,
                        stage="captcha_verify",
                        status="retry",
                        message="验证码 verify 返回 error=50，直接刷新重试",
                        details={"attempt": attempt, "error_code": error_code},
                        level=logging.WARNING,
                    )
                    if push_progress:
                        self._push_runtime_message(
                            account_id,
                            f"验证码 verify 返回 error=50，第 {attempt} 轮重试中",
                        )
                    continue
                if (error_code and error_code != "0") or not result["ticket"] or not result["randstr"]:
                    result["ticket"] = ""
                    result["randstr"] = ""
                    attempts.append(result)
                    last_result = result
                    self.runtime_logs.log_event(
                        flow,
                        stage="captcha_verify",
                        status="retry",
                        message="验证码 verify 未通过，刷新重试",
                        details={
                            "attempt": attempt,
                            "error_code": error_code,
                            "ticket_ready": bool(verify.get("ticket")),
                            "randstr_ready": bool(verify.get("randstr")),
                        },
                        level=logging.WARNING,
                    )
                    if push_progress:
                        self._push_runtime_message(
                            account_id,
                            f"验证码 verify 未通过，第 {attempt} 轮重试中",
                        )
                    continue
                attempts.append(result)
                last_result = result
                self.runtime_logs.log_event(
                    flow,
                    stage="captcha_attempt",
                    status="success",
                    message="验证码识别成功",
                    details={"attempt": attempt, "ticket_ready": True, "randstr_ready": True},
                )
                if push_progress:
                    self._push_runtime_message(
                        account_id,
                        f"验证码识别成功，共尝试 {attempt} 轮",
                    )
                solved = {
                    **result,
                    "attempts": attempts,
                }
                if finish_own_flow:
                    self.runtime_logs.finish_run(
                        flow,
                        status="success",
                        message=f"验证码识别成功，共尝试 {attempt} 轮",
                        details={"attempts": attempt},
                    )
                return solved
        except Exception as exc:
            if finish_own_flow:
                self.runtime_logs.finish_run(
                    flow,
                    status="failed",
                    message=f"验证码识别失败：{exc}",
                    details={"attempt": attempt, "last_status": last_result.get("status"), "error": exc.__class__.__name__},
                    level=logging.ERROR,
                )
            raise

    # ------------------------------------------------------------------
    # Ticket pool mode
    # ------------------------------------------------------------------

    def clear_ticket_pool(self, account_id: str) -> dict[str, Any]:
        """Discard all tickets in the pool and return the cleared session state."""
        session = self.state_service.clear_ticket_pool(account_id)
        pool = list(session.ticket_pool)
        return {
            "account_id": account_id,
            "cleared": True,
            "pool_size": len(pool),
        }

    def get_ticket_pool(self, account_id: str) -> dict[str, Any]:
        """Return current pool status for an account."""
        account = self.state_service.get_account(account_id)
        session = self.state_service.load_session(account_id)
        pool = list(session.ticket_pool)
        unused = [e for e in pool if not e.used]
        used = [e for e in pool if e.used]
        return {
            "account_id": account_id,
            "target": account.ticket_pool_size,
            "collected": len(unused),
            "used": len(used),
            "total": len(pool),
            "pool": [e.model_dump() for e in pool],
        }

    def _fill_ticket_pool(
        self,
        account_id: str,
        account: AccountRecord,
        session: AccountSessionState,
        target_size: int,
        *,
        flow: FlowRun,
        deadline_time: str = "",
    ) -> AccountSessionState:
        """Collect captcha tickets until pool has ``target_size`` unused entries.

        Stops early (without error) if ``deadline_time`` has already passed or
        passes while filling.  The caller is responsible for draining afterwards.
        """
        pool = list(session.ticket_pool)  # mutable copy
        unused_count = sum(1 for e in pool if not e.used)

        self.runtime_logs.log_event(
            flow,
            stage="ticket_pool_fill",
            status="started",
            message=f"开始填充 ticket 池，目标 {target_size} 个，当前已有 {unused_count} 个未使用",
            details={"target": target_size, "already_collected": unused_count},
        )
        self._push_runtime_message(account_id, f"ticket 池填充中 ({unused_count}/{target_size})")

        while unused_count < target_size:
            self._ensure_not_paused(account_id)
            # If deadline has arrived, stop filling and let drain run immediately
            if deadline_time and self._preview_concurrency_wait_seconds(deadline_time) <= 0:
                self.runtime_logs.log_event(
                    flow,
                    stage="ticket_pool_fill",
                    status="deadline_reached",
                    message=f"并发时间已到，中止填充（已收集 {unused_count}/{target_size}）",
                    details={"target": target_size, "collected": unused_count},
                    level=logging.WARNING,
                )
                break

            try:
                solved = self._solve_captcha_for_session(
                    account_id,
                    account,
                    session,
                    flow=flow,
                    persist_session=False,
                    push_progress=False,
                )
            except Exception as exc:
                self.runtime_logs.log_event(
                    flow,
                    stage="ticket_pool_fill",
                    status="retry",
                    message=f"ticket 池填充：验证码识别异常，重试 ({exc})",
                    details={"error": str(exc)},
                    level=logging.WARNING,
                )
                continue

            ticket = str(solved.get("ticket") or "")
            randstr = str(solved.get("randstr") or "")
            if not ticket or not randstr:
                continue

            from app.models import TicketPoolEntry
            entry = TicketPoolEntry(
                ticket=ticket,
                randstr=randstr,
                collected_at=utc_now_iso(),
                used=False,
            )
            pool.append(entry)
            unused_count += 1
            session.ticket_pool = pool
            self.state_service.save_session(session)

            self.runtime_logs.log_event(
                flow,
                stage="ticket_pool_fill",
                status="progress",
                message=f"ticket 池: {unused_count}/{target_size} 已收集",
                details={"collected": unused_count, "target": target_size, "ticket_prefix": ticket[:12]},
            )
            self._push_runtime_message(account_id, f"ticket 池 {unused_count}/{target_size} 已就绪")

        self.runtime_logs.log_event(
            flow,
            stage="ticket_pool_fill",
            status="done",
            message=f"ticket 池填充完成，共 {unused_count} 个可用 ticket",
            details={"collected": unused_count, "target": target_size},
        )
        return session

    @staticmethod
    def _ticket_pool_drain_interval_ms(account: AccountRecord) -> int:
        """Return the per-account ticket drain interval in milliseconds."""
        return max(0, min(10_000, int(account.ticket_pool_drain_interval_ms or 0)))

    def _sleep_ticket_pool_interval(self, account_id: str, interval_ms: int) -> None:
        if interval_ms <= 0:
            return
        deadline = time.monotonic() + interval_ms / 1000.0
        while True:
            self._ensure_not_paused(account_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def _drain_ticket_pool(
        self,
        account_id: str,
        account: AccountRecord,
        session: AccountSessionState,
        request: PreviewPaymentRequest,
        invitation: str,
        *,
        flow: FlowRun,
    ) -> PreviewResult:
        """Consume pool tickets using parallel mode or serial fixed-interval mode."""
        drain_interval_ms = self._ticket_pool_drain_interval_ms(account)
        if drain_interval_ms <= 0:
            return self._drain_ticket_pool_parallel(
                account_id,
                account,
                session,
                request,
                invitation,
                flow=flow,
            )
        return self._drain_ticket_pool_serial(
            account_id,
            account,
            session,
            request,
            invitation,
            drain_interval_ms=drain_interval_ms,
            flow=flow,
        )

    def _drain_ticket_pool_serial(
        self,
        account_id: str,
        account: AccountRecord,
        session: AccountSessionState,
        request: PreviewPaymentRequest,
        invitation: str,
        *,
        drain_interval_ms: int,
        flow: FlowRun,
    ) -> PreviewResult:
        """Consume pool tickets one by one until /preview returns a bizId."""
        pool = list(session.ticket_pool)
        unused = [e for e in pool if not e.used]

        if not unused:
            raise UpstreamRequestError(
                "ticket 池为空，没有可用的 ticket",
                details={"account_id": account_id, "pool_total": len(pool)},
            )

        self.runtime_logs.log_event(
            flow,
            stage="ticket_pool_drain",
            status="started",
            message=f"开始串行消耗 ticket 池，共 {len(unused)} 个 ticket 待用",
            details={
                "count": len(unused),
                "mode": "serial",
                "drain_interval_ms": drain_interval_ms,
            },
        )
        self._push_runtime_message(
            account_id,
            f"ticket 池串行消耗中，共 {len(unused)} 个 ticket，间隔 {drain_interval_ms}ms",
        )

        for idx, entry in enumerate(unused, start=1):
            self._ensure_not_paused(account_id)
            ticket = entry.ticket
            randstr = entry.randstr

            entry.used = True
            session.ticket_pool = pool
            self.state_service.save_session(session)

            self.runtime_logs.log_event(
                flow,
                stage="ticket_pool_drain",
                status="calling_preview",
                message=f"pool 串行第 {idx}/{len(unused)} 个 ticket → /preview",
                details={
                    "idx": idx,
                    "total": len(unused),
                    "mode": "serial",
                    "ticket_prefix": ticket[:12],
                    "collected_at": entry.collected_at,
                },
            )

            try:
                result = self.bigmodel_client.preview_payment(
                    account,
                    session,
                    request,
                    invitation_code=invitation,
                    ticket=ticket,
                    randstr=randstr,
                    allow_fallback_proxy=True,
                )
            except UpstreamRequestError as exc:
                self.runtime_logs.log_event(
                    flow,
                    stage="ticket_pool_drain",
                    status="error",
                    message=f"pool 串行第 {idx} 个 ticket 请求异常: {exc.message}",
                    details={
                        "idx": idx,
                        "mode": "serial",
                        "error": exc.message,
                        "error_details": exc.details,
                        **self._captcha_ticket_log_details(ticket, randstr),
                    },
                    level=logging.WARNING,
                )
                if idx < len(unused):
                    self._sleep_ticket_pool_interval(account_id, drain_interval_ms)
                continue

            raw = result.raw
            code = raw.get("code")
            data = result.data if isinstance(result.data, dict) else {}
            biz_id = str(data.get("bizId") or "").strip()
            self._log_ticket_pool_preview_response(
                flow=flow,
                idx=idx,
                total=len(unused),
                mode="serial",
                raw=raw,
                ticket=ticket,
                randstr=randstr,
            )

            if code == 200 and biz_id:
                self._push_runtime_message(account_id, f"pool 串行第 {idx} 个 ticket 成功，bizId: {biz_id}")
                return self._preview_from_upstream_payload(raw)

            if idx < len(unused):
                self._sleep_ticket_pool_interval(account_id, drain_interval_ms)

        raise UpstreamRequestError(
            "ticket 池已耗尽，所有 ticket 均未能拿到 bizId",
            details={"account_id": account_id, "tickets_tried": len(unused), "mode": "serial"},
        )

    def _drain_ticket_pool_parallel(
        self,
        account_id: str,
        account: AccountRecord,
        session: AccountSessionState,
        request: PreviewPaymentRequest,
        invitation: str,
        *,
        flow: FlowRun,
    ) -> PreviewResult:
        """Stagger-submit pool tickets in parallel and return the first bizId winner."""
        pool = list(session.ticket_pool)
        unused = [e for e in pool if not e.used]

        if not unused:
            raise UpstreamRequestError(
                "ticket 池为空，没有可用的 ticket",
                details={"account_id": account_id, "pool_total": len(pool)},
            )

        self.runtime_logs.log_event(
            flow,
            stage="ticket_pool_drain",
            status="started",
            message=f"开始并行消耗 ticket 池，共 {len(unused)} 个 ticket 待用",
            details={
                "count": len(unused),
                "mode": "parallel",
                "drain_interval_ms": 0,
            },
        )
        self._push_runtime_message(account_id, f"ticket 池并行消耗中，共 {len(unused)} 个 ticket")

        lane_specs: list[tuple[int, Any, float]] = []
        for idx, entry in enumerate(unused, start=1):
            lane_specs.append((idx, entry, 0.0))

        for _, entry, _ in lane_specs:
            entry.used = True
        session.ticket_pool = pool
        self.state_service.save_session(session)

        stop_event = threading.Event()
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(lane_specs),
            thread_name_prefix=f"ticket-pool-{account_id}",
        )
        futures: dict[concurrent.futures.Future[PreviewResult | None], int] = {}
        try:
            for idx, entry, delay_ms in lane_specs:
                futures[
                    executor.submit(
                        self._drain_ticket_pool_parallel_lane,
                        account_id,
                        account,
                        session,
                        request,
                        invitation,
                        idx,
                        len(lane_specs),
                        entry,
                        delay_ms,
                        stop_event,
                        flow,
                    )
                ] = idx

            for future in concurrent.futures.as_completed(futures):
                try:
                    preview = future.result()
                except RunPausedError:
                    stop_event.set()
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
                except Exception as exc:
                    self.runtime_logs.log_event(
                        flow,
                        stage="ticket_pool_drain",
                        status="error",
                        message=f"pool 并行 lane {futures[future]} 执行异常: {exc}",
                        details={
                            "idx": futures[future],
                            "mode": "parallel",
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                        },
                        level=logging.WARNING,
                    )
                    continue

                if preview and preview.biz_id:
                    stop_event.set()
                    for pending in futures:
                        if pending is not future:
                            pending.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    self._push_runtime_message(account_id, f"pool 并行抢到 bizId: {preview.biz_id}")
                    return preview
        finally:
            if not stop_event.is_set():
                executor.shutdown(wait=True, cancel_futures=False)

        raise UpstreamRequestError(
            "ticket 池已耗尽，所有 ticket 均未能拿到 bizId",
            details={"account_id": account_id, "tickets_tried": len(unused), "mode": "parallel"},
        )

    def _drain_ticket_pool_parallel_lane(
        self,
        account_id: str,
        account: AccountRecord,
        session: AccountSessionState,
        request: PreviewPaymentRequest,
        invitation: str,
        idx: int,
        total: int,
        entry: Any,
        delay_ms: float,
        stop_event: threading.Event,
        flow: FlowRun,
    ) -> PreviewResult | None:
        if delay_ms > 0:
            deadline = time.monotonic() + delay_ms / 1000.0
            while True:
                if stop_event.is_set():
                    return None
                self._ensure_not_paused(account_id)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))

        if stop_event.is_set():
            return None
        self._ensure_not_paused(account_id)

        ticket = entry.ticket
        randstr = entry.randstr
        self.runtime_logs.log_event(
            flow,
            stage="ticket_pool_drain",
            status="calling_preview",
            message=f"pool 并行第 {idx}/{total} 个 ticket → /preview",
            details={
                "idx": idx,
                "total": total,
                "mode": "parallel",
                "dispatch_delay_ms": round(delay_ms, 3),
                "ticket_prefix": ticket[:12],
                "collected_at": entry.collected_at,
            },
        )

        try:
            result = self.bigmodel_client.preview_payment(
                account,
                session,
                request,
                invitation_code=invitation,
                ticket=ticket,
                randstr=randstr,
                allow_fallback_proxy=True,
            )
        except UpstreamRequestError as exc:
            self.runtime_logs.log_event(
                flow,
                stage="ticket_pool_drain",
                status="error",
                message=f"pool 并行第 {idx} 个 ticket 请求异常: {exc.message}",
                details={
                    "idx": idx,
                    "total": total,
                    "mode": "parallel",
                    "dispatch_delay_ms": round(delay_ms, 3),
                    "error": exc.message,
                    "error_details": exc.details,
                    **self._captcha_ticket_log_details(ticket, randstr),
                },
                level=logging.WARNING,
            )
            return None

        raw = result.raw
        code = raw.get("code")
        data = result.data if isinstance(result.data, dict) else {}
        biz_id = str(data.get("bizId") or "").strip()
        self._log_ticket_pool_preview_response(
            flow=flow,
            idx=idx,
            total=total,
            mode="parallel",
            raw=raw,
            ticket=ticket,
            randstr=randstr,
            extra={"dispatch_delay_ms": round(delay_ms, 3)},
        )

        if code == 200 and biz_id:
            stop_event.set()
            return self._preview_from_upstream_payload(raw)
        return None

    def _log_ticket_pool_preview_response(
        self,
        *,
        flow: FlowRun,
        idx: int,
        total: int,
        mode: str,
        raw: dict[str, Any],
        ticket: str,
        randstr: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        code = raw.get("code")
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        biz_id = str(data.get("bizId") or "").strip() if isinstance(data, dict) else ""
        self.runtime_logs.log_event(
            flow,
            stage="ticket_pool_drain",
            status="success" if (code == 200 and biz_id) else "no_biz_id",
            message=(
                f"pool {mode} 第 {idx} 个 ticket 成功拿到 bizId!"
                if (code == 200 and biz_id)
                else f"pool {mode} 第 {idx} 个 ticket 未返回 bizId"
            ),
            details=self._preview_response_log_details(
                raw=raw,
                ticket=ticket,
                randstr=randstr,
                extra={"idx": idx, "total": total, "pool_mode": True, "drain_mode": mode, **(extra or {})},
            ),
            level=logging.INFO if (code == 200 and biz_id) else logging.WARNING,
        )

    def collect_captcha_tdc(self, account_id: str) -> dict[str, Any]:
        account = self.state_service.get_account(account_id)
        session = self.state_service.load_session(account_id)
        result = self.tdc_service.collect_for_challenge(account, session.captcha_challenge_raw or {})
        return result.to_payload()

    def preview_payment(
        self,
        account_id: str,
        request: PreviewPaymentRequest,
        *,
        flow: FlowRun | None = None,
    ) -> PreviewResult:
        own_flow = flow is None
        if own_flow:
            flow = self.runtime_logs.start_run(
                account_id=account_id,
                action="preview_payment",
                product_id=request.product_id,
            )
        try:
            account, session = self._ensure_context(account_id)
            invitation = account.invitation_code.strip()
            preview_attempts: list[dict[str, Any]] = []
            preview_round = 0

            while True:
                self._ensure_not_paused(account_id)
                preview_round += 1
                self._push_runtime_message(
                    account_id,
                    f"正在进行 preview 验证，第 {preview_round} 轮",
                )
                self.runtime_logs.log_event(
                    flow,
                    stage="preview",
                    status="started",
                    message=f"开始第 {preview_round} 轮 preview，将先获取验证码票据",
                    details={
                        "round": preview_round,
                        "product_id": request.product_id,
                        "captcha_required": True,
                        "invitation_code": invitation,
                    },
                )
                captcha_request = request if preview_round == 1 else request.model_copy(update={"ticket": None, "randstr": None})
                try:
                    ticket, randstr = self._resolve_or_solve_preview_captcha(account_id, session, captcha_request, flow=flow)
                except UpstreamRequestError as exc:
                    preview_attempts.append(
                        {
                            "round": preview_round,
                            "code": None,
                            "biz_id": None,
                            "sold_out": None,
                            "msg": exc.message,
                            "ticket": "",
                        }
                    )
                    if len(preview_attempts) > 100:
                        preview_attempts.pop(0)
                    self.runtime_logs.log_event(
                        flow,
                        stage="preview",
                        status="retry",
                        message="preview 验证码票据获取异常，下一轮重新获取并重试",
                        details={
                            "round": preview_round,
                            "product_id": request.product_id,
                            "error": exc.message,
                            "error_details": exc.details,
                        },
                        level=logging.WARNING,
                    )
                    self._push_runtime_message(
                        account_id,
                        f"preview 验证码异常，第 {preview_round} 轮重试中",
                    )
                    session = self.state_service.load_session(account_id)
                    continue

                try:
                    result = self.bigmodel_client.preview_payment(
                        account,
                        session,
                        request,
                        invitation_code=invitation,
                        ticket=ticket,
                        randstr=randstr,
                    )
                except UpstreamRequestError as exc:
                    preview_attempts.append(
                        {
                            "round": preview_round,
                            "code": None,
                            "biz_id": None,
                            "sold_out": None,
                            "msg": exc.message,
                            "ticket": ticket[:12] + "***" if ticket else "",
                        }
                    )
                    if len(preview_attempts) > 100:
                        preview_attempts.pop(0)
                    self.runtime_logs.log_event(
                        flow,
                        stage="preview_response",
                        status="retry",
                        message="preview 请求异常，未拿到接口成功响应",
                        details={
                            "round": preview_round,
                            "product_id": request.product_id,
                            **self._captcha_ticket_log_details(ticket, randstr),
                            "error": exc.message,
                            "error_details": exc.details,
                        },
                        level=logging.WARNING,
                    )
                    self._push_runtime_message(
                        account_id,
                        f"preview 请求失败，第 {preview_round} 轮重试中",
                    )
                    session = self.state_service.load_session(account_id)
                    continue
                raw = result.raw
                code = raw.get("code")
                data = result.data if isinstance(result.data, dict) else {}

                biz_id = str(data.get("bizId") or "").strip()
                self.runtime_logs.log_event(
                    flow,
                    stage="preview_response",
                    status="success" if code == 200 and biz_id else "failed",
                    message="preview 接口响应已返回" if code == 200 and biz_id else "preview 接口响应未拿到 bizId",
                    details=self._preview_response_log_details(
                        raw=raw,
                        ticket=ticket,
                        randstr=randstr,
                        extra={"round": preview_round, "product_id": request.product_id},
                    ),
                    level=logging.INFO if code == 200 and biz_id else logging.WARNING,
                )
                attempt_info = {
                    "round": preview_round,
                    "code": code,
                    "biz_id": biz_id or None,
                    "sold_out": bool(data.get("soldOut")),
                    "msg": raw.get("msg") or "",
                    "ticket": ticket[:12] + "***" if ticket else "",
                }
                preview_attempts.append(attempt_info)
                if len(preview_attempts) > 100:
                    preview_attempts.pop(0)

                preview = PreviewResult(
                    biz_id=biz_id,
                    third_party_amount=self._string_value(data, "thirdPartyAmount"),
                    sold_out=bool(data.get("soldOut")),
                    original_amount=self._string_value(data, "originalAmount"),
                    pay_amount=self._string_value(data, "payAmount"),
                    residual_amount=self._string_value(data, "residualAmount"),
                    give_amount=self._string_value(data, "giveAmount"),
                    cash_amount=self._string_value(data, "cashAmount"),
                    renew_amount=self._string_value(data, "renewAmount"),
                    campaign_discount_details=list(data.get("campaignDiscountDetails") or []),
                    last_subscription_summary=self._ensure_dict(
                        data.get("lastSubscriptionSummary") or {},
                        label="preview.lastSubscriptionSummary",
                    ),
                    raw=raw,
                )

                if code == 200 and biz_id:
                    session.selected_product_id = request.product_id
                    session.preview = preview
                    session.captcha_ticket = ticket
                    session.captcha_randstr = randstr
                    self.state_service.save_session(session)
                    self.runtime_logs.log_event(
                        flow,
                        stage="preview",
                        status="success",
                        message="preview 成功拿到 bizId",
                        details={
                            "round": preview_round,
                            "biz_id": biz_id,
                            "sold_out": preview.sold_out,
                            "third_party_amount": preview.third_party_amount,
                        },
                    )
                    self._push_runtime_message(
                        account_id,
                        f"preview 成功，已获取 bizId：{biz_id}",
                    )
                    if own_flow:
                        self.runtime_logs.finish_run(
                            flow,
                            status="success",
                            message="preview 成功",
                            details={"biz_id": biz_id, "round": preview_round},
                        )
                    return preview

                self.runtime_logs.log_event(
                    flow,
                    stage="preview",
                    status="retry",
                    message="preview 未拿到 bizId，下一轮将重新获取验证码票据并再次请求 preview",
                    details={
                        "round": preview_round,
                        "code": code,
                        "biz_id": biz_id,
                        "sold_out": bool(data.get("soldOut")),
                        "msg": raw.get("msg") or "",
                    },
                    level=logging.WARNING,
                )
                self._push_runtime_message(
                    account_id,
                    f"获取 bizId 失败，第 {preview_round} 轮继续重试",
                )
                session = self.state_service.load_session(account_id)
        except Exception as exc:
            if own_flow:
                self.runtime_logs.finish_run(
                    flow,
                    status="failed",
                    message=f"preview 失败：{exc}",
                    details={"error": exc.__class__.__name__},
                    level=logging.ERROR,
                )
            raise

    def race_preview_payment(
        self,
        account_id: str,
        request: PreviewPaymentRequest,
        *,
        concurrency: int = 1,
        preview_concurrency_time: str = "",
        flow: FlowRun | None = None,
    ) -> PreviewResult:
        normalized_concurrency = max(1, min(4, int(concurrency or 1)))
        preview_concurrency_time = (preview_concurrency_time or "").strip()
        own_flow = flow is None
        if own_flow:
            flow = self.runtime_logs.start_run(
                account_id=account_id,
                action="preview_payment_race",
                product_id=request.product_id,
                details={"concurrency": normalized_concurrency, "preview_concurrency_time": preview_concurrency_time},
            )
        capacity_lease_id = ""
        capacity = self.ocr_service.reserve_capacity(normalized_concurrency)
        capacity_lease_id = str(capacity.get("lease_id") or "")
        self.runtime_logs.log_event(
            flow,
            stage="ocr_capacity",
            status="ready",
            message="OCR worker 已按 preview 并发需求预热",
            details={
                "preview_concurrency": normalized_concurrency,
                "reserved_demand": capacity.get("reserved_demand"),
                "active_demand": capacity.get("active_demand"),
                "inflight_workers": capacity.get("inflight_workers"),
                "workers_limit": capacity.get("workers"),
                "warmed_workers": capacity.get("warmed_workers"),
                "warmed_worker_pids": capacity.get("warmed_worker_pids"),
            },
        )
        if normalized_concurrency <= 1:
            try:
                return self.preview_payment(account_id, request, flow=flow)
            finally:
                self.ocr_service.release_capacity(capacity_lease_id)

        try:
            account, base_session = self._ensure_context(account_id)
        except Exception:
            self.ocr_service.release_capacity(capacity_lease_id)
            raise
        invitation = account.invitation_code.strip()
        stop_event = threading.Event()
        errors: list[str] = []
        self.runtime_logs.log_event(
            flow,
            stage="preview_race",
            status="started",
            message=f"preview 竞速启动，并发 {normalized_concurrency} 路",
            details={
                "concurrency": normalized_concurrency,
                "product_id": request.product_id,
                "invitation_code": invitation,
                "preview_concurrency_time": preview_concurrency_time,
                "preview_concurrency_wait_enabled": bool(preview_concurrency_time),
            },
        )
        self._push_runtime_message(
            account_id,
            f"正在并发获取 bizId，preview 并发 {normalized_concurrency} 路",
        )
        try:
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=normalized_concurrency,
                thread_name_prefix=f"preview-race-{account_id}",
            )
            try:
                futures = [
                    executor.submit(
                        self._preview_race_lane,
                        account_id,
                        account,
                        base_session,
                        request,
                        invitation,
                        lane,
                        stop_event,
                        preview_concurrency_time,
                        flow,
                    )
                    for lane in range(1, normalized_concurrency + 1)
                ]
                self.runtime_logs.log_event(
                    flow,
                    stage="preview_race",
                    status="started",
                    message=f"preview 竞速已提交 {len(futures)} 个并发 lane",
                    details={
                        "concurrency": normalized_concurrency,
                        "max_rounds_per_lane": PREVIEW_RACE_MAX_ROUNDS,
                    },
                )
                winner: PreviewRaceWinner | None = None
                paused_error: RunPausedError | None = None
                for future in concurrent.futures.as_completed(futures):
                    try:
                        winner = future.result()
                    except RunPausedError as exc:
                        if not stop_event.is_set():
                            paused_error = exc
                            stop_event.set()
                            for item in futures:
                                if item is not future:
                                    item.cancel()
                        continue
                    except Exception as exc:
                        error_message = str(exc)
                        errors.append(error_message)
                        self.runtime_logs.log_event(
                            flow,
                            stage="preview_race",
                            status="failed",
                            message=f"preview 竞速子任务失败：{error_message}",
                            details={"error": exc.__class__.__name__, "message": error_message},
                            level=logging.WARNING,
                        )
                        continue
                    stop_event.set()
                    for item in futures:
                        if item is not future:
                            item.cancel()
                    break
            finally:
                stop_event.set()
                executor.shutdown(wait=False, cancel_futures=True)

            if winner is None:
                if paused_error is not None:
                    raise paused_error
                message = errors[-1] if errors else "preview 竞速没有拿到 bizId"
                raise UpstreamRequestError(
                    f"preview 竞速失败，所有并发任务都未拿到 bizId：{message}",
                    details={"concurrency": normalized_concurrency, "last_error": message, "errors": errors[-6:]},
                )

            session = self.state_service.load_session(account_id)
            session.selected_product_id = request.product_id
            session.preview = winner.preview
            session.captcha_ticket = winner.ticket
            session.captcha_randstr = winner.randstr
            self.state_service.save_session(session)
            self.runtime_logs.log_event(
                flow,
                stage="preview_race",
                status="success",
                message="preview 竞速成功，已选用最先返回 bizId 的结果",
                details={
                    "concurrency": normalized_concurrency,
                    "lane": winner.lane,
                    "round": winner.round,
                    "biz_id": winner.preview.biz_id,
                },
            )
            self._push_runtime_message(
                account_id,
                f"preview 竞速成功，lane {winner.lane} 获取 bizId：{winner.preview.biz_id}",
            )
            if own_flow:
                self.runtime_logs.finish_run(
                    flow,
                    status="success",
                    message="preview 竞速成功",
                    details={"biz_id": winner.preview.biz_id, "lane": winner.lane, "round": winner.round},
                )
            return winner.preview
        except Exception as exc:
            stop_event.set()
            if own_flow:
                self.runtime_logs.finish_run(
                    flow,
                    status="failed",
                    message=f"preview 竞速失败：{exc}",
                    details={"error": exc.__class__.__name__},
                    level=logging.ERROR,
                )
            raise
        finally:
            self.ocr_service.release_capacity(capacity_lease_id)

    def _preview_race_lane(
        self,
        account_id: str,
        account: AccountRecord,
        base_session: AccountSessionState,
        request: PreviewPaymentRequest,
        invitation: str,
        lane: int,
        stop_event: threading.Event,
        preview_concurrency_time: str,
        flow: FlowRun | None,
    ) -> PreviewRaceWinner:
        session = base_session.model_copy(deep=True)
        round_no = 0
        preview_wait_used = False
        while not stop_event.is_set() and round_no < PREVIEW_RACE_MAX_ROUNDS:
            self._ensure_not_paused(account_id)
            round_no += 1
            details = {"lane": lane, "round": round_no, "race": True}
            self.runtime_logs.log_event(
                flow,
                stage="preview_race",
                status="started",
                message=f"preview 竞速 lane {lane} 开始第 {round_no} 轮",
                details=details,
            )
            try:
                solved = self._solve_captcha_for_session(
                    account_id,
                    account,
                    session,
                    flow=flow,
                    persist_session=False,
                    stop_event=stop_event,
                    details=details,
                    push_progress=False,
                )
            except UpstreamRequestError as exc:
                self.runtime_logs.log_event(
                    flow,
                    stage="preview_race",
                    status="retry",
                    message=f"preview 竞速 lane {lane} 验证码上游异常，继续重试",
                    details={**details, "error": exc.message, "error_details": exc.details},
                    level=logging.WARNING,
                )
                continue
            if stop_event.is_set():
                raise RunPausedError("preview race 已有其他任务胜出")
            ticket = str(solved.get("ticket") or "")
            randstr = str(solved.get("randstr") or "")
            if not ticket or not randstr:
                continue
            if preview_concurrency_time and not preview_wait_used:
                self._wait_preview_concurrency_time(
                    account_id,
                    target_time=preview_concurrency_time,
                    lane=lane,
                    round_no=round_no,
                    stop_event=stop_event,
                    flow=flow,
                    ticket=ticket,
                    randstr=randstr,
                )
                preview_wait_used = True
            try:
                result = self.bigmodel_client.preview_payment(
                    account,
                    session,
                    request,
                    invitation_code=invitation,
                    ticket=ticket,
                    randstr=randstr,
                )
            except UpstreamRequestError as exc:
                self.runtime_logs.log_event(
                    flow,
                    stage="preview_response",
                    status="retry",
                    message=f"preview 竞速 lane {lane} 请求上游异常，继续重试",
                    details={
                        **details,
                        "product_id": request.product_id,
                        **self._captcha_ticket_log_details(ticket, randstr),
                        "error": exc.message,
                        "error_details": exc.details,
                    },
                    level=logging.WARNING,
                )
                continue
            if stop_event.is_set():
                raise RunPausedError("preview race 已有其他任务胜出")
            raw = result.raw
            code = raw.get("code")
            data = result.data if isinstance(result.data, dict) else {}
            biz_id = str(data.get("bizId") or "").strip()
            self.runtime_logs.log_event(
                flow,
                stage="preview_response",
                status="success" if code == 200 and biz_id else "failed",
                message=f"preview 竞速 lane {lane} 接口响应已返回"
                if code == 200 and biz_id
                else f"preview 竞速 lane {lane} 接口响应未拿到 bizId",
                details=self._preview_response_log_details(
                    raw=raw,
                    ticket=ticket,
                    randstr=randstr,
                    extra={"lane": lane, "round": round_no, "race": True, "product_id": request.product_id},
                ),
                level=logging.INFO if code == 200 and biz_id else logging.WARNING,
            )
            if code == 200 and biz_id:
                stop_event.set()
                preview = PreviewResult(
                    biz_id=biz_id,
                    third_party_amount=self._string_value(data, "thirdPartyAmount"),
                    sold_out=bool(data.get("soldOut")),
                    original_amount=self._string_value(data, "originalAmount"),
                    pay_amount=self._string_value(data, "payAmount"),
                    residual_amount=self._string_value(data, "residualAmount"),
                    give_amount=self._string_value(data, "giveAmount"),
                    cash_amount=self._string_value(data, "cashAmount"),
                    renew_amount=self._string_value(data, "renewAmount"),
                    campaign_discount_details=list(data.get("campaignDiscountDetails") or []),
                    last_subscription_summary=self._ensure_dict(
                        data.get("lastSubscriptionSummary") or {},
                        label="preview.lastSubscriptionSummary",
                    ),
                    raw=raw,
                )
                return PreviewRaceWinner(preview=preview, ticket=ticket, randstr=randstr, round=round_no, lane=lane)

            self.runtime_logs.log_event(
                flow,
                stage="preview_race",
                status="retry",
                message=f"preview 竞速 lane {lane} 未拿到 bizId，继续重试",
                details={
                    **details,
                    "code": code,
                    "biz_id": biz_id,
                    "sold_out": bool(data.get("soldOut")),
                    "msg": raw.get("msg") or "",
                },
                level=logging.WARNING,
            )
        if round_no >= PREVIEW_RACE_MAX_ROUNDS:
            raise UpstreamRequestError(
                f"preview 竞速 lane {lane} 已达到最大重试 {PREVIEW_RACE_MAX_ROUNDS} 轮",
                details={"lane": lane, "rounds": round_no, "max_rounds": PREVIEW_RACE_MAX_ROUNDS},
            )
        raise RunPausedError("preview race 已有其他任务胜出")

    def seed_preview(self, account_id: str, request: PreviewSeedRequest) -> PreviewResult:
        session = self.state_service.load_session(account_id)
        preview = self._preview_from_upstream_payload(request.preview)
        session.preview = preview
        product_id = str((preview.raw.get("data") or {}).get("productId") or "").strip()
        if product_id:
            session.selected_product_id = product_id
        self.state_service.save_session(session)
        return preview

    def create_qr(
        self,
        account_id: str,
        request: CreateQrRequest,
        *,
        flow: FlowRun | None = None,
    ) -> PaymentTaskRecord:
        own_flow = flow is None
        if own_flow:
            flow = self.runtime_logs.start_run(
                account_id=account_id,
                action="create_qr",
                product_id=request.product_id,
                pay_type=request.pay_type,
            )
        try:
            account, session = self._ensure_context(account_id)
            product_id = request.product_id.strip() or session.selected_product_id
            if not product_id:
                raise BadRequestError("缺少 product_id，二维码没法生成")
            invitation = account.invitation_code.strip()
            if not session.preview or not session.preview.biz_id:
                raise BadRequestError("请先调用 preview，拿到 bizId 之后再生成二维码")
            qr_cycles: list[dict[str, Any]] = []
            cycle = 0

            while True:
                self._ensure_not_paused(account_id)
                cycle += 1
                session = self.state_service.load_session(account_id)
                if not session.preview or not session.preview.biz_id:
                    self._push_runtime_message(
                        account_id,
                        "当前没有可用 bizId，正在重新拉起 preview 链路",
                    )
                    self.runtime_logs.log_event(
                        flow,
                        stage="sign",
                        status="retry",
                        message="当前 preview 无 bizId，重新拉起 preview 链路",
                        details={"cycle": cycle, "product_id": product_id},
                        level=logging.WARNING,
                    )
                    current_account = self.state_service.get_account(account_id)
                    self.race_preview_payment(
                        account_id,
                        PreviewPaymentRequest(
                            product_id=product_id,
                            invitation_code=invitation,
                        ),
                        concurrency=current_account.preview_concurrency,
                        flow=flow,
                    )
                    session = self.state_service.load_session(account_id)

                biz_id = (session.preview.biz_id or request.biz_id or "").strip() if session.preview else ""
                sign_mode = session.purchase_mode or ("upgrade" if session.is_subscribed else "new_purchase")
                sign_attempts: list[dict[str, Any]] = []
                self._push_runtime_message(
                    account_id,
                    f"已获取 bizId，正在生成支付二维码，第 {cycle} 轮",
                )
                self.runtime_logs.log_event(
                    flow,
                    stage="sign",
                    status="started",
                    message=f"开始第 {cycle} 轮签单",
                    details={"cycle": cycle, "biz_id": biz_id, "mode": sign_mode},
                )

                for sign_attempt in range(1, 4):
                    self._ensure_not_paused(account_id)
                    try:
                        result = self._request_sign(
                            account,
                            session,
                            pay_type=request.pay_type,
                            product_id=product_id,
                            biz_id=biz_id,
                            invitation_code=invitation,
                        )
                        payload = self._ensure_dict(result.data, label="createSign.data")
                        sign = str(payload.get("sign") or "")
                        order_id = str(payload.get("orderId") or "")
                        if not sign:
                            raise UpstreamRequestError(
                                "create-sign 没返回 sign，二维码自然也出不来。",
                                details={"payload": result.raw},
                            )

                        sign_attempts.append(
                            {
                                "attempt": sign_attempt,
                                "ok": True,
                                "mode": sign_mode,
                                "order_id": order_id,
                                "biz_id": biz_id,
                                "response": result.raw,
                            }
                        )
                        qr_cycles.append(
                            {
                                "cycle": cycle,
                                "preview_biz_id": biz_id,
                                "sign_attempts": sign_attempts,
                            }
                        )

                        product_name = next(
                            (item.product_name for item in session.products if item.product_id == product_id),
                            "",
                        )
                        amount = session.preview.third_party_amount if session.preview else ""
                        now = utc_now_iso()
                        task = PaymentTaskRecord(
                            id=make_id("task"),
                            account_id=account_id,
                            product_id=product_id,
                            product_name=product_name,
                            pay_type=request.pay_type,
                            biz_id=biz_id,
                            amount=amount,
                            sign=sign,
                            qr_base64=self._build_qr_base64(sign),
                            status="PENDING",
                            raw_preview=session.preview.raw if session.preview else {},
                            raw_sign={
                                "mode": sign_mode,
                                "cycle": cycle,
                                "attempt": sign_attempt,
                                "attempts": qr_cycles,
                                "response": result.raw,
                            },
                            created_at=now,
                            updated_at=now,
                        )
                        session.last_sign = sign
                        session.last_order_id = order_id
                        session.selected_product_id = product_id
                        self.state_service.save_session(session)
                        saved_task = self.state_service.save_task(task)
                        self.state_service.touch_account_updated_at(account_id)
                        self.runtime_logs.log_event(
                            flow,
                            stage="sign",
                            status="success",
                            message="签单成功并生成二维码",
                            details={
                                "cycle": cycle,
                                "attempt": sign_attempt,
                                "biz_id": biz_id,
                                "order_id": order_id,
                                "mode": sign_mode,
                                "amount": amount,
                                "product_name": product_name,
                            },
                        )
                        self._push_runtime_message(
                            account_id,
                            f"二维码生成成功，bizId：{biz_id}",
                        )
                        if own_flow:
                            self.runtime_logs.finish_run(
                                flow,
                                status="success",
                                message="二维码生成成功",
                                details={"biz_id": biz_id, "order_id": order_id, "mode": sign_mode},
                            )
                        return saved_task
                    except UpstreamRequestError as exc:
                        sign_attempts.append(
                            {
                                "attempt": sign_attempt,
                                "ok": False,
                                "mode": sign_mode,
                                "biz_id": biz_id,
                                "message": exc.message,
                                "details": exc.details,
                            }
                        )
                        self.runtime_logs.log_event(
                            flow,
                            stage="sign_attempt",
                            status="retry",
                            message="签单失败，准备重试",
                            details={
                                "cycle": cycle,
                                "attempt": sign_attempt,
                                "biz_id": biz_id,
                                "mode": sign_mode,
                                "error": exc.message,
                            },
                            level=logging.WARNING,
                        )
                        self._push_runtime_message(
                            account_id,
                            f"支付二维码签单失败，第 {sign_attempt} 次重试中",
                        )

                qr_cycles.append(
                    {
                        "cycle": cycle,
                        "preview_biz_id": biz_id,
                        "sign_attempts": sign_attempts,
                        "fallback": "rerun_preview_chain",
                    }
                )
                self.runtime_logs.log_event(
                    flow,
                    stage="sign",
                    status="retry",
                    message="签单连续失败 3 次，清空 preview 后重跑整条链路",
                    details={"cycle": cycle, "biz_id": biz_id, "mode": sign_mode},
                    level=logging.WARNING,
                )
                self._push_runtime_message(
                    account_id,
                    "签单连续失败 3 次，正在重新获取 bizId",
                )
                session.preview = None
                session.last_sign = ""
                session.last_order_id = ""
                self.state_service.save_session(session)
        except Exception as exc:
            if own_flow:
                self.runtime_logs.finish_run(
                    flow,
                    status="failed",
                    message=f"二维码生成失败：{exc}",
                    details={"error": exc.__class__.__name__},
                    level=logging.ERROR,
                )
            raise

    def run_payment_flow(
        self,
        account_id: str,
        *,
        product_id: str | None = None,
        pay_type: str = "ALI",
        source: str = "manual",
    ) -> PaymentTaskRecord:
        self._ensure_not_paused(account_id)
        session = self.state_service.load_session(account_id)
        selected_product_id = (product_id or session.selected_product_id).strip()
        if not selected_product_id:
            raise BadRequestError("请先选择套餐，再启动支付链路")
        flow = self.runtime_logs.start_run(
            account_id=account_id,
            action="run_payment_flow",
            source=source,
            product_id=selected_product_id,
            pay_type=pay_type,
            details={"purchase_mode": session.purchase_mode},
        )
        try:
            self._push_runtime_message(
                account_id,
                "任务已启动，正在准备支付链路",
            )
            current_account = self.state_service.get_account(account_id)
            ticket_pool_size = current_account.ticket_pool_size

            if ticket_pool_size > 0:
                # Pool mode: pre-collect N tickets, then drain them into /preview one by one.
                # The pool is used ONLY for the first attempt; if all tickets are exhausted
                # without a bizId, _run_pool_preview falls back internally to
                # race_preview_payment — no exception escapes this branch.
                deadline_time = current_account.preview_concurrency_time
                self.runtime_logs.log_event(
                    flow,
                    stage="ticket_pool",
                    status="started",
                    message=f"ticket 池模式启动，目标 {ticket_pool_size} 个 ticket",
                    details={
                        "pool_size": ticket_pool_size,
                        "deadline": deadline_time or "disabled",
                        "drain_mode": "parallel"
                        if current_account.ticket_pool_drain_interval_ms <= 0
                        else "serial",
                        "drain_interval_ms": current_account.ticket_pool_drain_interval_ms,
                    },
                )
                self._push_runtime_message(account_id, f"ticket 池模式，目标 {ticket_pool_size} 个 ticket")
                preview = self._run_pool_preview(
                    account_id=account_id,
                    product_id=selected_product_id,
                    ticket_pool_size=ticket_pool_size,
                    deadline_time=deadline_time,
                    flow=flow,
                )
            else:
                preview = self.race_preview_payment(
                    account_id,
                    PreviewPaymentRequest(product_id=selected_product_id),
                    concurrency=current_account.preview_concurrency,
                    preview_concurrency_time=current_account.preview_concurrency_time,
                    flow=flow,
                )
            task = self.create_qr(
                account_id,
                CreateQrRequest(
                    product_id=selected_product_id,
                    pay_type=pay_type,
                    biz_id=preview.biz_id,
                ),
                flow=flow,
            )
            self.runtime_logs.finish_run(
                flow,
                status="success",
                message="完整支付链路执行成功",
                details={"biz_id": task.biz_id, "task_id": task.id, "amount": task.amount},
            )
            return task
        except RunPausedError as exc:
            self.runtime_logs.finish_run(
                flow,
                status="paused",
                message=str(exc),
                level=logging.WARNING,
            )
            raise
        except Exception as exc:
            self.runtime_logs.finish_run(
                flow,
                status="failed",
                message=f"完整支付链路失败：{exc}",
                details={"error": exc.__class__.__name__},
                level=logging.ERROR,
            )
            raise

    def _run_pool_preview(
        self,
        account_id: str,
        product_id: str,
        ticket_pool_size: int,
        deadline_time: str,
        flow: "FlowRun",
    ) -> PreviewResult:
        """Fill the ticket pool then drain it one-by-one against /preview.

        Executes only **once** — does NOT loop on exhaustion.
        If all pool tickets are consumed without a bizId, falls back directly
        to ``race_preview_payment`` without raising, so the caller's chain
        is never interrupted by the pool exhaustion case.
        """
        self._ensure_not_paused(account_id)
        current_account, session = self._ensure_context(account_id)

        # Step 1: fill until we have ticket_pool_size unused entries
        session = self._fill_ticket_pool(
            account_id,
            current_account,
            session,
            ticket_pool_size,
            flow=flow,
            deadline_time=deadline_time,
        )

        # Reload after fill — session was saved incrementally inside _fill_ticket_pool
        current_account = self.state_service.get_account(account_id)
        session = self.state_service.load_session(account_id)
        invitation = current_account.invitation_code.strip()

        # If deadline is configured and pool filled BEFORE the deadline,
        # hold here until the deadline arrives so that drain fires exactly on time.
        if deadline_time:
            wait_secs = self._preview_concurrency_wait_seconds(deadline_time)
            if wait_secs > 0:
                unused_count = sum(1 for e in session.ticket_pool if not e.used)
                self.runtime_logs.log_event(
                    flow,
                    stage="ticket_pool_wait",
                    status="waiting",
                    message=f"ticket 池已满（{unused_count} 张），等待并发时间 {deadline_time}（剩余 {wait_secs:.1f} 秒）",
                    details={"deadline": deadline_time, "wait_seconds": round(wait_secs, 3), "pool_collected": unused_count},
                )
                self._push_runtime_message(account_id, f"ticket 池已满，等待 {deadline_time} 开始抢购…")
                while True:
                    self._ensure_not_paused(account_id)
                    remaining = self._preview_concurrency_wait_seconds(deadline_time)
                    if remaining <= 0:
                        break
                    import time as _time
                    _time.sleep(min(0.1, remaining))
                self.runtime_logs.log_event(
                    flow,
                    stage="ticket_pool_wait",
                    status="ready",
                    message="并发时间到，开始消耗 ticket 池",
                    details={"deadline": deadline_time, "pool_collected": unused_count},
                )
                self._push_runtime_message(account_id, "并发时间到，开始消耗 ticket 池抢购")

        # Step 2: drain pool tickets into /preview until bizId is received.
        # If all tickets are exhausted without success, fall back directly to
        # race_preview_payment — no exception propagates out of this method.
        try:
            preview = self._drain_ticket_pool(
                account_id,
                current_account,
                session,
                PreviewPaymentRequest(product_id=product_id),
                invitation,
                flow=flow,
            )
        except UpstreamRequestError as exc:
            if "已耗尽" not in exc.message:
                raise
            self.runtime_logs.log_event(
                flow,
                stage="ticket_pool",
                status="fallback",
                message="ticket 池已耗尽未拿到 bizId，切换竞速模式继续抢购",
                details={
                    "pool_size": ticket_pool_size,
                    "fallback_proxy_ticket_pool_only": self.settings.fallback_proxy_ticket_pool_only,
                    "fallback_retry_uses_fallback_proxy": not self.settings.fallback_proxy_ticket_pool_only,
                },
                level=logging.WARNING,
            )
            self._push_runtime_message(account_id, "ticket 池已耗尽，切换竞速模式继续抢购…")
            return self.race_preview_payment(
                account_id,
                PreviewPaymentRequest(product_id=product_id),
                concurrency=current_account.preview_concurrency,
                preview_concurrency_time=current_account.preview_concurrency_time,
                flow=flow,
            )

        # Persist preview to session so create_qr can read it
        session = self.state_service.load_session(account_id)
        session.preview = preview
        session.selected_product_id = product_id
        self.state_service.save_session(session)
        return preview

    def _ensure_not_paused(self, account_id: str) -> None:
        from app.services.scheduler_service import get_scheduler_service

        if get_scheduler_service().is_pause_requested(account_id):
            raise RunPausedError("任务已暂停")

    def _push_runtime_message(
        self,
        account_id: str,
        message: str,
        *,
        schedule_status: str = "running",
        account_status_message: str | None = None,
    ) -> None:
        self.state_service.update_runtime_progress(
            account_id,
            schedule_status=schedule_status,
            schedule_message=message,
            account_status_message=account_status_message,
        )
        logger.info("[%s] %s", account_id, message)

    def _create_upgrade_sign(
        self,
        account: AccountRecord,
        session: AccountSessionState,
        *,
        pay_type: str,
        product_id: str,
        biz_id: str,
    ):
        summary = session.preview.last_subscription_summary if session.preview else {}
        old_product_id = str(summary.get("productId") or "").strip()
        agreement_no = str(summary.get("agreementNo") or "").strip()
        if not old_product_id or not agreement_no:
            raise BadRequestError(
                "升级签单缺少 lastSubscriptionSummary.productId 或 agreementNo",
                details={
                    "purchase_mode": session.purchase_mode,
                    "last_subscription_summary": summary,
                    "product_id": product_id,
                    "biz_id": biz_id,
                },
            )
        return self.bigmodel_client.update_sign(
            account,
            session,
            pay_type=pay_type,
            old_product_id=old_product_id,
            new_product_id=product_id,
            customer_id=session.customer_number,
            agreement_no=agreement_no,
            biz_id=biz_id,
        )

    def _request_sign(
        self,
        account: AccountRecord,
        session: AccountSessionState,
        *,
        pay_type: str,
        product_id: str,
        biz_id: str,
        invitation_code: str,
    ):
        sign_mode = session.purchase_mode or ("upgrade" if session.is_subscribed else "new_purchase")
        if sign_mode == "upgrade":
            return self._create_upgrade_sign(
                account,
                session,
                pay_type=pay_type,
                product_id=product_id,
                biz_id=biz_id,
            )
        return self.bigmodel_client.create_sign(
            account,
            session,
            pay_type=pay_type,
            product_id=product_id,
            customer_id=session.customer_number,
            biz_id=biz_id,
            invitation_code=invitation_code,
        )

    def check_payment(self, account_id: str, biz_id: str) -> PaymentCheckResult:
        account, session = self._ensure_context(account_id)
        result = self.bigmodel_client.check_payment(account, session, biz_id=biz_id)
        data = result.data
        if isinstance(data, str):
            status = data.strip()
        elif isinstance(data, dict):
            status = str(data.get("status") or data.get("payStatus") or data.get("result") or "")
        else:
            status = str(data)
        status = status or "PENDING"

        task = self.state_service.get_task_by_biz_id(account_id, biz_id)
        if task:
            task.status = status
            task.last_check = result.raw
            task.updated_at = utc_now_iso()
            self.state_service.save_task(task)

        self.runtime_logs.log_account_event(
            account_id=account_id,
            action="check_payment",
            stage="payment_status",
            status="success",
            message="支付状态查询完成",
            details={"biz_id": biz_id, "status": status},
        )

        return PaymentCheckResult(biz_id=biz_id, status=status, raw=result.raw)

    def list_tasks(self, account_id: str) -> list[PaymentTaskRecord]:
        return self.state_service.list_tasks(account_id)

    def _attach_pow_if_needed(
        self,
        session: AccountSessionState,
        request: CaptchaVerifyPayloadRequest,
        payload_bundle: dict[str, Any],
    ) -> None:
        raw_challenge = session.captcha_challenge_raw or {}
        pow_cfg = (((raw_challenge.get("data") or {}).get("comm_captcha_cfg") or {}).get("pow_cfg") or {})
        if request.pow_answer or not pow_cfg.get("prefix") or not pow_cfg.get("md5"):
            return
        pow_result = self.captcha_service.solve_pow(
            str(pow_cfg.get("prefix") or ""),
            str(pow_cfg.get("md5") or ""),
        )
        payload_bundle["payload"]["pow_answer"] = pow_result["pow_answer"]
        payload_bundle["payload"]["pow_calc_time"] = pow_result["pow_calc_time"]
        payload_bundle["pow"] = pow_result

    def _resolve_or_solve_preview_captcha(
        self,
        account_id: str,
        session: AccountSessionState,
        request: PreviewPaymentRequest,
        *,
        flow: FlowRun | None = None,
    ) -> tuple[str, str]:
        if (request.ticket or "").strip() and (request.randstr or "").strip():
            if flow is not None:
                self.runtime_logs.log_event(
                    flow,
                    stage="captcha",
                    status="reused",
                    message="复用外部传入的验证码票据",
                    details={"ticket_ready": True, "randstr_ready": True},
                )
            return self.captcha_service.resolve_preview_captcha(session, request)

        solved = self.solve_captcha(account_id, flow=flow)
        verify = solved.get("verify") if isinstance(solved.get("verify"), dict) else {}
        error_code = str(verify.get("error_code") or "")
        if error_code == "50":
            raise UpstreamRequestError(
                "验证码识别失败（error=50），已刷新重试但仍未通过，停止 preview 请求。",
                details={
                    "verify": verify,
                    "attempts": solved.get("attempts") or [],
                },
            )
        ticket = str(solved.get("ticket") or "")
        randstr = str(solved.get("randstr") or "")
        if not ticket or not randstr:
            raise UpstreamRequestError(
                "自动验证码没通过，已停止 preview 请求。",
                details={
                    "verify": solved.get("verify") or {},
                    "attempts": solved.get("attempts") or [],
                },
            )
        if flow is not None:
            self.runtime_logs.log_event(
                flow,
                stage="captcha",
                status="success",
                message="已拿到可用于 preview 的验证码票据",
                details={"attempts": len(solved.get("attempts") or []), "ticket_ready": True, "randstr_ready": True},
            )
        return ticket, randstr

    def _captcha_ocr_gate(self, challenge: dict[str, Any]) -> dict[str, Any]:
        ocr = challenge.get("ocr") if isinstance(challenge.get("ocr"), dict) else {}
        points = ocr.get("points") if isinstance(ocr.get("points"), list) else []
        target_chars = ocr.get("target_chars") if isinstance(ocr.get("target_chars"), list) else []
        instruction_chars = self._extract_instruction_target_chars(str(challenge.get("instruction") or ""))
        expected_points = max(3, len(target_chars), len(instruction_chars))
        confidence = self._safe_float(ocr.get("confidence"))
        min_confidence = max(getattr(self.tencent_captcha_client.settings, "tencent_captcha_min_confidence", 0.0), 0.0)
        point_labels = [
            str(item.get("label") or "").strip()
            for item in points
            if isinstance(item, dict)
        ]
        expected_labels = target_chars or instruction_chars
        labels_match = bool(expected_labels) and point_labels[: len(expected_labels)] == expected_labels
        usable = bool(points) and len(points) >= expected_points and confidence >= min_confidence and labels_match
        return {
            "usable": usable,
            "points": len(points),
            "expected_points": expected_points,
            "confidence": confidence,
            "min_confidence": min_confidence,
            "target_chars": target_chars,
            "instruction_chars": instruction_chars,
            "point_labels": point_labels,
            "labels_match": labels_match,
        }

    def _extract_instruction_target_chars(self, instruction: str) -> list[str]:
        compact = instruction.replace(" ", "").replace("\n", "").strip()
        marker = "请依次点击"
        if marker in compact:
            compact = compact.split(marker, 1)[1]
        compact = compact.replace("：", "").replace(":", "")
        return [char for char in compact if "\u4e00" <= char <= "\u9fff"]

    def _safe_float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _hydrate_tdc_if_needed(
        self,
        account: AccountRecord,
        session: AccountSessionState,
        request: CaptchaVerifyPayloadRequest,
    ) -> tuple[CaptchaVerifyPayloadRequest, dict[str, Any] | None]:
        if (request.collect or "").strip() and (request.eks or "").strip():
            return request, None
        result = self.tdc_service.collect_for_challenge(account, session.captcha_challenge_raw or {})
        hydrated = request.model_copy(
            update={
                "collect": (request.collect or "").strip() or result.collect_raw,
                "eks": (request.eks or "").strip() or result.eks,
            }
        )
        return hydrated, result.to_payload()

    def _is_retryable_flow_error(self, exc: AegisFlowError) -> bool:
        """Return whether a captcha/preview failure is safe to retry inside the same flow."""
        if isinstance(exc, UpstreamRequestError):
            return True
        if not isinstance(exc, BadRequestError):
            return False

        message = exc.message
        hard_markers = (
            "Node.js 命令不可用",
            "TDC VM runner 不存在",
            "账号缺少 token",
            "缺少 token",
            "缺少 product_id",
            "请先选择套餐",
            "请先调用 preview",
            "升级签单缺少",
            "本地 OCR 已关闭",
            "本地 OCR 依赖没装全",
        )
        if any(marker in message for marker in hard_markers):
            return False

        retryable_markers = (
            "TDC VM 执行失败",
            "TDC VM 输出不是合法 JSON",
            "TDC VM 没产出 collect/eks",
            "challenge 里没有 tdc_path",
            "tdc.js 内容看着不对劲",
            "缺少 challenge sess",
            "缺少点击点位",
            "验证码 OCR 识别超时",
            "验证码 OCR 识别失败",
        )
        return any(marker in message for marker in retryable_markers)

    def _ensure_context(self, account_id: str) -> tuple[AccountRecord, AccountSessionState]:
        account = self.state_service.get_account(account_id)
        session = self.state_service.load_session(account_id)
        if not session.customer_number or not (session.org_id or account.org_id) or not (session.project_id or account.project_id):
            detail = self.bootstrap_account(account_id)
            account = self.state_service.get_account(account_id)
            session = detail.session
        return account, session

    def _merge_products(self, payload: dict[str, Any], *, purchase_mode: str = "new_purchase") -> list[ProductOffer]:
        dynamic_products = payload.get("productList") or []
        dynamic_map = {
            str(item.get("productId")): item
            for item in dynamic_products
            if isinstance(item, dict) and item.get("productId")
        }
        merged: list[ProductOffer] = []
        for static in STATIC_PRODUCTS:
            dynamic = dynamic_map.get(static.product_id, {})
            merged.append(
                ProductOffer(
                    product_id=static.product_id,
                    product_name=static.product_name,
                    unit=static.unit,
                    sale_price=static.sale_price,
                    plan_type=static.plan_type,
                    purchase_mode=purchase_mode,
                    version=static.version,
                    sold_out=False,
                    forbidden=bool(dynamic.get("forbidden")),
                    last_valid=bool(dynamic.get("lastValid")),
                    can_repurchase=bool(dynamic.get("canRepurchase")),
                    delay=bool(dynamic.get("delay")),
                    effective_time=str(dynamic.get("effectiveTime") or ""),
                    monthly_renew_amount=self._string_value(dynamic, "monthlyRenewAmount"),
                    monthly_original_amount=self._string_value(dynamic, "monthlyOriginalAmount"),
                    campaign_discount_details=list(dynamic.get("campaignDiscountDetails") or []),
                    raw=dynamic,
                )
            )
        for product_id, dynamic in dynamic_map.items():
            if any(item.product_id == product_id for item in merged):
                continue
            merged.append(
                ProductOffer(
                    product_id=product_id,
                    product_name=str(dynamic.get("productName") or product_id),
                    unit=str(dynamic.get("unit") or ""),
                    sale_price=self._string_value(dynamic, "salePrice"),
                    plan_type=str(dynamic.get("type") or "unknown"),
                    purchase_mode=purchase_mode,
                    version=str(dynamic.get("version") or "upstream"),
                    sold_out=False,
                    forbidden=bool(dynamic.get("forbidden")),
                    last_valid=bool(dynamic.get("lastValid")),
                    can_repurchase=bool(dynamic.get("canRepurchase")),
                    delay=bool(dynamic.get("delay")),
                    effective_time=str(dynamic.get("effectiveTime") or ""),
                    monthly_renew_amount=self._string_value(dynamic, "monthlyRenewAmount"),
                    monthly_original_amount=self._string_value(dynamic, "monthlyOriginalAmount"),
                    campaign_discount_details=list(dynamic.get("campaignDiscountDetails") or []),
                    raw=dynamic,
                )
            )
        return merged

    def _choose_org_and_project(
        self,
        *,
        organizations: list[dict[str, Any]],
        preferred_org_id: str,
        preferred_project_id: str,
    ) -> tuple[str, str]:
        selected_org = None
        if preferred_org_id:
            selected_org = next(
                (
                    item
                    for item in organizations
                    if self._entity_id(item, ("id", "organizationId", "orgId")) == preferred_org_id
                ),
                None,
            )
        if selected_org is None:
            selected_org = next((item for item in organizations if bool(item.get("isDefault"))), None)
        if selected_org is None and organizations:
            selected_org = organizations[0]
        if selected_org is None:
            return "", ""

        org_id = self._entity_id(selected_org, ("id", "organizationId", "orgId"))
        projects = self._extract_projects(selected_org)
        selected_project = None
        if preferred_project_id:
            selected_project = next(
                (
                    item
                    for item in projects
                    if self._entity_id(item, ("id", "projectId")) == preferred_project_id
                ),
                None,
            )
        if selected_project is None:
            selected_project = next((item for item in projects if int(item.get("projectType") or 0) == 2), None)
        if selected_project is None:
            selected_project = next((item for item in projects if bool(item.get("isDefault"))), None)
        if selected_project is None and projects:
            selected_project = projects[0]
        project_id = self._entity_id(selected_project, ("id", "projectId")) if selected_project else ""
        return org_id, project_id

    def _extract_organizations(self, user_info: dict[str, Any]) -> list[dict[str, Any]]:
        organizations = user_info.get("organizations") or user_info.get("organizationList") or []
        if not isinstance(organizations, list):
            return []
        return [item for item in organizations if isinstance(item, dict)]

    def _extract_projects(self, organization: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("projects", "projectList", "projectVOList"):
            value = organization.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def _entity_id(self, payload: dict[str, Any] | None, fields: tuple[str, ...]) -> str:
        if not payload:
            return ""
        for field in fields:
            value = payload.get(field)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    def _build_qr_base64(self, content: str) -> str:
        qr = qrcode.QRCode(border=4, box_size=12, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(content)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

    def _ensure_dict(self, payload: Any, *, label: str) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        raise UpstreamRequestError(f"{label} 结构异常", details={"payload_type": type(payload).__name__})

    def _preview_from_upstream_payload(self, raw: dict[str, Any]) -> PreviewResult:
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        return PreviewResult(
            biz_id=str(data.get("bizId") or ""),
            third_party_amount=self._string_value(data, "thirdPartyAmount"),
            sold_out=bool(data.get("soldOut")),
            original_amount=self._string_value(data, "originalAmount"),
            pay_amount=self._string_value(data, "payAmount"),
            residual_amount=self._string_value(data, "residualAmount"),
            give_amount=self._string_value(data, "giveAmount"),
            cash_amount=self._string_value(data, "cashAmount"),
            renew_amount=self._string_value(data, "renewAmount"),
            campaign_discount_details=list(data.get("campaignDiscountDetails") or []),
            last_subscription_summary=self._ensure_dict(
                data.get("lastSubscriptionSummary") or {},
                label="preview.lastSubscriptionSummary",
            ),
            raw=raw,
        )

    def _string_value(self, payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if value is None:
            return ""
        return str(value)


@lru_cache(maxsize=1)
def get_payment_service() -> PaymentService:
    """Get the shared payment service."""
    return PaymentService()
