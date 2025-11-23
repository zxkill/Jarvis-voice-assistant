"""Сбор характеристик устройства для авторизации на сервере Xiaozhi."""

from __future__ import annotations

import hashlib
import platform
import socket
import uuid
from dataclasses import dataclass
from typing import Dict

from core.logging_json import configure_logging


log = configure_logging("core.xiaozhi.device")


@dataclass(frozen=True)
class DeviceProfile:
    """Статическая информация о машине.

    Поля подобраны так, чтобы максимально повторить набор данных из py-xiaozhi:
    система, имя хоста, аппаратные идентификаторы и сетевые сведения. Значения
    используются для вычисления устойчивого хэша и формирования полезной нагрузки
    при запросе конфигурации и активации устройства.
    """

    system: str
    hostname: str
    hardware_hash: str
    mac_address: str
    machine_id: str
    ip_address: str


class XiaozhiDeviceInfo:
    """Утилита для безопасного сбора идентификаторов устройства.

    Выделена в отдельный класс, чтобы её можно было легко подменить в тестах
    и переиспользовать при разных сценариях активации. Внутри присутствует
    подробное логирование, облегчающее отладку на различных платформах.
    """

    def __init__(self) -> None:
        self._machine_id = self._collect_machine_id()
        self._mac_address = self._collect_mac()
        self._ip_address = self._collect_ip()
        self._hardware_hash = self._build_hardware_hash()

    def profile(self) -> DeviceProfile:
        """Возвращает агрегированный профиль устройства."""

        return DeviceProfile(
            system=platform.system(),
            hostname=platform.node(),
            hardware_hash=self._hardware_hash,
            mac_address=self._mac_address,
            machine_id=self._machine_id,
            ip_address=self._ip_address,
        )

    def _collect_machine_id(self) -> str:
        """Возвращает устойчивый идентификатор машины.

        Используем комбинацию UUID по MAC и системной информации, чтобы добиться
        стабильности между перезапусками. В лог пишем укороченный отпечаток, не
        раскрывая полный идентификатор.
        """

        node = uuid.getnode()
        namespace = uuid.UUID(int=uuid.getnode())
        machine_uuid = uuid.uuid5(namespace, platform.platform())
        compact = machine_uuid.hex
        log.debug("определён machine_id", extra={"ctx": {"machine_id": compact[:12]}})
        return compact

    def _collect_mac(self) -> str:
        """Пытается извлечь MAC‑адрес сетевого интерфейса.

        Если стандартный `uuid.getnode()` возвращает псевдо‑значение, пытаемся
        нормализовать строку и используем её даже в состоянии по умолчанию — это
        лучше, чем полностью отсутствующий идентификатор.
        """

        mac = uuid.getnode()
        if (mac >> 40) % 2:
            log.warning("uuid.getnode() вернул локальный MAC, использую заглушку")
        normalized = ":".join(f"{(mac >> ele) & 0xFF:02x}" for ele in range(40, -8, -8))
        log.debug("mac выбран", extra={"ctx": {"mac": normalized}})
        return normalized

    def _collect_ip(self) -> str:
        """Определяет локальный IP через UDP‑сокет без отправки данных."""

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                ip_addr = sock.getsockname()[0]
                log.debug("определён локальный IP", extra={"ctx": {"ip": ip_addr}})
                return ip_addr
        except Exception:
            log.exception("не удалось определить IP, возвращаю 127.0.0.1")
            return "127.0.0.1"

    def _build_hardware_hash(self) -> str:
        """Создаёт SHA256‑хэш на основе набора аппаратных признаков."""

        raw = "::".join(
            [platform.node(), platform.platform(), self._collect_machine_id(), self._collect_mac()]
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        log.debug("собран hardware_hash", extra={"ctx": {"hash": digest[:12]}})
        return digest

    def as_payload(self) -> Dict[str, str]:
        """Возвращает словарь, готовый для отправки на сервер OTA."""

        profile = self.profile()
        return {
            "system": profile.system,
            "hostname": profile.hostname,
            "hardware_hash": profile.hardware_hash,
            "mac": profile.mac_address,
            "machine_id": profile.machine_id,
            "ip": profile.ip_address,
        }

