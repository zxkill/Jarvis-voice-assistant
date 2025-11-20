import configparser
import pytest

import start


class _DummyStream:
    """Простой тестовый поток, имитирующий поведение ``RobotAudioStream``."""

    def __init__(self, *, endpoint: str, **_: object) -> None:
        from urllib.parse import urlparse

        self.started = False
        self.endpoint = endpoint
        self._parsed = urlparse(endpoint)

    async def start(self) -> None:  # pragma: no cover - вызывается явно в тестах
        self.started = True


@pytest.mark.asyncio
async def test_start_robot_audio_stream_success(monkeypatch):
    """Успешный запуск возвращает созданный поток и помечает его как запущенный."""

    cfg = configparser.ConfigParser()
    cfg.read_dict(
        {
            "ROBOT_AUDIO": {"endpoint": "ws://127.0.0.1:0/", "ping_interval": "1"},
            "AUDIO": {"queue_max": "2", "sample_rate": "8000"},
        }
    )

    created: list[_DummyStream] = []

    def _factory(**kwargs):
        stream = _DummyStream(**kwargs)
        created.append(stream)
        return stream

    monkeypatch.setattr(start, "RobotAudioStream", _factory, raising=False)

    stream = await start.start_robot_audio_stream(cfg)

    assert stream is created[0]
    assert stream.started
    assert stream._parsed.port == 0


@pytest.mark.asyncio
async def test_start_robot_audio_stream_handles_oserror(monkeypatch, caplog):
    """При сетевой ошибке возвращается ``None`` без выбрасывания исключения."""

    cfg = configparser.ConfigParser()
    cfg.read_dict(
        {
            "ROBOT_AUDIO": {"endpoint": "ws://192.0.2.1:8765/", "ping_interval": "1"},
            "AUDIO": {"queue_max": "2", "sample_rate": "8000"},
        }
    )

    class _FailingStream(_DummyStream):
        async def start(self) -> None:
            raise OSError(99, "cannot assign requested address")

    monkeypatch.setattr(start, "RobotAudioStream", _FailingStream, raising=False)

    with caplog.at_level("ERROR", logger="app"):
        stream = await start.start_robot_audio_stream(cfg)

    assert stream is None
