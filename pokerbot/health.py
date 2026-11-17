"""HTTP health endpoint for external uptime pollers.

The bot talks to Discord over a websocket, so it has no inbound HTTP surface
of its own. This module adds a deliberately small one: a single ``GET /health``
route that answers ``200`` while the Discord client is connected and ``503``
while it is starting, reconnecting, or shutting down.

Only the standard library is used. The endpoint is intentionally **not** rate
limited: uptime pollers and container/orchestrator health checks must be able
to ask "are you alive?" as often as they like, and throttling them causes
false outages and restart loops. Any future route should get a limiter;
``/health`` should stay exempt.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)

# A poller that connects and then goes quiet must not pin a task forever.
_READ_TIMEOUT = 5.0
# Cap the request head so a single client cannot make us buffer indefinitely.
_STREAM_LIMIT = 16 * 1024
_MAX_HEADER_LINES = 64

_REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    503: "Service Unavailable",
}

# Returns (healthy, details). Details are merged into the JSON body.
StatusCallback = Callable[[], tuple[bool, dict[str, Any]]]


class HealthServer:
    """A minimal HTTP/1.1 server exposing ``GET /health``."""

    def __init__(
        self,
        status: StatusCallback,
        *,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self._status = status
        self._host = host
        self._port = port
        self._server: asyncio.AbstractServer | None = None

    @property
    def port(self) -> int:
        """The bound port (differs from the configured one when it was 0)."""
        if self._server is None or not self._server.sockets:
            return self._port
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        """Bind and start serving. Raises ``OSError`` if the port is taken."""
        self._server = await asyncio.start_server(
            self._handle,
            self._host,
            self._port,
            limit=_STREAM_LIMIT,
            backlog=16,
        )
        logger.info(
            "Health endpoint listening on http://%s:%s/health",
            self._host,
            self.port,
        )

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        with suppress(Exception):
            await server.wait_closed()

    # -------------------------------------------------------------- handling
    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            # One deadline for the whole request head, so a slowloris client
            # cannot hold a task open by dribbling one header line at a time.
            request_line = await asyncio.wait_for(
                self._read_head(reader), _READ_TIMEOUT
            )
            if request_line is None:
                return

            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                await self._respond(
                    writer,
                    400,
                    {"service": "pokerbot", "status": "bad request"},
                )
                return

            method, target = parts[0].upper(), parts[1]
            path = target.split("?", 1)[0]

            # "/" is an alias because many platform and container health
            # checks probe the root path by default.
            if path not in ("/health", "/"):
                await self._respond(
                    writer,
                    404,
                    {"service": "pokerbot", "status": "not found"},
                )
                return
            if method not in ("GET", "HEAD"):
                await self._respond(
                    writer,
                    405,
                    {
                        "service": "pokerbot",
                        "status": "method not allowed",
                    },
                    head=False,
                    extra_headers=(("Allow", "GET, HEAD"),),
                )
                return

            await self._respond(writer, *self._health(), head=method == "HEAD")
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except ValueError:
            # Request head exceeded the stream limit; drop the connection.
            logger.debug("Dropped an oversized health request")
        except Exception:
            logger.debug("Health request failed", exc_info=True)
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _read_head(
        self, reader: asyncio.StreamReader
    ) -> bytes | None:
        """Read the request line and drain the headers above it."""
        request_line = await reader.readline()
        if not request_line:
            return None
        for _ in range(_MAX_HEADER_LINES):
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        return request_line

    def _health(self) -> tuple[int, dict[str, Any]]:
        try:
            healthy, details = self._status()
        except Exception:
            logger.exception("Health status callback failed")
            return 503, {
                "service": "pokerbot",
                "status": "unavailable",
                "error": "status unavailable",
            }
        body = {
            "service": "pokerbot",
            "status": "ok" if healthy else "unavailable",
            **details,
        }
        return (200 if healthy else 503), body

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict[str, Any],
        *,
        head: bool = False,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        headers = [
            f"HTTP/1.1 {status} {_REASONS.get(status, 'Unknown')}",
            "Content-Type: application/json; charset=utf-8",
            # HEAD must advertise the length of the body it omits.
            f"Content-Length: {len(body)}",
            "Cache-Control: no-store",
            "Connection: close",
        ]
        headers.extend(f"{name}: {value}" for name, value in extra_headers)
        data = ("\r\n".join(headers) + "\r\n\r\n").encode("latin-1")
        writer.write(data if head else data + body)
        await writer.drain()
