from __future__ import annotations

import os

DEFAULT_AGENT_CONTROL_URL = os.environ.get(
    "AGENT_CONTROL_URL",
    "https://agent-control-test-evals.gcp-dev.galileo.ai",
)
DEFAULT_AGENT_NAME = os.environ.get("AGENT_CONTROL_AGENT_NAME", "galileo-control-span-demo")
DEFAULT_PROJECT = os.environ.get("GALILEO_PROJECT", "test-evals-project")
DEFAULT_LOG_STREAM = os.environ.get("GALILEO_LOG_STREAM", "test-evals-logstream")
DEFAULT_CONSOLE_URL = os.environ.get(
    "GALILEO_CONSOLE_URL",
    "https://console-test-evals.gcp-dev.galileo.ai",
)
DEFAULT_API_URL = os.environ.get(
    "GALILEO_API_URL",
    "https://api-test-evals.gcp-dev.galileo.ai",
)


def resolve_agent_control_api_key() -> str | None:
    """Use the Galileo API key as the Agent Control credential unless overridden."""
    api_key = os.environ.get("AGENT_CONTROL_API_KEY") or os.environ.get("GALILEO_API_KEY")
    if api_key and "AGENT_CONTROL_API_KEY" not in os.environ:
        os.environ["AGENT_CONTROL_API_KEY"] = api_key
    return api_key


def resolve_agent_control_api_key_header() -> str:
    """Use the header forwarded by the devstack upstream auth deployment."""
    header = os.environ.get("AGENT_CONTROL_API_KEY_HEADER", "Galileo-API-Key")
    os.environ["AGENT_CONTROL_API_KEY_HEADER"] = header
    return header
