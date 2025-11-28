"""Проверки подготовки аудио к распознаванию.

Здесь покрываем вспомогательную функцию ``_prepare_pcm_for_stt``, чтобы
убедиться: при расхождении частот кадров робота и движка Vosk выполняется
ресемплинг, а при совпадении параметров байтовый поток остаётся неизменным.
"""

from __future__ import annotations

from types import SimpleNamespace

import start


class _LogCollector:
    """Простой логгер, собирающий вызовы ``info`` в список для ассертов."""

    def __init__(self) -> None:
        self.records: list[SimpleNamespace] = []

    def info(self, message: str, extra: dict | None = None) -> None:  # pragma: no cover - типовая сигнатура
        # Сохраняем текст и атрибуты, чтобы тесты могли проверить содержимое.
        self.records.append(SimpleNamespace(message=message, extra=extra or {}))


def test_prepare_pcm_resamples_when_rates_differ(monkeypatch):
    """При разных частотах вызывается ресемплинг и возвращается новый поток."""

    captured = {}

    def fake_resample(pcm: bytes, from_rate: int, to_rate: int, channels: int, sample_bits: int):
        captured["args"] = (from_rate, to_rate, channels, sample_bits, len(pcm))
        # Возвращаем удвоенный объём, чтобы точно увидеть изменение размеров.
        return b"\x01\x00\x02\x00"

    monkeypatch.setattr(start, "_resample_pcm", fake_resample)
    log = _LogCollector()

    pcm, samples = start._prepare_pcm_for_stt(
        raw_pcm=b"\x00\x01",
        source_sample_rate=44_100,
        stt_sample_rate=16_000,
        stt_log=log,
    )

    assert pcm == b"\x01\x00\x02\x00"
    assert samples == 2
    assert captured["args"] == (44_100, 16_000, 1, 16, 2)
    # Проверяем, что в логах появились оба сообщения о начале и конце ресемплинга.
    assert any("Ресемплирую" in rec.message for rec in log.records)
    assert any("Ресемплирование завершено" in rec.message for rec in log.records)


def test_prepare_pcm_no_resample_when_rate_matches(monkeypatch):
    """При одинаковых частотах байты остаются без изменений, лог пуст."""

    def fail_resample(*_args, **_kwargs):  # pragma: no cover - должен быть пропущен
        raise AssertionError("_resample_pcm не должен вызываться при совпадении частот")

    monkeypatch.setattr(start, "_resample_pcm", fail_resample)
    log = _LogCollector()

    pcm, samples = start._prepare_pcm_for_stt(
        raw_pcm=b"\x10\x27",
        source_sample_rate=16_000,
        stt_sample_rate=16_000,
        stt_log=log,
    )

    assert pcm == b"\x10\x27"
    assert samples == 1
    assert log.records == []
