"""BigModel upstream API wrapper."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.browser_profiles import resolve_user_agent
from app.clients.fingerprint_http import FingerprintHttpClient
from app.config import get_settings
from app.errors import BadRequestError, UpstreamRequestError
from app.models import AccountRecord, AccountSessionState, PreviewPaymentRequest


@dataclass
class ApiCallResult:
    """Normalized upstream response wrapper."""

    data: Any
    raw: dict[str, Any]


class BigModelClient:
    """Call BigModel payment-related APIs with browser-like headers."""

    def __init__(self, http_client: FingerprintHttpClient | None = None) -> None:
        self.settings = get_settings()
        self.http_client = http_client or FingerprintHttpClient(self.settings)

    @property
    def transport_name(self) -> str:
        return self.http_client.transport_name

    def get_customer_info(self, account: AccountRecord, session: AccountSessionState) -> ApiCallResult:
        return self._request(account, session, "GET", "/biz/customer/getCustomerInfo")

    def batch_preview(
        self,
        account: AccountRecord,
        session: AccountSessionState,
        invitation_code: str,
    ) -> ApiCallResult:
        return self._request(
            account,
            session,
            "POST",
            "/biz/pay/batch-preview",
            json_body={"invitationCode": invitation_code},
        )

    def preview_payment(
        self,
        account: AccountRecord,
        session: AccountSessionState,
        request: PreviewPaymentRequest,
        *,
        invitation_code: str,
        ticket: str,
        randstr: str,
        allow_fallback_proxy: bool = False,
    ) -> ApiCallResult:
        return self._request(
            account,
            session,
            "POST",
            "/biz/pay/preview",
            json_body={
                "productId": request.product_id,
                "invitationCode": invitation_code,
                "ticket": ticket,
                "randstr": randstr,
            },
            allow_fallback_proxy=allow_fallback_proxy,
        )

    def create_sign(
        self,
        account: AccountRecord,
        session: AccountSessionState,
        *,
        pay_type: str,
        product_id: str,
        customer_id: str,
        biz_id: str,
        invitation_code: str,
    ) -> ApiCallResult:
        return self._request(
            account,
            session,
            "POST",
            "/biz/pay/create-sign",
            json_body={
                "payType": pay_type,
                "productId": product_id,
                "customerId": customer_id,
                "bizId": biz_id,
                "invitationCode": invitation_code,
            },
        )

    def update_sign(
        self,
        account: AccountRecord,
        session: AccountSessionState,
        *,
        pay_type: str,
        old_product_id: str,
        new_product_id: str,
        customer_id: str,
        agreement_no: str,
        biz_id: str,
    ) -> ApiCallResult:
        return self._request(
            account,
            session,
            "POST",
            "/biz/pay/product/update/sign",
            json_body={
                "payType": pay_type,
                "oldProductId": old_product_id,
                "newProductId": new_product_id,
                "customerId": customer_id,
                "agreementNo": agreement_no,
                "bizId": biz_id,
            },
        )

    def check_payment(
        self,
        account: AccountRecord,
        session: AccountSessionState,
        *,
        biz_id: str,
    ) -> ApiCallResult:
        return self._request(
            account,
            session,
            "GET",
            "/biz/pay/check",
            params={"bizId": biz_id},
        )

    def _request(
        self,
        account: AccountRecord,
        session: AccountSessionState,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        allow_fallback_proxy: bool = False,
    ) -> ApiCallResult:
        token = account.token.strip() or account.cookies.get("bigmodel_token_production", "").strip()
        if not token:
            raise BadRequestError("账号缺少 token，没法请求 BigModel")

        headers = {
            "Authorization": f"Bearer {token}",
            "Bigmodel-Organization": session.org_id or account.org_id,
            "Bigmodel-Project": session.project_id or account.project_id,
        }
        headers = {key: value for key, value in headers.items() if value}
        payload = self.http_client.request_json(
            method,
            self._build_url(path),
            headers=headers,
            cookies=account.cookies,
            json_body=json_body,
            params=params,
            proxy_url=account.proxy_url or None,
            allow_fallback_proxy=allow_fallback_proxy,
            user_agent=resolve_user_agent(account.user_agent, account.browser_impersonate),
            browser_impersonate=account.browser_impersonate or None,
        )
        if not isinstance(payload, dict):
            raise UpstreamRequestError(
                "上游返回结构异常",
                details={"path": path, "payload_type": type(payload).__name__},
            )

        code = payload.get("code")
        if code not in (None, 0, 200):
            # preview 555 (系统繁忙) 不抛异常，让上层循环重试
            if code == 555 and path.endswith("/preview"):
                return ApiCallResult(data=payload.get("data"), raw=payload)
            raise UpstreamRequestError(
                str(payload.get("msg") or payload.get("message") or "上游业务返回失败"),
                details={"path": path, "payload": payload},
            )

        if "data" in payload:
            return ApiCallResult(data=payload.get("data"), raw=payload)
        if "result" in payload:
            return ApiCallResult(data=payload.get("result"), raw=payload)
        return ApiCallResult(data=payload, raw=payload)

    def _build_url(self, path: str) -> str:
        return f"{self.settings.bigmodel_api_base}/{path.lstrip('/')}"


@lru_cache(maxsize=1)
def get_bigmodel_client() -> BigModelClient:
    """Get the shared BigModel client."""
    return BigModelClient()
