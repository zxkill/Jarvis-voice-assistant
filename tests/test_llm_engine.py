from core import llm_engine
from core import llm_engine
from context import short_term, long_term


class DummyClient:
    def __init__(self):
        self.last_prompt = None
        self.last_profile = None

    def generate(self, prompt: str, profile: str = "light") -> str:
        self.last_prompt = prompt
        self.last_profile = profile
        return "ответ"


def clear_short_term():
    # Доступ к внутреннему буферу для очистки между тестами
    short_term._buffer.clear()


def test_think_uses_context_and_light_profile(monkeypatch):
    clear_short_term()
    dummy = DummyClient()
    monkeypatch.setattr(llm_engine, "_client", dummy)
    monkeypatch.setattr(long_term, "get_events_by_label", lambda label: ["факт"])
    result = llm_engine.think("тема")
    assert result == "ответ"
    assert dummy.last_profile == "light"
    assert "тема" in dummy.last_prompt
    assert "факт" in dummy.last_prompt
    assert short_term.get_last()[-1]["text"] == "ответ"


def test_summarise_saves_to_long_term(monkeypatch):
    clear_short_term()
    dummy = DummyClient()
    saved = []
    monkeypatch.setattr(llm_engine, "_client", dummy)
    monkeypatch.setattr(long_term, "add_daily_event", lambda text, labels: saved.append((text, list(labels))))
    result = llm_engine.summarise("текст", labels=["news"])
    assert result == "ответ"
    assert dummy.last_profile == "heavy"
    assert saved == [("ответ", ["news"])]


def test_mood_uses_history(monkeypatch):
    clear_short_term()
    dummy = DummyClient()
    history = ["хорошо"]
    saved = []
    monkeypatch.setattr(llm_engine, "_client", dummy)
    monkeypatch.setattr(long_term, "get_events_by_label", lambda label: history if label == "mood" else [])
    monkeypatch.setattr(long_term, "add_daily_event", lambda text, labels: saved.append(text))
    result = llm_engine.mood("радость")
    assert result == "ответ"
    assert "радость" in dummy.last_prompt
    assert "хорошо" in dummy.last_prompt
    assert saved == ["ответ"]
