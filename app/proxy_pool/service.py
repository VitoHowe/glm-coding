"""Built-in proxy pool service.

The AegisFlow app uses a local HTTP proxy URL as its egress abstraction.  This
module provides that local proxy directly in Python so the project no longer
needs an external proxy binary.
"""

from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import ipaddress
import logging
import re
import select
import socket
import ssl
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import PROJECT_ROOT, get_settings

try:
    import yaml
except ImportError:  # pragma: no cover - dependency guard for clearer startup logs
    yaml = None

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "proxy_pool.yaml"
LOCAL_PROXY_HOSTS = {"127.0.0.1", "localhost", "::1"}
HTTP_PROXY_SCHEMES = {"http", "https"}
SOCKS_PROXY_SCHEMES = {"socks5", "socks5h"}
IP_PORT_PATTERN = re.compile(r"([0-9]{1,3}(?:\.[0-9]{1,3}){3}):([0-9]{1,5})")


@dataclass(frozen=True)
class HealthCheckConfig:
    total_timeout_seconds: float = 8
    tls_handshake_threshold_seconds: float = 5
    target: str = "www.bigmodel.cn:443"


@dataclass(frozen=True)
class ProxyPoolPorts:
    socks5_strict: str = ":17283"
    socks5_relaxed: str = ":17284"
    http_strict: str = ":17285"
    http_relaxed: str = ":17286"


@dataclass(frozen=True)
class ProxyPoolConfig:
    source_base_dir: Path = PROJECT_ROOT
    proxy_list_urls: list[str] = field(default_factory=lambda: ["good_proxies.txt"])
    special_proxy_list_urls: list[str] = field(default_factory=list)
    health_check_concurrency: int = 200
    update_interval_minutes: int = 5
    health_check: HealthCheckConfig = field(default_factory=HealthCheckConfig)
    ports: ProxyPoolPorts = field(default_factory=ProxyPoolPorts)


@dataclass(frozen=True)
class RuntimeProxyConfig:
    whiteip_enabled: bool = False
    whiteip_secret_id: str = ""
    whiteip_secret_key: str = ""
    whiteip_secret_token_api: str = ""
    whiteip_sign_type: str = "token"
    whiteip_signature: str = ""
    whiteip_api: str = ""
    whiteip_list: str = ""
    whiteip_wait_seconds: int = 5
    max_latency_ms: int = 3000
    failure_cooldown_seconds: int = 60


@dataclass(frozen=True)
class UpstreamProxy:
    raw: str
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def address(self) -> str:
        auth = ""
        if self.username:
            auth = f"{self.username}:***@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"


@dataclass(frozen=True)
class ProxyHealth:
    proxy: UpstreamProxy
    latency_seconds: float


class ProxyPool:
    """Thread-safe latency-sorted proxy pool with failure cooldown."""

    def __init__(self, *, name: str, cooldown_seconds: int) -> None:
        self.name = name
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self._lock = threading.RLock()
        self._proxies: list[UpstreamProxy] = []
        self._cooldown_until: dict[str, float] = {}
        self._index = 0

    def update(self, proxies: list[UpstreamProxy]) -> None:
        now = time.time()
        with self._lock:
            old_count = len(self._proxies)
            self._proxies = list(proxies)
            valid_keys = {proxy.raw for proxy in self._proxies}
            self._cooldown_until = {
                key: until
                for key, until in self._cooldown_until.items()
                if key in valid_keys and until > now
            }
            self._index = 0
            logger.info(
                "[%s] Proxy pool updated: %d -> %d active proxies",
                self.name,
                old_count,
                len(self._proxies),
            )

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._proxies)

    def snapshot(self) -> list[str]:
        with self._lock:
            return [proxy.address for proxy in self._proxies]

    def mark_failure(self, proxy: UpstreamProxy | None) -> None:
        if proxy is None or self.cooldown_seconds <= 0:
            return
        until = time.time() + self.cooldown_seconds
        with self._lock:
            self._cooldown_until[proxy.raw] = until
        logger.warning(
            "[%s] Upstream proxy %s cooled down for %ds",
            self.name,
            proxy.address,
            self.cooldown_seconds,
        )

    def get_next(self) -> UpstreamProxy:
        with self._lock:
            if not self._proxies:
                raise RuntimeError("no available proxies")

            now = time.time()
            # 全池按延迟顺序轮询，跳过冷却中的代理
            for _ in range(len(self._proxies)):
                proxy = self._proxies[self._index % len(self._proxies)]
                self._index += 1
                until = self._cooldown_until.get(proxy.raw)
                if until is None or until <= now:
                    self._cooldown_until.pop(proxy.raw, None)
                    return proxy

            # 全池都在冷却，返回轮询到的下一个（即使它正在冷却）
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
            return proxy


class ManagedSocket:
    """Small context manager that closes sockets quietly."""

    def __init__(self, sock: socket.socket | None = None) -> None:
        self.sock = sock

    def __enter__(self) -> socket.socket:
        if self.sock is None:
            raise RuntimeError("socket is not initialized")
        return self.sock

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        close_socket(self.sock)


class ThreadedTcpServer:
    """Tiny stoppable TCP server used for local HTTP/SOCKS proxy ports."""

    def __init__(
        self,
        *,
        name: str,
        address: tuple[str, int],
        handler: "ProxyConnectionHandler",
    ) -> None:
        self.name = name
        self.address = address
        self.handler = handler
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._server_socket: socket.socket | None = None
        self._startup_error: Exception | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._thread = threading.Thread(target=self._serve, name=f"proxy-pool-{self.name}", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=3):
            raise RuntimeError(f"{self.name} did not start in time")
        if self._startup_error is not None:
            raise RuntimeError(f"{self.name} failed to start") from self._startup_error

    def stop(self) -> None:
        self._stop.set()
        close_socket(self._server_socket)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _serve(self) -> None:
        host, port = self.address
        try:
            server = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if server.family == socket.AF_INET6:
                server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            server.bind((host, port))
            server.listen(256)
            server.settimeout(0.5)
            self._server_socket = server
            logger.info("[%s] Listening on %s:%d", self.name, host, port)
            self._ready.set()
        except Exception:
            self._startup_error = sys.exc_info()[1]
            logger.exception("[%s] Failed to start on %s:%d", self.name, host, port)
            self._ready.set()
            return

        with ManagedSocket(self._server_socket):
            while not self._stop.is_set():
                try:
                    client, addr = self._server_socket.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                thread = threading.Thread(
                    target=self.handler.handle,
                    args=(client, addr),
                    name=f"proxy-pool-{self.name}-client",
                    daemon=True,
                )
                thread.start()


class ProxyConnectionHandler:
    """Handle one accepted local proxy client connection."""

    def __init__(
        self,
        *,
        mode: str,
        protocol: str,
        pool: ProxyPool,
        timeout_seconds: float,
    ) -> None:
        self.mode = mode
        self.protocol = protocol
        self.pool = pool
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def handle(self, client: socket.socket, client_addr: Any) -> None:
        client.settimeout(self.timeout_seconds)
        with ManagedSocket(client):
            try:
                if self.protocol == "http":
                    self._handle_http(client, client_addr)
                else:
                    self._handle_socks5(client, client_addr)
            except Exception as exc:
                logger.warning("[%s-%s] Client %s failed: %s", self.protocol.upper(), self.mode, client_addr, exc)

    def _handle_http(self, client: socket.socket, client_addr: Any) -> None:
        header, leftover = read_http_header(client)
        if not header:
            return
        request_line, headers = parse_http_request(header)
        method, target, version = request_line

        if method.upper() == "CONNECT":
            host, port = parse_host_port(target, default_port=443)
            self._handle_connect(client, host, port, version)
            return

        host, port, path = parse_plain_http_target(target, headers)
        try:
            upstream_proxy = self.pool.get_next()
        except Exception:
            with contextlib.suppress(OSError):
                client.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n")
            raise
        upstream: socket.socket | None = None
        try:
            upstream = dial_via_upstream(
                upstream_proxy,
                host,
                port,
                timeout_seconds=self.timeout_seconds,
            )
            rewritten = rewrite_http_request(header, method, path, version)
            upstream.sendall(rewritten + leftover)
            logger.info(
                "[HTTP-%s] %s %s via %s for %s",
                self.mode,
                method,
                target,
                upstream_proxy.address,
                client_addr,
            )
            pump_bidirectional(client, upstream)
        except Exception:
            self.pool.mark_failure(upstream_proxy)
            with contextlib.suppress(OSError):
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            raise
        finally:
            close_socket(upstream)

    def _handle_connect(self, client: socket.socket, host: str, port: int, version: str) -> None:
        try:
            upstream_proxy = self.pool.get_next()
        except Exception:
            with contextlib.suppress(OSError):
                client.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n")
            raise
        upstream: socket.socket | None = None
        try:
            upstream = dial_via_upstream(
                upstream_proxy,
                host,
                port,
                timeout_seconds=self.timeout_seconds,
            )
            client.sendall(f"{version} 200 Connection Established\r\n\r\n".encode("ascii"))
            logger.info(
                "[HTTPS-%s] Tunnel established to %s:%d via %s",
                self.mode,
                host,
                port,
                upstream_proxy.address,
            )
            pump_bidirectional(client, upstream)
        except Exception:
            self.pool.mark_failure(upstream_proxy)
            with contextlib.suppress(OSError):
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            raise
        finally:
            close_socket(upstream)

    def _handle_socks5(self, client: socket.socket, client_addr: Any) -> None:
        target_host, target_port = read_socks5_connect_request(client)
        try:
            upstream_proxy = self.pool.get_next()
        except Exception:
            with contextlib.suppress(OSError):
                client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            raise
        upstream: socket.socket | None = None
        try:
            upstream = dial_via_upstream(
                upstream_proxy,
                target_host,
                target_port,
                timeout_seconds=self.timeout_seconds,
            )
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            logger.info(
                "[SOCKS5-%s] Tunnel established to %s:%d via %s for %s",
                self.mode,
                target_host,
                target_port,
                upstream_proxy.address,
                client_addr,
            )
            pump_bidirectional(client, upstream)
        except Exception:
            self.pool.mark_failure(upstream_proxy)
            with contextlib.suppress(OSError):
                client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            raise
        finally:
            close_socket(upstream)


class BuiltinProxyPoolService:
    """Lifecycle owner for the built-in proxy pool."""

    def __init__(self, *, config_path: Path | None = None) -> None:
        self.config_path = config_path or resolve_config_path()
        self.settings = get_settings()
        self.runtime = load_runtime_proxy_config()
        self.config = load_proxy_pool_config(self.config_path)
        self.strict_pool = ProxyPool(name="STRICT", cooldown_seconds=self.runtime.failure_cooldown_seconds)
        self.relaxed_pool = ProxyPool(name="RELAXED", cooldown_seconds=self.runtime.failure_cooldown_seconds)
        self._servers: list[ThreadedTcpServer] = []
        self._stop = threading.Event()
        self._refresh_lock = threading.Lock()
        self._started = False
        self._last_refresh_at: float | None = None
        self._last_refresh_error = ""

    @property
    def is_started(self) -> bool:
        return self._started

    def start(self) -> None:
        if self._started:
            return
        self._stop.clear()
        self.runtime = load_runtime_proxy_config()
        self._add_proxy_whiteip()
        timeout = self.config.health_check.total_timeout_seconds + 2
        self._servers = [
            self._make_server("socks5-strict", self.config.ports.socks5_strict, "socks5", "STRICT", self.strict_pool, timeout),
            self._make_server("socks5-relaxed", self.config.ports.socks5_relaxed, "socks5", "RELAXED", self.relaxed_pool, timeout),
            self._make_server("http-strict", self.config.ports.http_strict, "http", "STRICT", self.strict_pool, timeout),
            self._make_server("http-relaxed", self.config.ports.http_relaxed, "http", "RELAXED", self.relaxed_pool, timeout),
        ]
        try:
            for server in self._servers:
                server.start()
        except Exception:
            for server in self._servers:
                server.stop()
            raise
        self._started = True
        threading.Thread(target=self.refresh_once, name="proxy-pool-refresh-initial", daemon=True).start()
        threading.Thread(target=self._refresh_loop, name="proxy-pool-refresh-loop", daemon=True).start()
        logger.info(
            "Built-in proxy pool started from %s; HTTP strict=%s relaxed=%s",
            self.config_path,
            self.config.ports.http_strict,
            self.config.ports.http_relaxed,
        )

    def stop(self) -> None:
        self._stop.set()
        for server in self._servers:
            server.stop()
        self._started = False

    def refresh_once(self) -> None:
        if not self._refresh_lock.acquire(blocking=False):
            logger.info("Proxy refresh already running; skip")
            return
        try:
            logger.info("Refreshing proxy pool from %d regular sources and %d special sources",
                        len(self.config.proxy_list_urls), len(self.config.special_proxy_list_urls))
            proxies = fetch_proxy_sources(self.config)
            if not proxies:
                raise RuntimeError("no proxies fetched from any source")
            result = health_check_proxies(proxies, self.config, self.runtime)
            if result["strict"]:
                self.strict_pool.update([item.proxy for item in result["strict"]])
            else:
                logger.warning("[STRICT] No healthy proxies found; keeping existing pool")
            if result["relaxed"]:
                self.relaxed_pool.update([item.proxy for item in result["relaxed"]])
            else:
                logger.warning("[RELAXED] No healthy proxies found; keeping existing pool")
            self._last_refresh_at = time.time()
            self._last_refresh_error = ""
        except Exception as exc:
            self._last_refresh_error = str(exc)
            logger.exception("Proxy pool refresh failed: %s", exc)
        finally:
            self._refresh_lock.release()

    def status_payload(self, *, port: int | None = None) -> dict[str, Any]:
        mode = self._mode_for_port(port)
        pool = self.relaxed_pool if mode == "relaxed" else self.strict_pool
        count = pool.count
        return {
            "enabled": True,
            "available": self._started and count > 0,
            "service": "python",
            "mode": mode,
            "proxies": count,
            "strict_proxies": self.strict_pool.count,
            "relaxed_proxies": self.relaxed_pool.count,
            "config_path": str(self.config_path),
            "last_refresh_at": self._last_refresh_at,
            "last_refresh_error": self._last_refresh_error,
            "message": (
                f"Python 代理池可用，{mode} 池 {count} 个代理"
                if self._started and count > 0
                else self._status_message(mode)
            ),
        }

    def _status_message(self, mode: str) -> str:
        if not self._started:
            return "Python 代理池服务未启动"
        if self._last_refresh_error:
            return f"Python 代理池暂无可用代理：{self._last_refresh_error}"
        return f"Python 代理池已启动，{mode} 池正在后台检验连通性"

    def _mode_for_port(self, port: int | None) -> str:
        if port is None:
            return "relaxed"
        mapping = {
            parse_listen_address(self.config.ports.http_strict)[1]: "strict",
            parse_listen_address(self.config.ports.socks5_strict)[1]: "strict",
            parse_listen_address(self.config.ports.http_relaxed)[1]: "relaxed",
            parse_listen_address(self.config.ports.socks5_relaxed)[1]: "relaxed",
        }
        return mapping.get(port, "relaxed")

    def _make_server(
        self,
        name: str,
        listen: str,
        protocol: str,
        mode: str,
        pool: ProxyPool,
        timeout: float,
    ) -> ThreadedTcpServer:
        handler = ProxyConnectionHandler(
            mode=mode,
            protocol=protocol,
            pool=pool,
            timeout_seconds=timeout,
        )
        return ThreadedTcpServer(name=name, address=parse_listen_address(listen), handler=handler)

    def _refresh_loop(self) -> None:
        interval = max(1, int(self.config.update_interval_minutes)) * 60
        while not self._stop.wait(interval):
            threading.Thread(target=self.refresh_once, name="proxy-pool-refresh", daemon=True).start()

    def _add_proxy_whiteip(self) -> None:
        if not self.runtime.whiteip_enabled:
            return
        if not self.runtime.whiteip_secret_id:
            logger.warning("[ProxyWhiteIP] PROXY_WHITEIP_SECRET_ID is empty; skip")
            return
        if not self.runtime.whiteip_api:
            logger.warning("[ProxyWhiteIP] PROXY_WHITEIP_API is empty; skip")
            return
        signature = self.runtime.whiteip_signature
        if self.runtime.whiteip_secret_key:
            signature = self._fetch_proxy_whiteip_secret_token() or signature
        if not signature:
            logger.warning("[ProxyWhiteIP] No usable signature; skip")
            return
        params = {
            "secret_id": self.runtime.whiteip_secret_id,
            "signature": signature,
            "sign_type": self.runtime.whiteip_sign_type,
        }
        if self.runtime.whiteip_list:
            params["iplist"] = self.runtime.whiteip_list
        try:
            with httpx.Client(timeout=15, verify=False, trust_env=False) as client:
                response = client.get(self.runtime.whiteip_api, params=params)
            logger.info("[ProxyWhiteIP] WhiteIP status=%s response=%s", response.status_code, response.text[:500])
            if self.runtime.whiteip_wait_seconds > 0:
                time.sleep(self.runtime.whiteip_wait_seconds)
        except Exception as exc:
            logger.warning("[ProxyWhiteIP] WhiteIP API failed: %s", exc)

    def _fetch_proxy_whiteip_secret_token(self) -> str:
        if not self.runtime.whiteip_secret_token_api:
            logger.warning("[ProxyWhiteIP] PROXY_WHITEIP_SECRET_TOKEN_API is empty; skip token fetch")
            return ""
        data = {
            "secret_id": self.runtime.whiteip_secret_id,
            "secret_key": self.runtime.whiteip_secret_key,
        }
        try:
            with httpx.Client(timeout=15, verify=False, trust_env=False) as client:
                response = client.post(self.runtime.whiteip_secret_token_api, data=data)
            payload = response.json()
            token = str(((payload.get("data") or {}).get("secret_token") or "")).strip()
            if token:
                logger.info("[ProxyWhiteIP] secret_token fetched")
            return token
        except Exception as exc:
            logger.warning("[ProxyWhiteIP] secret_token fetch failed: %s", exc)
            return ""


def resolve_config_path() -> Path:
    configured = str(getattr(get_settings(), "proxy_pool_config_path", "") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path
    return DEFAULT_CONFIG_PATH


def load_proxy_pool_config(path: Path) -> ProxyPoolConfig:
    if yaml is None:
        raise RuntimeError("PyYAML is required for proxy_pool.yaml; run pip install -r requirements.txt")
    if not path.exists():
        raise FileNotFoundError(f"proxy pool config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    health_raw = raw.get("health_check") or {}
    ports_raw = raw.get("ports") or {}
    config = ProxyPoolConfig(
        source_base_dir=path.parent,
        proxy_list_urls=normalize_string_list(raw.get("proxy_list_urls")) or ["good_proxies.txt"],
        special_proxy_list_urls=normalize_string_list(raw.get("special_proxy_list_urls")),
        health_check_concurrency=max(1, int(raw.get("health_check_concurrency") or 200)),
        update_interval_minutes=max(1, int(raw.get("update_interval_minutes") or 5)),
        health_check=HealthCheckConfig(
            total_timeout_seconds=max(1, float(health_raw.get("total_timeout_seconds") or 8)),
            tls_handshake_threshold_seconds=max(
                1,
                float(health_raw.get("tls_handshake_threshold_seconds") or 5),
            ),
            target=str(health_raw.get("target") or "www.bigmodel.cn:443").strip(),
        ),
        ports=ProxyPoolPorts(
            socks5_strict=str(ports_raw.get("socks5_strict") or ":17283").strip(),
            socks5_relaxed=str(ports_raw.get("socks5_relaxed") or ":17284").strip(),
            http_strict=str(ports_raw.get("http_strict") or ":17285").strip(),
            http_relaxed=str(ports_raw.get("http_relaxed") or ":17286").strip(),
        ),
    )
    if not config.proxy_list_urls and not config.special_proxy_list_urls:
        raise ValueError("proxy_pool.yaml must define proxy_list_urls or special_proxy_list_urls")
    return config


def load_runtime_proxy_config() -> RuntimeProxyConfig:
    return RuntimeProxyConfig(
        whiteip_enabled=getenv_bool("PROXY_WHITEIP_ENABLED", False),
        whiteip_secret_id=getenv("PROXY_WHITEIP_SECRET_ID"),
        whiteip_secret_key=getenv("PROXY_WHITEIP_SECRET_KEY"),
        whiteip_secret_token_api=getenv("PROXY_WHITEIP_SECRET_TOKEN_API"),
        whiteip_sign_type=getenv("PROXY_WHITEIP_SIGN_TYPE", "token"),
        whiteip_signature=getenv("PROXY_WHITEIP_SIGNATURE"),
        whiteip_api=getenv("PROXY_WHITEIP_API"),
        whiteip_list=getenv("PROXY_WHITEIP_LIST"),
        whiteip_wait_seconds=getenv_int("PROXY_WHITEIP_WAIT_SECONDS", 5),
        max_latency_ms=getenv_int("PROXY_POOL_MAX_LATENCY_MS", 3000),
        failure_cooldown_seconds=getenv_int("PROXY_POOL_FAILURE_COOLDOWN_SECONDS", 60),
    )


def getenv(key: str, default: str = "") -> str:
    import os

    return os.getenv(key, default).strip()


def getenv_bool(key: str, default: bool) -> bool:
    raw = getenv(key)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def getenv_int(key: str, default: int) -> int:
    raw = getenv(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", key, raw, default)
        return default


def normalize_string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def fetch_proxy_sources(config: ProxyPoolConfig) -> list[UpstreamProxy]:
    proxies: dict[str, UpstreamProxy] = {}
    for source in config.proxy_list_urls:
        try:
            content = read_proxy_source(source, base_dir=config.source_base_dir)
        except Exception as exc:
            logger.warning("Failed to read proxy source %s: %s", source, exc)
            continue
        for line in content.splitlines():
            proxy = parse_upstream_proxy(line)
            if proxy:
                proxies[proxy.raw] = proxy
    for source in config.special_proxy_list_urls:
        try:
            content = read_proxy_source(source, base_dir=config.source_base_dir)
        except Exception as exc:
            logger.warning("Failed to read special proxy source %s: %s", source, exc)
            continue
        for match in IP_PORT_PATTERN.finditer(content):
            proxy = parse_upstream_proxy(f"{match.group(1)}:{match.group(2)}")
            if proxy:
                proxies[proxy.raw] = proxy
    logger.info("Fetched %d unique upstream proxies", len(proxies))
    return list(proxies.values())


def read_proxy_source(source: str, *, base_dir: Path = PROJECT_ROOT) -> str:
    source = source.strip()
    if source.startswith(("http://", "https://")):
        with httpx.Client(timeout=30, verify=False, trust_env=False) as client:
            response = client.get(source)
            response.raise_for_status()
            return response.text
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.read_text(encoding="utf-8")


def parse_upstream_proxy(line: str) -> UpstreamProxy | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    text = text.split("#", 1)[0].strip()
    if not text:
        return None
    has_explicit_scheme = "://" in text
    if "://" not in text:
        text = f"socks5://{text}"
    parsed = urlparse(text)
    scheme = (parsed.scheme or "socks5").lower()
    if scheme not in SOCKS_PROXY_SCHEMES | HTTP_PROXY_SCHEMES:
        if has_explicit_scheme:
            return None
        scheme = "socks5"
    host = parsed.hostname or ""
    port = parsed.port or 0
    if not host or port <= 0 or port > 65535:
        match = IP_PORT_PATTERN.search(text)
        if not match:
            return None
        host = match.group(1)
        port = int(match.group(2))
        scheme = "socks5"
    return UpstreamProxy(
        raw=f"{scheme}://{parsed.username or ''}:{parsed.password or ''}@{host}:{port}"
        if parsed.username
        else f"{scheme}://{host}:{port}",
        scheme=scheme,
        host=host,
        port=port,
        username=parsed.username or "",
        password=parsed.password or "",
    )


def health_check_proxies(
    proxies: list[UpstreamProxy],
    config: ProxyPoolConfig,
    runtime: RuntimeProxyConfig,
) -> dict[str, list[ProxyHealth]]:
    strict: list[ProxyHealth] = []
    relaxed: list[ProxyHealth] = []
    lock = threading.Lock()

    def check(proxy: UpstreamProxy) -> None:
        strict_result = check_proxy_health(proxy, config, strict_mode=True)
        if strict_result:
            with lock:
                strict.append(strict_result)
                relaxed.append(strict_result)
            return
        relaxed_result = check_proxy_health(proxy, config, strict_mode=False)
        if relaxed_result:
            with lock:
                relaxed.append(relaxed_result)

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.health_check_concurrency) as executor:
        futures = [executor.submit(check, proxy) for proxy in proxies]
        for _ in concurrent.futures.as_completed(futures):
            pass

    strict = filter_and_sort_health(strict, runtime.max_latency_ms)
    relaxed = filter_and_sort_health(relaxed, runtime.max_latency_ms)
    log_latency_summary("STRICT", strict)
    log_latency_summary("RELAXED", relaxed)
    return {"strict": strict, "relaxed": relaxed}


def check_proxy_health(
    proxy: UpstreamProxy,
    config: ProxyPoolConfig,
    *,
    strict_mode: bool,
) -> ProxyHealth | None:
    target_host, target_port = parse_host_port(config.health_check.target, default_port=443)
    started = time.perf_counter()
    sock: socket.socket | None = None
    tls_sock: ssl.SSLSocket | None = None
    try:
        sock = dial_via_upstream(
            proxy,
            target_host,
            target_port,
            timeout_seconds=config.health_check.total_timeout_seconds,
        )
        elapsed = time.perf_counter() - started
        if elapsed > config.health_check.tls_handshake_threshold_seconds:
            return None
        context = ssl.create_default_context() if strict_mode else ssl._create_unverified_context()
        tls_sock = context.wrap_socket(sock, server_hostname=target_host)
        tls_sock.settimeout(config.health_check.total_timeout_seconds)
        elapsed = time.perf_counter() - started
        if elapsed > config.health_check.tls_handshake_threshold_seconds:
            return None
        return ProxyHealth(proxy=proxy, latency_seconds=elapsed)
    except Exception:
        return None
    finally:
        close_socket(tls_sock)
        if tls_sock is None:
            close_socket(sock)


def filter_and_sort_health(items: list[ProxyHealth], max_latency_ms: int) -> list[ProxyHealth]:
    if max_latency_ms > 0:
        max_latency = max_latency_ms / 1000
        items = [item for item in items if item.latency_seconds <= max_latency]
    return sorted(items, key=lambda item: (item.latency_seconds, item.proxy.raw))


def log_latency_summary(mode: str, items: list[ProxyHealth]) -> None:
    if not items:
        logger.warning("[%s] No healthy proxies", mode)
        return
    fastest = items[0]
    slowest = items[-1]
    avg = sum(item.latency_seconds for item in items) / len(items)
    logger.info(
        "[%s] Latency sorted: count=%d fastest=%s/%dms slowest=%s/%dms avg=%dms",
        mode,
        len(items),
        fastest.proxy.address,
        int(fastest.latency_seconds * 1000),
        slowest.proxy.address,
        int(slowest.latency_seconds * 1000),
        int(avg * 1000),
    )


def dial_via_upstream(
    proxy: UpstreamProxy,
    target_host: str,
    target_port: int,
    *,
    timeout_seconds: float,
) -> socket.socket:
    if proxy.scheme in SOCKS_PROXY_SCHEMES:
        return socks5_connect(proxy, target_host, target_port, timeout_seconds=timeout_seconds)
    return http_proxy_connect(proxy, target_host, target_port, timeout_seconds=timeout_seconds)


def socks5_connect(
    proxy: UpstreamProxy,
    target_host: str,
    target_port: int,
    *,
    timeout_seconds: float,
) -> socket.socket:
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout_seconds)
    sock.settimeout(timeout_seconds)
    try:
        methods = b"\x00\x02" if proxy.username else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        response = recv_exact(sock, 2)
        if response[0] != 5 or response[1] == 0xFF:
            raise RuntimeError("SOCKS5 server rejected authentication methods")
        if response[1] == 0x02:
            username = proxy.username.encode("utf-8")
            password = proxy.password.encode("utf-8")
            if len(username) > 255 or len(password) > 255:
                raise RuntimeError("SOCKS5 credentials are too long")
            sock.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
            auth_response = recv_exact(sock, 2)
            if auth_response != b"\x01\x00":
                raise RuntimeError("SOCKS5 authentication failed")

        request = b"\x05\x01\x00" + encode_socks5_address(target_host) + struct.pack("!H", target_port)
        sock.sendall(request)
        head = recv_exact(sock, 4)
        if head[0] != 5 or head[1] != 0:
            raise RuntimeError(f"SOCKS5 connect failed with code {head[1]}")
        atyp = head[3]
        if atyp == 1:
            recv_exact(sock, 4)
        elif atyp == 3:
            size = recv_exact(sock, 1)[0]
            recv_exact(sock, size)
        elif atyp == 4:
            recv_exact(sock, 16)
        else:
            raise RuntimeError(f"SOCKS5 returned unsupported address type {atyp}")
        recv_exact(sock, 2)
        return sock
    except Exception:
        close_socket(sock)
        raise


def http_proxy_connect(
    proxy: UpstreamProxy,
    target_host: str,
    target_port: int,
    *,
    timeout_seconds: float,
) -> socket.socket:
    raw_sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout_seconds)
    raw_sock.settimeout(timeout_seconds)
    sock: socket.socket | ssl.SSLSocket = raw_sock
    try:
        if proxy.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=proxy.host)
        target = f"{target_host}:{target_port}"
        lines = [
            f"CONNECT {target} HTTP/1.1",
            f"Host: {target}",
            "Proxy-Connection: keep-alive",
        ]
        if proxy.username:
            token = base64.b64encode(f"{proxy.username}:{proxy.password}".encode("utf-8")).decode("ascii")
            lines.append(f"Proxy-Authorization: Basic {token}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
        sock.sendall(request)
        response = read_until(sock, b"\r\n\r\n", limit=65536)
        status_line = response.split(b"\r\n", 1)[0]
        if b" 200 " not in status_line:
            raise RuntimeError(f"HTTP proxy CONNECT failed: {status_line.decode('latin1', errors='replace')}")
        return sock
    except Exception:
        close_socket(sock)
        raise


def encode_socks5_address(host: str) -> bytes:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        encoded = host.encode("idna")
        if len(encoded) > 255:
            raise RuntimeError("SOCKS5 target host is too long")
        return b"\x03" + bytes([len(encoded)]) + encoded
    if ip.version == 4:
        return b"\x01" + ip.packed
    return b"\x04" + ip.packed


def read_socks5_connect_request(client: socket.socket) -> tuple[str, int]:
    greeting = recv_exact(client, 2)
    if greeting[0] != 5:
        raise RuntimeError("not a SOCKS5 client")
    methods = recv_exact(client, greeting[1])
    if 0 not in methods:
        client.sendall(b"\x05\xff")
        raise RuntimeError("SOCKS5 client does not offer no-auth")
    client.sendall(b"\x05\x00")
    head = recv_exact(client, 4)
    if head[:3] != b"\x05\x01\x00":
        raise RuntimeError("only SOCKS5 CONNECT is supported")
    atyp = head[3]
    if atyp == 1:
        host = socket.inet_ntoa(recv_exact(client, 4))
    elif atyp == 3:
        size = recv_exact(client, 1)[0]
        host = recv_exact(client, size).decode("idna")
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, recv_exact(client, 16))
    else:
        raise RuntimeError(f"unsupported SOCKS5 address type {atyp}")
    port = struct.unpack("!H", recv_exact(client, 2))[0]
    return host, port


def read_http_header(client: socket.socket) -> tuple[bytes, bytes]:
    data = read_until(client, b"\r\n\r\n", limit=65536)
    if not data:
        return b"", b""
    marker = data.index(b"\r\n\r\n") + 4
    return data[:marker], data[marker:]


def read_until(sock: socket.socket, marker: bytes, *, limit: int) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            raise RuntimeError("incoming header is too large")
    return bytes(data)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("connection closed unexpectedly")
        data.extend(chunk)
    return bytes(data)


def parse_http_request(header: bytes) -> tuple[tuple[str, str, str], dict[str, str]]:
    text = header.decode("latin1")
    lines = text.split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) != 3:
        raise RuntimeError("malformed HTTP request line")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return (parts[0], parts[1], parts[2]), headers


def parse_plain_http_target(target: str, headers: dict[str, str]) -> tuple[str, int, str]:
    parsed = urlparse(target)
    if parsed.scheme and parsed.hostname:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return parsed.hostname, port, path
    host_header = headers.get("host") or ""
    host, port = parse_host_port(host_header, default_port=80)
    path = target if target.startswith("/") else f"/{target}"
    return host, port, path


def parse_host_port(value: str, *, default_port: int) -> tuple[str, int]:
    value = value.strip()
    if not value:
        raise RuntimeError("missing host")
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host, port
    if value.count(":") == 1:
        host, port_text = value.rsplit(":", 1)
        return host, int(port_text)
    return value, default_port


def rewrite_http_request(header: bytes, method: str, path: str, version: str) -> bytes:
    lines = header.decode("latin1").split("\r\n")
    rewritten = [f"{method} {path} {version}"]
    for line in lines[1:]:
        if not line:
            continue
        name = line.split(":", 1)[0].strip().lower()
        if name in {"proxy-connection", "proxy-authorization"}:
            continue
        rewritten.append(line)
    rewritten.extend(["Connection: close", "", ""])
    return "\r\n".join(rewritten).encode("latin1")


def pump_bidirectional(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while sockets:
        readable, _, exceptional = select.select(sockets, [], sockets, 60)
        if exceptional:
            break
        if not readable:
            break
        for src in readable:
            dst = right if src is left else left
            try:
                data = src.recv(65536)
            except OSError:
                return
            if not data:
                with contextlib.suppress(OSError):
                    dst.shutdown(socket.SHUT_WR)
                sockets.remove(src)
                continue
            try:
                dst.sendall(data)
            except OSError:
                return


def close_socket(sock: socket.socket | ssl.SSLSocket | None) -> None:
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


def parse_listen_address(raw: str) -> tuple[str, int]:
    value = raw.strip()
    if not value:
        raise RuntimeError("listen address must not be empty")
    if value.startswith(":"):
        return "127.0.0.1", int(value[1:])
    parsed = urlparse(f"tcp://{value}" if "://" not in value else value)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if port is None:
        raise RuntimeError(f"listen address missing port: {raw}")
    return host, port


def is_local_proxy_url(proxy_url: str) -> bool:
    parsed = urlparse(proxy_url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "socks5", "socks5h"} and host in LOCAL_PROXY_HOSTS and bool(parsed.port)


@lru_cache(maxsize=1)
def get_builtin_proxy_pool_service() -> BuiltinProxyPoolService:
    return BuiltinProxyPoolService()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    service = get_builtin_proxy_pool_service()
    service.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        service.stop()


if __name__ == "__main__":
    main()
