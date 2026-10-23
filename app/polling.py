"""Hold Telegram updates while MAX is offline to avoid dropping TG→MAX replies."""
from __future__ import annotations

import logging

from telegram import Update

log = logging.getLogger(__name__)


def bind_polling_to_max(client, tg_app):
    """Start/resume polling only after MAX is authorized and history is reconciled.

    Telegram retains unconsumed updates for a limited period (up to 24 hours).
    This protects short MAX interruptions without a second-direction outbox.
    """
    if tg_app is None:
        return
    old_ready=client._on_ready_cb
    old_disconnect=client._on_disconnect_cb
    client._polling_should_run=False

    async def ready(snapshot):
        if old_ready is not None:
            await old_ready(snapshot)
        if not getattr(client, "_authorized", False):
            return
        client._polling_should_run=True
        if not tg_app.updater.running:
            try:
                await tg_app.updater.start_polling(
                    drop_pending_updates=False,
                    allowed_updates=Update.ALL_TYPES,
                )
                log.info("Telegram polling resumed after MAX authorization")
            except Exception:
                log.exception("Could not resume Telegram polling after MAX auth")
                # HealthSupervisor will retry, without discarding updates.

    async def disconnected():
        client._polling_should_run=False
        if tg_app.updater.running:
            try:
                await tg_app.updater.stop()
                log.info("Telegram polling paused while MAX is disconnected")
            except Exception:
                log.exception("Failed to pause Telegram polling")
        if old_disconnect is not None:
            await old_disconnect()

    client._on_ready_cb=ready
    client._on_disconnect_cb=disconnected
