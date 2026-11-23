"""External personal bridge dotenv contract: synthetic credentials only."""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import load_settings

FAKE = (
    "MAX_TOKEN=synthetic-max-token\n"
    "MAX_DEVICE_ID=synthetic-device\n"
    "TG_BOT_TOKEN=111111:synthetic-telegram-token\n"
    "TG_CHAT_ID=-1001\n"
)


def fake_file(tmp_path: Path) -> Path:
    path = tmp_path / "private.env"
    path.write_text(FAKE)
    path.chmod(0o600)
    return path


def test_absolute_private_external_file_loads_without_repo_env(tmp_path):
    f = fake_file(tmp_path)
    with patch.dict(os.environ, {"MAX2TG_DOTENV_PATH": str(f)}, clear=True):
        cfg = load_settings()
        assert cfg.max_token == "synthetic-max-token"
        assert cfg.tg_chat_id == "-1001"


def test_existing_environment_values_have_precedence(tmp_path):
    f = fake_file(tmp_path)
    with patch.dict(os.environ, {"MAX2TG_DOTENV_PATH": str(f),
                                 "MAX_TOKEN": "already-configured"}, clear=True):
        assert load_settings().max_token == "already-configured"


def test_capsule_flag_blocks_external_personal_env_loading(tmp_path):
    f = fake_file(tmp_path)
    with patch.dict(os.environ, {
        "MAX2TG_NO_DOTENV": "1", "MAX2TG_DOTENV_PATH": str(f),
    }, clear=True):
        with pytest.raises(SystemExit, match="Missing required environment variables"):
            load_settings()
        assert os.environ.get("MAX_TOKEN") is None


@pytest.mark.parametrize("invalid", ["relative.env", "not/absolute.env"])
def test_explicit_path_must_be_absolute(invalid):
    with patch.dict(os.environ, {"MAX2TG_DOTENV_PATH": invalid}, clear=True):
        with pytest.raises(SystemExit, match="absolute regular file"):
            load_settings()


def test_missing_external_file_rejected_without_credential_values(tmp_path):
    missing = tmp_path / "missing.env"
    with patch.dict(os.environ, {"MAX2TG_DOTENV_PATH": str(missing)}, clear=True):
        with pytest.raises(SystemExit, match="not accessible") as exc:
            load_settings()
        assert str(missing) not in str(exc.value)


def test_symlink_not_accepted(tmp_path):
    actual = fake_file(tmp_path)
    link = tmp_path / "link.env"
    link.symlink_to(actual)
    with patch.dict(os.environ, {"MAX2TG_DOTENV_PATH": str(link)}, clear=True):
        with pytest.raises(SystemExit, match="absolute regular file"):
            load_settings()


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only permission policy")
def test_external_env_must_not_be_group_or_world_readable(tmp_path):
    f = fake_file(tmp_path)
    f.chmod(0o644)
    try:
        with patch.dict(os.environ, {"MAX2TG_DOTENV_PATH": str(f)}, clear=True):
            with pytest.raises(SystemExit, match="owner-only"):
                load_settings()
    finally:
        f.chmod(0o600)


def test_legacy_default_load_path_preserved():
    with patch("app.config.load_dotenv") as load, patch.dict(
        os.environ, {"MAX_TOKEN": "fake", "MAX_DEVICE_ID": "fake",
                     "TG_BOT_TOKEN": "fake", "TG_CHAT_ID": "42"}, clear=True,
    ):
        assert load_settings().tg_chat_id == "42"
        load.assert_called_once_with()
