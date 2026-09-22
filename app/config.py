import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    max_token: str
    max_device_id: str
    tg_bot_token: str
    tg_chat_id: str
    max_chat_ids: str | None = None
    tg_proxy: str | None = None
    debug: bool = False
    reply_enabled: bool = False
    state_dir: str = "state"
    tg_allowed_user_id: int | None = None
    tg_allowed_user_ids: frozenset[int] = frozenset()
    tg_admin_user_id: int | None = None


def load_settings() -> Settings:
    load_dotenv()

    required = ["MAX_TOKEN", "MAX_DEVICE_ID", "TG_BOT_TOKEN", "TG_CHAT_ID"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in the values."
        )

    tg_chat_id = os.environ["TG_CHAT_ID"]
    try:
        int(tg_chat_id)
    except ValueError:
        raise SystemExit(
            f"TG_CHAT_ID must be a valid integer, got: {tg_chat_id!r}"
        )

    allowed_raw = os.environ.get("TG_ALLOWED_USER_ID") or None
    allowed_many_raw = os.environ.get("TG_ALLOWED_USER_IDS") or ""
    admin_raw = os.environ.get("TG_ADMIN_USER_ID") or None
    allowed_user_id: int | None = None
    if allowed_raw:
        try:
            allowed_user_id = int(allowed_raw)
        except ValueError:
            raise SystemExit(
                f"TG_ALLOWED_USER_ID must be a valid integer, got: {allowed_raw!r}"
            )

    allowed_user_ids: set[int] = set()
    if allowed_user_id is not None:
        allowed_user_ids.add(allowed_user_id)
    for raw in allowed_many_raw.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            allowed_user_ids.add(int(raw))
        except ValueError:
            raise SystemExit(
                f"TG_ALLOWED_USER_IDS must be comma-separated integers, got: {allowed_many_raw!r}"
            )

    admin_user_id: int | None = None
    if admin_raw:
        try:
            admin_user_id = int(admin_raw)
        except ValueError:
            raise SystemExit(
                f"TG_ADMIN_USER_ID must be a valid integer, got: {admin_raw!r}"
            )
        allowed_user_ids.add(admin_user_id)

    return Settings(
        max_token=os.environ["MAX_TOKEN"],
        max_device_id=os.environ["MAX_DEVICE_ID"],
        tg_bot_token=os.environ["TG_BOT_TOKEN"],
        tg_chat_id=tg_chat_id,
        max_chat_ids=os.environ.get("MAX_CHAT_IDS") or None,
        tg_proxy=os.environ.get("TG_PROXY") or None,
        debug=os.environ.get("DEBUG", "").lower() in ("1", "true", "yes"),
        reply_enabled=os.environ.get("REPLY_ENABLED", "").lower() in ("1", "true", "yes"),
        state_dir=os.environ.get("STATE_DIR") or "state",
        tg_allowed_user_id=allowed_user_id,
        tg_allowed_user_ids=frozenset(allowed_user_ids),
        tg_admin_user_id=admin_user_id,
    )
