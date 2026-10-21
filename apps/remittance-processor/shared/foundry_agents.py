"""Invoke deployed Microsoft Foundry agents (ACH Extraction Expert, ACH Assurance Expert).

Agents are defined and versioned in Foundry (agents/*.py, deployed by the `agents` phase); the Function
only references them by name. Each is invoked through the OpenAI Responses API with an
``agent_reference`` - the agent's instructions, tools and output format live in Foundry, not here.
"""

from __future__ import annotations

import json
import os
import re

from shared import usage

_client = None


def _openai_client():
    global _client
    if _client is None:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential

        project = AIProjectClient(
            endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            credential=DefaultAzureCredential(),
        )
        _client = project.get_openai_client()
    return _client


def agent_reference(agent: str) -> dict:
    """'name' -> latest version; 'name:version' pins a version."""
    if ":" in agent:
        name, version = agent.rsplit(":", 1)
    else:
        name, version = agent, None
    ref = {"type": "agent_reference", "name": name}
    if version:
        ref["version"] = version
    return ref


def parse_agent_json(text: str) -> dict:
    """Parse the agent's JSON, tolerating ```json fences and leading prose."""
    if not text:
        return {}
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    try:
        value = json.loads(candidate)
        return value if isinstance(value, dict) else {}
    except ValueError:
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if match:
            try:
                value = json.loads(match.group(0))
                return value if isinstance(value, dict) else {}
            except ValueError:
                pass
    return {}


def _annotations(response) -> list[dict]:
    found = []
    for item in getattr(response, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            for annotation in getattr(content, "annotations", None) or []:
                title = getattr(annotation, "title", None)
                url = getattr(annotation, "url", None)
                if title or url:
                    found.append({"title": title, "url": url})
    return found


def invoke_agent(agent: str, prompt: str) -> tuple[str, list[dict]]:
    """Run the named Foundry agent on ``prompt``; return (output text, citation annotations)."""
    response = _openai_client().responses.create(
        input=prompt,
        extra_body={"agent_reference": agent_reference(agent)},
    )
    usage.record_agent_response(agent, response)
    status = getattr(response, "status", None)
    if status and status != "completed":
        raise RuntimeError(f"agent run status: {status}")
    return getattr(response, "output_text", "") or "", _annotations(response)


def invoke_agent_with_tools(agent: str, prompt: str, tools: dict, *, max_turns: int = 8, max_seconds: float = 240.0) -> tuple[str, list[dict]]:
    """Run the named Foundry agent with function tools that the Function executes.

    The agent's tool calls are answered by ``tools`` (name -> callable(**arguments) -> JSON-able), up
    to ``max_turns`` rounds and ``max_seconds`` overall. Returns (final output text, tool-call trace).
    A tool error is returned to the agent as {"error": ...} rather than failing the run."""
    import time

    started = time.monotonic()
    client = _openai_client()
    reference = {"agent_reference": agent_reference(agent)}
    response = client.responses.create(input=prompt, extra_body=reference)
    usage.record_agent_response(agent, response)  # every turn is billed: recorded per call
    trace: list[dict] = []
    for _ in range(max_turns):
        calls = [item for item in getattr(response, "output", None) or [] if getattr(item, "type", None) == "function_call"]
        if not calls:
            break
        if time.monotonic() - started > max_seconds:
            raise TimeoutError(f"agent exceeded {max_seconds:.0f}s")
        outputs = []
        for call in calls:
            try:
                arguments = json.loads(call.arguments or "{}")
                result = tools[call.name](**arguments)
            except Exception as exc:  # the agent sees the error and can adjust
                arguments, result = getattr(call, "arguments", None), {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            trace.append({"tool": call.name, "arguments": arguments, "ok": not (isinstance(result, dict) and "error" in result)})
            outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result, default=str)})
        response = client.responses.create(input=outputs, previous_response_id=response.id, extra_body=reference)
        usage.record_agent_response(agent, response)
    else:
        raise RuntimeError(f"agent did not finish within {max_turns} tool rounds")
    status = getattr(response, "status", None)
    if status and status != "completed":
        raise RuntimeError(f"agent run status: {status}")
    return getattr(response, "output_text", "") or "", trace


_agent_info: dict[str, dict] = {}


def describe_agent(agent: str) -> dict:
    """{"agent", "agentVersion", "model"} of the version that answers ``agent`` (cached per process),
    recorded on every AI result so each decision traces to the exact agent definition in Foundry."""
    name = agent.split(":", 1)[0]
    if name in _agent_info:
        return _agent_info[name]
    info = {"agent": name}
    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential

        project = AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=DefaultAzureCredential())
        latest = project.agents.get(agent_name=name).versions.latest
        info.update({"agentVersion": str(getattr(latest, "version", "") or "") or None,
                     "model": getattr(getattr(latest, "definition", None), "model", None)})
    except Exception:  # audit detail only - never blocks a run
        pass
    if ":" in agent:
        info["agentVersion"] = agent.split(":", 1)[1]  # pinned version wins
    _agent_info[name] = info
    return info


def agent_configured(setting: str) -> str | None:
    """The agent name from ``setting`` when it and the Foundry project endpoint are both set."""
    agent = os.environ.get(setting, "").strip()
    return agent if agent and os.environ.get("FOUNDRY_PROJECT_ENDPOINT") else None
