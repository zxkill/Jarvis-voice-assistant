from __future__ import annotations
"""Утилиты для разбора и выполнения голосовых команд.

Модуль содержит общий набор функций, которые используются стартовым скриптом
Jarvis: разбор фраз, поиск совпадений с именем ассистента, фильтрация
служебных слов и вызов соответствующих реакций. Комментарии призваны
объяснить внутреннюю логику, чтобы модуль было легко поддерживать.
"""

from typing import Any, Dict, List

import asyncio
from rapidfuzz import fuzz

import jarvis_skills
handle_utterance = jarvis_skills.handle_utterance
from core.nlp import normalize
from working_tts import speak_async
from core.request_source import get_request_source
from core.logging_json import configure_logging
from core import events as core_events
from memory.writer import add_suggestion_feedback
from proactive.engine import is_awaiting_response, pop_awaiting

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


def _is_positive_answer(text: str) -> bool:
    """Определить, является ли ответ пользователя согласием."""

    text = text.lower()
    positives = ("да", "ок", "хорошо", "ладно")
    negatives = ("нет", "не", "потом", "позже")
    if any(word in text for word in positives):
        return True
    if any(word in text for word in negatives):
        return False
    # По умолчанию считаем ответ отрицательным.
    return False


def process_suggestion_answer(text: str) -> None:
    """Обработать ответ пользователя на проактивную подсказку."""

    # Забираем из ``ProactiveEngine`` информацию об ожидаемой подсказке.
    awaiting = pop_awaiting()
    if not awaiting:
        log.debug("нет ожидания подсказки, пропускаем ответ: %r", text)
        return
    suggestion_id = awaiting.get("id")
    log.info(
        "processing suggestion answer",
        extra={"ctx": {"suggestion_id": suggestion_id, "text": text}},
    )
    accepted = _is_positive_answer(text)
    # Сохраняем отзыв пользователя в памяти.
    add_suggestion_feedback(suggestion_id, text, accepted)
    # Публикуем событие для остальных компонентов системы.
    core_events.publish(
        core_events.Event(
            kind="suggestion.response",
            attrs={"suggestion_id": suggestion_id, "text": text, "accepted": accepted},
        )
    )
    log.debug(
        "suggestion response published",
        extra={"ctx": {"suggestion_id": suggestion_id, "accepted": accepted}},
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


async def va_respond(voice: str) -> bool:
    """Главная реакция ассистента на распознанный текст.

    1. Отделяем команду от слова активации.
    2. Пробуем передать команду в систему навыков ``jarvis_skills``.
    3. Если ни один навык не сработал — пытаемся сопоставить её с
       набором встроенных команд.
    Возвращает ``True``, если что‑то было выполнено.
    """
    cmd = extract_cmd(voice)
    if not cmd:
        return False
    # Если ``ProactiveEngine`` ждёт ответа на подсказку, обрабатываем его
    # отдельно и выходим, чтобы не запускать обычный пайплайн команд.
    if is_awaiting_response():
        process_suggestion_answer(cmd)
        return True
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
        return False
    return await execute_cmd(cmd_info["cmd"], voice)
