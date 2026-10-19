#!/usr/bin/env python3
"""Check model propagation across adapter overrides and credential filtering."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.engine.alpha_agent.launcher import _strip_unneeded_child_secrets
from backend.services.engine.alpha_agent.llm_client import LLMConfig
from backend.services.engine.rd_agent.llm_env import build_llm_env


def check_model(model: str, provider: str) -> None:
    source = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "DEEPSEEK_API_KEY": "test-deepseek-credential" if provider == "deepseek" else "",
        "DEEPSEEK_MODEL": model,
        # A stale inherited model must yield to the resolved service config.
        "LITELLM_CHAT_MODEL": "openai/gpt-4-turbo",
    }
    config = LLMConfig(
        api_key="test-selected-credential",
        base_url="https://api.deepseek.com/v1" if provider == "deepseek" else "https://example.invalid/v1",
        model=model,
        protocol="openai",
    )
    with patch.dict(os.environ, source, clear=True):
        env = {**os.environ, **config.llm_env_overrides()}
        build_llm_env(env)
        env.update({"CHAT_MODEL": "", "REASONING_MODEL": ""})
        build_llm_env(env)
        _strip_unneeded_child_secrets(env, needs_llm=True, needs_factor_name_llm=True)
    assert "DEEPSEEK_API_KEY" not in env
    # Reproduce RDLoopWrapper's second pass in a fresh, filtered process env.
    with patch.dict(os.environ, env, clear=True):
        env.update({"CHAT_MODEL": "", "REASONING_MODEL": ""})
        build_llm_env(env)
    expected = f"openai/{model}"
    assert env["LITELLM_CHAT_MODEL"] == env["CHAT_MODEL"] == env["REASONING_MODEL"] == expected
    assert env["LITELLM_OPENAI_API_BASE"] == config.base_url
    assert env["LITELLM_OPENAI_API_KEY"] == config.api_key
    assert env["OPENAI_API_KEY"] == config.api_key
    assert env["OPENAI_BASE_URL"] == config.base_url
    # Import the actual installed RD-Agent settings to catch prefix/config issues.
    code = (
        "from rdagent.oai.backend.litellm import LITELLM_SETTINGS; "
        "import sys; assert LITELLM_SETTINGS.chat_model == sys.argv[1], LITELLM_SETTINGS.chat_model"
    )
    subprocess.run([sys.executable, "-c", code, expected], env=env, check=True, timeout=30)
    print(f"PASS resolved {expected} survives adapters, secret filtering, and RD-Agent import")


if __name__ == "__main__":
    check_model("deepseek-flash", "deepseek")
    check_model("custom-chat-model", "openai")
