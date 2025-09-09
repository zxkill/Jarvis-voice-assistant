import datetime as dt
import types
import sys
import importlib


def test_router_handles_today_question(monkeypatch):
    """Маршрутизатор должен выбирать календарный скилл."""
    # Подменяем зависимость pymorphy2, чтобы не тянуть словари
    class FakeMorph:
        def parse(self, word):
            return [types.SimpleNamespace(normal_form=word)]

    fake_morph = FakeMorph()
    monkeypatch.setitem(sys.modules, "pymorphy2", types.SimpleNamespace(MorphAnalyzer=lambda: fake_morph))

    # Теперь можно импортировать модуль маршрутизатора
    jarvis_skills = importlib.import_module("jarvis_skills")
    from jarvis_skills import handle_utterance, _loaded
    from skills import date_ru

    # Упрощаем нормализацию: вернём строку без изменений
    monkeypatch.setattr("core.nlp.normalize", lambda s: s)

    # Заглушки, чтобы не обращаться к реальным подсистемам
    sent: list[str] = []
    fake_voice = types.SimpleNamespace(send=lambda text: sent.append(text))
    monkeypatch.setitem(sys.modules, "notifiers.voice", fake_voice)
    monkeypatch.setattr(jarvis_skills.llm_engine, "summarise", lambda *a, **k: "summary")
    monkeypatch.setattr(jarvis_skills.daily_memory, "add", lambda *a, **k: None)
    monkeypatch.setattr("context.short_term.add", lambda *a, **k: None)

    # Фиксируем дату для маршрутизатора и самого скилла
    monkeypatch.setattr(jarvis_skills.current_date, "refresh", lambda: dt.date(2024, 5, 7))
    monkeypatch.setattr(date_ru.current_date, "refresh", lambda: dt.date(2024, 5, 7))

    # Регистрируем только календарный скилл
    _loaded[:] = [([p for p in date_ru.PATTERNS], date_ru.handle)]

    handled = handle_utterance("а какой сегодня день?")
    assert handled is True
    assert sent == ["7 мая, вторник"]

