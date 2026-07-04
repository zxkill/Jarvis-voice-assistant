from __future__ import annotations

"""Безопасный серверный контроллер тела робота.

Модуль не управляет моторами напрямую. Он общается с уже существующим
HTTP API ESP32:

* GET /api/status
* GET /api/move?direction=forward|backward&distance=<m>&duty=<0..1023>
* GET /api/rotate?direction=left|right&angle=<deg>&duty=<0..1023>
* GET /api/stop

Главная задача этого слоя — не дать голосовым командам отправить опасные
параметры: ограничиваем расстояния, углы, duty, проверяем busy и подробно
логируем каждое действие.
"""

import configparser
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

from core.logging_json import configure_logging

log = configure_logging("robot.body")


class RobotBodyError(RuntimeError):
    """Базовая ошибка управления телом робота."""


class RobotBodyUnavailable(RobotBodyError):
    """Робот недоступен или base_url не удалось определить."""


@dataclass(slots=True)
class BodyConfig:
    """Настройки безопасного управления телом."""

    base_url: str = "auto"
    request_timeout_sec: float = 1.2
    status_timeout_sec: float = 0.8
    max_voice_distance_m: float = 0.55
    max_voice_angle_deg: float = 90.0
    default_move_distance_m: float = 0.25
    default_turn_angle_deg: float = 35.0
    default_move_duty: int = 450
    default_turn_duty: int = 420
    max_duty: int = 650
    require_not_busy: bool = True


def _read_config(path: str = "config.ini") -> BodyConfig:
    """Читает секцию [ROBOT_BODY] с безопасными fallback-значениями."""

    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    section = "ROBOT_BODY"

    def get_float(name: str, default: float) -> float:
        try:
            return parser.getfloat(section, name, fallback=default)
        except Exception:
            log.exception("Некорректное число в [ROBOT_BODY].%s, использую %.3f", name, default)
            return default

    def get_int(name: str, default: int) -> int:
        try:
            return parser.getint(section, name, fallback=default)
        except Exception:
            log.exception("Некорректное целое в [ROBOT_BODY].%s, использую %d", name, default)
            return default

    def get_bool(name: str, default: bool) -> bool:
        try:
            return parser.getboolean(section, name, fallback=default)
        except Exception:
            log.exception("Некорректный bool в [ROBOT_BODY].%s, использую %s", name, default)
            return default

    env_url = os.environ.get("JARVIS_ROBOT_BASE_URL", "").strip()
    base_url = env_url or parser.get(section, "base_url", fallback="auto").strip() or "auto"

    cfg = BodyConfig(
        base_url=base_url,
        request_timeout_sec=get_float("request_timeout_sec", 1.2),
        status_timeout_sec=get_float("status_timeout_sec", 0.8),
        max_voice_distance_m=get_float("max_voice_distance_m", 0.55),
        max_voice_angle_deg=get_float("max_voice_angle_deg", 90.0),
        default_move_distance_m=get_float("default_move_distance_m", 0.25),
        default_turn_angle_deg=get_float("default_turn_angle_deg", 35.0),
        default_move_duty=get_int("default_move_duty", 450),
        default_turn_duty=get_int("default_turn_duty", 420),
        max_duty=get_int("max_duty", 650),
        require_not_busy=get_bool("require_not_busy", True),
    )
    log.debug("Конфигурация тела загружена: %s", cfg)
    return cfg


def _auto_base_url_from_audio_peer() -> str | None:
    """Пытается взять IP ESP32 из последнего аудио-WebSocket подключения."""

    try:
        from audio.robot_stream import get_last_robot_base_url, get_last_robot_peer

        base_url = get_last_robot_base_url()
        peer = get_last_robot_peer()
        if base_url:
            log.info(
                "Адрес робота определён по аудиоканалу",
                extra={"attrs": {"base_url": base_url, "peer": peer or ""}},
            )
            return base_url
    except Exception:
        log.debug("Не удалось получить адрес робота из audio.robot_stream", exc_info=True)
    return None


class BodyController:
    """Высокоуровневый контроллер движения через HTTP API ESP32."""

    def __init__(self, config: BodyConfig | None = None) -> None:
        self.cfg = config or _read_config()
        self._session = requests.Session()
        self._last_resolved_url: str | None = None

    @property
    def base_url(self) -> str:
        """Возвращает реальный base_url ESP32, поддерживая режим auto."""

        raw = (self.cfg.base_url or "auto").strip()
        if raw.lower() in {"auto", "", "detect"}:
            detected = _auto_base_url_from_audio_peer()
            if not detected:
                raise RobotBodyUnavailable(
                    "Не знаю IP робота. Укажи [ROBOT_BODY] base_url = http://IP_ESP32 "
                    "или дождись подключения ESP32 к аудиоканалу."
                )
            raw = detected
        if not raw.startswith(("http://", "https://")):
            raw = "http://" + raw
        raw = raw.rstrip("/") + "/"
        if raw != self._last_resolved_url:
            log.info("Использую HTTP API робота: %s", raw)
            self._last_resolved_url = raw
        return raw

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _get_json(self, path: str, *, timeout: float | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Выполняет GET и возвращает JSON с подробным логированием."""

        url = self._url(path)
        started = time.perf_counter()
        try:
            log.debug("HTTP GET роботу", extra={"attrs": {"url": url, "params": params or {}}})
            response = self._session.get(url, params=params, timeout=timeout or self.cfg.request_timeout_sec)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            log.info(
                "Ответ HTTP API робота",
                extra={"attrs": {"url": url, "status": response.status_code, "elapsed_ms": round(elapsed_ms, 1)}},
            )
            response.raise_for_status()
            if not response.text.strip():
                return {"success": True}
            return response.json()
        except requests.RequestException as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            log.warning(
                "HTTP API робота недоступен",
                extra={"attrs": {"url": url, "elapsed_ms": round(elapsed_ms, 1), "error": str(exc)[:180]}},
            )
            raise RobotBodyUnavailable(f"Робот недоступен по {url}: {exc}") from exc
        except ValueError as exc:
            log.warning("Робот вернул не JSON", extra={"attrs": {"url": url}})
            raise RobotBodyError(f"Робот вернул не JSON по {url}") from exc

    def status(self) -> dict[str, Any]:
        """Возвращает /api/status."""

        return self._get_json("/api/status", timeout=self.cfg.status_timeout_sec)

    def status_summary(self) -> str:
        """Короткая русская сводка для голосового ответа."""

        data = self.status()
        busy = bool(data.get("busy", False))
        voltage = float(data.get("busVoltage", 0.0) or 0.0)
        current = float(data.get("currentA", 0.0) or 0.0)
        battery = float(data.get("batteryPercent", 0.0) or 0.0)
        heading = float(data.get("headingDeg", 0.0) or 0.0)
        return (
            f"Робот на связи. Состояние: {'движется' if busy else 'стоит'}. "
            f"Батарея примерно {battery:.0f} процентов, напряжение {voltage:.1f} вольт, "
            f"ток {current:.2f} ампера, курс {heading:.0f} градусов."
        )

    def _ensure_ready_for_motion(self) -> None:
        if not self.cfg.require_not_busy:
            return
        try:
            data = self.status()
        except RobotBodyUnavailable:
            raise
        except Exception as exc:
            raise RobotBodyError(f"Не удалось проверить состояние робота: {exc}") from exc
        if bool(data.get("busy", False)):
            log.info("Команда движения отклонена: робот уже занят", extra={"attrs": {"status": data}})
            raise RobotBodyError("Робот уже выполняет движение. Сначала останови его или дождись завершения.")

    def _safe_distance(self, distance_m: float | None) -> float:
        value = self.cfg.default_move_distance_m if distance_m is None else float(distance_m)
        value = max(0.03, min(value, self.cfg.max_voice_distance_m))
        return round(value, 3)

    def _safe_angle(self, angle_deg: float | None) -> float:
        value = self.cfg.default_turn_angle_deg if angle_deg is None else float(angle_deg)
        value = max(5.0, min(value, self.cfg.max_voice_angle_deg))
        return round(value, 1)

    def _safe_duty(self, duty: int | None, default: int) -> int:
        value = default if duty is None else int(duty)
        value = max(0, min(value, self.cfg.max_duty, 1023))
        return value

    def move(self, direction: str, distance_m: float | None = None, duty: int | None = None) -> dict[str, Any]:
        """Безопасно отправляет линейное движение."""

        direction = "backward" if direction.lower() in {"back", "backward", "назад"} else "forward"
        distance = self._safe_distance(distance_m)
        safe_duty = self._safe_duty(duty, self.cfg.default_move_duty)
        self._ensure_ready_for_motion()
        log.info(
            "Отправляю движение роботу",
            extra={"attrs": {"direction": direction, "distance_m": distance, "duty": safe_duty}},
        )
        return self._get_json(
            "/api/move",
            params={"direction": direction, "distance": f"{distance:.3f}", "duty": str(safe_duty)},
        )

    def rotate(self, direction: str, angle_deg: float | None = None, duty: int | None = None) -> dict[str, Any]:
        """Безопасно отправляет поворот."""

        direction = "right" if direction.lower() in {"right", "вправо", "право"} else "left"
        angle = self._safe_angle(angle_deg)
        safe_duty = self._safe_duty(duty, self.cfg.default_turn_duty)
        self._ensure_ready_for_motion()
        log.info(
            "Отправляю поворот роботу",
            extra={"attrs": {"direction": direction, "angle_deg": angle, "duty": safe_duty}},
        )
        return self._get_json(
            "/api/rotate",
            params={"direction": direction, "angle": f"{angle:.1f}", "duty": str(safe_duty)},
        )

    def stop(self) -> dict[str, Any]:
        """Аварийная остановка. Должна проходить даже если робот busy."""

        log.warning("Отправляю аварийную остановку роботу")
        return self._get_json("/api/stop")
