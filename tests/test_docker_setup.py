"""Тесты инфраструктуры Docker и связанной конфигурации.

Проверяем базовые свойства файлов `docker-compose.yml` и `promtail-config.yml`,
чтобы убедиться, что стек разворачивается корректно и логи будут отправляться в
Loki. Такие тесты позволяют быстро отлавливать ошибки в YAML или структуре
сервисов после изменений.
"""

from __future__ import annotations

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_compose_services_present() -> None:
    """Убеждаемся, что в docker-compose описаны все необходимые сервисы."""
    compose_file = ROOT / "docker-compose.yml"
    data = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    services = data.get("services", {})
    assert {"jarvis", "loki", "promtail", "grafana"} <= set(services)


def test_promtail_loki_url() -> None:
    """Проверяем, что Promtail отправляет логи в Loki."""
    promtail_cfg = ROOT / "promtail-config.yml"
    data = yaml.safe_load(promtail_cfg.read_text(encoding="utf-8"))
    urls = [client["url"] for client in data.get("clients", [])]
    assert "http://loki:3100/loki/api/v1/push" in urls
