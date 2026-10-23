"""Persistent per-chat delivery preferences for personal MAX2TG.

Do not store secret data or private message bodies. All existing chats
default to their previous behavior. Invalid persisted flags fail safe.
"""
from __future__ import annotations

from dataclasses import dataclass

KEY="chat_preferences"
HISTORY_CHOICES=(0,20,50,100)
DEFAULT_HISTORY=20
BOOL_FIELDS=("sync","mute","media","archived","title_sync")


@dataclass(frozen=True)
class ChatPreferences:
    sync: bool = True
    mute: bool = False
    media: bool = True
    archived: bool = False
    title_sync: bool = False
    history_depth: int | None = None

    def effective_history(self, global_limit: int = DEFAULT_HISTORY) -> int:
        value=global_limit if self.history_depth is None else self.history_depth
        try: value=int(value)
        except (ValueError,TypeError,OverflowError):value=DEFAULT_HISTORY
        return max(0,min(value,100))


def get_chat_prefs(store,chat_id) -> ChatPreferences:
    all_prefs=store.get_ui(KEY,{}) or {}
    values=all_prefs.get(str(chat_id),{}) if isinstance(all_prefs,dict) else {}
    if not isinstance(values,dict):
        values={}
    safe={key:values[key] if type(values.get(key)) is bool
          else getattr(ChatPreferences(),key) for key in BOOL_FIELDS}
    depth=values.get("history_depth")
    safe["history_depth"]=depth if type(depth) is int and depth in HISTORY_CHOICES else None
    return ChatPreferences(**safe)


def set_chat_pref(store,chat_id,field,value) -> ChatPreferences:
    if field not in (*BOOL_FIELDS,"history_depth"):
        raise ValueError("Unknown chat preference")
    if field=="history_depth":
        if value is not None and (type(value) is not int or value not in HISTORY_CHOICES):
            raise ValueError("Unsupported history depth")
    elif type(value) is not bool:
        raise ValueError("Only boolean setting accepted")
    all_prefs=dict(store.get_ui(KEY,{}) or {})
    chat_key=str(chat_id)
    current=dict(all_prefs.get(chat_key,{}) or {})
    # Keep overrides only when necessary; no migration/downtime.
    current[field]=value
    all_prefs[chat_key]=current
    store.set_ui(KEY,all_prefs)
    return get_chat_prefs(store,chat_id)


def remove_chat_prefs(store,chat_id):
    all_prefs=dict(store.get_ui(KEY,{}) or {})
    if all_prefs.pop(str(chat_id),None) is not None:
        store.set_ui(KEY,all_prefs)
