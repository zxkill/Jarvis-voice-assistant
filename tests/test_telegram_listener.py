import sys
from types import SimpleNamespace

import pytest
import requests
import asyncio
import threading
import time


class DummyResp:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._data


def _load_listener(monkeypatch):
    cfg = SimpleNamespace(
        telegram=SimpleNamespace(token="TOKEN"),
        user=SimpleNamespace(telegram_user_id=123),
    )
    dummy_cmd = SimpleNamespace()
    async def _dummy_va(_):
        return None
    dummy_cmd.va_respond = _dummy_va
    monkeypatch.setitem(sys.modules, "app.command_processing", dummy_cmd)
    monkeypatch.setattr("core.config.load_config", lambda: cfg)
    monkeypatch.delitem(sys.modules, "notifiers.telegram_listener", raising=False)
    import notifiers.telegram_listener as tl
    return tl


def test_listener_requires_token(monkeypatch):
    """При отсутствии токена импорт должен завершаться ошибкой."""

    cfg = SimpleNamespace(
        telegram=SimpleNamespace(token=""),
        user=SimpleNamespace(telegram_user_id=0),
    )
    monkeypatch.setattr("core.config.load_config", lambda: cfg)
    monkeypatch.delitem(sys.modules, "notifiers.telegram_listener", raising=False)

    with pytest.raises(RuntimeError):
        import notifiers.telegram_listener  # noqa: F401


def test_listener_processes_and_updates_offset(monkeypatch):
    """Проверяем корректный разбор обновлений и продвижение offset."""
    tl = _load_listener(monkeypatch)
    calls = []

    async def fake_va(text):
        # Сохраняем полученную команду для проверки.
        calls.append(text)

    metrics = {"count": 0}

    def fake_inc(name):
        # Подсчитываем количество обработанных сообщений.
        metrics["count"] += 1

    responses = [
        DummyResp(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 5,
                        "message": {"chat": {"id": 123}, "text": "cmd"},
                    }
                ],
            }
        ),
        DummyResp({"ok": True, "result": []}),
    ]
    offsets = []

    def fake_get(url, params, timeout):
        # Фиксируем переданный ``offset`` для проверки продвижения указателя.
        offsets.append(params["offset"])
        return responses.pop(0)

    monkeypatch.setattr(tl, "va_respond", fake_va)
    monkeypatch.setattr(tl, "inc_metric", fake_inc)
    monkeypatch.setattr(tl.requests, "get", fake_get)

    tl.listen(max_iterations=2)

    assert calls == ["cmd"]
    assert metrics["count"] == 1
    assert offsets == [0, 6]


def test_listener_filters_control_commands(monkeypatch):
    """Служебные команды не должны передаваться в ``va_respond``."""

    tl = _load_listener(monkeypatch)
    calls = []

    async def fake_va(text):
        calls.append(text)

    responses = [
        DummyResp(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {"chat": {"id": 123}, "text": "/help"},
                    }
                ],
            }
        ),
        DummyResp({"ok": True, "result": []}),
    ]

    def fake_get(url, params, timeout):
        return responses.pop(0)

    monkeypatch.setattr(tl, "va_respond", fake_va)
    monkeypatch.setattr(tl.requests, "get", fake_get)
    monkeypatch.setattr(tl, "_handle_control_command", lambda text: text == "/help")

    tl.listen(max_iterations=2)

    assert calls == []


def test_listener_sends_reply_via_notifier(monkeypatch):
    """Убеждаемся, что обработчик может отправлять ответ через Telegram."""
    tl = _load_listener(monkeypatch)
    sent = []

    # Подменяем модуль ``notifiers.telegram`` с функцией ``send``.
    fake_pkg = SimpleNamespace(send=lambda text: sent.append(text))
    monkeypatch.setitem(sys.modules, "notifiers.telegram", fake_pkg)
    import notifiers
    monkeypatch.setattr(notifiers, "telegram", fake_pkg, raising=False)

    async def fake_va(text):
        # При обработке команды отправляем ответ владельцу.
        import notifiers.telegram as tg

        tg.send("pong")

    responses = [
        DummyResp(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {"chat": {"id": 123}, "text": "ping"},
                    }
                ],
            }
        ),
        DummyResp({"ok": True, "result": []}),
    ]

    def fake_get(url, params, timeout):
        return responses.pop(0)

    monkeypatch.setattr(tl, "va_respond", fake_va)
    monkeypatch.setattr(tl.requests, "get", fake_get)

    tl.listen(max_iterations=2)

    assert sent == ["pong"]


def test_listener_ignores_foreign_chat(monkeypatch):
    tl = _load_listener(monkeypatch)
    calls = []

    async def fake_va(text):
        calls.append(text)

    metrics = {"count": 0}

    def fake_inc(name):
        metrics["count"] += 1

    def fake_get(url, params, timeout):
        return DummyResp(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {"chat": {"id": 999}, "text": "no"},
                    }
                ],
            }
        )

    monkeypatch.setattr(tl, "va_respond", fake_va)
    monkeypatch.setattr(tl, "inc_metric", fake_inc)
    monkeypatch.setattr(tl.requests, "get", fake_get)

    tl.listen(max_iterations=1)

    assert calls == []
    assert metrics["count"] == 0


def test_listener_handles_api_error(monkeypatch):
    tl = _load_listener(monkeypatch)
    calls = []

    async def fake_va(text):
        calls.append(text)

    def fake_get(url, params, timeout):
        return DummyResp({"ok": False, "result": []}, status_code=500)

    monkeypatch.setattr(tl, "va_respond", fake_va)
    monkeypatch.setattr(tl.requests, "get", fake_get)

    tl.listen(max_iterations=1)

    assert calls == []


def test_listener_handles_invalid_json(monkeypatch):
    """Сетевой ответ с некорректным JSON не должен приводить к сбою."""
    tl = _load_listener(monkeypatch)
    calls = []

    async def fake_va(text):
        calls.append(text)

    class BadResp:
        def __init__(self):
            self.status_code = 200
            self.text = "bad"

        def json(self):
            raise ValueError("broken")

    def fake_get(url, params, timeout):
        return BadResp()

    monkeypatch.setattr(tl, "va_respond", fake_va)
    monkeypatch.setattr(tl.requests, "get", fake_get)
    monkeypatch.setattr(tl.time, "sleep", lambda s: None)

    tl.listen(max_iterations=1)

    assert calls == []


def test_listener_retries_on_network_error(monkeypatch):
    tl = _load_listener(monkeypatch)
    calls = []

    async def fake_va(text):
        calls.append(text)

    metrics = {"count": 0}

    def fake_inc(name):
        metrics["count"] += 1

    def fake_sleep(sec):
        pass

    call_counter = {"n": 0}

    def fake_get(url, params, timeout):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            raise requests.RequestException("boom")
        return DummyResp(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 7,
                        "message": {"chat": {"id": 123}, "text": "hi"},
                    }
                ],
            }
        )

    monkeypatch.setattr(tl, "va_respond", fake_va)
    monkeypatch.setattr(tl, "inc_metric", fake_inc)
    monkeypatch.setattr(tl.requests, "get", fake_get)
    monkeypatch.setattr(tl.time, "sleep", fake_sleep)

    tl.listen(max_iterations=2)

    assert calls == ["hi"]
    assert metrics["count"] == 1
    assert call_counter["n"] == 2


def test_listener_skips_duplicate_update(monkeypatch):
    """Повторяющийся update_id не должен приводить к повторной обработке."""
    tl = _load_listener(monkeypatch)
    calls = []

    async def fake_va(text):
        calls.append(text)

    metrics = {"count": 0}

    def fake_inc(name):
        metrics["count"] += 1

    responses = [
        DummyResp(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 2,
                        "message": {"chat": {"id": 123}, "text": "one"},
                    }
                ],
            }
        ),
        DummyResp(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 2,
                        "message": {"chat": {"id": 123}, "text": "one"},
                    }
                ],
            }
        ),
    ]
    offsets = []

    def fake_get(url, params, timeout):
        offsets.append(params["offset"])
        return responses.pop(0)

    monkeypatch.setattr(tl, "va_respond", fake_va)
    monkeypatch.setattr(tl, "inc_metric", fake_inc)
    monkeypatch.setattr(tl.requests, "get", fake_get)

    tl.listen(max_iterations=2)

    assert calls == ["one"]
    assert metrics["count"] == 1
    assert offsets == [0, 3]


def test_listener_uses_external_loop(monkeypatch):
    """Проверяем, что ``listen`` умеет выполнять обработчик в переданном loop."""
    tl = _load_listener(monkeypatch)
    loops = []

    async def fake_va(text):
        # Сохраняем цикл событий, в котором выполнен обработчик.
        loops.append(asyncio.get_running_loop())

    responses = [
        DummyResp(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {"chat": {"id": 123}, "text": "hi"},
                    }
                ],
            }
        ),
        DummyResp({"ok": True, "result": []}),
    ]

    def fake_get(url, params, timeout):
        return responses.pop(0)

    loop = asyncio.new_event_loop()
    thr = threading.Thread(target=loop.run_forever)
    thr.start()
    try:
        monkeypatch.setattr(tl, "va_respond", fake_va)
        monkeypatch.setattr(tl.requests, "get", fake_get)
        tl.listen(max_iterations=2, loop=loop)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thr.join()
        loop.close()

    assert loops == [loop]


def test_launch_stops_on_event(monkeypatch, caplog):
    tl = _load_listener(monkeypatch)

    # Заглушка ``listen``: ждёт, пока событие не будет установлено.
    def fake_listen(*, stop_event=None, max_iterations=None, loop=None):
        assert stop_event is not None
        stop_event.wait()

    monkeypatch.setattr(tl, "listen", fake_listen)

    # Перехватываем вызовы log.info для проверки сообщений.
    messages = []

    def fake_info(msg, *a, **kw):
        messages.append(msg)

    monkeypatch.setattr(tl.log, "info", fake_info)

    stop = threading.Event()

    async def _run():
        task = asyncio.create_task(tl.launch(stop_event=stop))
        await asyncio.sleep(0.01)
        assert tl.is_active() is True
        stop.set()
        await task

    asyncio.run(_run())

    assert tl.is_active() is False
    assert "telegram listener started" in messages
    assert "telegram listener stopped" in messages


def test_control_help(monkeypatch):
    """Команда ``/help`` возвращает подсказку в Telegram."""

    tl = _load_listener(monkeypatch)
    sent: list[str] = []

    monkeypatch.setattr(tl, "_send_telegram_message", lambda text: sent.append(text))

    handled = tl._handle_control_command("/help")

    assert handled is True
    assert sent and "Доступные команды" in sent[0]


def test_control_status(monkeypatch):
    """Команда ``/status`` выводит сведения о состоянии слушателя."""

    tl = _load_listener(monkeypatch)
    sent: list[str] = []

    monkeypatch.setattr(tl, "_send_telegram_message", lambda text: sent.append(text))
    monkeypatch.setattr(tl, "_RUNNING", True)
    monkeypatch.setattr(tl, "_STARTED_AT", time.time() - 65)

    handled = tl._handle_control_command("/status")

    assert handled is True
    assert sent and "Ассистент активен." in sent[0]
    assert "мин" in sent[0] or "с" in sent[0]


def test_control_history(monkeypatch):
    """Команда ``/history`` выводит последние сообщения."""

    tl = _load_listener(monkeypatch)
    sent: list[str] = []

    monkeypatch.setattr(tl, "_send_telegram_message", lambda text: sent.append(text))

    history = [
        {"ts": 1, "direction": "incoming", "text": "привет"},
        {"ts": 2, "direction": "outgoing", "text": "ответ"},
    ]

    monkeypatch.setattr(
        tl,
        "fetch_history",
        lambda *, limit, channel, ascending: history,
    )

    handled = tl._handle_control_command("/history 5")

    assert handled is True
    assert sent and "Последние сообщения" in sent[0]


def test_control_history_invalid_limit(monkeypatch):
    """Некорректное значение лимита должно возвращать предупреждение."""

    tl = _load_listener(monkeypatch)
    sent: list[str] = []

    monkeypatch.setattr(tl, "_send_telegram_message", lambda text: sent.append(text))

    handled = tl._handle_control_command("/history abc")

    assert handled is True
    assert sent == ["Нужно указать число от 1 до 50."]


def test_control_unknown(monkeypatch):
    """Неизвестные команды должны игнорироваться."""

    tl = _load_listener(monkeypatch)

    handled = tl._handle_control_command("/unknown")

    assert handled is False


def test_format_uptime_variants():
    """Формат продолжительности выдаёт человекочитаемые значения."""

    from notifiers.telegram_listener import _format_uptime

    assert _format_uptime(0) == "0 с"
    assert _format_uptime(65).startswith("1 мин")
    assert "д" in _format_uptime(60 * 60 * 24)
