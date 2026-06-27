"""Runtime network egress mode selection."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.config import get_settings
from app.errors import BadRequestError
from app.models import NetworkEgressMode
from app.proxy_pool.service import get_builtin_proxy_pool_service
from app.storage.json_store import JsonFileStore

VALID_NETWORK_MODES: set[str] = {"local", "proxy_pool"}


class NetworkModeService:
    """Persist and validate the active outbound network mode."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.store = JsonFileStore(
            self.settings.data_dir / "network_mode.json",
            default_factory=lambda: {"mode": self.settings.network_egress_mode},
        )

    def get_mode(self) -> NetworkEgressMode:
        payload = self.store.read()
        mode = str((payload or {}).get("mode") or self.settings.network_egress_mode).strip()
        if mode not in VALID_NETWORK_MODES:
            return "local"
        return mode  # type: ignore[return-value]

    def set_mode(self, mode: NetworkEgressMode) -> dict[str, Any]:
        normalized = str(mode).strip()
        if normalized not in VALID_NETWORK_MODES:
            raise BadRequestError("网络出口模式不支持", details={"mode": mode})
        self._apply_mode(normalized)
        self.store.write({"mode": normalized})
        return self.status_payload()

    def _apply_mode(self, mode: str) -> None:
        """启停内置代理池服务；连通性检验由代理池后台异步完成，不阻塞切换。"""
        service = get_builtin_proxy_pool_service()
        if mode == "proxy_pool":
            url = self.settings.fallback_proxy_url.strip()
            if not url:
                raise BadRequestError("代理池模式缺少 FALLBACK_PROXY_URL", details={"mode": mode})
            if not service.is_started:
                service.start()
        elif service.is_started:
            service.stop()

    def status_payload(self, *, mode: str | None = None) -> dict[str, Any]:
        active_mode = str(mode or self.get_mode()).strip()
        modes = {
            "local": {
                "available": True,
                "message": "本地出口模式已启用",
                "label": "本地",
            },
            "proxy_pool": {
                "available": bool(self.settings.fallback_proxy_url.strip()),
                "message": (
                    f"代理池模式：{self.settings.fallback_proxy_url}"
                    if self.settings.fallback_proxy_url.strip()
                    else "代理池模式缺少 FALLBACK_PROXY_URL"
                ),
                "label": "代理池",
                "url": self.settings.fallback_proxy_url,
            },
        }
        current = modes.get(active_mode, modes["local"])
        if active_mode == "proxy_pool":
            # 顶层 available 反映代理池服务真实状态（含后台连通性检验结果）
            pool_status = get_builtin_proxy_pool_service().status_payload()
            available = bool(pool_status.get("available"))
            message = str(pool_status.get("message") or current.get("message") or "")
        else:
            available = bool(current.get("available"))
            message = current.get("message", "")
        return {
            "mode": active_mode,
            "available": available,
            "message": message,
            "label": current.get("label", active_mode),
            "ticket_pool_only": self.settings.fallback_proxy_ticket_pool_only,
            "modes": modes,
        }


@lru_cache(maxsize=1)
def get_network_mode_service() -> NetworkModeService:
    """Return the process-wide network mode service."""
    return NetworkModeService()
