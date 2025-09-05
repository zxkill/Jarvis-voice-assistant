"""Тесты, проверяющие соблюдение приватности и безопасности."""

from __future__ import annotations

import json
import os
import configparser

import pytest
from cryptography.fernet import Fernet

# Перед импортом модулей, использующих шифрование, задаём ключ
os.environ.setdefault("JARVIS_DB_KEY", Fernet.generate_key().decode())

from display import DisplayDriver, DisplayItem, init_driver  # noqa: E402
import sensors  # noqa: E402
from memory import writer, db  # noqa: E402
from core.logging_json import configure_logging  # noqa: E402


class DummyDriver(DisplayDriver):
    """Минимальный драйвер дисплея для захвата выводимых элементов."""

    def __init__(self) -> None:  # noqa: D401
        self.items: list[DisplayItem] = []

    def draw(self, item: DisplayItem) -> None:  # noqa: D401
        self.items.append(item)

    def process_events(self) -> None:  # noqa: D401
        pass


def test_consent_and_indicator() -> None:
    """Без согласия сенсор не активируется, индикатор появляется."""

    driver = DummyDriver()
    init_driver(driver=driver)
    sensors.revoke_consent("camera")
    with pytest.raises(PermissionError):
        sensors.set_active("camera", True)
    sensors.grant_consent("camera")
    sensors.set_active("camera", True)
    assert any(i.kind == "camera" and i.payload for i in driver.items)
    sensors.set_active("camera", False)
    assert any(i.kind == "camera" and i.payload is None for i in driver.items)


def test_encryption_roundtrip() -> None:
    """Данные шифруются перед сохранением и корректно расшифровываются."""

    event_id = writer.write_event("t", {"secret": 1})
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT payload FROM events WHERE id = ?", (event_id,)
        ).fetchone()
    assert row is not None
    stored = row["payload"]
    assert stored != json.dumps({"secret": 1})
    assert db.decrypt(stored) == json.dumps({"secret": 1})


def test_logging_anonymization(capsys: pytest.CaptureFixture[str]) -> None:
    """Персональные данные в логах маскируются."""

    logger = configure_logging("test")
    logger.info(
        "Пользователь test@example.com", extra={"attrs": {"email": "user@example.com", "phone": "1234567890"}}
    )
    out = capsys.readouterr().err.strip()
    data = json.loads(out)
    assert data["message"] == "Пользователь <email>"
    assert data["attrs"]["email"] == "<email>"
    assert data["attrs"]["phone"] == "<num>"


def test_config_privacy_section() -> None:
    """Файл конфигурации содержит секцию PRIVACY с нужными полями."""

    cfg = configparser.ConfigParser()
    cfg.read("config.ini")
    assert cfg.get("PRIVACY", "quiet_hours_start") == "23:00"
    assert cfg.get("PRIVACY", "quiet_hours_end") == "08:00"
    assert cfg.getint("PRIVACY", "initiative_limit_per_day") == 5

