"""Install a minimal Runtime SDK stub when the host lacks the project SDK."""

from __future__ import annotations

import sys
import types


def install() -> None:
    try:
        from bedrock_agentcore import BedrockAgentCoreApp  # noqa: F401
        return
    except (ImportError, ModuleNotFoundError):
        pass

    class BedrockAgentCoreApp:
        def entrypoint(self, function):
            return function

        def run(self) -> None:  # pragma: no cover - never called by unit tests
            raise RuntimeError("the AgentCore test stub cannot serve requests")

    package = types.ModuleType("bedrock_agentcore")
    runtime = types.ModuleType("bedrock_agentcore.runtime")
    package.BedrockAgentCoreApp = BedrockAgentCoreApp
    runtime.BedrockAgentCoreApp = BedrockAgentCoreApp
    sys.modules["bedrock_agentcore"] = package
    sys.modules["bedrock_agentcore.runtime"] = runtime
