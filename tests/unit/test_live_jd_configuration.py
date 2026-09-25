"""Strict process settings for explicitly enabled JD access."""

import pytest

from personal_shopping_agent.infrastructure.settings import (
    LIVE_JD_ACCESS_ENV,
    LIVE_JD_HEADLESS_ENV,
    LiveJDConfigurationError,
    LiveJDSettings,
    load_live_jd_settings,
)


def test_live_jd_defaults_to_disabled_headless_access() -> None:
    assert load_live_jd_settings({}) == LiveJDSettings(enabled=False, headless=True)


def test_live_jd_settings_accept_only_explicit_trimmed_booleans() -> None:
    assert load_live_jd_settings(
        {
            LIVE_JD_ACCESS_ENV: " TRUE ",
            LIVE_JD_HEADLESS_ENV: "false",
        }
    ) == LiveJDSettings(enabled=True, headless=False)


@pytest.mark.parametrize("name", [LIVE_JD_ACCESS_ENV, LIVE_JD_HEADLESS_ENV])
def test_live_jd_settings_reject_truthy_typos(name: str) -> None:
    with pytest.raises(LiveJDConfigurationError) as captured:
        load_live_jd_settings({name: "1"})

    assert captured.value.code == "invalid_boolean"
    assert name in str(captured.value)
