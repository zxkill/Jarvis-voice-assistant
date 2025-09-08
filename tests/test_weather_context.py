"""Проверка влияния погоды на контекст и настроение."""

from skills import weather_ru as wr


def test_calc_coefficients_sunny_warm():
    """Ясная и тёплая погода должна улучшать настроение."""
    val, ar = wr.calc_mood_coefficients(26, "ясно")
    assert val > 0
    assert ar > 0


def test_calc_coefficients_cold_rain():
    """Холодный дождь понижает настроение."""
    val, ar = wr.calc_mood_coefficients(-5, "сильный дождь")
    assert val < 0
    assert ar < 0


def test_handle_updates_context(monkeypatch):
    """Скилл публикует события и обновляет контекст."""
    events = []
    monkeypatch.setattr(wr.core_events, "publish", lambda ev: events.append(ev))
    ctx = []
    monkeypatch.setattr(wr, "ctx_add", lambda data: ctx.append(data))
    monkeypatch.setattr(wr, "_detect_offset", lambda text: 0)
    monkeypatch.setattr(wr, "_build_answer", lambda offset: "Сейчас 26 градусов, ясно")
    monkeypatch.setattr(wr, "_conditions", lambda offset: (26, "ясно"))
    wr.handle("погода")
    assert events and events[0].kind == "weather.update"
    assert ctx and ctx[0]["temperature"] == 26
    assert ctx[0]["condition"] == "ясно"
    assert ctx[0]["valence"] > 0
    assert ctx[0]["arousal"] > 0
