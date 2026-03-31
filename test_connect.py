#!/usr/bin/env python3.9
"""Quick connection test: connect to meet.jit.si and dump XMPP traffic."""

import asyncio
import logging
import sys

import websockets

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test")


async def test_xmpp_connect(server="meet.jit.si"):
    ws_url = f"wss://{server}/xmpp-websocket"
    logger.info(f"Connecting to {ws_url}")

    async with websockets.connect(
        ws_url,
        subprotocols=["xmpp"],
        additional_headers={"Origin": f"https://{server}"},
        open_timeout=15,
    ) as ws:
        logger.info("WebSocket connected!")

        # Send stream open
        open_stanza = (
            f'<open xmlns="urn:ietf:params:xml:ns:xmpp-framing" '
            f'to="{server}" version="1.0"/>'
        )
        logger.info(f"Sending: {open_stanza}")
        await ws.send(open_stanza)

        # Read first 5 stanzas
        for i in range(10):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                logger.info(f"[{i}] RECV: {msg[:500]}")

                if "ANONYMOUS" in msg:
                    # Send SASL ANONYMOUS auth
                    auth = '<auth xmlns="urn:ietf:params:xml:ns:xmpp-sasl" mechanism="ANONYMOUS"/>'
                    logger.info(f"Sending: {auth}")
                    await ws.send(auth)

                elif "<success" in msg:
                    # Restart stream
                    restart = (
                        f'<open xmlns="urn:ietf:params:xml:ns:xmpp-framing" '
                        f'to="{server}" version="1.0"/>'
                    )
                    logger.info(f"Auth success! Restarting stream: {restart}")
                    await ws.send(restart)

                elif "<bind" in msg and 'result' not in msg:
                    # Send bind
                    bind = (
                        '<iq xmlns="jabber:client" type="set" id="bind1">'
                        '<bind xmlns="urn:ietf:params:xml:ns:xmpp-bind">'
                        '<resource>recorder-test</resource>'
                        '</bind>'
                        '</iq>'
                    )
                    logger.info(f"Sending bind: {bind}")
                    await ws.send(bind)

                elif 'bind1' in msg and 'result' in msg:
                    logger.info("Bind successful! Connection test PASSED.")
                    return True

            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for stanza")
                break

    return False


if __name__ == "__main__":
    server = sys.argv[1] if len(sys.argv) > 1 else "meet.jit.si"
    result = asyncio.run(test_xmpp_connect(server))
    sys.exit(0 if result else 1)
