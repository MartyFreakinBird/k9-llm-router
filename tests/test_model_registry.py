"""
Model Registry tests — verifies build_model_registry correctly registers
all backends including the Gemini agent (which was previously a bug where
the ModelBackend was constructed but never stored in the registry).
"""

import pytest
import os
from unittest.mock import patch


def test_local_mode_registers_all_local_backends():
    """In local mode, all 6 local Ollama backends should be registered."""
    from main import build_model_registry

    registry = build_model_registry("local")
    expected = ["glm5", "deepseek_v4", "qwen35", "kimi_k25", "llama4", "mistral"]
    for key in expected:
        assert key in registry, f"Missing local backend: {key}"
        assert registry[key].provider == "ollama"


def test_cloud_mode_registers_anthropic_backends():
    """In cloud mode with ANTHROPIC_KEY, cloud fallbacks should be registered."""
    with patch("main.ANTHROPIC_KEY", "test-key"):
        from main import build_model_registry
        registry = build_model_registry("cloud")
        assert "glm5_cloud" in registry
        assert "deepseek_v4_cloud" in registry
        assert registry["glm5_cloud"].provider == "anthropic"


def test_gemini_backend_registered_when_url_set():
    """CRITICAL: Gemini backend must be in registry when K9_GEMINI_URL is set."""
    with patch("main.ANTHROPIC_KEY", "test-key"), \
         patch.dict(os.environ, {"K9_GEMINI_URL": "http://localhost:8770"}):
        from main import build_model_registry
        registry = build_model_registry("hybrid")
        assert "k9-gemini-agent" in registry, (
            "k9-gemini-agent must be in registry when K9_GEMINI_URL is set — "
            "this was the bug: ModelBackend was constructed but never stored"
        )
        backend = registry["k9-gemini-agent"]
        assert backend.provider == "gemini"
        assert backend.model_id == "gemini-2.5-computer-use-preview"
        assert backend.priority == 8


def test_gemini_backend_absent_when_url_not_set():
    """When K9_GEMINI_URL is not set, Gemini should NOT be in registry
    (avoids ghost backend that's always unhealthy in the fallback chain)."""
    with patch("main.ANTHROPIC_KEY", "test-key"), \
         patch.dict(os.environ, {}, clear=True):
        from main import build_model_registry
        registry = build_model_registry("cloud")
        assert "k9-gemini-agent" not in registry, (
            "k9-gemini-agent should not be registered when K9_GEMINI_URL is absent"
        )


def test_hybrid_mode_has_both_local_and_cloud():
    """Hybrid mode should have both local and cloud backends."""
    with patch("main.ANTHROPIC_KEY", "test-key"), \
         patch.dict(os.environ, {"K9_GEMINI_URL": "http://localhost:8770"}):
        from main import build_model_registry
        registry = build_model_registry("hybrid")
        # Local
        assert "llama4" in registry
        # Cloud
        assert "glm5_cloud" in registry
        # Gemini
        assert "k9-gemini-agent" in registry


def test_router_request_size_limits():
    """Verify that oversized requests are rejected (DoS mitigation)."""
    from main import RouterRequest
    from pydantic import ValidationError

    # Normal request is fine
    req = RouterRequest(
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=1000,
    )
    assert len(req.messages) == 1

    # max_tokens above 32768 should be rejected by pydantic
    with pytest.raises(ValidationError):
        RouterRequest(
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=40000,
        )
