"""Высокоуровневый интерфейс для работы с локальной LLM через Ollama.

Модуль предоставляет функции ``think``, ``act``, ``reflect``, ``summarise`` и
``mood``.  Каждая функция подставляет данные в соответствующий шаблон из
каталога ``prompts`` и отправляет запрос к локальной модели по HTTP.

Полученные ответы сохраняются в краткосрочном и долговременном контексте,
что позволяет накапливать знания между вызовами.  Все действия подробно
логируются для удобной отладки.  Код снабжён комментариями на русском
языке, упрощающими поддержку проекта.
"""

from __future__ import annotations

import logging
import json
from pathlib import Path
from typing import Iterable
import os
import datetime as dt  # получение текущих даты и времени

import requests

from context import long_term, short_term, current_date
from memory import long_memory, preferences

logger = logging.getLogger(__name__)

# Путь к каталогу с текстовыми шаблонами
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Профили генерации и соответствующие имена моделей
# Названия можно переопределить через переменные окружения
PROFILES = {
    "light": os.getenv("OLLAMA_LIGHT_MODEL", "llama3.1:8b"),
    "heavy": os.getenv("OLLAMA_HEAVY_MODEL", "llama3.1:8b"),
}

# Базовый URL локального сервера Ollama
BASE_URL = "http://localhost:11434"

# Файл для записи подробных запросов и ответов LLM. Путь можно переопределить
# переменной окружения ``LLM_LOG_FILE``.  По умолчанию журнал создаётся в
# рабочем каталоге под именем ``llm_interactions.log``.
LLM_LOG_FILE = Path(os.getenv("LLM_LOG_FILE", "llm_interactions.log"))


def _log_llm_exchange(
    stage: str,
    prompt: str,
    reply: str,
    trace_id: str,
    context: str,
) -> None:
    """Сохранить обмен с LLM в отдельный файл для дальнейшего анализа.

    Записываем каждое взаимодействие в формате JSON: этап, trace-id,
    сформированный prompt, изначальный контекст и ответ модели.  Это позволяет
    легко воспроизводить диалог и понимать, какой контекст отправлялся в LLM.
    В случае ошибки логирование не должно прерывать основной поток исполнения,
    поэтому любые исключения перехватываются и выводятся в стандартный логгер.
    """

    try:
        entry = {
            "timestamp": dt.datetime.now().isoformat(),
            "stage": stage,
            "trace_id": trace_id,
            "context": context,
            "prompt": prompt,
            "reply": reply,
        }
        # Гарантируем существование каталога и дописываем строку в конец файла
        LLM_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LLM_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # pragma: no cover - сбой логирования не критичен
        logger.error("Не удалось записать лог LLM", exc_info=exc)


def _query_ollama(prompt: str, profile: str, trace_id: str = "") -> str:
    """Отправить HTTP-запрос к Ollama и вернуть ответ.

    Параметр ``trace_id`` используется только для логирования и удобства
    отладки.  Сервер Ollama его игнорирует, но информация попадает в логи.
    """

    if profile not in PROFILES:
        raise ValueError(f"Неизвестный профиль: {profile}")
    model = PROFILES[profile]

    # Подготавливаем запрос для современного эндпоинта /v1/chat/completions
    url = f"{BASE_URL}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,  # просим сервер вернуть единый JSON без чанков
    }

    # trace_id не обязателен для сервера, но полезен для сопоставления логов,
    # поэтому передаём его в заголовке X-Trace-Id
    headers = {"X-Trace-Id": trace_id} if trace_id else None

    logger.debug(
        "Отправка запроса в Ollama",
        extra={"url": url, "model": model, "trace_id": trace_id},
    )

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=60)
    except requests.RequestException as exc:
        # Сетевые ошибки: сервер недоступен или таймаут
        logger.error("Ошибка при обращении к Ollama: %s", exc)
        raise RuntimeError("Ollama недоступна") from exc

    use_legacy = False  # признак использования старого API /api/generate
    if response.status_code == 404:
        # Разбираем тело ответа: если модель не найдена, нет смысла делать
        # повторный запрос на старый эндпоинт.
        try:
            error_msg = response.json().get("error", "")
        except Exception:
            error_msg = response.text
        if "model" in error_msg.lower() and "not found" in error_msg.lower():
            logger.error(
                "Модель %s не найдена: %s",
                model,
                error_msg,
                extra={"trace_id": trace_id},
            )
            raise RuntimeError(f"Модель {model} не найдена")

        # Старые версии Ollama не знают про /v1/chat/completions.
        # Логируем предупреждение и пробуем fallback на /api/generate.
        logger.warning(
            "Эндпоинт /v1/chat/completions не найден, пробуем /api/generate",
            extra={"trace_id": trace_id},
        )
        url = f"{BASE_URL}/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=60)
        except requests.RequestException as exc:
            logger.error("Ошибка при обращении к Ollama: %s", exc)
            raise RuntimeError("Ollama недоступна") from exc

        if response.status_code == 404:
            try:
                error_msg = response.json().get("error", "")
            except Exception:
                error_msg = response.text
            logger.error(
                "Модель %s не найдена: %s",
                model,
                error_msg,
                extra={"trace_id": trace_id},
            )
            raise RuntimeError(f"Модель {model} не найдена")

        try:
            response.raise_for_status()
            use_legacy = True
        except requests.RequestException as exc:
            logger.error("Ошибка при обращении к Ollama: %s", exc)
            raise RuntimeError("Ollama недоступна") from exc
    else:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Ошибка при обращении к Ollama: %s", exc)
            raise RuntimeError("Ollama недоступна") from exc

    try:
        data = response.json()
        if not isinstance(data, dict):
            raise TypeError(f"unexpected JSON type: {type(data)!r}")
        if use_legacy:
            # Ответ старого API: {"response": "текст"}
            text = str(data.get("response", ""))
        else:
            # Новый формат: {"choices": [{"message": {"content": "текст"}}]}
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                raise KeyError("choices")
            message = choices[0].get("message", {})
            if not isinstance(message, dict):
                raise TypeError("message should be dict")
            text = str(message.get("content", ""))
    except Exception as exc:
        logger.error("Некорректный ответ от Ollama: %s", exc)
        raise RuntimeError("Ollama вернула невалидный JSON") from exc

    logger.debug(
        "Получен ответ от Ollama",
        extra={"length": len(text), "trace_id": trace_id, "legacy": use_legacy},
    )
    return text


def _load_prompt(name: str) -> str:
    """Загрузить шаблон с диска."""
    path = PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")


def _compose_context() -> str:
    """Собрать контекст недавнего диалога и добавить штамп текущей даты."""

    # Обновляем глобальную дату и фиксируем время без микросекунд
    day = current_date.refresh()
    now = dt.datetime.now().time().replace(microsecond=0)

    # Преобразуем дату в человеко‑читаемый вид «дд.мм.гггг»
    human_day = day.strftime("%d.%m.%Y")
    timestamp = f"Текущая дата {human_day}, время {now.isoformat()}"

    # Извлекаем последние реплики из краткосрочной памяти как строки
    history = list(map(str, short_term.get_last()))

    # Логируем сформированный контекст для отладки
    logger.debug("compose_context", extra={"ctx": {"history": history, "datetime": timestamp}})

    # Возвращаем историю и штамп текущей даты
    return "\n".join([*history, timestamp])


def _run(
    prompt_name: str,
    profile: str = "light",
    *,
    user_input: str = "",
    trace_id: str = "",
    **kwargs: str,
) -> str:
    """Универсальный запуск запроса к LLM.

    :param prompt_name: имя файла шаблона без расширения;
    :param profile: ключ профиля генерации (``light``/``heavy``);
    :param user_input: исходный текст пользователя для сохранения в память;
    :param trace_id: уникальный идентификатор взаимодействия;
    :param kwargs: параметры для подстановки в шаблон.
    """

    template = _load_prompt(prompt_name)
    prompt = template.format(**kwargs)
    logger.debug("Готовый prompt %s: %s", prompt_name, prompt)
    reply = _query_ollama(prompt, profile=profile, trace_id=trace_id)
    # Логируем каждый запрос и ответ в отдельный файл для последующего анализа
    _log_llm_exchange(
        stage=prompt_name,
        prompt=prompt,
        reply=reply,
        trace_id=trace_id,
        context=str(kwargs.get("context", "")),
    )

    if user_input:
        # Сохраняем диалог в краткосрочную и долговременную память
        short_term.add({"trace_id": trace_id, "user": user_input, "reply": reply})
        long_term.add_daily_event(
            f"user: {user_input}\nassistant: {reply}", [prompt_name]
        )
    else:
        # Для вспомогательных запросов сохраняем только ответ
        short_term.add({"stage": prompt_name, "text": reply})

    logger.info("Ответ %s: %s", prompt_name, reply)
    return reply


def think(topic: str, *, trace_id: str) -> str:
    """Сформировать размышление по заданной теме.

    ``trace_id`` передаётся для связи всех логов и записей в памяти.
    """

    context_text = _compose_context()
    # Извлекаем исторические события с меткой "think"
    events = long_term.get_events_by_label("think")
    # Находим семантически похожие воспоминания по текущей теме
    similar = [text for text, _ in long_memory.retrieve_similar(topic)]
    # Загружаем известные предпочтения пользователя, чтобы учитывать их в ответе
    prefs = preferences.load_preferences()
    logger.debug(
        "Релевантные события для темы %r: %s", topic, similar
    )
    logger.debug("Учтённые предпочтения: %s", prefs)
    long_context = "\n".join(events + similar + prefs)
    return _run(
        "think",
        topic=topic,
        context=context_text,
        long_context=long_context,
        profile="light",
        user_input=topic,
        trace_id=trace_id,
    )


def act(command: str, *, trace_id: str) -> str:
    """Предложить действие на основании команды пользователя."""
    context_text = _compose_context()
    # Извлекаем события с меткой "act" и похожие команды из долговременной памяти
    events = long_term.get_events_by_label("act")
    similar = [text for text, _ in long_memory.retrieve_similar(command)]
    # Предпочтения пользователя также важны при выборе действия
    prefs = preferences.load_preferences()
    logger.debug(
        "Релевантные события для команды %r: %s", command, similar
    )
    logger.debug("Учтённые предпочтения: %s", prefs)
    long_context = "\n".join(events + similar + prefs)
    return _run(
        "act",
        command=command,
        context=context_text,
        long_context=long_context,
        profile="light",
        user_input=command,
        trace_id=trace_id,
    )


def reflect(note: str | None = None) -> dict:
    """Проанализировать недавний опыт и вернуть структурированный результат.

    Функция ожидает от LLM строго валидный JSON с полями ``digest``,
    ``priorities`` и ``mood``.  При любой ошибке парсинга возбуждается
    ``ValueError``.  Дополнительные сведения выводятся в лог для удобства
    отладки.
    """

    context_text = _compose_context()
    events = "\n".join(long_term.get_events_by_label("reflect"))
    raw = _run(
        "reflect",
        context=context_text,
        note=note or "",
        long_context=events,
        profile="heavy",
    )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("рефлексия вернула невалидный JSON", extra={"raw": raw})
        raise ValueError("ответ рефлексии должен быть JSON") from exc
    if not isinstance(data, dict):
        logger.error("рефлексия ожидает JSON-объект, получено %s", type(data))
        raise ValueError("формат рефлексии должен быть объект JSON")
    try:
        digest = str(data["digest"])
        priorities = str(data.get("priorities", ""))
        mood = int(data["mood"])
    except Exception as exc:
        logger.error("некорректные поля в JSON рефлексии", extra={"json": data})
        raise ValueError("JSON рефлексии должен содержать digest, priorities, mood") from exc

    result = {"digest": digest, "priorities": priorities, "mood": mood}
    logger.debug("разобран результат рефлексии", extra={"ctx": result})
    return result


def summarise(text: str, labels: Iterable[str] | None = None) -> str:
    """Создать краткое резюме текста и сохранить в долгосрочную память."""
    context_text = _compose_context()
    summary = _run(
        "summarise",
        context=context_text + "\n" + text,
        profile="heavy",
    )
    long_term.add_daily_event(summary, labels or ["summary"])
    return summary


def mood(feeling: str) -> str:
    """Определить настроение на основе текущего ощущения пользователя."""
    context_text = _compose_context()
    history = "\n".join(long_term.get_events_by_label("mood"))
    result = _run(
        "mood",
        context=context_text + ("\n" + history if history else ""),
        feeling=feeling,
        profile="light",
    )
    long_term.add_daily_event(result, ["mood"])
    return result
