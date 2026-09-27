"""A small MCP client for configured stdio and Streamable HTTP servers.

Implements initialize, tools/list+call, resources/list+read, pagination, ping and
roots/list. Sampling/elicitation requests are explicitly rejected, never executed.
No third-party server is installed or launched until it is explicitly enabled.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import httpx
from agilecode.core.types import AgileCodeError, McpServerConfig
from .shell import shell_environment

MAX_MESSAGE = 4 * 1024 * 1024


class McpError(AgileCodeError):
    code = "mcp_error"


class McpConnection:
    def __init__(self, config: McpServerConfig, workspace: Path):
        self.config, self.workspace = config, workspace
        self.process: asyncio.subprocess.Process | None = None
        self.http: httpx.AsyncClient | None = None
        self.session_id: str | None = None
        self.version = "2025-03-26"
        self.counter = 0
        self.lock = asyncio.Lock()
        self.stderr_task: asyncio.Task | None = None
        self.stderr_tail = ""
        self.server_info: dict = {}
        self.initialized = False

    def _substitute(self, value: str) -> str:
        import re
        def credential(match):
            name = match.group(1)
            if name not in os.environ:
                raise McpError("MCP environment variable is missing: " + name)
            return os.environ[name]
        return re.sub(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}",credential,value).replace("{workspace}", str(self.workspace))

    async def start(self):
        if self.initialized:
            return
        if self.config.transport == "stdio":
            self.process = await asyncio.create_subprocess_exec(self.config.command, *(self._substitute(a) for a in self.config.args),
                cwd=self.workspace, env=shell_environment({k: self._substitute(v) for k,v in self.config.env.items()}),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=MAX_MESSAGE, start_new_session=True)
            async def stderr():
                assert self.process and self.process.stderr
                while chunk := await self.process.stderr.read(4096):
                    self.stderr_tail = (self.stderr_tail + chunk.decode(errors="replace"))[-16000:]
            self.stderr_task = asyncio.create_task(stderr())
        else:
            self.http = httpx.AsyncClient(follow_redirects=False, timeout=60)
        result = await self.request("initialize", {"protocolVersion": self.version,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": {"name": "AgileCode", "version": "0.1.0"}})
        self.version = result.get("protocolVersion", self.version)
        self.server_info = result.get("serverInfo", {})
        await self.notify("notifications/initialized", {})
        self.initialized = True

    async def _client_response(self, message: dict) -> dict:
        method = message.get("method")
        if method == "ping":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        if method == "roots/list":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {"roots": [{"uri": self.workspace.as_uri(), "name": "Ticket worktree"}]}}
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "This client does not provide sampling, elicitation or arbitrary server-initiated actions."}}

    async def _stdio_send(self, payload):
        assert self.process and self.process.stdin
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        if len(data) > MAX_MESSAGE:
            raise McpError("MCP request exceeds the supported size.")
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    def _headers(self) -> dict:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "MCP-Protocol-Version": self.version}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.config.api_key_env:
            value = os.getenv(self.config.api_key_env)
            if not value: raise McpError("Missing MCP credential environment variable: " + self.config.api_key_env)
            headers["Authorization"] = "Bearer " + value
        return headers

    async def _http_send(self, payload, expected_id=None) -> dict:
        assert self.http and self.config.url
        async with self.http.stream("POST", self.config.url, json=payload, headers=self._headers()) as response:
            if response.headers.get("Mcp-Session-Id"):
                self.session_id = response.headers["Mcp-Session-Id"]
            if response.status_code >= 400:
                raise McpError(f"MCP HTTP {response.status_code}: {(await response.aread()).decode(errors='replace')[:1000]}")
            if response.status_code == 202 or expected_id is None:
                await response.aread(); return {}
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                body = await response.aread()
                if len(body) > MAX_MESSAGE:
                    raise McpError("MCP response exceeds the supported size.")
                result = json.loads(body)
                if result.get("id") != expected_id:
                    raise McpError("MCP response ID does not match the request.")
                return result
            lines, total = [], 0
            async for line in response.aiter_lines():
                total += len(line)
                if total > MAX_MESSAGE:
                    raise McpError("MCP response stream exceeds the supported size.")
                if line.startswith("data:"):
                    lines.append(line[5:].lstrip())
                elif not line and lines:
                    result = json.loads("\n".join(lines)); lines = []
                    if result.get("id") == expected_id and "method" not in result:
                        return result
                    if "method" in result and "id" in result:
                        await self._http_send(await self._client_response(result))
            if lines:
                result = json.loads("\n".join(lines))
                if result.get("id") == expected_id:
                    return result
            raise McpError("MCP response stream ended without a matching result.")

    async def request(self, method: str, params: dict) -> dict:
        async with self.lock:
            self.counter += 1
            identity = self.counter
            payload = {"jsonrpc": "2.0", "id": identity, "method": method, "params": params}
            async with asyncio.timeout(60):
                if self.config.transport == "stdio":
                    if not self.process or self.process.returncode is not None:
                        raise McpError("MCP server process is not running.")
                    await self._stdio_send(payload)
                    assert self.process.stdout
                    while True:
                        raw = await self.process.stdout.readline()
                        if not raw:
                            raise McpError("MCP server exited before responding.")
                        if len(raw) > MAX_MESSAGE:
                            raise McpError("MCP response exceeds the supported size.")
                        response = json.loads(raw)
                        if "method" in response:
                            if "id" in response:
                                await self._stdio_send(await self._client_response(response))
                            continue
                        if response.get("id") == identity:
                            break
                else:
                    response = await self._http_send(payload, identity)
            if "error" in response:
                raise McpError(f"{method}: {response['error']}")
            return response.get("result", {})

    async def notify(self, method: str, params: dict):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if self.config.transport == "stdio":
            await self._stdio_send(payload)
        else:
            await self._http_send(payload)

    async def list_items(self, method: str, key: str) -> list[dict]:
        items, cursor = [], None
        for _ in range(100):
            result = await self.request(method, {"cursor": cursor} if cursor else {})
            items.extend(result.get(key, []))
            cursor = result.get("nextCursor")
            if not cursor:
                return items
        raise McpError("MCP pagination exceeded its supported limit.")

    async def close(self):
        if self.process:
            from .shell import ShellService
            await ShellService._kill(self.process)
        if self.stderr_task:
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)
        if self.http:
            if self.session_id and self.config.url:
                try:
                    await self.http.delete(self.config.url, headers=self._headers(), timeout=5)
                except httpx.HTTPError:
                    pass
            await self.http.aclose()
        self.initialized = False


class McpService:
    def __init__(self, configs: list[McpServerConfig], workspace: Path):
        self.configs, self.workspace = configs, workspace
        self.connections: dict[str, McpConnection] = {}
        self.tools: dict[str, dict] = {}
        self.resources: list[dict] = []
        self.errors: list[dict] = []
        self.loaded = False

    async def discover(self):
        if self.loaded:
            return
        self.loaded = True
        for config in self.configs:
            if not config.enabled:
                continue
            conn = McpConnection(config, self.workspace)
            try:
                await conn.start()
                discovered = {}
                for tool in await conn.list_items("tools/list", "tools"):
                    remote_name = tool["name"]
                    hash_ = hashlib.sha256(f"{config.id}\0{remote_name}".encode()).hexdigest()[:12]
                    alias = f"mcp_{config.id[:24]}_{hash_}"
                    if alias in self.tools or alias in discovered:
                        raise McpError("MCP server advertised a duplicate or colliding tool name.")
                    discovered[alias] = {"name": alias, "server": config.id, "remote_name": remote_name,
                        "description": f"[{config.id}; {config.scope} effect scope] {tool.get('description','')}",
                        "schema": tool.get("inputSchema", {"type": "object", "properties": {}}), "scope": config.scope}
                try:
                    self.resources.extend({**r, "server": config.id} for r in await conn.list_items("resources/list", "resources"))
                except McpError:
                    pass  # Servers may implement tools without resources.
                self.connections[config.id] = conn
                self.tools.update(discovered)
            except asyncio.CancelledError:
                await conn.close()
                self.loaded = False
                raise
            except Exception as exc:
                self.errors.append({"server": config.id, "error": str(exc)})
                await conn.close()
                self.connections.pop(config.id, None)

    async def call(self, alias: str, arguments: dict) -> dict:
        tool = self.tools[alias]
        return await self.connections[tool["server"]].request("tools/call", {"name": tool["remote_name"], "arguments": arguments})

    async def read_resource(self, server: str, uri: str) -> dict:
        if server not in self.connections:
            raise McpError("The requested server is not connected.")
        return await self.connections[server].request("resources/read", {"uri": uri})

    async def close(self):
        await asyncio.gather(*(conn.close() for conn in self.connections.values()), return_exceptions=True)
        self.connections.clear()
        self.tools.clear()
        self.resources.clear()
        self.errors.clear()
        self.loaded = False
