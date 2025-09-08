from __future__ import annotations
"""Утилиты для разбора и выполнения голосовых команд.

Модуль содержит общий набор функций, которые используются стартовым скриптом
Jarvis: разбор фраз, поиск совпадений с именем ассистента, фильтрация
служебных слов и вызов соответствующих реакций. Комментарии призваны
объяснить внутреннюю логику, чтобы модуль было легко поддерживать.
"""

from typing import Any, Dict, List

import asyncio
import uuid
from rapidfuzz import fuzz

import jarvis_skills
handle_utterance = jarvis_skills.handle_utterance
from core.nlp import normalize
from working_tts import speak_async
from core.request_source import get_request_source
from core.logging_json import configure_logging
from core import events as core_events, llm_engine
from memory.writer import add_suggestion_feedback
from memory.db import get_connection
from proactive.engine import (
    is_awaiting_response,
    pop_awaiting,
    classify_feedback,
)
from core.metrics import inc_metric
from context import daily_memory  # Дневная память для конспектов

# Инициализируем модульный логгер, чтобы отслеживать процесс разбора команд.
log = configure_logging("app.command_processing")

# ────────────────────────── КОНСТАНТЫ ──────────────────────────────
# Слова, которыми пользователь обращается к ассистенту.
VA_ALIAS = ("джарвис",)

# Фразы‑паразиты, которые обычно не несут смысловой нагрузки.
# При обработке команды они будут удалены.
VA_TBR = (
    "скажи",
    "покажи",
    "ответь",
    "произнеси",
    "расскажи",
    "сколько",
    "слушай",
)

# Минимальная уверенность (0‑100) для выбора команды из конфигурации.
CMD_CONFIDENCE_THRESHOLD = 70
# Допустимая «похожесть» слова активации, если пользователь сказал его неточно.
ACTIVATION_CONFIDENCE = 65

# Словарь доступных команд: ключ — имя команды,
# значение — список фраз‑вариантов. Заполняется при старте из ``commands.yaml``.
VA_CMD_LIST: Dict[str, List[str]] = {}

# ─── Переменные режима общения ────────────────────────────────────────────
# Флаг активности «режима общения», когда LLM ведёт диалог без слова активации.
CHAT_MODE_ACTIVE: bool = False
# История текущего диалога [(реплика пользователя, ответ ассистента), ...]
CHAT_HISTORY: list[tuple[str, str]] = []
# Таймер автоматического завершения режима общения при отсутствии речи.
_chat_timer: asyncio.TimerHandle | None = None


def _reset_chat_timer() -> None:
    """Перезапустить таймер ожидания ответа собеседника."""

    global _chat_timer
    loop = asyncio.get_running_loop()
    if _chat_timer is not None:
        _chat_timer.cancel()
    # Если за минуту не поступит новой реплики — завершаем режим общения
    _chat_timer = loop.call_later(60, lambda: asyncio.create_task(_end_chat()))


def is_chat_stop_phrase(text: str) -> bool:
    """Проверить, хочет ли пользователь завершить беседу."""

    text = text.lower().strip()
    return "хватит болтать" in text


async def _chat_llm(text: str) -> None:
    """Отправить реплику в LLM и озвучить ответ, сохранив историю."""

    trace_id = uuid.uuid4().hex
    log.info("chat message", extra={"ctx": {"text": text, "trace_id": trace_id}})
    try:
        reply = await asyncio.to_thread(llm_engine.think, text, trace_id=trace_id)
    except Exception:  # pragma: no cover - логируем сбои внешней LLM
        log.exception("llm think failed", extra={"ctx": {"trace_id": trace_id}})
        return
    try:
        await speak_async(reply)
    except Exception:  # pragma: no cover - синтез речи не критичен для тестов
        log.exception("failed to speak llm reply", extra={"ctx": {"trace_id": trace_id}})
    CHAT_HISTORY.append((text, reply))
    _reset_chat_timer()


async def _start_chat(text: str) -> None:
    """Активировать режим общения и обработать первую реплику."""

    global CHAT_MODE_ACTIVE, CHAT_HISTORY
    CHAT_MODE_ACTIVE = True
    CHAT_HISTORY = []
    log.debug("chat mode started")
    await _chat_llm(text)


async def _end_chat() -> None:
    """Завершить режим общения, сохранить конспект и озвучить финал."""

    global CHAT_MODE_ACTIVE, _chat_timer
    if not CHAT_MODE_ACTIVE:
        return
    CHAT_MODE_ACTIVE = False
    if _chat_timer is not None:
        _chat_timer.cancel()
        _chat_timer = None
    conversation = "\n".join(
        f"Пользователь: {u}\nАссистент: {r}" for u, r in CHAT_HISTORY
    )
    if conversation:
        try:
            summary = llm_engine.summarise(conversation, labels=["chat"])
            daily_memory.add({"label": "chat", "text": summary})
            log.debug("chat summary stored", extra={"ctx": {"summary": summary}})
        except Exception:  # pragma: no cover - сбой памяти не критичен
            log.exception("failed to store chat summary")
    CHAT_HISTORY.clear()
    try:
        await speak_async("Режим общения завершён", preset="neutral")
    except Exception:  # pragma: no cover - озвучка не критична
        log.exception("failed to speak chat end")


def process_suggestion_answer(text: str) -> None:
    """Обработать ответ пользователя на проактивную подсказку."""

    # Забираем из ``ProactiveEngine`` информацию об ожидаемой подсказке.
    awaiting = pop_awaiting()
    if not awaiting:
        log.debug("нет ожидания подсказки, пропускаем ответ: %r", text)
        return
    suggestion_id = awaiting.get("id")
    trace_id = awaiting.get("trace_id")
    log.info(
        "processing suggestion answer",
        extra={"ctx": {"suggestion_id": suggestion_id, "text": text, "trace_id": trace_id}},
    )
    suggestion_text = awaiting.get("text", "")
    channel = awaiting.get("channel", "telegram")
    # Запрашиваем у LLM анализ ответа: определяем отношение и возможную реакцию.
    accepted, reply = classify_feedback(suggestion_text, text, trace_id)
    # Сохраняем отзыв пользователя в памяти.
    add_suggestion_feedback(suggestion_id, text, accepted)
    # Фиксируем метрики реакции пользователя
    inc_metric("suggestions.responded")
    if accepted:
        inc_metric("suggestions.accepted")
    else:
        inc_metric("suggestions.declined")
    # Публикуем событие для остальных компонентов системы.
    core_events.publish(
        core_events.Event(
            kind="suggestion.response",
            attrs={
                "suggestion_id": suggestion_id,
                "text": text,
                "accepted": accepted,
                "trace_id": trace_id,
            },
        )
    )
    log.debug(
        "suggestion response published",
        extra={"ctx": {"suggestion_id": suggestion_id, "accepted": accepted, "trace_id": trace_id}},
    )

    # В зависимости от результата ответа публикуем событие
    # ``dialog.success`` или ``dialog.failure``. Это позволяет системе
    # эмоций и метрикам реагировать на исход диалога, а ``trace_id``
    # обеспечивает сквозную корреляцию всех связанных событий.
    dialog_kind = "dialog.success" if accepted else "dialog.failure"
    core_events.publish(
        core_events.Event(
            kind=dialog_kind,
            attrs={
                "text": text,
                "suggestion_id": suggestion_id,
                "trace_id": trace_id,
            },
        )
    )
    log.debug(
        "dialog result event published",
        extra={
            "ctx": {
                "suggestion_id": suggestion_id,
                "result": dialog_kind,
                "trace_id": trace_id,
            }
        },
    )

    # Отправляем пользователю реакцию: голосом или через Telegram
    reply_text = reply or ("Отлично, записал" if accepted else "Хорошо, отложим")
    try:
        if channel == "voice":
            # Планируем асинхронное озвучивание, чтобы не блокировать поток
            asyncio.create_task(speak_async(reply_text))
        else:
            from notifiers import telegram as notifier

            notifier.send(reply_text)
    except Exception:
        log.exception(
            "failed to send ack", extra={"ctx": {"suggestion_id": suggestion_id, "trace_id": trace_id}}
        )

    # После отправки реакции фиксируем краткое резюме разговора и сохраняем
    # его в дневную память с меткой исходной подсказки. Это позволяет позже
    # агрегировать события дня и перенести их в долговременный контекст.
    try:
        reason_code = ""
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT reason_code FROM suggestions WHERE id = ?",
                    (suggestion_id,),
                ).fetchone()
            if row and row["reason_code"]:
                reason_code = str(row["reason_code"])
        except Exception:
            log.exception(
                "failed to fetch reason_code",
                extra={"ctx": {"suggestion_id": suggestion_id}},
            )
        summary = llm_engine.summarise(
            f"Подсказка: {suggestion_text}\nОтвет: {text}\nРеакция: {reply_text}",
            labels=[reason_code] if reason_code else None,
        )
        daily_memory.add({"label": reason_code or "suggestion", "text": summary})
        log.debug(
            "daily memory updated after suggestion",
            extra={"ctx": {"suggestion_id": suggestion_id, "label": reason_code}},
        )
    except Exception:
        log.exception(
            "failed to store suggestion summary",
            extra={"ctx": {"suggestion_id": suggestion_id, "trace_id": trace_id}},
        )


async def execute_cmd(cmd: str, voice: str) -> bool:
    """Обработать простые встроенные команды, не требующие навыков.

    Возвращает ``True``, если команда распознана и ответ озвучен.
    """
    if cmd == "thanks":
        # Вежливый ответ на благодарность
        await speak_async("Пожалуйста", preset="happy")
    elif cmd == "stupid":
        # Эмоциональная реакция на оскорбление
        await speak_async("Мне неприятно это слышать", preset="sad")
    elif cmd == "offf":
        # Перевод ассистента в режим ожидания
        await speak_async("Переходим в спящий режим", preset="neutral")
    else:
        return False
    return True


async def recognize_cmd(raw: str) -> Dict[str, Any]:
    """Выбрать из конфигурации наиболее похожую команду.

    Алгоритм проходит по всем вариантам и использует ``fuzz.ratio`` для оценки
    схожести. В результате возвращается словарь с ключами ``cmd`` и ``percent``.
    """
    best = {"cmd": "", "percent": 0}
    for key, variants in VA_CMD_LIST.items():
        for alias in variants:
            score = fuzz.ratio(raw, alias)
            if score > best["percent"]:
                best = {"cmd": key, "percent": score}
    return best


async def filter_cmd(raw: str) -> str:
    """Удалить служебные слова и вернуть чистый текст команды."""
    text = raw.lower()
    for stop in VA_TBR:
        text = text.replace(stop, "").strip()
    return text


def _matches_activation(word: str) -> bool:
    """Проверить, похоже ли слово на имя ассистента."""
    return any(fuzz.ratio(word, alias) >= ACTIVATION_CONFIDENCE for alias in VA_ALIAS)


def extract_cmd(text: str) -> str:
    """Выделить часть фразы после слова активации.

    Для голосовых запросов требуется слово обращения «Джарвис».
    Если команда пришла из Telegram, считаем, что она уже адресована
    ассистенту, поэтому возвращаем текст без проверки.
    При отсутствии активации у голосового запроса — вернётся пустая строка.
    """

    text = text.lower().strip()
    if not text:
        return ""

    source = get_request_source()
    if source == "telegram":
        # Команды в Telegram не нуждаются в слове активации.
        log.debug("telegram command: %r", text)
        return text

    words = text.split()
    if _matches_activation(words[0]):
        return " ".join(words[1:]).strip()

    log.debug("activation word missing: %r", text)
    return ""


def is_stop_cmd(text: str) -> bool:
    """Проверить, произносит ли пользователь команду «стоп» после активации."""
    return extract_cmd(text) == "стоп"


def contains_stop(text: str) -> bool:
    """Определить, встречается ли в фразе слово, похожее на «стоп».

    Используется для прерывания речи синтезатора даже без слова активации.
    """
    words = text.lower().split()
    for word in words:
        if word.startswith("ст"):
            if word.startswith("сто") or fuzz.ratio(word, "стоп") >= 75:
                return True
        elif fuzz.ratio(word, "стоп") >= 80:
            return True
    return False


def is_exit_phrase(text: str) -> bool:
    """Распознать команду выхода из режима общения."""

    text = text.lower().strip()
    phrases = ("закончим общение", "перейди в режим команд", "хватит болтать")
    return any(p in text for p in phrases)


async def va_respond(voice: str) -> bool:
    """Главная реакция ассистента на распознанный текст.

    1. Отделяем команду от слова активации.
    2. Пробуем передать команду в систему навыков ``jarvis_skills``.
    3. Если ни один навык не сработал — пытаемся сопоставить её с
       набором встроенных команд.
    Возвращает ``True``, если что‑то было выполнено.
    """
    # Сначала проверяем, не ждёт ли система ответа на подсказку.
    if is_awaiting_response():
        text = voice.strip()
        if is_exit_phrase(text):
            pop_awaiting()
            await speak_async("Режим общения завершён", preset="neutral")
            return True
        process_suggestion_answer(text)
        return True

    text = voice.strip()

    # Если активирован режим общения, все реплики сразу отправляются в LLM
    # без проверки слова активации.
    if CHAT_MODE_ACTIVE:
        if is_chat_stop_phrase(text):
            await _end_chat()
            return True
        await _chat_llm(text)
        return True

    cmd = extract_cmd(voice)
    if not cmd:
        return False
    # Сохраняем текущий event loop, чтобы jarvis_skills мог
    # безопасно отправлять ответы из побочного потока.
    getattr(jarvis_skills, "set_main_loop", lambda loop: None)(
        asyncio.get_running_loop()
    )
    # handle_utterance может блокировать, поэтому вызываем в отдельном потоке
    if await asyncio.to_thread(handle_utterance, cmd):
        return True
    raw = await filter_cmd(cmd)
    raw_norm = normalize(raw)
    cmd_info = await recognize_cmd(raw_norm)
    if not cmd_info["cmd"] or cmd_info["percent"] < CMD_CONFIDENCE_THRESHOLD:
        await _start_chat(raw)
        return True
    return await execute_cmd(cmd_info["cmd"], voice)
