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

from jarvis_skills import handle_utterance
from core.nlp import normalize
from working_tts import speak_async

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

    Если активация не обнаружена, возвращается пустая строка.
    """
    text = text.lower().strip()
    if not text:
        return ""
    words = text.split()
    if _matches_activation(words[0]):
        return " ".join(words[1:]).strip()
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
    # handle_utterance может блокировать, поэтому вызываем в отдельном потоке
    if await asyncio.to_thread(handle_utterance, cmd):
        return True
    raw = await filter_cmd(cmd)
    raw_norm = normalize(raw)
    cmd_info = await recognize_cmd(raw_norm)
    if not cmd_info["cmd"] or cmd_info["percent"] < CMD_CONFIDENCE_THRESHOLD:
        return False
    return await execute_cmd(cmd_info["cmd"], voice)
