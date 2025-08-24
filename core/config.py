"""Загрузка и валидация конфигурации приложения."""

from dataclasses import dataclass
from configparser import ConfigParser


class ConfigError(Exception):
    """Исключение, выбрасываемое при отсутствии или ошибке в конфигурации."""


@dataclass
class UserConfig:
    """Параметры пользователя."""

    name: str
    telegram_user_id: int


@dataclass
class IntelConfig:
    """Настройки интеграции с внешним ИИ‑провайдером."""

    api_key: str
    absent_after_sec: float


@dataclass
class TelegramConfig:
    """Конфигурация для Telegram‑бота."""

    token: str


@dataclass
class PresenceConfig:
    """Параметры, отвечающие за присутствие ассистента."""

    enabled: bool
    camera_index: int
    frame_interval_ms: int


@dataclass
class AppConfig:
    """Сводная конфигурация приложения."""

    user: UserConfig
    intel: IntelConfig
    telegram: TelegramConfig
    presence: PresenceConfig


def _require(cfg: ConfigParser, section: str, option: str) -> str:
    """Возвращает значение *option* из секции *section*.

    Если параметр отсутствует или пуст, выбрасывается :class:`ConfigError`.
    """

    # Проверяем наличие параметра и его непустое значение
    if not cfg.has_option(section, option) or not cfg.get(section, option).strip():
        raise ConfigError(f"Missing option '{option}' in section '{section}'")
    return cfg.get(section, option)


def load_config(path: str = "config.ini") -> AppConfig:
    """Загружает и валидирует конфигурацию из файла *path*.

    Ожидаются секции: ``USER``, ``INTEL``, ``TELEGRAM``, ``PRESENCE``.
    """

    parser = ConfigParser()
    # Загружаем файл конфигурации; если его нет — сообщаем об ошибке
    if not parser.read(path, encoding="utf-8"):
        raise ConfigError(f"Configuration file '{path}' not found")

    # Проверяем наличие всех обязательных секций
    for section in ("USER", "INTEL", "TELEGRAM", "PRESENCE"):
        if section not in parser:
            raise ConfigError(f"Missing section '{section}'")

    # Формируем dataclass-объекты для каждой секции конфигурации
    user = UserConfig(
        name=_require(parser, "USER", "name"),
        telegram_user_id=parser.getint("USER", "telegram_user_id"),
    )
    intel = IntelConfig(
        api_key=_require(parser, "INTEL", "api_key"),
        absent_after_sec=parser.getfloat("INTEL", "absent_after_sec"),
    )
    telegram = TelegramConfig(token=_require(parser, "TELEGRAM", "token"))
    # Параметры камеры берём целочисленными значениями
    presence = PresenceConfig(
        enabled=parser.getboolean("PRESENCE", "enabled"),
        camera_index=parser.getint("PRESENCE", "camera_index"),
        frame_interval_ms=parser.getint("PRESENCE", "frame_interval_ms"),
    )

    return AppConfig(user=user, intel=intel, telegram=telegram, presence=presence)
