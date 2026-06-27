"""Tencent captcha prehandle and image download client."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urljoin

from app.browser_profiles import resolve_user_agent
from app.clients.fingerprint_http import FingerprintHttpClient
from app.config import Settings, get_settings
from app.errors import UpstreamRequestError
from app.models import AccountRecord
from app.services.network_mode_service import get_network_mode_service

@dataclass(frozen=True)
class CaptchaChallenge:
    """Normalized Tencent captcha challenge bundle."""

    callback: str
    sess: str
    sid: str
    instruction: str
    image_url: str
    image_path: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class CaptchaVerifyResult:
    """Normalized Tencent captcha verify response bundle."""

    ticket: str
    randstr: str
    ret: Any
    error_code: str
    error_message: str
    sess: str
    raw: dict[str, Any]


class TencentCaptchaClient:
    """Fetch Tencent captcha challenge metadata and image bytes."""

    def __init__(
        self,
        http_client: FingerprintHttpClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.http_client = http_client or FingerprintHttpClient(self.settings)

    def prehandle(self, account: AccountRecord) -> CaptchaChallenge:
        callback = f"_aq_{int(time.time() * 1000)}"
        user_agent = resolve_user_agent(account.user_agent, account.browser_impersonate)
        allow_dynamic_proxy_channel = self._allow_dynamic_proxy_channel()
        response_text = self.http_client.request_text(
            "GET",
            f"{self.settings.tencent_captcha_domain}/cap_union_prehandle",
            headers={
                "Accept": "*/*",
                "Referer": self.settings.tencent_captcha_entry_url,
            },
            sec_fetch_site="cross-site",
            params={
                "aid": self.settings.tencent_captcha_aid,
                "protocol": "https",
                "accver": "1",
                "showtype": "popup",
                "ua": base64.b64encode(user_agent.encode("utf-8")).decode("ascii"),
                "noheader": "0",
                "fb": "1",
                "aged": "0",
                "enableAged": "0",
                "enableDarkMode": "0",
                "grayscale": "1",
                "clientype": "2",
                "lang": "zh-cn",
                "entry_url": self.settings.tencent_captcha_entry_url,
                "elder_captcha": "0",
                "js": "",
                "login_appid": "",
                "wb": "1",
                "subsid": "1",
                "callback": callback,
                "sess": "",
            },
            proxy_url=account.proxy_url or None,
            allow_fallback_proxy=allow_dynamic_proxy_channel,
            user_agent=user_agent,
            browser_impersonate=account.browser_impersonate or None,
        )
        payload = self._parse_jsonp(response_text)
        image_path = str(
            (((payload.get("data") or {}).get("dyn_show_info") or {}).get("bg_elem_cfg") or {}).get("img_url")
            or ""
        )
        instruction = str(((payload.get("data") or {}).get("dyn_show_info") or {}).get("instruction") or "")
        sess = str(payload.get("sess") or "")
        sid = str(payload.get("sid") or "")
        if not image_path:
            raise UpstreamRequestError(
                "prehandle 没给验证码图片地址，八成是参数不对或者策略升级了。",
                details={"payload": payload},
            )
        return CaptchaChallenge(
            callback=callback,
            sess=sess,
            sid=sid,
            instruction=instruction,
            image_path=image_path,
            image_url=urljoin(f"{self.settings.tencent_captcha_domain}/", image_path.lstrip("/")),
            raw=payload,
        )

    def fetch_image_bytes(self, account: AccountRecord, challenge: CaptchaChallenge) -> bytes:
        user_agent = resolve_user_agent(account.user_agent, account.browser_impersonate)
        allow_dynamic_proxy_channel = self._allow_dynamic_proxy_channel()
        return self.http_client.request_bytes(
            "GET",
            challenge.image_url,
            headers={
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer": self.settings.tencent_captcha_entry_url,
            },
            proxy_url=account.proxy_url or None,
            allow_fallback_proxy=allow_dynamic_proxy_channel,
            user_agent=user_agent,
            browser_impersonate=account.browser_impersonate or None,
            sec_fetch_site="cross-site",
        )

    def verify(self, account: AccountRecord, payload: dict[str, Any]) -> CaptchaVerifyResult:
        user_agent = resolve_user_agent(account.user_agent, account.browser_impersonate)
        allow_dynamic_proxy_channel = self._allow_dynamic_proxy_channel()
        form_body = {
            str(key): str(value)
            for key, value in payload.items()
            if value is not None and str(value) != ""
        }
        response = self.http_client.request_json(
            "POST",
            f"{self.settings.tencent_captcha_domain}/cap_union_new_verify",
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": self.settings.tencent_captcha_domain,
                "Referer": self.settings.tencent_captcha_entry_url,
                "X-Requested-With": "XMLHttpRequest",
            },
            form_body=form_body,
            proxy_url=account.proxy_url or None,
            allow_fallback_proxy=allow_dynamic_proxy_channel,
            user_agent=user_agent,
            browser_impersonate=account.browser_impersonate or None,
            sec_fetch_site="cross-site",
        )
        if not isinstance(response, dict):
            raise UpstreamRequestError(
                "verify 返回结构异常",
                details={"payload_type": type(response).__name__, "payload": response},
            )
        return CaptchaVerifyResult(
            ticket=str(response.get("ticket") or ""),
            randstr=str(response.get("randstr") or ""),
            ret=response.get("ret"),
            error_code=str(response.get("errorCode") or response.get("errCode") or ""),
            error_message=str(response.get("errorMessage") or response.get("msg") or ""),
            sess=str(response.get("sess") or ""),
            raw=response,
        )

    def _parse_jsonp(self, raw_text: str) -> dict[str, Any]:
        content = raw_text.strip()
        if not content:
            raise UpstreamRequestError("prehandle 返回空内容")
        start = content.find("(")
        end = content.rfind(")")
        if start < 0 or end <= start:
            raise UpstreamRequestError(
                "prehandle 返回的不是合法 JSONP",
                details={"body_preview": content[:300]},
            )
        json_text = content[start + 1 : end].strip()
        try:
            payload = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise UpstreamRequestError(
                "prehandle JSONP 解析失败",
                details={"body_preview": content[:300]},
            ) from exc
        if not isinstance(payload, dict):
            raise UpstreamRequestError(
                "prehandle 返回结构异常",
                details={"payload_type": type(payload).__name__},
            )
        return payload

    def _allow_dynamic_proxy_channel(self) -> bool:
        return get_network_mode_service().get_mode() == "dynamic_proxy"


@lru_cache(maxsize=1)
def get_tencent_captcha_client() -> TencentCaptchaClient:
    """Get the shared Tencent captcha client."""
    return TencentCaptchaClient()
