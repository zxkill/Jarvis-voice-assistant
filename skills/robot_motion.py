from __future__ import annotations

"""Голосовые команды движения робота.

Скилл намеренно работает маленькими безопасными шагами. Для «поехали далеко»
нужна будущая навигация, а здесь только Body Control v1: короткое движение,
поворот, стоп и статус.
"""

import re
from typing import Any

from core.logging_json import configure_logging
from core.nlp import normalize
from robot.body_controller import BodyController, RobotBodyError, RobotBodyUnavailable

log = configure_logging("skills.robot_motion")

PATTERNS = [
    "вперёд",
    "вперед",
    "едь вперёд",
    "поехали вперёд",
    "проедь вперёд",
    "на один метр вперёд",
    "на метр вперёд",
    "на полметра вперёд",
    "подъедь",
    "подъедь ко мне",
    "ближе",
    "назад",
    "едь назад",
    "проедь назад",
    "на один метр назад",
    "на метр назад",
    "на полметра назад",
    "отъедь",
    "отъедь назад",
    "поверни налево",
    "поверни влево",
    "поверни налево на тридцать градусов",
    "налево",
    "влево",
    "поверни направо",
    "поверни вправо",
    "поверни направо на тридцать градусов",
    "направо",
    "вправо",
    "стоп",
    "остановись",
    "аварийная остановка",
    "статус робота",
    "состояние робота",
    "как робот",
    "робот статус",
]

_SMALL_WORDS = {"чуть", "слегка", "немного", "маленько"}
_NUMBER_WORDS = {
    "один": 1.0,
    "одна": 1.0,
    "одно": 1.0,
    "два": 2.0,
    "две": 2.0,
    "три": 3.0,
    "четыре": 4.0,
    "пять": 5.0,
    "десять": 10.0,
    "пятнадцать": 15.0,
    "двадцать": 20.0,
    "тридцать": 30.0,
    "сорок": 40.0,
    "пятьдесят": 50.0,
    "шестьдесят": 60.0,
    "девяносто": 90.0,
}


def _norm(text: str) -> str:
    return normalize(text).lower().replace("ё", "е")


def _contains_any(text: str, words: set[str] | tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _extract_first_number(text: str) -> float | None:
    """Достаёт первое число из текста, включая простые русские числительные."""

    match = re.search(r"(\d+(?:[\.,]\d+)?)", text)
    if match:
        return float(match.group(1).replace(",", "."))
    words = text.split()
    for word in words:
        if word in _NUMBER_WORDS:
            return _NUMBER_WORDS[word]
    if "полметра" in text or "пол метра" in text:
        return 0.5
    return None


def _distance_from_text(text: str) -> float | None:
    """Парсит дистанцию в метрах. None означает использовать безопасный default."""

    if _contains_any(text, _SMALL_WORDS):
        return 0.15
    number = _extract_first_number(text)
    if number is None:
        return None
    if "см" in text or "сантим" in text:
        return number / 100.0
    if "миллимет" in text or "мм" in text:
        return number / 1000.0
    return number


def _angle_from_text(text: str) -> float | None:
    """Парсит угол в градусах. None означает использовать безопасный default."""

    if _contains_any(text, _SMALL_WORDS):
        return 20.0
    number = _extract_first_number(text)
    if number is None:
        return None
    return number


def _reply_from_result(prefix: str, result: dict[str, Any]) -> str:
    message = str(result.get("message") or "").strip()
    if message:
        log.debug("Ответ ESP32: %s", message)
    return prefix


def handle(text: str, trace_id: str | None = None) -> str:
    """Обрабатывает голосовую команду движения."""

    raw = _norm(text)
    log.info("Команда движения получена", extra={"ctx": {"trace_id": trace_id}, "attrs": {"text": raw}})
    controller = BodyController()

    try:
        if _contains_any(raw, ("стоп", "останов", "аварийн")):
            controller.stop()
            return "Остановил."

        if _contains_any(raw, ("статус", "состояние", "как робот", "робот как")):
            return controller.status_summary()

        if _contains_any(raw, ("назад", "отъедь", "отъехать")):
            distance = _distance_from_text(raw)
            result = controller.move("backward", distance_m=distance)
            return _reply_from_result("Отъезжаю назад.", result)

        if _contains_any(raw, ("вперед", "подъедь", "поехали", "ближе")):
            distance = _distance_from_text(raw)
            result = controller.move("forward", distance_m=distance)
            return _reply_from_result("Еду вперёд.", result)

        if _contains_any(raw, ("налево", "влево", "лево")):
            angle = _angle_from_text(raw)
            result = controller.rotate("left", angle_deg=angle)
            return _reply_from_result("Поворачиваю налево.", result)

        if _contains_any(raw, ("направо", "вправо", "право")):
            angle = _angle_from_text(raw)
            result = controller.rotate("right", angle_deg=angle)
            return _reply_from_result("Поворачиваю направо.", result)

        log.info("Фраза похожа на движение, но направление не найдено", extra={"attrs": {"text": raw}})
        return "Я понял, что это команда движения, но не понял направление. Скажи: вперёд, назад, налево, направо или стоп."

    except RobotBodyUnavailable as exc:
        log.warning("Робот недоступен для движения", extra={"attrs": {"error": str(exc)}})
        return "Я пока не вижу робота в сети. Проверь IP ESP32 или подключение к Wi-Fi."
    except RobotBodyError as exc:
        log.warning("Команда движения отклонена", extra={"attrs": {"error": str(exc)}})
        return str(exc)
    except Exception as exc:  # pragma: no cover - защита от неожиданностей на железе
        log.exception("Неожиданная ошибка команды движения")
        return f"Не смог выполнить команду движения: {exc}"
