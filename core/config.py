"""Загрузка и валидация конфигурации приложения."""

from dataclasses import dataclass
from configparser import ConfigParser
import os

from core.logging_json import configure_logging

try:
    from dotenv import load_dotenv  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - fallback for missing dependency
    def load_dotenv(dotenv_path: str | None = None) -> None:
        """Minimal replacement for :func:`dotenv.load_dotenv`.

        Parses ``KEY=VALUE`` pairs from *dotenv_path* (default ``.env``) and
        inserts them into :data:`os.environ` if the keys are not already
        present. Lines starting with ``#`` or without ``=`` are ignored.
        """

        path = dotenv_path or ".env"
        try:
            with open(path, encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
        except FileNotFoundError:
            pass

load_dotenv()

log = configure_logging("core.config")


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


def _env_or_cfg(
    cfg: ConfigParser, section: str, option: str, env_name: str, default: str | None = None
) -> str:
    """Возвращает значение из переменной окружения или конфигурации."""

    value = os.getenv(env_name)
    if value:
        log.info("%s=%s (env)", env_name, value)
        return value
    if cfg.has_option(section, option) and cfg.get(section, option).strip():
        cfg_value = cfg.get(section, option)
        log.info("%s=%s (cfg)", option, cfg_value)
        return cfg_value
    if default is not None:
        log.info("%s=%s (default)", option, default)
        return default
    raise ConfigError(
        f"Missing option '{option}' in section '{section}' or env '{env_name}'"
    )


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
        telegram_user_id=int(
            _env_or_cfg(
                parser, "USER", "telegram_user_id", "TELEGRAM_USER_ID", default="0"
            )
        ),
    )
    intel = IntelConfig(
        api_key=_env_or_cfg(parser, "INTEL", "api_key", "INTEL_API_KEY"),
        absent_after_sec=parser.getfloat("INTEL", "absent_after_sec"),
    )
    telegram = TelegramConfig(
        token=_env_or_cfg(parser, "TELEGRAM", "token", "TELEGRAM_TOKEN")
    )
    # Параметры камеры берём целочисленными значениями
    presence = PresenceConfig(
        enabled=parser.getboolean("PRESENCE", "enabled"),
        camera_index=parser.getint("PRESENCE", "camera_index"),
        frame_interval_ms=parser.getint("PRESENCE", "frame_interval_ms"),
    )

    return AppConfig(user=user, intel=intel, telegram=telegram, presence=presence)
