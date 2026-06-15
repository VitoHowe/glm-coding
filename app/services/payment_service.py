"""Business orchestration for BigModel payment flow."""

from __future__ import annotations

import base64
import concurrent.futures
import logging
import threading
from dataclasses import asdict, dataclass
from functools import lru_cache
from io import BytesIO
from typing import Any

import qrcode

from app.clients.bigmodel_client import BigModelClient, get_bigmodel_client
from app.clients.tencent_captcha_client import TencentCaptchaClient, get_tencent_captcha_client
from app.config import get_settings
from app.errors import BadRequestError, GlmDeskError, UpstreamRequestError
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
    get_account_state_service,
    make_id,
    utc_now_iso,
)
from app.services.captcha_service import CaptchaService, get_captcha_service
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
        return {
            "status": "ok",
            "transport": self.bigmodel_client.transport_name,
            "ocr": self.ocr_service.status_payload(),
            "tdc": self.tdc_service.status_payload(),
        }

    def list_accounts(self):
        return self.state_service.list_accounts()

    def get_account_detail(self, account_id: str) -> AccountDetailResponse:
        return self.state_service.get_account_detail(account_id)

    def import_account(self, request):
        return self.state_service.import_account(request)

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

            invitation = invitation_code if invitation_code is not None else account.invitation_code
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
                    "ticket_ready": bool(verify_result.ticket),
                    "randstr_ready": bool(verify_result.randstr),
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
                except GlmDeskError as exc:
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
                        message="OCR 识别异常，刷新验证码重试",
                        details={"attempt": attempt, "error": exc.message, "details": exc.details},
                        level=logging.WARNING,
                    )
                    if push_progress:
                        self._push_runtime_message(
                            account_id,
                            f"验证码 OCR 异常，第 {attempt} 轮重试中",
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

                verify = self._submit_captcha_verify_for_session(
                    account_id,
                    account,
                    session,
                    CaptchaVerifyPayloadRequest(),
                    flow=flow,
                    persist_session=persist_session,
                    details={"attempt": attempt, **(details or {})},
                )
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
            invitation = (request.invitation_code or account.invitation_code).strip()
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
                    details={"round": preview_round, "product_id": request.product_id, "captcha_required": True},
                )
                captcha_request = request if preview_round == 1 else request.model_copy(update={"ticket": None, "randstr": None})
                ticket, randstr = self._resolve_or_solve_preview_captcha(account_id, session, captcha_request, flow=flow)

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
                        stage="preview",
                        status="retry",
                        message="preview 请求失败，下一轮将重新获取验证码票据并再次请求 preview",
                        details={"round": preview_round, "error": exc.message, "details": exc.details},
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
        flow: FlowRun | None = None,
    ) -> PreviewResult:
        normalized_concurrency = max(1, min(4, int(concurrency or 1)))
        own_flow = flow is None
        if own_flow:
            flow = self.runtime_logs.start_run(
                account_id=account_id,
                action="preview_payment_race",
                product_id=request.product_id,
                details={"concurrency": normalized_concurrency},
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
        invitation = (request.invitation_code or account.invitation_code).strip()
        stop_event = threading.Event()
        errors: list[str] = []
        self.runtime_logs.log_event(
            flow,
            stage="preview_race",
            status="started",
            message=f"preview 竞速启动，并发 {normalized_concurrency} 路",
            details={"concurrency": normalized_concurrency, "product_id": request.product_id},
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
                        errors.append(str(exc))
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
                    "preview 竞速失败，所有并发任务都未拿到 bizId",
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
        flow: FlowRun | None,
    ) -> PreviewRaceWinner:
        session = base_session.model_copy(deep=True)
        round_no = 0
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
            if stop_event.is_set():
                raise RunPausedError("preview race 已有其他任务胜出")
            ticket = str(solved.get("ticket") or "")
            randstr = str(solved.get("randstr") or "")
            if not ticket or not randstr:
                continue
            result = self.bigmodel_client.preview_payment(
                account,
                session,
                request,
                invitation_code=invitation,
                ticket=ticket,
                randstr=randstr,
            )
            if stop_event.is_set():
                raise RunPausedError("preview race 已有其他任务胜出")
            raw = result.raw
            code = raw.get("code")
            data = result.data if isinstance(result.data, dict) else {}
            biz_id = str(data.get("bizId") or "").strip()
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
            invitation = (request.invitation_code or account.invitation_code).strip()
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
            preview = self.race_preview_payment(
                account_id,
                PreviewPaymentRequest(product_id=selected_product_id),
                concurrency=self.state_service.get_account(account_id).preview_concurrency,
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
