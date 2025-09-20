"""Логирование диалогов пользователя с ассистентом в SQLite."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime
from typing import Any, Iterable

from core.logging_json import configure_logging, TRACE_ID, new_trace_id

from .db import get_connection, encrypt, decrypt

# Инициализируем модульный логгер. Компонент ``memory.dialogs`` выводит
# подробные сообщения при записи и чтении истории, что облегчает отладку
# и аудит пользовательских обращений.
log = configure_logging("memory.dialogs")

# Допустимые значения направления сообщения, чтобы избежать опечаток
# в вызывающем коде. Храним их в виде множества для быстрого доступа.
_ALLOWED_DIRECTIONS = {"incoming", "outgoing"}


def _safe_encrypt(value: str | None) -> str | None:
    """Аккуратно зашифровать значение, не прерывая основной поток."""

    if value is None:
        return None
    try:
        return encrypt(value)
    except RuntimeError:
        # Если ключ шифрования не задан, фиксируем предупреждение и
        # сохраняем значение в открытом виде, чтобы не потерять данные.
        log.warning(
            "encryption key missing, storing dialog message as plain text"
        )
        return value
    except Exception:
        # Неожиданная ошибка при шифровании — логируем стек, но не
        # мешаем основному сценарию, чтобы диалог всё равно сохранился.
        log.exception("failed to encrypt dialog payload")
        return value


def _safe_decrypt(value: str | None) -> str | None:
    """Расшифровать значение, если оно было зашифровано ранее."""

    if value is None:
        return None
    try:
        return decrypt(value)
    except RuntimeError:
        # Если ключ отсутствует, считаем, что данные сохранены в открытом виде.
        log.warning("decryption key missing, returning plain text")
        return value
    except Exception:
        # При сбоях возвращаем исходную строку, чтобы журнал был доступен.
        log.exception("failed to decrypt dialog payload")
        return value


def log_message(
    text: str,
    *,
    direction: str,
    channel: str,
    trace_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Сохранить сообщение диалога в базе данных."""

    if direction not in _ALLOWED_DIRECTIONS:
        raise ValueError(f"unknown direction: {direction}")

    payload = text.strip()
    ts = int(time.time())
    meta_json = json.dumps(metadata or {}, ensure_ascii=False) if metadata else None
    enc_payload = _safe_encrypt(payload)
    enc_meta = _safe_encrypt(meta_json)

    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO dialog_messages (ts, direction, channel, trace_id, message, meta)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ts, direction, channel, trace_id or "", enc_payload, enc_meta),
            )
            row_id = int(cur.lastrowid)
    except sqlite3.Error:
        log.exception(
            "failed to store dialog message",
            extra={
                "ctx": {
                    "direction": direction,
                    "channel": channel,
                    "trace_id": trace_id,
                }
            },
        )
        return -1

    log.debug(
        "dialog message stored",
        extra={
            "ctx": {
                "id": row_id,
                "direction": direction,
                "channel": channel,
                "trace_id": trace_id or "",
                "length": len(payload),
            }
        },
    )
    return row_id


def fetch_history(
    *,
    limit: int = 50,
    channel: str | None = None,
    direction: str | None = None,
    trace_id: str | None = None,
    ascending: bool = False,
) -> list[dict[str, Any]]:
    """Вернуть список сообщений диалога с учётом фильтров."""

    clauses: list[str] = []
    params: list[Any] = []
    if channel:
        clauses.append("channel = ?")
        params.append(channel)
    if direction:
        if direction not in _ALLOWED_DIRECTIONS:
            raise ValueError(f"unknown direction: {direction}")
        clauses.append("direction = ?")
        params.append(direction)
    if trace_id:
        clauses.append("trace_id = ?")
        params.append(trace_id)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order = "ASC" if ascending else "DESC"
    query = (
        "SELECT id, ts, direction, channel, trace_id, message, meta "
        "FROM dialog_messages "
        f"{where} "
        f"ORDER BY ts {order} "
        "LIMIT ?"
    )
    params.append(int(limit))

    log.debug(
        "fetching dialog history",
        extra={"ctx": {"channel": channel, "direction": direction, "trace_id": trace_id, "limit": limit}},
    )

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()

    history: list[dict[str, Any]] = []
    for row in rows:
        raw_meta = _safe_decrypt(row["meta"])
        try:
            meta_obj = json.loads(raw_meta) if raw_meta else {}
        except json.JSONDecodeError:
            log.warning(
                "stored dialog meta is not valid JSON",
                extra={"ctx": {"id": row["id"], "meta": raw_meta}},
            )
            meta_obj = {"raw": raw_meta} if raw_meta else {}
        history.append(
            {
                "id": int(row["id"]),
                "ts": int(row["ts"]),
                "datetime": datetime.fromtimestamp(int(row["ts"])),
                "direction": row["direction"],
                "channel": row["channel"],
                "trace_id": row["trace_id"],
                "text": _safe_decrypt(row["message"]) or "",
                "meta": meta_obj,
            }
        )

    return history


def ensure_trace_id() -> str:
    """Гарантировать наличие ``trace_id`` в контексте и вернуть его."""

    trace = TRACE_ID.get()
    if trace:
        return trace
    trace = new_trace_id()
    TRACE_ID.set(trace)
    log.debug("generated new trace id for dialog", extra={"ctx": {"trace_id": trace}})
    return trace


def log_dialog_pair(user_text: str, assistant_text: str, *, channel: str) -> Iterable[int]:
    """Сохранить пару «пользователь/ассистент» одним вызовом."""

    trace = ensure_trace_id()
    incoming_id = log_message(
        user_text,
        direction="incoming",
        channel=channel,
        trace_id=trace,
    )
    outgoing_id = log_message(
        assistant_text,
        direction="outgoing",
        channel=channel,
        trace_id=trace,
    )
    return incoming_id, outgoing_id


if __name__ == "__main__":  # pragma: no cover - вспомогательная утилита
    import argparse

    parser = argparse.ArgumentParser(description="Просмотр истории диалогов Jarvis")
    parser.add_argument("--limit", type=int, default=20, help="Сколько сообщений показать")
    parser.add_argument("--channel", type=str, default=None, help="Фильтр по каналу (voice/telegram)")
    parser.add_argument(
        "--direction",
        type=str,
        default=None,
        choices=sorted(_ALLOWED_DIRECTIONS),
        help="Фильтр по направлению",
    )
    parser.add_argument("--trace-id", dest="trace_id", type=str, default=None, help="Поиск по trace_id")
    parser.add_argument("--asc", action="store_true", help="Показывать от старых к новым")
    args = parser.parse_args()

    for item in fetch_history(
        limit=args.limit,
        channel=args.channel,
        direction=args.direction,
        trace_id=args.trace_id,
        ascending=args.asc,
    ):
        timestamp = item["datetime"].strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"[{timestamp}] {item['channel']} {item['direction']} "
            f"trace={item['trace_id']} -> {item['text']}"
        )
