"""Process entrypoint.

Shutdown order matters and is enforced here plus in :meth:`AcmHubBot.close`:
stop the scheduler so no new work starts, close the Discord gateway, close the
collectors' HTTP clients, then dispose the database engine. Doing it in any
other order can leave a job mid-flight writing to a disposed engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

from pydantic import ValidationError

from acm_hub.config.settings import Settings, get_settings
from acm_hub.db.session import Database
from acm_hub.observability.logging import configure_logging, get_logger

logger = get_logger(__name__)


def load_settings() -> Settings:
    """Load settings, reporting configuration problems in a readable form.

    Raises:
        SystemExit: when configuration is invalid, so the container restarts
            with a clear reason instead of half-running.
    """
    try:
        return get_settings()
    except ValidationError as exc:
        # Written directly to stderr rather than logged: logging is configured
        # from these very settings, so it does not exist yet. Only field names
        # and pydantic's messages are shown, never the offending values, so a
        # malformed token cannot end up in a container log.
        lines = ["ACM Hub configuration is invalid:"]
        lines += [
            f"  - {'.'.join(str(part) for part in error['loc']).upper()}: {error['msg']}"
            for error in exc.errors()
        ]
        lines.append("\nSee .env.example for the full list of required variables.")
        sys.stderr.write("\n".join(lines) + "\n")
        raise SystemExit(2) from exc


async def run_bot(settings: Settings) -> None:
    """Start the bot and keep it running until a shutdown signal arrives."""
    from acm_hub.bot.client import AcmHubBot

    database = Database(settings.database_url)
    bot = AcmHubBot(settings, database)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def request_shutdown(signal_name: str) -> None:
        logger.info("shutdown.signal_received", signal=signal_name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows has no signal handlers
            loop.add_signal_handler(sig, request_shutdown, sig.name)

    start_task = asyncio.create_task(bot.start(settings.discord_token.get_secret_value()))
    stop_task = asyncio.create_task(stop_event.wait())

    done, pending = await asyncio.wait({start_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    if not bot.is_closed():
        await bot.close()
    else:
        # The gateway task ended on its own; the remaining resources still need
        # releasing because AcmHubBot.close() never ran.
        await database.dispose()

    for task in done:
        if task is start_task and not task.cancelled():
            # Re-raise a gateway failure (bad token, forbidden intents) so the
            # process exits non-zero and the supervisor notices.
            task.result()


def run() -> None:
    """Console-script entrypoint."""
    settings = load_settings()
    configure_logging(level=settings.log_level, log_format=settings.log_format)
    logger.info(
        "bot.starting",
        guild_id=settings.discord_guild_id,
        scheduler_enabled=settings.scheduler_enabled,
    )
    try:
        asyncio.run(run_bot(settings))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        logger.info("shutdown.keyboard_interrupt")


if __name__ == "__main__":
    run()
