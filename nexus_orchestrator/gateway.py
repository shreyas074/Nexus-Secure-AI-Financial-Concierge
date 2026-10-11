"""The ONLY path from the orchestrator to backend data: an MCP client.

Two transports, one code path (a real `mcp.ClientSession` either way):
  memory - the server objects run inside this process, connected by in-memory streams. Same protocol,
           validation, masking and tracing as HTTP, but no separate processes (used by the demo + tests).
  http   - real streamable-HTTP connections to the four server processes on ports 8001-8004.

Every string coming back from a tool is untrusted: it is passed through sanitize_free_text, and anything
that looks like a prompt injection is replaced by a marker and recorded in `self.quarantined`.
"""
import asyncio
import importlib
import json
import os
from contextlib import asynccontextmanager

import config
from errors import NexusToolError, PromptInjectionDetected, ServerUnavailableError
from guardrails import redact_for_logging, sanitize_free_text
from logging_config import get_logger, trace

logger = get_logger("gateway")
_REMOVED = "[removed: suspected prompt injection]"


class Gateway:
    def __init__(self, mode=None, down=(), timeout=20):
        self.mode = mode or os.getenv("NEXUS_GATEWAY", "memory")
        self.down = set(down)       # servers to treat as offline (used to demonstrate Stress Test 4)
        self.timeout = timeout
        self.calls = []             # (server, tool/resource/prompt) in order - handy for tests and demos
        self.quarantined = []
        self._memory_servers = {}

    # ---------------------------------------------------------------- connection handling
    def _memory_server(self, server):
        if server not in self._memory_servers:
            self._memory_servers[server] = importlib.import_module(f"{server}.server").build_server()
        return self._memory_servers[server]

    @asynccontextmanager
    async def _open(self, server):
        if self.mode == "memory":
            from mcp.shared.memory import create_connected_server_and_client_session
            async with create_connected_server_and_client_session(self._memory_server(server)._mcp_server) as session:
                yield session
        else:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
            async with streamablehttp_client(config.server_url(server)) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    def _run(self, server, operation):
        """Run one async operation against one server. Any connection-level failure becomes a typed error."""
        if server in self.down:
            raise ServerUnavailableError(f"{server} is offline", server=server)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Gateway is synchronous: call it from a thread (asyncio.to_thread)")

        async def go():
            async with self._open(server) as session:
                return await operation(session)

        try:
            return asyncio.run(asyncio.wait_for(go(), self.timeout))
        except Exception as exc:  # connection refused, timeout, protocol failure ...
            raise ServerUnavailableError(f"{server} did not respond: {type(exc).__name__}", server=server) from exc

    # ---------------------------------------------------------------- sanitising untrusted output
    def _clean(self, value, where):
        if isinstance(value, str):
            try:
                return sanitize_free_text(value, max_len=2000)
            except PromptInjectionDetected as exc:
                self.quarantined.append({"source": where, "signature": exc.details.get("signature")})
                logger.warning("quarantined tool output", extra={"fields": {"source": where}})
                return _REMOVED
        if isinstance(value, dict):
            return {k: self._clean(v, where) for k, v in value.items()}
        if isinstance(value, list):
            return [self._clean(v, where) for v in value]
        return value

    # ---------------------------------------------------------------- public API
    @trace(logger, redact=redact_for_logging)
    def call(self, server, tool, **arguments):
        """Call a tool. Returns its `data`; raises NexusToolError (ok=False) or ServerUnavailableError."""
        self.calls.append((server, tool))

        async def op(session):
            result = await session.call_tool(tool, arguments)
            return result.isError, result.content[0].text

        is_error, text = self._run(server, op)
        if is_error:
            raise NexusToolError(text, code="TOOL_PROTOCOL_ERROR", server=server, tool=tool)
        payload = json.loads(text)
        if not payload.get("ok"):
            err = payload.get("error", {})
            raise NexusToolError(err.get("message", ""), code=err.get("code", "TOOL_ERROR"), server=server, tool=tool)
        return self._clean(payload["data"], f"{server}.{tool}")

    @trace(logger, redact=redact_for_logging)
    def read_resource(self, server, uri):
        """Read a templated resource (e.g. customer://CUS-20077/profile). Returns its data dict."""
        from pydantic import AnyUrl
        self.calls.append((server, uri))

        async def op(session):
            result = await session.read_resource(AnyUrl(uri))
            return result.contents[0].text

        payload = json.loads(self._run(server, op))
        if not payload.get("ok"):
            err = payload.get("error", {})
            raise NexusToolError(err.get("message", ""), code=err.get("code", "RESOURCE_ERROR"), server=server)
        return self._clean(payload["data"], f"{server}.{uri}")

    @trace(logger, redact=redact_for_logging)
    def get_prompt(self, server, name, **arguments):
        """Render an MCP prompt to text."""
        self.calls.append((server, f"prompt:{name}"))

        async def op(session):
            result = await session.get_prompt(name, arguments)
            return result.messages[0].content.text

        return self._run(server, op)

    @trace(logger)
    def list_primitives(self, server):
        async def op(session):
            tools = await session.list_tools()
            templates = await session.list_resource_templates()
            prompts = await session.list_prompts()
            return {"tools": sorted(t.name for t in tools.tools),
                    "resources": sorted(str(r.uriTemplate) for r in templates.resourceTemplates),
                    "prompts": sorted(p.name for p in prompts.prompts)}

        return self._run(server, op)

    def drain_quarantine(self):
        items, self.quarantined = self.quarantined, []
        return items
