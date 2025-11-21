"""Клиент для обращения к облачному агенту Xiaozhi.

Модуль изолирует сетевое общение с сервисом, чтобы легко переключать
бэкенды LLM через конфигурацию. Запросы и ответы подробно логируются,
в коде добавлены русские комментарии для быстрой поддержки.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)


@dataclass
class XiaozhiSettings:
    """Настройки соединения с облачным агентом."""

    endpoint: str
    agent_code: str
    timeout: float = 40.0


class XiaozhiClient:
    """Простой HTTP‑клиент для обмена текстовыми запросами.

    Используется POST‑endpoint облачного сервиса, куда передаём share code
    агента и текстовый prompt. Клиент не делает предположений о конкретном
    формате ответа: он извлекает текст либо из поля ``text``, либо из
    ``message`` / ``content`` внутри словаря ``data``.
    """

    def __init__(self, settings: XiaozhiSettings) -> None:
        self.settings = settings

    def _build_payload(self, prompt: str) -> Dict[str, Any]:
        """Сформировать JSON‑тело запроса.

        На большинстве сайтов Xiaozhi ожидает два ключа: share code агента и
        входной текст. Выносим подготовку в отдельный метод, чтобы упростить
        тестирование и последующую адаптацию под изменения API.
        """

        return {
            "share_code": self.settings.agent_code,
            "input": prompt,
            "stream": False,
        }

    def ask(self, prompt: str, *, trace_id: str = "") -> str:
        """Отправить запрос в Xiaozhi и вернуть текстовый ответ.

        При сетевой ошибке или пустом ответе возбуждается :class:`RuntimeError`.
        """

        payload = self._build_payload(prompt)
        headers = {"Content-Type": "application/json"}
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        logger.debug(
            "Отправка запроса в Xiaozhi", extra={"endpoint": self.settings.endpoint, "trace_id": trace_id}
        )

        try:
            response = requests.post(
                self.settings.endpoint,
                json=payload,
                headers=headers,
                timeout=self.settings.timeout,
            )
        except requests.RequestException as exc:  # pragma: no cover - сетевые сбои вне тестов
            logger.error("Сервис Xiaozhi недоступен: %s", exc)
            raise RuntimeError("Не удалось связаться с Xiaozhi") from exc

        if not response.ok:
            logger.error(
                "Xiaozhi вернул ошибку", extra={"status": response.status_code, "body": response.text}
            )
            raise RuntimeError(f"Сервис Xiaozhi вернул {response.status_code}")

        try:
            data: Dict[str, Any] = response.json()
        except ValueError as exc:
            logger.error("Некорректный JSON от Xiaozhi: %s", response.text)
            raise RuntimeError("Xiaozhi вернул невалидный JSON") from exc

        # Пытаемся аккуратно извлечь текстовую часть ответа
        text = self._extract_text(data)
        if not text:
            logger.error("Пустой ответ от Xiaozhi", extra={"raw": data})
            raise RuntimeError("Xiaozhi не вернул текст ответа")

        logger.info("Ответ Xiaozhi длиной %d символов", len(text), extra={"trace_id": trace_id})
        return text

    @staticmethod
    def _extract_text(payload: Dict[str, Any]) -> str:
        """Выделить осмысленный текст из произвольной структуры.

        Многие реализации возвращают поле ``text`` или вложенный объект
        ``data`` с ключами ``text``/``message``/``content``. Чтобы оставаться
        совместимыми с бесплатными облачными прокси, извлекаем строку по этим
        ключам в порядке приоритета.
        """

        direct = payload.get("text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("text", "message", "content"):
                candidate = data.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()

        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()

        return ""

