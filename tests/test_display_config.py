import configparser
import os
import pytest

# Устанавливаем заглушечный ключ, чтобы импорт ``start`` не падал на проверке API
os.environ.setdefault("INTEL_API_KEY", "test")
os.environ.setdefault("TELEGRAM_TOKEN", "test")

from start import init_display_from_config
from display.drivers.console import ConsoleDisplayDriver
from display.drivers.none import NoneDisplayDriver
import display


@pytest.fixture(autouse=True)
def reset_display_driver():
    """Сбрасываем глобальный драйвер перед каждым тестом."""
    display._driver = None  # type: ignore[attr-defined]
    yield
    display._driver = None  # type: ignore[attr-defined]


def test_init_display_console():
    """Проверяем, что по настройке ``console`` выбирается консольный драйвер."""
    cfg = configparser.ConfigParser()
    cfg.add_section("DISPLAY")
    cfg.set("DISPLAY", "driver", "console")
    driver = init_display_from_config(cfg)
    assert isinstance(driver, ConsoleDisplayDriver)


def test_init_display_wait_ready(monkeypatch):
    """Если драйвер сообщает о неготовности, должна подниматься ошибка."""
    cfg = configparser.ConfigParser()
    cfg.add_section("DISPLAY")
    cfg.set("DISPLAY", "driver", "serial")

    class DummyDriver:
        def wait_ready(self):
            return False

    def fake_init(name):
        return DummyDriver()

    # подменяем фабрику драйверов
    monkeypatch.setattr("start.init_driver", fake_init)

    with pytest.raises(RuntimeError):
        init_display_from_config(cfg)


def test_init_display_none():
    """Если выбран драйвер ``none``, вывод полностью отключается."""
    cfg = configparser.ConfigParser()
    cfg.add_section("DISPLAY")
    cfg.set("DISPLAY", "driver", "none")
    driver = init_display_from_config(cfg)
    assert isinstance(driver, NoneDisplayDriver)


def test_init_display_default_none():
    """При отсутствии секции ``DISPLAY`` используется драйвер ``none``."""
    cfg = configparser.ConfigParser()
    driver = init_display_from_config(cfg)
    assert isinstance(driver, NoneDisplayDriver)
