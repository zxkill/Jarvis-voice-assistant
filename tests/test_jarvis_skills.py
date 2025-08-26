from core.request_source import (
    set_request_source,
    reset_request_source,
    get_request_source,
)


def test_handle_utterance_uses_voice_notifier_for_telegram(monkeypatch):
    """Убедиться, что ответ скилла отправляется через нотифайер,
    который далее переведёт его в текст для Telegram."""
    import types, sys

    # Подменяем модуль core.nlp, чтобы не тянуть тяжёлые зависимости
    # (pymorphy2) при импорте jarvis_skills.
    fake_nlp = types.SimpleNamespace(normalize=lambda s: s)
    monkeypatch.setitem(sys.modules, "core.nlp", fake_nlp)

    import jarvis_skills

    # Создаём заглушку для notifiers.voice, чтобы не импортировать реальные
    # зависимости TTS.  Функция send сохраняет текст и источник запроса.
    sent: list[tuple[str, str]] = []

    def fake_send(text: str, *, pitch=None, speed=None, emotion=None):
        sent.append((text, get_request_source()))

    fake_voice = types.SimpleNamespace(send=fake_send)
    monkeypatch.setitem(sys.modules, "notifiers.voice", fake_voice)

    # Подменяем список зарегистрированных скиллов на один тестовый.
    def fake_skill(text: str) -> str:
        return "ответ"

    monkeypatch.setattr(jarvis_skills, "_loaded", [(["тест"], fake_skill)])

    token = set_request_source("telegram")
    try:
        assert jarvis_skills.handle_utterance("тест") is True
    finally:
        reset_request_source(token)

    assert sent == [("ответ", "telegram")]
