from __future__ import annotations

import pytest

from llmgate.config import Settings
from llmgate.core.errors import InvalidRequestError
from llmgate.core.timeouts import build_upstream_timeout


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "upstream_timeout_seconds": 60,
            "upstream_connect_timeout_seconds": 10,
        }
    )


def test_header_can_shorten_but_not_extend_the_upstream_timeout() -> None:
    shortened = build_upstream_timeout(_settings(), "5")
    assert shortened.read == 5
    assert shortened.connect == 10
    capped = build_upstream_timeout(_settings(), "120")
    assert capped.read == 60
    default = build_upstream_timeout(_settings(), None)
    assert default.read == 60


def test_bad_timeout_header_is_a_client_error() -> None:
    with pytest.raises(InvalidRequestError) as exc:
        build_upstream_timeout(_settings(), "soon")
    assert exc.value.param == "x-llmgate-timeout"
    with pytest.raises(InvalidRequestError):
        build_upstream_timeout(_settings(), "0")
