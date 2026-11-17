"""Tests for the HTTP health endpoint."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from pokerbot import config
from pokerbot.bot import PokerBot
from pokerbot.health import HealthServer


async def request(port: int, raw: bytes) -> tuple[int, dict[str, str], bytes]:
    """Send a raw HTTP request and return (status, headers, body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(raw)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 5)
    finally:
        writer.close()
        await writer.wait_closed()

    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers, body


class HealthServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.healthy = True
        self.details = {"bot": "online"}
        self.status_error = False
        self.server = HealthServer(self._status, host="127.0.0.1", port=0)
        await self.server.start()
        self.addAsyncCleanup(self.server.close)

    def _status(self) -> tuple[bool, dict[str, str]]:
        if self.status_error:
            raise RuntimeError("boom")
        return self.healthy, self.details

    async def test_healthy_returns_200(self) -> None:
        status, headers, body = await request(
            self.server.port, b"GET /health HTTP/1.1\r\nHost: test\r\n\r\n"
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            headers["content-type"], "application/json; charset=utf-8"
        )
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["connection"], "close")

        payload = json.loads(body)
        self.assertEqual(payload["service"], "pokerbot")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["bot"], "online")

    async def test_not_ready_returns_503(self) -> None:
        self.healthy = False
        self.details = {"bot": "offline"}

        status, _, body = await request(
            self.server.port, b"GET /health HTTP/1.1\r\n\r\n"
        )

        self.assertEqual(status, 503)
        parsed = json.loads(body)
        self.assertEqual(parsed["status"], "unavailable")
        self.assertEqual(parsed["bot"], "offline")

    async def test_query_string_is_ignored(self) -> None:
        status, _, _ = await request(
            self.server.port, b"GET /health?probe=1 HTTP/1.1\r\n\r\n"
        )
        self.assertEqual(status, 200)

    async def test_root_is_an_alias(self) -> None:
        status, _, body = await request(
            self.server.port, b"GET / HTTP/1.1\r\n\r\n"
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    async def test_unknown_path_returns_404(self) -> None:
        status, _, body = await request(
            self.server.port, b"GET /metrics HTTP/1.1\r\n\r\n"
        )
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["status"], "not found")

    async def test_post_returns_405(self) -> None:
        status, headers, _ = await request(
            self.server.port, b"POST /health HTTP/1.1\r\n\r\n"
        )
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, HEAD")

    async def test_head_omits_body_but_reports_length(self) -> None:
        get_status, get_headers, get_body = await request(
            self.server.port, b"GET /health HTTP/1.1\r\n\r\n"
        )
        head_status, head_headers, head_body = await request(
            self.server.port, b"HEAD /health HTTP/1.1\r\n\r\n"
        )

        self.assertEqual(get_status, 200)
        self.assertEqual(head_status, 200)
        self.assertEqual(head_body, b"")
        self.assertEqual(
            head_headers["content-length"], get_headers["content-length"]
        )
        self.assertEqual(int(head_headers["content-length"]), len(get_body))

    async def test_status_failure_reports_503(self) -> None:
        self.status_error = True

        with self.assertLogs("pokerbot.health", level="ERROR"):
            status, _, body = await request(
                self.server.port, b"GET /health HTTP/1.1\r\n\r\n"
            )

        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["status"], "unavailable")

    async def test_health_is_not_rate_limited(self) -> None:
        """Pollers must never get a 429, however often they ask."""
        responses = await asyncio.gather(
            *(
                request(self.server.port, b"GET /health HTTP/1.1\r\n\r\n")
                for _ in range(40)
            )
        )

        self.assertEqual([status for status, _, _ in responses], [200] * 40)

    async def test_silent_client_does_not_block_others(self) -> None:
        _idle_reader, idle_writer = await asyncio.open_connection(
            "127.0.0.1", self.server.port
        )
        try:
            status, _, _ = await request(
                self.server.port, b"GET /health HTTP/1.1\r\n\r\n"
            )
            self.assertEqual(status, 200)
        finally:
            idle_writer.close()
            await idle_writer.wait_closed()


class _User:
    def __str__(self) -> str:
        return "PokerBot#0001"


class HealthReportTests(unittest.TestCase):
    """The bot-side snapshot, without building a Discord client or database."""

    def report(self, **state: object) -> tuple[bool, dict[str, object]]:
        stub = SimpleNamespace(
            is_ready=lambda: state.get("ready", True),
            is_closed=lambda: state.get("closed", False),
            _closing=state.get("closing", False),
            user=_User(),
            guilds=[object(), object()],
            games={1: object()},
            _started_at=time.monotonic() - 12.5,
            ws=SimpleNamespace(latency=state.get("latency", 0.05)),
        )
        return PokerBot._health_report(stub)  # type: ignore[arg-type]

    def test_connected_bot_is_online_with_latency(self) -> None:
        healthy, report = self.report()

        self.assertTrue(healthy)
        self.assertEqual(report["bot"], "online")
        self.assertEqual(report["user"], "PokerBot#0001")
        self.assertEqual(report["guilds"], 2)
        self.assertEqual(report["tables"], 1)
        self.assertGreaterEqual(report["uptime_seconds"], 12)
        self.assertEqual(report["latency_ms"], 50.0)

    def test_nan_latency_is_omitted(self) -> None:
        # Latency is nan until the first heartbeat, and nan is not JSON.
        _, report = self.report(latency=float("nan"))
        self.assertNotIn("latency_ms", report)

    def test_no_websocket_is_omitted(self) -> None:
        stub = SimpleNamespace(
            is_ready=lambda: False,
            is_closed=lambda: False,
            _closing=False,
            user=None,
            guilds=[],
            games={},
            _started_at=time.monotonic(),
            ws=None,
        )
        healthy, report = PokerBot._health_report(stub)  # type: ignore[arg-type]

        self.assertFalse(healthy)
        self.assertEqual(report["bot"], "offline")
        self.assertNotIn("latency_ms", report)

    def test_shutting_down_is_unhealthy(self) -> None:
        healthy, report = self.report(closing=True)

        self.assertFalse(healthy)
        self.assertEqual(report["bot"], "offline")
        self.assertTrue(report["shutting_down"])
        self.assertNotIn("latency_ms", report)


class PortConfigTests(unittest.TestCase):
    """The injected PORT must win over the local HEALTH_PORT fallback."""

    def tearDown(self) -> None:
        # Reload with the real environment so later imports see normal values.
        importlib.reload(config)

    def reload_port(self, **env: str) -> int:
        clean = {
            name: value
            for name, value in os.environ.items()
            if name not in ("PORT", "HEALTH_PORT")
        }
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=True):
            return importlib.reload(config).HEALTH_PORT

    def test_injected_port_wins(self) -> None:
        self.assertEqual(
            self.reload_port(PORT="10000", HEALTH_PORT="9000"), 10000
        )

    def test_health_port_is_the_fallback(self) -> None:
        self.assertEqual(self.reload_port(HEALTH_PORT="9000"), 9000)

    def test_default_is_8080(self) -> None:
        self.assertEqual(self.reload_port(), 8080)

    def test_invalid_ports_fall_back(self) -> None:
        self.assertEqual(self.reload_port(PORT="not-a-port"), 8080)
        self.assertEqual(self.reload_port(PORT="70000"), 8080)
        self.assertEqual(self.reload_port(PORT="0"), 8080)


if __name__ == "__main__":
    unittest.main()
