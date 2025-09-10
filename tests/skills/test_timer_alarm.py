"""Тесты для вспомогательных функций timer_alarm."""

import importlib


def test_parse_duration_words(monkeypatch):
    monkeypatch.setenv("INTEL_API_KEY", "test")
    monkeypatch.setenv("TELEGRAM_TOKEN", "x")
    ta = importlib.import_module("skills.timer_alarm")
    sec, label = ta._parse_duration("поставь таймер на пять минут чай", "таймер")
    assert sec == 300
    assert label == "чай"

