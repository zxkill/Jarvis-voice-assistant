"""Тесты для загрузки YAML-конфигураций из каталога config."""

from pathlib import Path

import yaml

import config


def test_affect_partial_and_defaults(tmp_path):
    """При отсутствии некоторых параметров берутся значения по умолчанию."""
    path = tmp_path / "affect.yaml"
    path.write_text("valence_factor: 2.5\n", encoding="utf-8")
    cfg = config.load_affect(path)
    assert cfg.valence_factor == 2.5
    assert cfg.arousal_factor == 1.0
    assert cfg.ema_alpha == 0.5


def test_display_missing_file(tmp_path):
    """Если файл отсутствует, возвращаются дефолтные настройки."""
    path = tmp_path / "display.yaml"  # файл не создаём
    cfg = config.load_display(path)
    assert cfg.driver == "console"
    assert cfg.serial_port is None


def test_proactive_custom(tmp_path):
    """Загрузка всех параметров из файла."""
    data = {
        "enabled": True,
        "suggestion_min_interval_min": 15,
        "force_telegram": True,
        "silence_window": {"start": "23:00", "end": "07:00"},
    }
    path = tmp_path / "proactive.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    cfg = config.load_proactive(path)
    assert cfg.enabled is True
    assert cfg.suggestion_min_interval_min == 15
    assert cfg.force_telegram is True
    assert cfg.silence_window == ("23:00", "07:00")


def test_proactive_defaults(tmp_path):
    """Отсутствие файла приводит к использованию безопасных значений."""
    path = tmp_path / "no_file.yaml"
    cfg = config.load_proactive(path)
    assert cfg.enabled is False
    assert cfg.suggestion_min_interval_min == 60
    assert cfg.force_telegram is False
    assert cfg.silence_window is None
