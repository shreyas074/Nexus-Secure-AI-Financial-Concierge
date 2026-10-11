"""Optional Claude layer. Every agent role is a CrewAI `Agent` + `Task` run in its own tiny Crew.

Why one mini-Crew per role instead of one big sequential Crew? Plain Python must run BETWEEN the roles
(sanitise retrieved text, call MCP tools, enforce the hard escalation gate). Chaining five roles inside one
Crew would hide those steps from us. This is only used when NEXUS_LLM=claude; the default offline mode needs
no API key and no CrewAI.

NOTE: written against the CrewAI public API (Agent / Task / Crew / LLM / BaseTool). Pin and check the version
in requirements-llm.txt before the live demo.
"""
import json
import os
import re

import config
from errors import LLMUnavailable
from logging_config import get_logger, trace

logger = get_logger("llm")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")  # no telemetry from a banking demo
os.environ.setdefault("OTEL_SDK_DISABLED", "true")


def llm_enabled():
    return config.llm_mode() == "claude"


def parse_json(text):
    """Pull the first {...} object out of an LLM reply. Returns {} if there is none."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except ValueError:
        return {}


@trace(logger)
def run_agent(role, goal, backstory, prompt, expected_output, tools=None):
    """Run ONE CrewAI agent on ONE task and return its raw text answer. Fails closed (typed error)."""
    try:
        from crewai import Agent, Crew, LLM, Process, Task
    except ImportError as exc:
        raise LLMUnavailable("CrewAI is not installed. Run: pip install -r requirements-llm.txt") from exc
    try:
        llm = LLM(model=os.getenv("NEXUS_CLAUDE_MODEL", "anthropic/claude-sonnet-5-5"), temperature=0, max_tokens=1200)
        agent = Agent(role=role, goal=goal, backstory=backstory, llm=llm, tools=tools or [],
                      allow_delegation=False, verbose=False)
        task = Task(description=prompt, expected_output=expected_output, agent=agent)
        crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
        output = crew.kickoff()
        return getattr(output, "raw", None) or str(output)
    except Exception as exc:  # no silent fallback: the caller gets a typed error
        raise LLMUnavailable(f"The language-model step failed: {type(exc).__name__}") from exc


def make_escalation_tool(gateway):
    """A CrewAI tool that lets the Escalation agent call run_escalation_check through the MCP gateway."""
    from typing import Type

    from crewai.tools import BaseTool
    from pydantic import BaseModel

    class Args(BaseModel):
        customer_id: str
        request_category: str
        draft_confidence: float

    class RunEscalationCheck(BaseTool):
        name: str = "run_escalation_check"
        description: str = "Decide whether this request must go to a human. Always call this before answering."
        args_schema: Type[BaseModel] = Args

        def _run(self, customer_id: str, request_category: str, draft_confidence: float) -> str:
            return json.dumps(gateway.call("concierge_ops_server", "run_escalation_check", customer_id=customer_id,
                                           request_category=request_category, draft_confidence=draft_confidence))

    return RunEscalationCheck()
