"""Модуль управления настроением на основе координат valence/arousal.

Значения хранятся в БД и обновляются с использованием экспоненциального
скользящего среднего (EMA). Все операции сопровождаются
структурированным JSON‑логированием для удобной отладки.
"""

from __future__ import annotations

# Стандартные библиотеки
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import yaml

from memory import db


@dataclass
class Mood:
    """Класс, описывающий текущее настроение ассистента."""

    valence: float = 0.0  # горизонтальная ось: удовольствие/неудовольствие
    arousal: float = 0.0  # вертикальная ось: возбуждённость/подавленность
    config_path: Path = Path("config/affect.yaml")

    def __post_init__(self) -> None:
        """Загружаем конфигурацию при создании объекта."""
        self._logger = logging.getLogger(__name__)
        self._load_config()

    # ------------------------------------------------------------------ utils
    def _load_config(self) -> None:
        """Прочитать коэффициенты из YAML‑файла конфигурации."""
        if self.config_path.exists():
            with self.config_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        else:
            data = {}
        # Коэффициенты масштабирования и сглаживания
        self._valence_factor = float(data.get("valence_factor", 1.0))
        self._arousal_factor = float(data.get("arousal_factor", 1.0))
        self._ema_alpha = float(data.get("ema_alpha", 0.5))

    def _ema(self, previous: float, new: float) -> float:
        """Расчёт EMA для плавного изменения значения."""
        return (1 - self._ema_alpha) * previous + self._ema_alpha * new

    @staticmethod
    def _clamp(value: float, min_value: float = -1.0, max_value: float = 1.0) -> float:
        """Ограничить значение диапазоном [min_value, max_value]."""
        return max(min_value, min(max_value, value))

    # --------------------------------------------------------------- operations
    def update(self, valence_delta: float, arousal_delta: float, trace_id: str | None = None) -> None:
        """Обновить настроение с учётом дельт и коэффициентов.

        Параметры
        ---------
        valence_delta: float
            Изменение по оси valence (до масштабирования).
        arousal_delta: float
            Изменение по оси arousal (до масштабирования).
        trace_id: str | None
            Идентификатор запроса для трассировки логов.
        """
        start = time.time()

        # Применяем масштабирование из конфигурации
        target_valence = self.valence + valence_delta * self._valence_factor
        target_arousal = self.arousal + arousal_delta * self._arousal_factor

        # Ограничиваем значения в диапазоне [-1.0, 1.0]
        target_valence = self._clamp(target_valence)
        target_arousal = self._clamp(target_arousal)

        # EMA‑сглаживание для плавного перехода
        self.valence = self._ema(self.valence, target_valence)
        self.arousal = self._ema(self.arousal, target_arousal)

        duration = int((time.time() - start) * 1000)
        self._logger.info(
            json.dumps(
                {
                    "event": "mood.update",
                    "trace_id": trace_id,
                    "valence": self.valence,
                    "arousal": self.arousal,
                    "duration_ms": duration,
                },
                ensure_ascii=False,
            )
        )

    # --------------------------------------------------------------- persistence
    def save(self, trace_id: str | None = None) -> None:
        """Сохранить текущее состояние в БД."""
        start = time.time()
        db.set_mood_state(self.valence, self.arousal, trace_id=trace_id)
        duration = int((time.time() - start) * 1000)
        self._logger.info(
            json.dumps(
                {
                    "event": "mood.save",
                    "trace_id": trace_id,
                    "duration_ms": duration,
                },
                ensure_ascii=False,
            )
        )

    @classmethod
    def load(cls, trace_id: str | None = None) -> "Mood":
        """Восстановить объект из БД."""
        start = time.time()
        valence, arousal = db.get_mood_state(trace_id=trace_id)
        duration = int((time.time() - start) * 1000)
        logging.getLogger(__name__).info(
            json.dumps(
                {
                    "event": "mood.load",
                    "trace_id": trace_id,
                    "duration_ms": duration,
                },
                ensure_ascii=False,
            )
        )
        return cls(valence=valence, arousal=arousal)

    # --------------------------------------------------------------- utilities
    def as_tuple(self) -> Tuple[float, float]:
        """Вернуть текущее состояние в виде кортежа."""
        return self.valence, self.arousal

