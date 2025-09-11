"""Тесты инфраструктуры Docker и связанной конфигурации.

Проверяем базовые свойства файлов `docker-compose.yml` и `promtail-config.yml`,
чтобы убедиться, что стек разворачивается корректно и логи будут отправляться в
Loki. Такие тесты позволяют быстро отлавливать ошибки в YAML или структуре
сервисов после изменений.
"""

from __future__ import annotations

import logging
import pathlib

import yaml

logging.basicConfig(level=logging.INFO)
# Логгер для отладки тестов. Если что-то пойдёт не так, логи помогут быстрее понять причину.
LOGGER = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_compose_services_present() -> None:
    """Убеждаемся, что в docker-compose описаны все необходимые сервисы."""
    compose_file = ROOT / "docker-compose.yml"
    data = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    services = data.get("services", {})
    LOGGER.info("Найденные сервисы: %s", list(services))
    assert {"jarvis", "loki", "promtail", "grafana"} <= set(services)


def test_promtail_loki_url() -> None:
    """Проверяем, что Promtail отправляет логи в Loki."""
    promtail_cfg = ROOT / "promtail-config.yml"
    data = yaml.safe_load(promtail_cfg.read_text(encoding="utf-8"))
    urls = [client["url"] for client in data.get("clients", [])]
    LOGGER.info("Пути отправки логов: %s", urls)
    assert "http://loki:3100/loki/api/v1/push" in urls


def test_dockerfile_no_source_copy() -> None:
    """Dockerfile не должен копировать исходники, чтобы избежать пересборки образа."""
    dockerfile_text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    LOGGER.debug("Содержимое Dockerfile:\n%s", dockerfile_text)
    assert "COPY . ." not in dockerfile_text


def test_dockerignore_minimal_context() -> None:
    """Проверяем, что .dockerignore исключает все файлы кроме зависимостей."""
    dockerignore_lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    LOGGER.debug("Содержимое .dockerignore: %s", dockerignore_lines)
    assert "*" in dockerignore_lines
    assert "!requirements.txt" in dockerignore_lines


def test_jarvis_devices_mounted() -> None:
    """Проверяем, что в docker-compose проброшены камера и аудио устройства."""
    compose_file = ROOT / "docker-compose.yml"
    data = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    jarvis = data["services"]["jarvis"]
    LOGGER.info("Конфигурация устройств: %s", jarvis.get("devices"))
    devices = jarvis.get("devices", [])
    assert "/dev/video0:/dev/video0" in devices
    assert "/dev/snd:/dev/snd" in devices


def test_timezone_env_present() -> None:
    """Убеждаемся, что сервис Jarvis поддерживает установку часового пояса."""
    compose_file = ROOT / "docker-compose.yml"
    data = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    jarvis = data["services"]["jarvis"]
    env_list = jarvis.get("environment", [])
    LOGGER.info("Переменные окружения Jarvis: %s", env_list)
    assert any(str(item).startswith("TZ=") for item in env_list)
