"""A tiny relay from this computer to the Family Hub, for macOS.

macOS's Local Network privacy blocks background apps (like AMO's server, started at login) from
reaching other devices on your Wi-Fi — they get "No route to host" — and never asks you about it.
Daemons started by the system aren't blocked, so `scripts/mac-hub-relay.sh install` runs this relay as
one: it listens on 127.0.0.1 only (nothing else on the network can use it) and passes each connection
straight through to the hub (AMO_HUB_REMOTE, your HUB_URL). AMO then talks to http://127.0.0.1:<port>.
"""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def target() -> tuple[str, int]:
    url = urlparse(os.environ.get("AMO_HUB_REMOTE") or os.environ.get("HUB_URL") or "")
    if not url.hostname:
        raise SystemExit("Set HUB_URL in AMO's .env to the hub's address (e.g. http://192.168.1.208:8770).")
    return url.hostname, url.port or 80


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def serve(port: int, host: str, hub_port: int) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, hub_port), 10)
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("can't reach the hub at %s:%s (%s)", host, hub_port, e)
            writer.close()
            return
        await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    log.info("relaying 127.0.0.1:%s → %s:%s", port, host, hub_port)
    async with server:
        await server.serve_forever()


def run(port: int) -> None:
    host, hub_port = target()
    asyncio.run(serve(port, host, hub_port))


if __name__ == "__main__":  # run standalone by the system service (a locked copy; see mac-hub-relay.sh)
    import argparse

    p = argparse.ArgumentParser(description="Relay 127.0.0.1:<port> to the Family Hub")
    p.add_argument("--port", type=int, default=8771)
    p.add_argument("--to", required=True, help="the hub, e.g. http://192.168.1.208:8770")
    a = p.parse_args()
    os.environ["AMO_HUB_REMOTE"] = a.to
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(a.port)
