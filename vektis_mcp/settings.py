"""Validated runtime configuration, read from the environment at startup."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass

TIMEOUT_VAR = "VEKTIS_HTTP_TIMEOUT_SECONDS"
DEADLINE_VAR = "VEKTIS_TOOL_DEADLINE_SECONDS"
INTERVAL_VAR = "VEKTIS_REQUEST_INTERVAL_SECONDS"
RETRIES_VAR = "VEKTIS_MAX_RETRIES"
USER_AGENT_VAR = "VEKTIS_USER_AGENT"
#: Plain mainstream-browser string so the traffic looks like any other
#: visitor of the public form rather than a named tool.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {raw!r}")
    return value


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


@dataclass(frozen=True)
class Settings:
    """Source-access budget. Validated on construction; raises ValueError."""

    http_timeout_seconds: float = 20
    tool_deadline_seconds: float = 60
    request_interval_seconds: float = 1.0
    max_retries: int = 2
    user_agent: str = DEFAULT_USER_AGENT

    def __post_init__(self) -> None:
        if self.http_timeout_seconds <= 0:
            raise ValueError(f"{TIMEOUT_VAR} must be greater than 0, got {self.http_timeout_seconds}")
        if self.tool_deadline_seconds <= 0:
            raise ValueError(f"{DEADLINE_VAR} must be greater than 0, got {self.tool_deadline_seconds}")
        if self.tool_deadline_seconds < self.http_timeout_seconds:
            raise ValueError(
                f"{DEADLINE_VAR} ({self.tool_deadline_seconds}) must be at least "
                f"{TIMEOUT_VAR} ({self.http_timeout_seconds})"
            )
        if self.request_interval_seconds < 0:
            raise ValueError(f"{INTERVAL_VAR} must not be negative, got {self.request_interval_seconds}")
        if self.max_retries < 0:
            raise ValueError(f"{RETRIES_VAR} must not be negative, got {self.max_retries}")
        if not self.user_agent.strip():
            raise ValueError(f"{USER_AGENT_VAR} must not be empty")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from environment variables, falling back to the defaults."""
        source = os.environ if env is None else env
        defaults = cls()
        user_agent = source.get(USER_AGENT_VAR)
        return cls(
            http_timeout_seconds=_float(source, TIMEOUT_VAR, defaults.http_timeout_seconds),
            tool_deadline_seconds=_float(source, DEADLINE_VAR, defaults.tool_deadline_seconds),
            request_interval_seconds=_float(source, INTERVAL_VAR, defaults.request_interval_seconds),
            max_retries=_int(source, RETRIES_VAR, defaults.max_retries),
            user_agent=defaults.user_agent if user_agent is None else user_agent,
        )
