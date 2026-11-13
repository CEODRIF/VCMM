"""TASK 011 configuration tests: every provider/materialization setting is
validated, secrets are normalized (empty = not configured), and half-set
credential groups fail fast (spec sections 10, 14)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def s(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# -- defaults: honest, unconfigured, safe --------------------------------------

def test_defaults_are_unconfigured_and_enabled():
    settings = s()
    # provider jobs are accepted by default, but NOTHING is credentialed:
    # youtube works keyless (oEmbed, partial), twitch reports unconfigured.
    assert settings.source_providers_enabled is True
    assert settings.youtube_api_key is None
    assert settings.youtube_oauth_client_id is None
    assert settings.youtube_oauth_client_secret is None
    assert settings.youtube_oauth_refresh_token is None
    assert settings.youtube_oauth_scopes == "openid"
    assert settings.twitch_client_id is None
    assert settings.twitch_client_secret is None
    # endpoint bases point at the OFFICIAL services
    assert settings.twitch_api_base == "https://api.twitch.tv/helix"
    assert settings.twitch_oauth_base == "https://id.twitch.tv"
    # transfer budgets
    assert settings.provider_request_timeout_seconds == 10.0
    assert settings.provider_max_retries == 2
    assert settings.provider_retry_backoff_seconds == 1.5
    assert settings.materialize_max_source_bytes == 2 * 1024**3
    assert settings.materialize_max_redirects == 3
    assert settings.materialize_chunk_bytes == 1024 * 1024
    assert settings.materialize_max_duration_seconds is None  # no cap (TASK 007)


# -- numeric bounds -------------------------------------------------------------

@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("provider_request_timeout_seconds", 0),
        ("provider_request_timeout_seconds", 121),
        ("provider_max_retries", 6),
        ("provider_retry_backoff_seconds", 61),
        ("materialize_max_redirects", 9),
        ("materialize_chunk_bytes", 4096),           # below 64 KiB
        ("materialize_chunk_bytes", 17 * 1024**2),   # above 16 MiB
        ("materialize_max_source_bytes", 1024),      # below 1 MiB
        ("materialize_max_duration_seconds", -1),
    ],
)
def test_bounds_enforced(field: str, bad):
    with pytest.raises(ValidationError):
        s(**{field: bad})


@pytest.mark.parametrize(
    ("field", "ok"),
    [
        ("provider_request_timeout_seconds", 120),
        ("provider_max_retries", 0),   # no retries is legal
        ("materialize_max_redirects", 0),
        ("materialize_chunk_bytes", 64 * 1024),
        ("materialize_max_duration_seconds", 7200),
    ],
)
def test_bounds_edges_accepted(field: str, ok):
    assert s(**{field: ok})


# -- secrets: empty means not configured ----------------------------------------

def test_empty_secret_becomes_none():
    settings = s(youtube_api_key="", youtube_oauth_client_id="",
                 twitch_client_id="", twitch_client_secret="")
    assert settings.youtube_api_key is None
    assert settings.youtube_oauth_client_id is None
    assert settings.twitch_client_id is None
    assert settings.twitch_client_secret is None


def test_secret_values_are_kept():
    settings = s(youtube_api_key="yt-key-abc", twitch_client_id="tw-id-abc")
    assert settings.youtube_api_key == "yt-key-abc"
    assert settings.twitch_client_id == "tw-id-abc"


# -- credential-group consistency ------------------------------------------------

def test_youtube_oauth_trio_must_be_set_together():
    # one or two of three → fail fast (never a 401 at request time)
    with pytest.raises(ValidationError, match="set together"):
        s(youtube_oauth_client_id="id-only")
    with pytest.raises(ValidationError, match="set together"):
        s(youtube_oauth_client_id="id", youtube_oauth_client_secret="sec")
    with pytest.raises(ValidationError, match="set together"):
        s(youtube_oauth_client_secret="sec-only")
    with pytest.raises(ValidationError, match="set together"):
        s(youtube_oauth_refresh_token="refresh-only")
    # all three → fine
    good = s(youtube_oauth_client_id="id",
             youtube_oauth_client_secret="sec",
             youtube_oauth_refresh_token="refresh")
    assert good.youtube_oauth_client_id == "id"
    # all empty → also fine (keyless mode)
    assert s().youtube_oauth_client_id is None


def test_twitch_client_id_alone_is_allowed():
    """Documented intentional exception: the official Client-Id-header mode
    for PUBLIC VOD metadata needs only the id."""
    settings = s(twitch_client_id="id-only")
    assert settings.twitch_client_id == "id-only"
    assert settings.twitch_client_secret is None
    # id + secret (app-access-token mode) is fine too
    assert s(twitch_client_id="id", twitch_client_secret="sec")


def test_youtube_api_key_and_oauth_are_alternatives():
    # either credential form enables the Data API path; both together is
    # legal (the key wins per the adapter's documented precedence)
    assert s(youtube_api_key="key")
    assert s(youtube_oauth_client_id="id",
             youtube_oauth_client_secret="sec",
             youtube_oauth_refresh_token="refresh",
             youtube_api_key="key")


# -- scopes are bounded (no giant env-var abuse) ---------------------------------

def test_oauth_scopes_bounded():
    assert s(youtube_oauth_scopes="openid").youtube_oauth_scopes == "openid"
    assert s(youtube_oauth_scopes="").youtube_oauth_scopes == ""
    with pytest.raises(ValidationError):
        s(youtube_oauth_scopes="x" * 501)
