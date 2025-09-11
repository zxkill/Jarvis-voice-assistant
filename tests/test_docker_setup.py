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
    """Проверяем, что docker-compose поднимает только стек логирования."""
    compose_file = ROOT / "docker-compose.yml"
    data = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    services = data.get("services", {})
    LOGGER.info("Найденные сервисы: %s", list(services))
    assert {"loki", "promtail", "grafana"} <= set(services)
    assert "jarvis" not in services


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


def test_promtail_mounts_logs_dir() -> None:
    """Убеждаемся, что Promtail видит файлы логов Jarvis."""
    compose_file = ROOT / "docker-compose.yml"
    data = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    promtail = data["services"]["promtail"]
    LOGGER.info("Тома Promtail: %s", promtail.get("volumes"))
    volumes = promtail.get("volumes", [])
    assert "./logs:/var/log/jarvis" in volumes


def test_promtail_scrapes_files() -> None:
    """Проверяем, что Promtail настроен на чтение файлов из /var/log/jarvis."""
    promtail_cfg = ROOT / "promtail-config.yml"
    data = yaml.safe_load(promtail_cfg.read_text(encoding="utf-8"))
    scrape = data.get("scrape_configs", [])[0]
    path = scrape["static_configs"][0]["labels"]["__path__"]
    LOGGER.info("Promtail читает из: %s", path)
    assert path == "/var/log/jarvis/*.log"
