"""MAX global-search response normalization independent of Telegram UI.

No transport calls or state mutations are performed here.
"""
from app.max_client import MaxClient

def _contact_display_name(contact: dict) -> str:
    names = contact.get("names")
    if isinstance(names, list) and names:
        first = names[0].get("firstName", "")
        last = names[0].get("lastName", "")
        name = f"{first} {last}".strip() or names[0].get("name", "")
        if name:
            return str(name)
    first = contact.get("firstName") or contact.get("first_name") or ""
    last = contact.get("lastName") or contact.get("last_name") or ""
    return (f"{first} {last}".strip()
            or str(contact.get("friendly") or contact.get("displayName")
                   or contact.get("name") or contact.get("link") or ""))


def _normalize_global_search_results(resp: dict | None,
                                     max_client: MaxClient) -> list[dict]:
    if not isinstance(resp, dict):
        return []
    records: list[dict] = []
    seen: set[int] = set()
    for item in resp.get("result") or []:
        if not isinstance(item, dict):
            continue

        chat = item.get("chat")
        if isinstance(chat, dict):
            chat_id = chat.get("id")
            try:
                chat_id = int(chat_id)
            except (TypeError, ValueError):
                continue
            ctype = chat.get("type")
            if ctype not in ("CHAT", "CHANNEL"):
                continue
            if chat_id in seen:
                continue
            title = str(chat.get("title") or chat.get("name") or chat_id)
            records.append({
                "chat_id": chat_id,
                "type": ctype,
                "title": title,
                "link": chat.get("link") or "",
                "raw": chat,
            })
            seen.add(chat_id)
            continue

        wrapper = item.get("contact")
        if not isinstance(wrapper, dict):
            continue
        contact = wrapper.get("contact")
        if not isinstance(contact, dict):
            contact = wrapper
        contact_id = contact.get("id") or contact.get("userId")
        try:
            contact_id = int(contact_id)
        except (TypeError, ValueError):
            continue
        if max_client._my_id is not None and contact_id == int(max_client._my_id):
            continue
        try:
            chat_id = max_client.dialog_chat_id(contact_id)
        except (RuntimeError, TypeError, ValueError):
            continue
        if chat_id in seen:
            continue
        title = _contact_display_name(contact) or str(contact_id)
        records.append({
            "chat_id": chat_id,
            "contact_id": contact_id,
            "type": "DIALOG",
            "title": title,
            "link": contact.get("link") or "",
            "raw": contact,
        })
        seen.add(chat_id)

    return records
