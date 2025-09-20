"""Сервисный слой для централизованного журнала диалогов."""

from __future__ import annotations

from contextvars import Token
from dataclasses import dataclass
from typing import Any

from core.logging_json import TRACE_ID, configure_logging, new_trace_id
from memory import dialogs

# Инициализируем отдельный логгер, чтобы в журналах можно было увидеть
# движение сообщений без включения глобального дебага. Подробные записи
# помогают анализировать сложные сценарии в боевом окружении.
log = configure_logging("memory.dialog_log")


@dataclass(frozen=True)
class DialogRecord:
    """Описывает одно сообщение диалога, возвращаемое из сервиса."""

    id: int
    text: str
    direction: str
    channel: str
    trace_id: str
    meta: dict[str, Any]
    ts: int


_DEFAULT_STATUS = {
    "incoming": "received",
    "outgoing": "sent",
}


def _default_user(channel: str) -> str:
    """Вернуть идентификатор пользователя по каналу при его отсутствии."""

    if channel == "voice":
        return "voice-user"
    if channel == "telegram":
        return "telegram-user"
    return f"{channel}-user"


def _ensure_trace(trace_id: str | None) -> tuple[str, Token | None]:
    """Убедиться, что в контексте есть ``trace_id``, и вернуть его."""

    current = TRACE_ID.get()
    if trace_id:
        if current == trace_id:
            return trace_id, None
        token = TRACE_ID.set(trace_id)
        log.debug("use provided trace id", extra={"ctx": {"trace_id": trace_id}})
        return trace_id, token
    if current:
        return current, None
    generated = new_trace_id()
    token = TRACE_ID.set(generated)
    log.debug("generated trace id in dialog service", extra={"ctx": {"trace_id": generated}})
    return generated, token


def _build_metadata(
    *,
    channel: str,
    user_id: str | int | None,
    status: str | None,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    """Объединить обязательные и пользовательские метаданные."""

    meta: dict[str, Any] = {"channel": channel}
    if user_id is not None:
        meta["user_id"] = str(user_id)
    if status:
        meta["status"] = status
    if extra:
        for key, value in extra.items():
            if value is not None:
                meta[key] = value
    return meta


def record_dialog_message(
    text: str,
    *,
    direction: str,
    channel: str,
    user_id: str | int | None = None,
    status: str | None = None,
    trace_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """Сохранить сообщение в журнале и вернуть его ID вместе с ``trace_id``."""

    trace, token = _ensure_trace(trace_id)
    effective_status = status or _DEFAULT_STATUS.get(direction, "unknown")
    resolved_user = user_id if user_id is not None else _default_user(channel)
    payload_meta = _build_metadata(
        channel=channel,
        user_id=resolved_user,
        status=effective_status,
        extra=metadata,
    )
    log.debug(
        "record dialog message",
        extra={
            "ctx": {
                "trace_id": trace,
                "direction": direction,
                "channel": channel,
                "user_id": resolved_user,
                "status": effective_status,
            }
        },
    )
    try:
        message_id = dialogs.log_message(
            text,
            direction=direction,
            channel=channel,
            trace_id=trace,
            metadata=payload_meta,
        )
    finally:
        if token is not None:
            TRACE_ID.reset(token)
    if message_id == -1:
        log.error(
            "dialog message was not stored",
            extra={"ctx": {"trace_id": trace, "direction": direction, "channel": channel}},
        )
    else:
        log.debug(
            "dialog message stored",
            extra={"ctx": {"id": message_id, "trace_id": trace, "direction": direction}},
        )
    return message_id, trace


def _normalize_history_entry(item: dict[str, Any]) -> DialogRecord:
    """Преобразовать строку БД в удобный объект ``DialogRecord``."""

    meta = dict(item.get("meta") or {})
    meta.setdefault("channel", item.get("channel"))
    if "user_id" in meta and meta["user_id"] is not None:
        meta["user_id"] = str(meta["user_id"])
    meta.setdefault("status", _DEFAULT_STATUS.get(item.get("direction"), "unknown"))
    meta.setdefault("user_id", _default_user(item.get("channel", "unknown")))
    return DialogRecord(
        id=int(item["id"]),
        text=item.get("text", ""),
        direction=item.get("direction", ""),
        channel=item.get("channel", ""),
        trace_id=item.get("trace_id", ""),
        meta=meta,
        ts=int(item["ts"]),
    )


def get_dialog_history(
    *,
    limit: int = 50,
    channel: str | None = None,
    direction: str | None = None,
    trace_id: str | None = None,
    ascending: bool = False,
) -> list[DialogRecord]:
    """Получить историю сообщений с возможностью фильтрации."""

    trace, token = _ensure_trace(trace_id)
    log.debug(
        "fetch dialog history",
        extra={
            "ctx": {
                "trace_id": trace,
                "limit": limit,
                "channel": channel,
                "direction": direction,
                "ascending": ascending,
            }
        },
    )
    try:
        rows = dialogs.fetch_history(
            limit=limit,
            channel=channel,
            direction=direction,
            trace_id=trace,
            ascending=ascending,
        )
    finally:
        if token is not None:
            TRACE_ID.reset(token)
    records = [_normalize_history_entry(row) for row in rows]
    log.debug(
        "history fetched",
        extra={"ctx": {"trace_id": trace, "returned": len(records)}},
    )
    return records
