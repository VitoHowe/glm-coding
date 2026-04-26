"""Pydantic models used across the application."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PayType = Literal["ALI", "WE_CHAT"]
PurchaseMode = Literal["new_purchase", "upgrade"]


class ProductOffer(BaseModel):
    """Merged static and dynamic product information."""

    model_config = ConfigDict(extra="ignore")

    product_id: str
    product_name: str
    unit: str
    sale_price: str
    plan_type: str
    purchase_mode: PurchaseMode = "new_purchase"
    version: str = "v2"
    sold_out: bool = False
    forbidden: bool = False
    last_valid: bool = False
    can_repurchase: bool = False
    delay: bool = False
    effective_time: str = ""
    monthly_renew_amount: str = ""
    monthly_original_amount: str = ""
    campaign_discount_details: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class PreviewResult(BaseModel):
    """Normalized preview response payload."""

    model_config = ConfigDict(extra="ignore")

    biz_id: str
    third_party_amount: str = ""
    sold_out: bool = False
    original_amount: str = ""
    pay_amount: str = ""
    residual_amount: str = ""
    give_amount: str = ""
    cash_amount: str = ""
    renew_amount: str = ""
    campaign_discount_details: list[dict[str, Any]] = Field(default_factory=list)
    last_subscription_summary: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class AccountRecord(BaseModel):
    """Persisted account credential record."""

    model_config = ConfigDict(extra="ignore")

    id: str
    label: str
    token: str = ""
    cookie_header: str = ""
    cookies: dict[str, str] = Field(default_factory=dict)
    org_id: str = ""
    project_id: str = ""
    invitation_code: str = ""
    proxy_url: str = ""
    user_agent: str = ""
    browser_impersonate: str = ""
    preview_concurrency: int = 1
    schedule_enabled: bool = False
    scheduled_start_time: str = ""
    last_scheduled_run_at: str | None = None
    last_scheduled_run_key: str = ""
    last_manual_run_at: str | None = None
    last_schedule_status: str = ""
    last_schedule_message: str = ""
    account_status: str = "unchecked"
    account_status_message: str = ""
    account_checked_at: str | None = None
    created_at: str
    updated_at: str
    last_bootstrap_at: str | None = None


class PublicAccountRecord(BaseModel):
    """Safe account summary exposed to the local UI."""

    model_config = ConfigDict(extra="ignore")

    id: str
    label: str
    org_id: str = ""
    project_id: str = ""
    invitation_code: str = ""
    proxy_url: str = ""
    user_agent: str = ""
    browser_impersonate: str = ""
    preview_concurrency: int = 1
    schedule_enabled: bool = False
    scheduled_start_time: str = ""
    last_scheduled_run_at: str | None = None
    last_scheduled_run_key: str = ""
    last_manual_run_at: str | None = None
    last_schedule_status: str = ""
    last_schedule_message: str = ""
    account_status: str = "unchecked"
    account_status_message: str = ""
    account_checked_at: str | None = None
    has_token: bool = False
    token_preview: str = ""
    has_cookie_header: bool = False
    last_bootstrap_at: str | None = None
    created_at: str
    updated_at: str


class AccountSessionState(BaseModel):
    """Persisted session state derived from upstream calls."""

    model_config = ConfigDict(extra="ignore")

    account_id: str
    org_id: str = ""
    project_id: str = ""
    customer_number: str = ""
    customer_name: str = ""
    organizations: list[dict[str, Any]] = Field(default_factory=list)
    user_info: dict[str, Any] = Field(default_factory=dict)
    products: list[ProductOffer] = Field(default_factory=list)
    is_subscribed: bool = False
    purchase_mode: PurchaseMode = "new_purchase"
    selected_product_id: str = ""
    captcha_ticket: str = ""
    captcha_randstr: str = ""
    captcha_updated_at: str | None = None
    captcha_challenge_sess: str = ""
    captcha_challenge_sid: str = ""
    captcha_challenge_instruction: str = ""
    captcha_challenge_raw: dict[str, Any] = Field(default_factory=dict)
    captcha_challenge_ocr: dict[str, Any] = Field(default_factory=dict)
    captcha_challenge_updated_at: str | None = None
    preview: PreviewResult | None = None
    last_sign: str = ""
    last_order_id: str = ""
    updated_at: str


class PaymentTaskRecord(BaseModel):
    """Persisted QR-code payment task."""

    model_config = ConfigDict(extra="ignore")

    id: str
    account_id: str
    product_id: str
    product_name: str = ""
    pay_type: PayType
    biz_id: str
    amount: str = ""
    sign: str = ""
    qr_base64: str = ""
    status: str = "PENDING"
    raw_preview: dict[str, Any] = Field(default_factory=dict)
    raw_sign: dict[str, Any] = Field(default_factory=dict)
    last_check: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class AccountImportRequest(BaseModel):
    """Account import payload."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    label: str
    token: str | None = None
    cookie_header: str | None = None
    cookies: dict[str, str] = Field(default_factory=dict)
    org_id: str = ""
    project_id: str = ""
    invitation_code: str = ""
    proxy_url: str = ""
    user_agent: str = ""
    browser_impersonate: str = ""

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("label 不能为空")
        return normalized

    @field_validator("cookies", mode="before")
    @classmethod
    def validate_cookies(cls, value: Any) -> dict[str, str]:
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise ValueError("cookies 必须是对象")
        return {str(key): str(item) for key, item in value.items()}

    @model_validator(mode="after")
    def validate_auth(self) -> "AccountImportRequest":
        has_auth = bool((self.token or "").strip() or (self.cookie_header or "").strip() or self.cookies)
        if not has_auth:
            raise ValueError("必须提供 token、cookie_header 或 cookies 之一")
        return self


class ManualCaptchaRequest(BaseModel):
    """Manual captcha ticket storage payload."""

    model_config = ConfigDict(extra="ignore")

    ticket: str
    randstr: str
    ret: int | None = 0


class PreviewPaymentRequest(BaseModel):
    """Preview payment request payload."""

    model_config = ConfigDict(extra="ignore")

    product_id: str
    invitation_code: str | None = None
    ticket: str | None = None
    randstr: str | None = None


class PreviewSeedRequest(BaseModel):
    """Seed a preview result into local session state for debugging."""

    model_config = ConfigDict(extra="ignore")

    preview: dict[str, Any]


class AccountPreferencesRequest(BaseModel):
    """Editable account/session preferences for list management UI."""

    model_config = ConfigDict(extra="ignore")

    invitation_code: str | None = None
    selected_product_id: str | None = None
    preview_concurrency: int | None = None
    schedule_enabled: bool | None = None
    scheduled_start_time: str | None = None

    @field_validator("preview_concurrency")
    @classmethod
    def validate_preview_concurrency(cls, value: int | None) -> int | None:
        if value is None:
            return None
        normalized = int(value)
        if normalized < 1 or normalized > 4:
            raise ValueError("preview_concurrency 必须在 1 到 4 之间")
        return normalized

    @field_validator("scheduled_start_time")
    @classmethod
    def validate_scheduled_start_time(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        if not normalized:
            return ""
        parts = normalized.split(":")
        if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
            raise ValueError("scheduled_start_time 必须是 HH:MM 或 HH:MM:SS")
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("scheduled_start_time 必须是有效的 HH:MM 或 HH:MM:SS")
        if second < 0 or second > 59:
            raise ValueError("scheduled_start_time 秒数必须在 00 到 59 之间")
        return f"{hour:02d}:{minute:02d}:{second:02d}"


class CaptchaPoint(BaseModel):
    """OCR-resolved click point in original captcha image coordinates."""

    model_config = ConfigDict(extra="ignore")

    x: float
    y: float
    order: int | None = None
    label: str = ""


class CaptchaVerifyPayloadRequest(BaseModel):
    """Build Tencent captcha verify payload from click points."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    sess: str | None = None
    points: list[CaptchaPoint] = Field(default_factory=list)
    collect: str | None = None
    eks: str | None = None
    pow_answer: str | None = None
    pow_calc_time: int | None = None
    vdata: str | None = Field(default=None, alias="vData")


class CreateQrRequest(BaseModel):
    """Create payment QR request payload."""

    model_config = ConfigDict(extra="ignore")

    product_id: str
    pay_type: PayType | Literal["alipay", "wechat"] = "ALI"
    biz_id: str | None = None
    invitation_code: str | None = None

    @field_validator("pay_type", mode="before")
    @classmethod
    def normalize_pay_type(cls, value: Any) -> PayType:
        raw = str(value or "").strip().upper()
        if raw in {"ALI", "ALIPAY"}:
            return "ALI"
        if raw in {"WE_CHAT", "WECHAT", "WECHATPAY", "WECHAT_PAY"}:
            return "WE_CHAT"
        if raw == "WECHAT":
            return "WE_CHAT"
        raise ValueError("pay_type 只支持 ALI / WE_CHAT / alipay / wechat")


class PaymentCheckResult(BaseModel):
    """Current upstream payment status."""

    model_config = ConfigDict(extra="ignore")

    biz_id: str
    status: str
    raw: dict[str, Any] = Field(default_factory=dict)


class AccountDetailResponse(BaseModel):
    """Account detail payload used by the UI."""

    model_config = ConfigDict(extra="ignore")

    account: PublicAccountRecord
    session: AccountSessionState
    tasks: list[PaymentTaskRecord] = Field(default_factory=list)
