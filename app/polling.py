"""Continuous Telegram polling with measured transport health.

The outbound ledger queues messages while MAX is offline. Do not cancel
Telegram long polls on MAX reconnects: cancellation can strand HTTP
connections and the Updater's running flag does not establish liveness.
"""
from __future__ import annotations

import logging
import time
from telegram import Update
from telegram.request import HTTPXRequest

log = logging.getLogger(__name__)
POLL_STALE_SEC = 90


class TrackedPollingRequest(HTTPXRequest):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_success = None
        self.started_at = time.monotonic()

    async def do_request(self, *args, **kwargs):
        result = await super().do_request(*args, **kwargs)
        # This request object is exclusively used for getUpdates. A successful
        # empty long-poll response is also proof of a working receive path.
        if result[0] == 200:
            self.last_success = time.monotonic()
        return result

    def age(self):
        return max(0, time.monotonic() - (self.last_success or self.started_at))

    def stale(self):
        return self.age() > POLL_STALE_SEC


def bind_polling_to_max(client, tg_app):
    """Keep polling active across MAX disconnects; durable sends wait in SQLite."""
    if tg_app is None:
        return
    old_ready = client._on_ready_cb
    old_disconnect = client._on_disconnect_cb
    client._polling_should_run = True

    async def ready(snapshot):
        if old_ready is not None:
            await old_ready(snapshot)
        if not tg_app.updater.running:
            try:
                await tg_app.updater.start_polling(
                    drop_pending_updates=False,
                    allowed_updates=Update.ALL_TYPES,
                )
                log.info("Telegram polling started")
            except Exception:
                log.exception("Could not start Telegram polling")

    async def disconnected():
        # No stop/restart of the Telegram transport on a MAX-only outage.
        # Telegram updates remain consumable and outbound texts are persisted.
        if old_disconnect is not None:
            await old_disconnect()

    client._on_ready_cb = ready
    client._on_disconnect_cb = disconnected
