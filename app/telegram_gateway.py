"""P2.2 one-bot Telegram ingress, not wired into personal launchd.

One Application receives Telegram updates. Each update is authorized and
durably journaled before being forwarded to its session over authenticated
local IPC. Worker credentials and session state are not shared in the
routing registry. The outgoing-bot API is a later integration stage.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable,Callable

from app.telegram_inbox import TelegramInbox
from app.telegram_ipc import CapsuleIpcClient
from app.telegram_control_plane import TelegramControlPlane

log=logging.getLogger(__name__)


class TelegramGatewayEngine:
    def __init__(self,plane:TelegramControlPlane,inbox:TelegramInbox,
                 deliver:Callable[[str,dict],Awaitable[bool]]|None=None):
        self.plane=plane
        self.inbox=inbox
        self.deliver=deliver or CapsuleIpcClient(plane.capsules).deliver
        self._locks={}

    async def accept(self,raw_update:dict) -> str:
        """No network if route invalid. Owner-only forum dispatch."""
        admitted=self.inbox.ingest(raw_update)
        if admitted is None:
            return "DENIED"
        update_id,session=admitted
        async with self._locks.setdefault(session,asyncio.Lock()):
            return await self.inbox.dispatch(update_id,self.deliver)

    async def drain(self,limit:int=100)->dict:
        """Replay only definitely unsent RECEIVED rows after recovery."""
        results={}
        for update_id in self.inbox.ready(limit):
            row=self.inbox.row(update_id)
            session=row["session_id"]
            async with self._locks.setdefault(session,asyncio.Lock()):
                result=await self.inbox.dispatch(update_id,self.deliver)
                results[result]=results.get(result,0)+1
        return results


def build_telegram_application(token:str,engine:TelegramGatewayEngine):
    """One PTB getUpdates consumer; caller must explicitly start it.

    This is a runnable ingress/control adapter, but the P2.1 workers are NOT
    yet wired to the shared bot for outbound API calls; see P2.2 roadmap.
    """
    from telegram import Update
    from telegram.ext import ApplicationBuilder,TypeHandler

    app=ApplicationBuilder().token(token).concurrent_updates(False).build()

    async def process(update,context):
        chat=update.effective_chat
        actor=update.effective_user
        msg=update.effective_message
        if chat and chat.type=="private":
            # Owner claims only in authenticated private chat. Never accept
            # an onboarding claim posted in a group or forwarded text.
            if not actor or int(chat.id)!=int(actor.id):
                return
            if msg is None or not getattr(msg,"text",None):
                return
            parts=msg.text.split(maxsplit=1)
            if len(parts)==2 and parts[0].split("@")[0]=="/start":
                try:
                    engine.plane.claim_owner(actor.id,parts[1].strip())
                except (ValueError,PermissionError):
                    await msg.reply_text("Приглашение недействительно или уже использовано.")
                else:
                    await msg.reply_text("Telegram подключён. MAX-сессия будет доступна после настройки.")
            elif msg.text.startswith("/start") or msg.text.startswith("/status"):
                sessions=engine.plane.accessible_capsules(actor.id)
                await msg.reply_text(
                    "MAX2TG: подключено сессий — "+str(len(sessions))+
                    ". Для привязки используй персональное приглашение.")
            return
        if chat is None or actor is None:
            return
        try:
            result=await engine.accept(update.to_dict())
        except Exception:
            log.exception("Telegram gateway ingestion failed")
            return
        if result in ("UNKNOWN","BLOCKED"):
            log.warning("Telegram gateway item needs operator review status=%s",
                        result)

    app.add_handler(TypeHandler(Update,process))
    return app
