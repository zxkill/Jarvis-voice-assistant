"""Тесты чтения конфигурации presence."""

import textwrap

from core.config import ConfigError, load_config


def test_presence_config_parsing(tmp_path):
    """Чтение параметров presence из INI-конфига."""
    cfg = tmp_path / "cfg.ini"
    cfg.write_text(
        textwrap.dedent(
            """
            [USER]
            name = u
            telegram_user_id = 0
            [INTEL]
            api_key = key
            absent_after_sec = 5
            [TELEGRAM]
            token = t
            [PRESENCE]
            enabled = true
            camera_index = 0
            frame_interval_ms = 200
            show_window = false
            frame_rotation = 180
            [QUIET]
            start = 23:00
            end = 08:00
            """
        )
    )
    app_cfg = load_config(str(cfg))
    assert app_cfg.presence.show_window is False
    assert app_cfg.presence.frame_rotation == 180


def test_invalid_rotation(tmp_path):
    """Неверное значение frame_rotation должно вызывать ошибку."""
    cfg = tmp_path / "cfg.ini"
    cfg.write_text(
        textwrap.dedent(
            """
            [USER]
            name = u
            telegram_user_id = 0
            [INTEL]
            api_key = key
            absent_after_sec = 5
            [TELEGRAM]
            token = t
            [PRESENCE]
            enabled = true
            camera_index = 0
            frame_interval_ms = 200
            show_window = true
            frame_rotation = 45
            [QUIET]
            start = 23:00
            end = 08:00
            """
        )
    )
    try:
        load_config(str(cfg))
    except ConfigError:
        pass
    else:  # pragma: no cover - требуется исключение
        raise AssertionError("ConfigError not raised")
