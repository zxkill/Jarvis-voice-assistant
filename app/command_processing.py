from __future__ import annotations
"""Утилиты для разбора и выполнения голосовых команд."""

import asyncio
import re
from contextvars import Token, copy_context
from typing import Any, Dict, List, Sequence

from rapidfuzz import fuzz

import jarvis_skills
handle_utterance = jarvis_skills.handle_utterance
from core.nlp import normalize
import working_tts as _working_tts
speak_async = _working_tts.speak_async
from core.logging_json import configure_logging, TRACE_ID, new_trace_id
from core import events as core_events
from core.request_source import get_request_source
from memory.writer import add_suggestion_feedback
from memory.db import get_connection
from memory.dialog_log import record_dialog_message
from proactive.engine import is_awaiting_response, pop_awaiting, classify_feedback
from core.metrics import inc_metric
from context import daily_memory

log = configure_logging("app.command_processing")
_TELEGRAM_USER_ID: str | int | None = None


def _resolve_default_user(channel: str) -> str | int:
    """Определить идентификатор пользователя по каналу."""

    global _TELEGRAM_USER_ID
    if channel == "voice":
        return "voice-user"
    if channel == "telegram":
        if _TELEGRAM_USER_ID is None:
            try:
                from core.config import load_config

                cfg = load_config()
                _TELEGRAM_USER_ID = getattr(cfg.user, "telegram_user_id", "telegram-user")
                log.debug("resolved telegram user id", extra={"ctx": {"user_id": _TELEGRAM_USER_ID}})
            except Exception:
                _TELEGRAM_USER_ID = "telegram-user"
                log.exception("failed to resolve telegram user id")
        return _TELEGRAM_USER_ID
    return f"{channel}-user"


def _ensure_trace_id() -> tuple[str, Token | None]:
    """Создать ``trace_id`` при отсутствии и вернуть его вместе с токеном."""

    trace = TRACE_ID.get()
    token: Token | None = None
    if not trace:
        trace = new_trace_id()
        token = TRACE_ID.set(trace)
        log.debug("generated trace id for command", extra={"ctx": {"trace_id": trace}})
    return trace, token


def _log_dialog(
    direction: str,
    text: str,
    channel: str,
    trace_id: str,
    meta: dict[str, Any] | None = None,
    user_id: str | int | None = None,
    status: str | None = None,
) -> None:
    """Безопасно записать сообщение в журнал диалогов через сервис."""

    if not text:
        return
    metadata = dict(meta or {})
    if user_id is None and "user_id" in metadata:
        user_id = metadata.pop("user_id")
    if status is None and "status" in metadata:
        status = metadata.pop("status")
    try:
        record_dialog_message(
            text,
            direction=direction,
            channel=channel,
            trace_id=trace_id,
            user_id=user_id or _resolve_default_user(channel),
            status=status,
            metadata=metadata,
        )
    except Exception:
        log.exception(
            "failed to log dialog message",
            extra={
                "ctx": {
                    "direction": direction,
                    "channel": channel,
                    "trace_id": trace_id,
                    "user_id": user_id,
                    "status": status,
                }
            },
        )


# Слова, которыми пользователь обращается к ассистенту. Варианты нужны не для
# красоты, а чтобы Vosk не терял wake-word, когда слышит «джервис», «жарвис» и т.п.
VA_ALIAS = (
    "джарвис",
    "жарвис",
    "джервис",
    "джарвиз",
    "джарвес",
    "чарвис",
    "дарвис",
    "ярвис",
)

VA_TBR = (
    "скажи",
    "покажи",
    "ответь",
    "произнеси",
    "расскажи",
    "слушай",
)

CMD_CONFIDENCE_THRESHOLD = 70
ACTIVATION_CONFIDENCE = 62
VA_CMD_LIST: Dict[str, List[str]] = {}
_WORD_RE = re.compile(r"[^а-яёa-z0-9]+", re.IGNORECASE)
_FILLER_BEFORE_WAKE = {"эй", "ну", "алло", "окей", "ок", "а", "слушай"}


def _clean_word(word: str) -> str:
    """Оставляет в слове только символы, полезные для сравнения."""

    return _WORD_RE.sub("", word.lower().replace("ё", "ё"))


def _matches_activation(word: str) -> bool:
    """Проверить, похоже ли слово на имя ассистента."""

    token = _clean_word(word)
    if not token:
        return False
    if token in VA_ALIAS:
        return True
    if token.startswith("джар") or token.endswith("арвис") or token.endswith("ервис"):
        return True
    return any(fuzz.ratio(token, alias) >= ACTIVATION_CONFIDENCE for alias in VA_ALIAS)


def find_activation_index(words: Sequence[str], *, search_limit: int = 5) -> int:
    """Ищет wake-word в первых словах фразы и возвращает индекс или -1."""

    limit = min(len(words), max(1, search_limit))
    for idx in range(limit):
        word = _clean_word(words[idx])
        if not word:
            continue
        if word in _FILLER_BEFORE_WAKE:
            continue
        if _matches_activation(word):
            return idx
    return -1


def process_suggestion_answer(text: str) -> None:
    """Обработать ответ пользователя на проактивную подсказку."""

    awaiting = pop_awaiting()
    if not awaiting:
        log.debug("нет ожидания подсказки, пропускаем ответ: %r", text)
        return

    suggestion_id = awaiting.get("id")
    trace_id = awaiting.get("trace_id") or TRACE_ID.get() or new_trace_id()
    token: Token | None = None
    if trace_id:
        token = TRACE_ID.set(trace_id)

    try:
        log.info(
            "processing suggestion answer",
            extra={"ctx": {"suggestion_id": suggestion_id, "text": text, "trace_id": trace_id}},
        )
        suggestion_text = awaiting.get("text", "")
        channel = awaiting.get("channel", "telegram")
        accepted, reply = classify_feedback(suggestion_text, text, trace_id)
        add_suggestion_feedback(suggestion_id, text, accepted)
        inc_metric("suggestions.responded")
        inc_metric("suggestions.accepted" if accepted else "suggestions.declined")

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
        dialog_kind = "dialog.success" if accepted else "dialog.failure"
        core_events.publish(
            core_events.Event(
                kind=dialog_kind,
                attrs={"text": text, "suggestion_id": suggestion_id, "trace_id": trace_id},
            )
        )

        reply_text = reply or ("Отлично, записал" if accepted else "Хорошо, отложим")
        try:
            if channel == "voice":
                asyncio.create_task(speak_async(reply_text))
                _log_dialog(
                    "outgoing",
                    reply_text,
                    channel,
                    trace_id,
                    {"suggestion_id": suggestion_id, "accepted": accepted},
                )
            else:
                from notifiers import telegram as notifier

                notifier.send(reply_text)
        except Exception:
            log.exception(
                "failed to send ack",
                extra={"ctx": {"suggestion_id": suggestion_id, "trace_id": trace_id}},
            )

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
                log.exception("failed to fetch reason_code", extra={"ctx": {"suggestion_id": suggestion_id}})
            daily_memory.add(
                {
                    "label": reason_code or "suggestion",
                    "text": f"Подсказка: {suggestion_text}\nОтвет: {text}\nРеакция: {reply_text}",
                }
            )
        except Exception:
            log.exception(
                "failed to store suggestion summary",
                extra={"ctx": {"suggestion_id": suggestion_id, "trace_id": trace_id}},
            )
    finally:
        if token:
            TRACE_ID.reset(token)


async def execute_cmd(cmd: str, voice: str) -> bool:
    """Обработать простые встроенные команды, не требующие навыков."""

    if cmd == "thanks":
        reply_text = "Пожалуйста"
        await speak_async(reply_text, preset="happy")
        _log_dialog("outgoing", reply_text, get_request_source(), TRACE_ID.get() or new_trace_id(), {"preset": "happy", "cmd": cmd})
    elif cmd == "stupid":
        reply_text = "Мне неприятно это слышать"
        await speak_async(reply_text, preset="sad")
        _log_dialog("outgoing", reply_text, get_request_source(), TRACE_ID.get() or new_trace_id(), {"preset": "sad", "cmd": cmd})
    elif cmd == "offf":
        reply_text = "Переходим в спящий режим"
        await speak_async(reply_text, preset="neutral")
        _log_dialog("outgoing", reply_text, get_request_source(), TRACE_ID.get() or new_trace_id(), {"preset": "neutral", "cmd": cmd})
    else:
        return False
    return True


async def recognize_cmd(raw: str) -> Dict[str, Any]:
    """Выбрать из конфигурации наиболее похожую команду."""

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


def extract_cmd(text: str) -> str:
    """Выделить часть фразы после wake-word.

    Раньше wake-word принимался только первым словом. Это нестабильно: Vosk часто
    добавляет перед ним «ну», «эй», «а» или кусок шума. Теперь ищем обращение в
    первых нескольких словах и берём всё, что идёт после него.
    """

    text = text.lower().strip()
    if not text:
        return ""

    source = get_request_source()
    if source == "telegram":
        log.debug("telegram command: %r", text)
        return text

    words = text.split()
    idx = find_activation_index(words)
    if idx >= 0:
        return " ".join(words[idx + 1:]).strip()

    log.debug("activation word missing: %r", text)
    return ""


def is_stop_cmd(text: str) -> bool:
    """Проверить, произносит ли пользователь команду «стоп» после активации."""

    return extract_cmd(text) == "стоп"


def contains_stop(text: str) -> bool:
    """Определить, встречается ли в фразе слово, похожее на «стоп»."""

    words = text.lower().split()
    for word in words:
        word = _clean_word(word)
        if not word:
            continue
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
    """Главная реакция ассистента на распознанный текст."""

    trace_id, token = _ensure_trace_id()
    channel = get_request_source()
    text = voice.strip()
    _log_dialog("incoming", text, channel, trace_id, {"raw": voice})

    try:
        if is_awaiting_response():
            if is_exit_phrase(text):
                pop_awaiting()
                await speak_async("Режим общения завершён", preset="neutral")
                _log_dialog("outgoing", "Режим общения завершён", channel, trace_id, {"preset": "neutral"})
                return True
            process_suggestion_answer(text)
            return True

        cmd = extract_cmd(voice)
        if not cmd:
            return False

        getattr(jarvis_skills, "set_main_loop", lambda loop: None)(asyncio.get_running_loop())
        ctx = copy_context()
        log.debug("dispatching utterance via thread with source=%s", get_request_source())
        if await asyncio.to_thread(ctx.run, handle_utterance, cmd):
            return True

        raw = await filter_cmd(cmd)
        raw_norm = normalize(raw)
        cmd_info = await recognize_cmd(raw_norm)
        if not cmd_info["cmd"] or cmd_info["percent"] < CMD_CONFIDENCE_THRESHOLD:
            reply = "можете повторить?"
            await speak_async(reply)
            log.info("fallback reply", extra={"ctx": {"text": cmd, "trace_id": trace_id}})
            _log_dialog("outgoing", reply, channel, trace_id, {"kind": "fallback"})
            try:
                from context.short_term import add as ctx_add

                ctx_add({"trace_id": trace_id, "user": cmd, "reply": reply})
                daily_memory.add({"label": "fallback", "text": f"Пользователь: {cmd}\nАссистент: {reply}"})
            except Exception:
                pass
            return True

        return await execute_cmd(cmd_info["cmd"], voice)
    finally:
        if token:
            TRACE_ID.reset(token)
