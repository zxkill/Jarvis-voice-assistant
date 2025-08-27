"""Загрузка YAML-конфигураций подсистем ассистента.

В каталоге ``config`` хранятся отдельные YAML-файлы, описывающие
настройки различных подсистем. Этот модуль предоставляет функции
для чтения таких файлов и преобразования их в датаклассы с удобными
значениями по умолчанию. Детальное логирование помогает понять,
какие параметры были прочитаны и какие значения подставлены
автоматически.
"""

from __future__ import annotations

# Стандартные библиотеки
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Tuple

import yaml  # type: ignore

from core.logging_json import configure_logging

# Папка, где лежат YAML-конфигурации
CONFIG_DIR = Path(__file__).resolve().parent

# Глобальный логгер модуля
log = configure_logging("config")


def _read_yaml(path: Path) -> Dict[str, Any]:
    """Прочитать YAML-файл и вернуть словарь параметров.

    При отсутствии файла или ошибке парсинга в лог пишется
    предупреждение, а возвращается пустой словарь. Это позволяет
    безопасно использовать функции загрузки в условиях, когда
    пользователь ещё не создал нужный конфиг.
    """
    if not path.exists():
        log.info("конфигурация %s отсутствует — используем значения по умолчанию", path.name)
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
            log.debug("файл %s успешно загружен: %s", path.name, data)
            return data
    except Exception as exc:  # pragma: no cover - защитный механизм
        log.warning("не удалось прочитать %s: %s", path, exc)
        return {}


# ---------------------------------------------------------------------------
#  Секция affect.yaml — управление настроением
# ---------------------------------------------------------------------------

@dataclass
class AffectConfig:
    """Параметры обработки настроения."""

    valence_factor: float = 1.0  # масштабирование изменения по оси valence
    arousal_factor: float = 1.0  # масштабирование изменения по оси arousal
    ema_alpha: float = 0.5       # коэффициент экспоненциального сглаживания


def load_affect(path: Path | None = None) -> AffectConfig:
    """Загрузить конфигурацию настроения из ``affect.yaml``."""

    path = path or CONFIG_DIR / "affect.yaml"
    data = _read_yaml(path)
    defaults = AffectConfig()
    return AffectConfig(
        valence_factor=float(data.get("valence_factor", defaults.valence_factor)),
        arousal_factor=float(data.get("arousal_factor", defaults.arousal_factor)),
        ema_alpha=float(data.get("ema_alpha", defaults.ema_alpha)),
    )


# ---------------------------------------------------------------------------
#  Секция display.yaml — параметры вывода на дисплей
# ---------------------------------------------------------------------------

@dataclass
class DisplayConfig:
    """Настройки драйвера дисплея."""

    driver: str = "console"           # имя драйвера из пакета ``display.drivers``
    serial_port: str | None = None    # порт для последовательного вывода, если нужен


def load_display(path: Path | None = None) -> DisplayConfig:
    """Загрузить параметры дисплея из ``display.yaml``."""

    path = path or CONFIG_DIR / "display.yaml"
    data = _read_yaml(path)
    defaults = DisplayConfig()
    return DisplayConfig(
        driver=str(data.get("driver", defaults.driver)),
        serial_port=data.get("serial_port", defaults.serial_port),
    )


# ---------------------------------------------------------------------------
#  Секция proactive.yaml — проактивные подсказки
# ---------------------------------------------------------------------------

@dataclass
class ProactiveConfig:
    """Параметры системы проактивных подсказок."""

    enabled: bool = False                            # включена ли подсистема
    suggestion_min_interval_min: int = 60            # минимальный интервал между подсказками
    force_telegram: bool = False                     # отправлять ли подсказки только в Telegram
    silence_window: Tuple[str, str] | None = None    # окно тишины, "HH:MM" → (start, end)


def load_proactive(path: Path | None = None) -> ProactiveConfig:
    """Загрузить конфигурацию проактивных подсказок из ``proactive.yaml``."""

    path = path or CONFIG_DIR / "proactive.yaml"
    data = _read_yaml(path)
    defaults = ProactiveConfig()
    window = data.get("silence_window")
    if isinstance(window, dict):
        start = str(window.get("start", ""))
        end = str(window.get("end", ""))
        silence_window = (start, end) if start and end else None
    else:
        silence_window = None
    return ProactiveConfig(
        enabled=bool(data.get("enabled", defaults.enabled)),
        suggestion_min_interval_min=int(
            data.get("suggestion_min_interval_min", defaults.suggestion_min_interval_min)
        ),
        force_telegram=bool(data.get("force_telegram", defaults.force_telegram)),
        silence_window=silence_window,
    )
