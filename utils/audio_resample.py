"""Утилиты для нормализации аудиопотока.

В модуле собраны вспомогательные функции, которые переиспользуются
различными компонентами, работающими с PCM-потоком.  Основная задача —
безопасно и предсказуемо приводить синтезированное аудио к единой
частоте дискретизации 16 кГц, необходимой для корректного обмена данными
между сервером и роботом.
"""
from __future__ import annotations

import logging
from typing import Final

import numpy as np

# Подробное логирование помогает быстро диагностировать артефакты и
# несоответствия в аудиопотоке, поэтому модуль настраивает собственный
# логер.  Название выбрано уникальным, чтобы его легко было фильтровать
# в сторонних системах наблюдаемости.
log: Final = logging.getLogger("utils.audio_resample")


def resample_pcm16(pcm: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Приводит сигнал ``pcm`` из ``source_rate`` к ``target_rate``.

    Функция специально реализована без сторонних зависимостей, чтобы
    избежать проблем с установкой во встраиваемых средах.  Алгоритм
    использует линейную интерполяцию, которая даёт достаточное качество
    для речевых сигналов и при этом предсказуемо работает на микроконтроллере.

    Parameters
    ----------
    pcm:
        Монофонический массив ``int16`` с исходным сигналом.
    source_rate:
        Частота дискретизации входного массива.
    target_rate:
        Требуемая частота дискретизации выходного массива.

    Returns
    -------
    numpy.ndarray
        Новый массив ``int16`` с нормализованной частотой
        дискретизации.  Если вход пустой или частоты совпадают, функция
        возвращает исходный объект без копирования.
    """
    if pcm.size == 0:
        log.debug(
            "Получен пустой массив для ресемплинга, возвращаем как есть",
            extra={"attrs": {"source_rate": source_rate, "target_rate": target_rate}},
        )
        return pcm

    if source_rate == target_rate:
        log.debug(
            "Частоты совпадают, ресемплинг не требуется",
            extra={"attrs": {"sample_rate": source_rate}},
        )
        return pcm

    if source_rate <= 0 or target_rate <= 0:
        raise ValueError(
            "Частоты дискретизации должны быть положительными числами"
        )

    duration = pcm.size / float(source_rate)
    target_size = max(int(round(duration * target_rate)), 1)

    log.debug(
        "Запускаем ресемплинг аудио",
        extra={
            "attrs": {
                "source_rate": source_rate,
                "target_rate": target_rate,
                "source_samples": pcm.size,
                "target_samples": target_size,
            }
        },
    )

    # Формируем временные шкалы для исходного и результирующего сигналов.
    source_times = np.linspace(0.0, duration, pcm.size, endpoint=False, dtype=np.float64)
    target_times = np.linspace(0.0, duration, target_size, endpoint=False, dtype=np.float64)

    # Проводим линейную интерполяцию в плавающей точке, после чего
    # возвращаем значения к диапазону int16.  Используем clip, чтобы
    # гарантировать отсутствие переполнений.
    resampled = np.interp(target_times, source_times, pcm.astype(np.float32))
    resampled = np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)

    log.debug(
        "Ресемплинг завершён",
        extra={
            "attrs": {
                "result_samples": int(resampled.size),
                "duration_sec": float(resampled.size) / float(target_rate),
            }
        },
    )
    return resampled
