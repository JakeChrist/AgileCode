"""Provider examples and retained profiles keep optional setup timeout semantics."""
import json
from pathlib import Path

import pytest

from agilecode.domain.types import ModelProfile, ProviderConfig


EXAMPLES = Path(__file__).parents[1] / "examples"


@pytest.mark.parametrize("filename", ["provider-lmstudio.json", "provider-llamacpp.json"])
def test_provider_examples_do_not_impose_timeout_defaults(filename):
    saved = json.loads((EXAMPLES / filename).read_text())
    assert saved["profile"]["timeout_seconds"] is None
    provider = ProviderConfig.model_validate(saved)
    assert provider.profile.timeout_seconds is None
    assert ProviderConfig.model_validate_json(provider.model_dump_json()) == provider


@pytest.mark.parametrize("timeout", [None, 180.0])
def test_retained_profile_timeout_round_trips_without_migration(timeout):
    # Retained attempt profiles use the same JSON boundary as new providers.
    saved = {"base_url": "http://127.0.0.1:1234/v1", "model": "local-model",
             "timeout_seconds": timeout, "request_options": {"temperature": 0.2},
             "round_trip_reasoning": True}
    profile = ModelProfile.model_validate_json(json.dumps(saved))
    restored = ModelProfile.model_validate_json(profile.model_dump_json())
    assert restored.timeout_seconds == timeout
    assert restored.request_options == saved["request_options"]
    assert restored.round_trip_reasoning is True
