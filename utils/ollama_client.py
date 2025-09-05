"""Клиент для взаимодействия с локальной моделью Ollama.

Поддерживаются два профиля генерации текста:
- "light" — лёгкая и быстрая модель;
- "heavy" — более качественная, но медленная модель.

Клиент отправляет HTTP-запросы к серверу Ollama и возвращает сгенерированный
текст. Весь код сопровождается подробными комментариями на русском языке
и расширенным логированием для облегчения отладки.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict

import requests


logger = logging.getLogger(__name__)


@dataclass
class OllamaClient:
    """Простой HTTP-клиент для локального сервера Ollama."""

    base_url: str = "http://localhost:11434"  # адрес сервера
    profiles: Dict[str, str] = field(
        default_factory=lambda: {"light": "llama2", "heavy": "llama2:13b"}
    )
    timeout: int = 60  # таймаут запросов в секундах

    def generate(self, prompt: str, profile: str = "light") -> str:
        """Сгенерировать ответ модели согласно выбранному профилю.

        :param prompt: Подготовленный текст запроса для модели.
        :param profile: Ключ из словаря ``profiles`` (``light`` или ``heavy``).
        :return: Строка с ответом модели.
        :raises ValueError: если запрошен неизвестный профиль.
        :raises RuntimeError: если запрос завершился ошибкой.
        """

        if profile not in self.profiles:
            raise ValueError(f"Неизвестный профиль: {profile}")
        model = self.profiles[profile]
        # Новый эндпоинт: /v1/chat/completions, совместимый с OpenAI API
        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }

        logger.debug(
            "Отправка запроса в Ollama", extra={"url": url, "model": model}
        )
        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:  # сетевые ошибки и HTTP !=200
            logger.error("Ошибка при обращении к Ollama: %s", exc)
            raise RuntimeError("Ollama недоступна") from exc

        # Ответ от Ollama: {"choices": [{"message": {"content": "..."}}]}
        try:
            data = response.json()
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                raise KeyError("choices")
            message = choices[0].get("message", {})
            if not isinstance(message, dict):
                raise TypeError("message should be dict")
            text = str(message.get("content", ""))
        except Exception as exc:  # ошибка разбора JSON
            logger.error("Некорректный ответ от Ollama: %s", exc)
            raise RuntimeError("Ollama вернула невалидный JSON") from exc

        logger.debug("Получен ответ от Ollama", extra={"length": len(text)})
        return text
