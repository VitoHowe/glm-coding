"""Stable per-account browser fingerprint profile helpers."""

from __future__ import annotations

from dataclasses import dataclass
import secrets


@dataclass(frozen=True)
class BrowserProfile:
    """A coherent browser profile: stored id, curl-cffi TLS profile and UA."""

    profile_id: str
    family: str
    version: str
    impersonate: str
    user_agent: str
    random_weight: int = 1
    enabled_for_random: bool = True


# Version choices were aligned with desktop browser-version share checked on
# 2026-04-26 and limited by curl-cffi 0.15.0 supported impersonation targets.
WINDOWS_CHROME_146_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
WINDOWS_CHROME_145_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
WINDOWS_FIREFOX_149_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) "
    "Gecko/20100101 Firefox/149.0"
)

AVAILABLE_BROWSER_PROFILES: dict[str, BrowserProfile] = {
    "chrome146": BrowserProfile(
        profile_id="chrome146",
        family="chrome",
        version="146",
        impersonate="chrome146",
        user_agent=WINDOWS_CHROME_146_UA,
        random_weight=36,
    ),
    "chrome145": BrowserProfile(
        profile_id="chrome145",
        family="chrome",
        version="145",
        impersonate="chrome145",
        user_agent=WINDOWS_CHROME_145_UA,
        random_weight=2,
    ),
    "firefox149": BrowserProfile(
        profile_id="firefox149",
        family="firefox",
        version="149",
        impersonate="firefox147",
        user_agent=WINDOWS_FIREFOX_149_UA,
        random_weight=5,
    ),
    # curl-cffi 0.15.0 only exposes edge101. Keep the UI-facing Edge profile
    # explicit, but route transport to chrome146 to avoid pairing modern Edge UA
    # with a very old Edge TLS fingerprint.
    "edge146": BrowserProfile(
        profile_id="edge146",
        family="edge",
        version="146",
        impersonate="chrome146",
        user_agent=(WINDOWS_CHROME_146_UA + " Edg/146.0.0.0"),
        random_weight=4,
    ),
}

DEFAULT_BROWSER_PROFILE_ID = "chrome146"
DEFAULT_BROWSER_IMPERSONATE = DEFAULT_BROWSER_PROFILE_ID
AVAILABLE_BROWSER_IMPERSONATIONS: tuple[str, ...] = tuple(AVAILABLE_BROWSER_PROFILES)

LEGACY_BROWSER_PROFILE_ALIASES: dict[str, str] = {
    "chrome": "chrome146",
    "chrome124": "chrome146",
    "chrome136": "chrome146",
    "edge": "edge146",
    "edge101": "edge146",
    "firefox": "firefox149",
    "firefox137": "firefox149",
    "firefox147": "firefox149",
}

DEFAULT_USER_AGENTS: dict[str, str] = {
    profile_id: profile.user_agent for profile_id, profile in AVAILABLE_BROWSER_PROFILES.items()
}


def random_browser_impersonate() -> str:
    """Pick a realistic browser fingerprint profile for a new account."""
    candidates = [
        profile.profile_id
        for profile in AVAILABLE_BROWSER_PROFILES.values()
        if profile.enabled_for_random
        for _ in range(max(1, profile.random_weight))
    ]
    return secrets.choice(candidates)


def resolve_browser_impersonate(raw: str | None) -> str:
    """Normalize stored profile id to a supported browser fingerprint profile."""
    normalized = (raw or "").strip().lower().replace("_", "").replace("-", "")
    if normalized in AVAILABLE_BROWSER_PROFILES:
        return normalized
    return LEGACY_BROWSER_PROFILE_ALIASES.get(normalized, DEFAULT_BROWSER_PROFILE_ID)


def resolve_transport_impersonate(browser_impersonate: str | None) -> str:
    """Return the curl-cffi impersonate value for a stored profile id."""
    profile_id = resolve_browser_impersonate(browser_impersonate)
    return AVAILABLE_BROWSER_PROFILES[profile_id].impersonate


def resolve_user_agent(user_agent: str | None, browser_impersonate: str | None) -> str:
    """Return explicit UA or a stable default matching the fingerprint profile."""
    explicit = (user_agent or "").strip()
    if explicit:
        return explicit
    profile_id = resolve_browser_impersonate(browser_impersonate)
    return AVAILABLE_BROWSER_PROFILES[profile_id].user_agent
