"""One browser-origin policy for HTTP and authenticated WebSockets."""
import re

from .settings import Settings


LOOPBACK_ORIGIN_PATTERN = r"http://(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?"


def localhost_origin_regex(settings: Settings) -> str | None:
    return LOOPBACK_ORIGIN_PATTERN if settings.allow_localhost_origins else None


def browser_origin_allowed(origin: str, settings: Settings) -> bool:
    if origin == str(settings.app_origin).rstrip("/"):
        return True
    pattern = localhost_origin_regex(settings)
    return pattern is not None and re.fullmatch(pattern, origin) is not None
