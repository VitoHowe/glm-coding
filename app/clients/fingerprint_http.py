"""HTTP client wrapper with browser-like TLS/HTTP impersonation."""

from __future__ import annotations

from typing import Any

import httpx

from app.browser_profiles import resolve_transport_impersonate
from app.config import Settings, get_settings
from app.errors import UpstreamRequestError

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - optional fallback
    curl_requests = None


class FingerprintHttpClient:
    """Send requests through curl-cffi when available, otherwise httpx."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def transport_name(self) -> str:
        if curl_requests is not None:
            return f"curl-cffi:{self.settings.browser_impersonate}"
        return "httpx"

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        json_body: Any | None = None,
        form_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        proxy_url: str | None = None,
        user_agent: str | None = None,
        browser_impersonate: str | None = None,
    ) -> dict[str, Any] | list[Any] | str | int | float | bool | None:
        merged_headers = self._build_headers(headers=headers, user_agent=user_agent)
        response = self._dispatch(
            method=method,
            url=url,
            headers=merged_headers,
            cookies=cookies or {},
            json_body=json_body,
            form_body=form_body,
            params=params,
            proxy_url=proxy_url,
            browser_impersonate=browser_impersonate,
        )
        return self._decode_response(response=response, url=url)

    def request_text(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        form_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        proxy_url: str | None = None,
        user_agent: str | None = None,
        browser_impersonate: str | None = None,
    ) -> str:
        merged_headers = self._build_headers(headers=headers, user_agent=user_agent)
        response = self._dispatch(
            method=method,
            url=url,
            headers=merged_headers,
            cookies=cookies or {},
            json_body=None,
            form_body=form_body,
            params=params,
            proxy_url=proxy_url,
            browser_impersonate=browser_impersonate,
        )
        self._ensure_success(response=response, url=url)
        return str(getattr(response, "text", ""))

    def request_bytes(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        form_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        proxy_url: str | None = None,
        user_agent: str | None = None,
        browser_impersonate: str | None = None,
    ) -> bytes:
        merged_headers = self._build_headers(headers=headers, user_agent=user_agent)
        response = self._dispatch(
            method=method,
            url=url,
            headers=merged_headers,
            cookies=cookies or {},
            json_body=None,
            form_body=form_body,
            params=params,
            proxy_url=proxy_url,
            browser_impersonate=browser_impersonate,
        )
        self._ensure_success(response=response, url=url)
        return bytes(getattr(response, "content", b""))

    def _build_headers(
        self,
        *,
        headers: dict[str, str] | None,
        user_agent: str | None,
    ) -> dict[str, str]:
        merged = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": self.settings.default_language,
            "Set-Language": self.settings.default_language,
            "Content-Type": "application/json;charset=utf-8",
            "Origin": self.settings.bigmodel_origin,
            "Referer": self.settings.bigmodel_referer,
        }
        if user_agent:
            merged["User-Agent"] = user_agent
        if headers:
            merged.update({key: value for key, value in headers.items() if value})
        return merged

    def _dispatch(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        cookies: dict[str, str],
        json_body: Any | None,
        form_body: dict[str, Any] | None,
        params: dict[str, Any] | None,
        proxy_url: str | None,
        browser_impersonate: str | None,
    ):
        if json_body is not None and form_body is not None:
            raise UpstreamRequestError(
                "HTTP 请求体配置冲突",
                details={"url": url, "reason": "json_body and form_body are mutually exclusive"},
            )
        if curl_requests is not None:
            session = curl_requests.Session(trust_env=False)
            try:
                kwargs: dict[str, Any] = {
                    "method": method.upper(),
                    "url": url,
                    "headers": headers,
                    "cookies": cookies,
                    "params": params,
                    "timeout": self.settings.request_timeout_seconds,
                    "allow_redirects": True,
                    "impersonate": resolve_transport_impersonate(
                        browser_impersonate or self.settings.browser_impersonate,
                    ),
                }
                if json_body is not None:
                    kwargs["json"] = json_body
                if form_body is not None:
                    kwargs["data"] = form_body
                if proxy_url:
                    kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
                else:
                    kwargs["proxies"] = {"all": ""}
                return session.request(**kwargs)
            except Exception as exc:
                raise UpstreamRequestError(
                    "上游请求失败",
                    details={"transport": self.transport_name, "url": url, "reason": str(exc)},
                ) from exc
            finally:
                session.close()

        try:
            kwargs = {
                "method": method.upper(),
                "url": url,
                "headers": headers,
                "cookies": cookies,
                "params": params,
            }
            if json_body is not None:
                kwargs["json"] = json_body
            if form_body is not None:
                kwargs["data"] = form_body
            client_kwargs: dict[str, Any] = {
                "timeout": self.settings.request_timeout_seconds,
                "follow_redirects": True,
                "trust_env": False,
            }
            if proxy_url:
                client_kwargs["proxy"] = proxy_url
            with httpx.Client(**client_kwargs) as client:
                return client.request(**kwargs)
        except Exception as exc:
            raise UpstreamRequestError(
                "上游请求失败",
                details={"transport": self.transport_name, "url": url, "reason": str(exc)},
            ) from exc

    def _decode_response(self, *, response: Any, url: str):
        self._ensure_success(response=response, url=url)
        try:
            payload = response.json()
        except Exception as exc:
            raise UpstreamRequestError(
                "上游返回了非 JSON 数据",
                details={
                    "transport": self.transport_name,
                    "url": url,
                    "status_code": getattr(response, "status_code", "unknown"),
                    "body_preview": getattr(response, "text", "")[:500],
                },
            ) from exc

        return payload

    def _ensure_success(self, *, response: Any, url: str) -> None:
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code < 400:
            return
        body_preview = str(getattr(response, "text", ""))[:500]
        raise UpstreamRequestError(
            "上游接口返回错误",
            details={"url": url, "status_code": status_code, "body_preview": body_preview},
        )

    def _extract_message(self, payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("message", "msg", "error", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if isinstance(payload, str):
            return payload
        return ""
