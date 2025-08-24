import textwrap
from pathlib import Path

import pytest

from core.config import load_config, ConfigError

BASE_CFG = textwrap.dedent("""
[USER]
name = Jarvis
telegram_user_id = 1

[INTEL]
api_key = key
absent_after_sec = 5

[TELEGRAM]
token = t

[PRESENCE]
enabled = true
camera_index = 0
frame_interval_ms = 1000
""")

def write_cfg(tmp_path, text=BASE_CFG):
    path = tmp_path / "config.ini"
    path.write_text(text, encoding="utf-8")
    return path

def test_load_config_success(tmp_path):
    cfg = load_config(write_cfg(tmp_path))
    assert cfg.user.name == "Jarvis"
    assert cfg.telegram.token == "t"

def test_missing_section(tmp_path):
    text = BASE_CFG.replace("[TELEGRAM]\n" "token = t\n\n", "")
    path = write_cfg(tmp_path, text)
    with pytest.raises(ConfigError):
        load_config(path)

def test_missing_option(tmp_path):
    text = BASE_CFG.replace("token = t\n", "")
    path = write_cfg(tmp_path, text)
    with pytest.raises(ConfigError):
        load_config(path)

def test_missing_file():
    with pytest.raises(ConfigError):
        load_config("nonexistent.ini")
