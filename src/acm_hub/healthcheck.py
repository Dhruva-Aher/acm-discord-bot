"""Container health probe.

ACM Hub is a gateway client, not an HTTP server, so there is no port to poll.
The meaningful liveness question is "can this process still load its
configuration and reach its database", which is what this checks. It exits 0
when healthy and 1 otherwise, which is the contract Docker's ``HEALTHCHECK``
and most orchestrators expect.
"""

from __future__ import annotations

import asyncio
import sys

from pydantic import ValidationError

from acm_hub.config.settings import get_settings
from acm_hub.db.session import Database


async def check() -> bool:
    """Return whether configuration is valid and the database answers."""
    try:
        settings = get_settings()
    except ValidationError as exc:
        sys.stderr.write(f"configuration invalid: {exc.error_count()} problem(s)\n")
        return False

    database = Database(settings.database_url)
    try:
        return await database.check_connection()
    finally:
        await database.dispose()


def main() -> int:
    return 0 if asyncio.run(check()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
