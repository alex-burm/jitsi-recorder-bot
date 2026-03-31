#!/usr/bin/env python3.9
"""
Jitsi Audio Recording Bot

Usage:
    python3.9 bot.py --room myroom --output recording.wav
    python3.9 bot.py --room myroom --duration 60 --output recording.wav
    python3.9 bot.py --room myroom --host meet.jit.si --output out.wav
    python3.9 bot.py --room myroom --token "eyJ..." --output out.wav
"""

import argparse
import asyncio
import logging
import sys


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)
    # Quiet down noisy libs
    if not verbose:
        logging.getLogger("slixmpp").setLevel(logging.WARNING)
        logging.getLogger("aioice").setLevel(logging.WARNING)
        logging.getLogger("aiortc").setLevel(logging.WARNING)
        logging.getLogger("aiortc.rtcdtlstransport").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(
        description="Native Jitsi audio recording bot"
    )
    parser.add_argument(
        "--room",
        required=True,
        help="Jitsi room name (e.g. 'myroom123')",
    )
    parser.add_argument(
        "--output",
        default="recording.wav",
        help="Output WAV file path (default: recording.wav)",
    )
    parser.add_argument(
        "--host", "--server",
        dest="server",
        default="meet.jit.si",
        help="Jitsi host/domain (default: meet.jit.si). --server is kept as alias.",
    )
    parser.add_argument(
        "--xmpp-domain",
        default=None,
        help="Override XMPP auth domain (optional, e.g. guest.meet.example.com)",
    )
    parser.add_argument(
        "--conference-domain",
        default=None,
        help="Override MUC domain (optional, e.g. conference.meet.example.com)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Recording duration in seconds (default: until Ctrl+C)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="JWT token for authenticated servers",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose logging",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    logger = logging.getLogger("bot")
    logger.info(f"Jitsi Audio Bot starting")
    logger.info(f"  Server : {args.server}")
    if args.xmpp_domain:
        logger.info(f"  XMPP Domain: {args.xmpp_domain}")
    if args.conference_domain:
        logger.info(f"  Conference Domain: {args.conference_domain}")
    logger.info(f"  Room   : {args.room}")
    logger.info(f"  Output : {args.output}")
    if args.duration:
        logger.info(f"  Duration: {args.duration}s")

    from jitsi_bot import JitsiBot

    bot = JitsiBot(
        server=args.server,
        room_name=args.room,
        output_path=args.output,
        duration=args.duration,
        token=args.token,
        xmpp_domain=args.xmpp_domain,
        conference_domain=args.conference_domain,
    )

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

    logger.info(f"Done. Audio saved to: {args.output}")


if __name__ == "__main__":
    main()
