"""Проверка подсказок по одежде в модуле :mod:`skills.weather_ru`."""

from skills import weather_ru as wr


def test_clothing_and_umbrella(monkeypatch):
    """При низкой температуре и дожде советует тёплую одежду и зонт."""
    # Подменяем функции, чтобы не обращаться к сети
    monkeypatch.setattr(wr, "_detect_offset", lambda text: 0)
    monkeypatch.setattr(wr, "_build_answer", lambda offset: "Сейчас 5 градусов, дождь")
    monkeypatch.setattr(wr, "_conditions", lambda offset: (5, "умеренный дождь"))
    monkeypatch.setattr(wr.core_events, "publish", lambda e: None)
    monkeypatch.setattr(wr, "ctx_add", lambda data: None)
    result = wr.handle("погода")
    assert "зонт" in result.lower()
    assert "куртк" in result.lower()
