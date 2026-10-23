import asyncio
import concurrent.futures
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

from telegram import BotCommand, Update

from app.config import load_settings
from app.max_listener import create_max_client
from app.tg_handler import build_tg_app, ensure_management_panel, ensure_quick_menu
from app.tg_sender import TelegramSender
from app.topics import TopicStore
from app.outbox import OutboxStore
from app.message_sync import MessageSync
from app.health import HealthSupervisor
from app.polling import bind_polling_to_max

threading.stack_size(524288)

log = logging.getLogger("max2tg")


class _SyncExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor that runs callables synchronously without spawning threads.

    Python 3.12 requires set_default_executor() to receive a ThreadPoolExecutor,
    so we subclass it and override submit() to bypass _adjust_thread_count().
    Used on low-resource servers where the OS cannot create new threads.
    DNS resolution (getaddrinfo) will block the event loop for a few ms,
    which is acceptable for a single-user forwarding bot.
    """

    def submit(self, fn, /, *args, **kwargs):
        f: concurrent.futures.Future = concurrent.futures.Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except Exception as exc:
            f.set_exception(exc)
        return f


async def main():
    loop = asyncio.get_running_loop()
    loop.set_default_executor(_SyncExecutor())

    settings = load_settings()

    level = logging.DEBUG if settings.debug else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    log_dir = os.environ.get("LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir, "max2tg.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    logging.basicConfig(level=level, handlers=[console_handler, file_handler], force=True)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING if not settings.debug else logging.DEBUG)

    log.info("Debug mode: %s", "ON" if settings.debug else "OFF")

    if settings.tg_proxy:
        log.info("Using Telegram proxy: %s", settings.tg_proxy.split("@")[-1])

    os.makedirs(settings.state_dir, exist_ok=True)
    topic_store = TopicStore(os.path.join(settings.state_dir, "topics.json"))

    outbox = OutboxStore(os.path.join(settings.state_dir, "outbox.sqlite3"))
    repaired = outbox.reconcile_mappings(topic_store)
    log.info("Persistent outbox initialized: %s (mappings repaired: %d)",
             outbox.counts(), repaired)
    sender = TelegramSender(settings.tg_bot_token, settings.tg_chat_id, topic_store,
                            proxy_url=settings.tg_proxy)
    sender.outbox = outbox
    await sender.start()

    client = create_max_client(
        settings.max_token, settings.max_device_id, sender, settings.max_chat_ids,
        debug=settings.debug,
    )
    sync = MessageSync(outbox,topic_store,sender.bot,settings.tg_chat_id)
    client.message_sync = sync
    client.on_mutation(sync.accept)
    log.info("MAX edit/delete/reaction event listener initialized")

    tg_app = None
    if settings.reply_enabled:
        tg_app = build_tg_app(settings.tg_bot_token, client, settings.tg_chat_id,
                              topic_store, allowed_user_id=settings.tg_allowed_user_id,
                              allowed_user_ids=settings.tg_allowed_user_ids,
                              admin_user_id=settings.tg_admin_user_id,
                              proxy_url=settings.tg_proxy)
        await tg_app.initialize()
        await tg_app.bot.set_my_commands([
            BotCommand("menu", "Действия с MAX-чатами"),
            BotCommand("search", "Найти человека, группу или канал MAX"),
            BotCommand("add", "Подключить чат/канал MAX по ссылке"),
            BotCommand("help", "Справка по bridge"),
            BotCommand("status", "Диагностика MAX2TG"),
            BotCommand("chatsettings", "Настройки текущего MAX-чата"),
        ])
        await tg_app.start()
        await ensure_management_panel(
            tg_app.bot, settings.tg_chat_id, topic_store,
        )
        await ensure_quick_menu(
            tg_app.bot, settings.tg_chat_id, topic_store,
        )
        log.info("Telegram updates will be processed after MAX is authorized")
    else:
        log.info("Reply to Max disabled (REPLY_ENABLED=false)")

    bind_polling_to_max(client,tg_app)
    health = HealthSupervisor(client,sender,outbox,topic_store,
                              tg_app,settings.state_dir)
    client.health = health
    health_task = asyncio.create_task(health.monitor(), name="max2tg-health")
    log.info("Starting Max listener...")
    try:
        await client.run()
    finally:
        health_task.cancel()
        await asyncio.gather(health_task,return_exceptions=True)
        log.info("Shutting down...")
        if tg_app:
            if tg_app.updater.running:
                await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        await sender.stop()
        outbox.close()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
