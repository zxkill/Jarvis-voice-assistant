"""Сбор характеристик устройства для авторизации на сервере Xiaozhi."""

from __future__ import annotations

import hashlib
import platform
import re
import socket
import uuid
from dataclasses import dataclass
from typing import Dict, Optional

from core.logging_json import configure_logging


log = configure_logging("core.xiaozhi.device")


def normalize_mac(raw_mac: Optional[str]) -> str:
    """Приводит MAC‑адрес к каноничному виду AA:BB:CC:DD:EE:FF.

    Сервер Xiaozhi строго валидирует MAC, поэтому мы:
    - удаляем разделители (двоеточия/тире/точки),
    - проверяем, что осталось ровно 12 шестнадцатеричных символов,
    - возвращаем строку в верхнем регистре, разделённую двоеточиями.

    При некорректном вводе выбрасываем ValueError, чтобы вызывающий код мог
    сообщить пользователю о необходимости поправить конфиг.
    """

    if not raw_mac:
        raise ValueError("MAC отсутствует")
    # Удаляем все типичные разделители, чтобы унифицировать строку.
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", raw_mac)
    if len(cleaned) != 12 or not re.fullmatch(r"[0-9A-Fa-f]{12}", cleaned):
        raise ValueError(f"некорректный MAC '{raw_mac}'")
    # Разбиваем на пары и приводим к верхнему регистру для строгой проверки OTA.
    pairs = [cleaned[i : i + 2].upper() for i in range(0, 12, 2)]
    normalized = ":".join(pairs)
    log.debug("нормализован MAC", extra={"ctx": {"mac": normalized}})
    return normalized


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
        # Сбор идентификаторов вынесен в конструктор, чтобы значения были
        # стабильны для всех последующих вызовов и попадали в хэш.
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
        normalized_raw = ":".join(f"{(mac >> ele) & 0xFF:02x}" for ele in range(40, -8, -8))
        try:
            normalized = normalize_mac(normalized_raw)
        except ValueError:
            # Даже если нормализация сломалась, лучше сохранить исходное значение
            # и позволить пользователю поправить конфиг руками.
            log.error("не удалось нормализовать MAC, возвращаю исходный", extra={"ctx": {"mac": normalized_raw}})
            return normalized_raw
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

