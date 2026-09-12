"""Environment parsing and startup validation of Settings."""

from __future__ import annotations

import pytest

from vektis_mcp.settings import DEFAULT_USER_AGENT, Settings


def test_defaults() -> None:
    settings = Settings()

    assert settings.http_timeout_seconds == 20
    assert settings.tool_deadline_seconds == 60
    assert settings.request_interval_seconds == 1.0
    assert settings.max_retries == 2
    assert settings.user_agent == DEFAULT_USER_AGENT


def test_from_env_without_variables_matches_the_defaults() -> None:
    assert Settings.from_env({}) == Settings()


def test_from_env_parses_every_variable() -> None:
    settings = Settings.from_env(
        {
            "VEKTIS_HTTP_TIMEOUT_SECONDS": "5",
            "VEKTIS_TOOL_DEADLINE_SECONDS": "12.5",
            "VEKTIS_REQUEST_INTERVAL_SECONDS": "0",
            "VEKTIS_MAX_RETRIES": "0",
            "VEKTIS_USER_AGENT": "probe/2.0",
        }
    )

    assert settings == Settings(
        http_timeout_seconds=5.0,
        tool_deadline_seconds=12.5,
        request_interval_seconds=0.0,
        max_retries=0,
        user_agent="probe/2.0",
    )


def test_from_env_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEKTIS_MAX_RETRIES", "7")

    assert Settings.from_env().max_retries == 7


def test_from_env_is_frozen() -> None:
    with pytest.raises(Exception):
        Settings().http_timeout_seconds = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("env", "variable"),
    [
        ({"VEKTIS_HTTP_TIMEOUT_SECONDS": "nope"}, "VEKTIS_HTTP_TIMEOUT_SECONDS"),
        ({"VEKTIS_TOOL_DEADLINE_SECONDS": ""}, "VEKTIS_TOOL_DEADLINE_SECONDS"),
        ({"VEKTIS_REQUEST_INTERVAL_SECONDS": "soon"}, "VEKTIS_REQUEST_INTERVAL_SECONDS"),
        ({"VEKTIS_MAX_RETRIES": "2.5"}, "VEKTIS_MAX_RETRIES"),
    ],
)
def test_unparsable_values_name_their_variable(env: dict[str, str], variable: str) -> None:
    with pytest.raises(ValueError, match=variable):
        Settings.from_env(env)


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="VEKTIS_HTTP_TIMEOUT_SECONDS"):
        Settings(http_timeout_seconds=0)


def test_deadline_must_be_positive() -> None:
    with pytest.raises(ValueError, match="VEKTIS_TOOL_DEADLINE_SECONDS"):
        Settings(http_timeout_seconds=1, tool_deadline_seconds=-1)


def test_deadline_must_cover_one_request_timeout() -> None:
    with pytest.raises(ValueError, match="at least"):
        Settings(http_timeout_seconds=30, tool_deadline_seconds=20)

    assert Settings(http_timeout_seconds=20, tool_deadline_seconds=20).tool_deadline_seconds == 20


def test_interval_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="VEKTIS_REQUEST_INTERVAL_SECONDS"):
        Settings(request_interval_seconds=-0.5)


def test_retries_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="VEKTIS_MAX_RETRIES"):
        Settings(max_retries=-1)


def test_user_agent_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="VEKTIS_USER_AGENT"):
        Settings(user_agent="   ")

    with pytest.raises(ValueError, match="VEKTIS_USER_AGENT"):
        Settings.from_env({"VEKTIS_USER_AGENT": ""})


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_from_env_rejects_non_finite_numbers(raw: str) -> None:
    # float() accepts these, but NaN passes every range check and inf never times out.
    with pytest.raises(ValueError, match="finite"):
        Settings.from_env({"VEKTIS_REQUEST_INTERVAL_SECONDS": raw})
