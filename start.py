from __future__ import annotations

"""Основная точка входа голосового ассистента Jarvis.

Запускает подсистемы распознавания речи, вывод на дисплей и обработку
пользовательских команд.  Файл старается оставаться как можно более
простым, поэтому отдельные функции вынесены в модули ``jarvis_skills``,
``emotion`` и др.
"""

import array
import asyncio
import configparser
import json
import signal
import sys
import time
import threading
from multiprocessing import Pipe, Process
from typing import Any, Dict, List

import vosk
import yaml
from pvrecorder import PvRecorder
from rapidfuzz import fuzz

from display import get_driver, init_driver
from emotion.manager import EmotionManager
from emotion.drivers import EmotionDisplayDriver  # вывод эмоций на экран
from emotion.sounds import EmotionSoundDriver  # звуковое сопровождение
from jarvis_skills import handle_utterance, load_all, start_skill_reloader
from core.config import load_config
from core.events import Event, subscribe, publish
from core.logging_json import TRACE_ID, configure_logging, new_trace_id
from core import stop as stop_mgr
from sensors.vision import PresenceDetector
from proactive.policy import Policy, PolicyConfig
from proactive.engine import ProactiveEngine
from analysis import suggestions as analysis_suggestions
from analysis.habits import schedule_daily_aggregation
from memory.writer import end_session, start_session
from memory.event_logger import setup_event_logging
import working_tts  # модуль синтеза речи
from working_tts import speak_async
from core.nlp import normalize

# ────────────────────────── LOGGING ──────────────────────────────
log = configure_logging("app")

# ────────────────────────── CONSTANTS ────────────────────────────
VA_ALIAS = ('джарвис',)  # ключевое слово для активации
VA_TBR = (
    'скажи', 'покажи', 'ответь', 'произнеси', 'расскажи', 'сколько', 'слушай'
)  # «мусорные» слова, отбрасываются перед анализом
CMD_CONFIDENCE_THRESHOLD = 70  # минимальный процент fuzzy‑совпадения
# при распознавании слова-активатора допускаем небольшие ошибки произношения
ACTIVATION_CONFIDENCE = 65

# ────────────────────────── STATE ────────────────────────────────
child_processes: List[Process] = []  # дочерние процессы (если будут)

# ────────────────────────── SIGNALS ──────────────────────────────

def _shutdown(signum: int, frame: Any):
    """Корректное завершение по Ctrl‑C/SIGTERM.""" 
    log.info("Получен сигнал %s, завершаюсь…", signum)
    for p in child_processes:
        if p.is_alive():
            p.terminate()
    try:
        loop = asyncio.get_running_loop()
        loop.stop()
    except RuntimeError:
        pass
    sys.exit(0)

signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ────────────────────────── HELPERS ──────────────────────────────

async def execute_cmd(cmd: str, voice: str) -> bool:
    """Обработка простых встроенных команд без скиллов.

    Возвращает ``True``, если команда распознана и выполнена.
    """
    if cmd == 'thanks':
        log.debug("Ответ на благодарность")  # пример улучшенного логирования
        await speak_async("Пожалуйста", preset="happy")
    elif cmd == 'stupid':
        log.debug("Получено оскорбление от пользователя")
        await speak_async("Мне неприятно это слышать", preset="sad")
    elif cmd == 'offf':
        log.debug("Перевод ассистента в спящий режим")
        await speak_async("Переходим в спящий режим", preset="neutral")
    else:
        return False
    return True

async def recognize_cmd(raw: str) -> Dict[str, Any]:
    """Подбирает наиболее подходящую команду из конфигурации."""
    best = {'cmd': '', 'percent': 0}
    for key, variants in VA_CMD_LIST.items():
        for alias in variants:
            score = fuzz.ratio(raw, alias)
            if score > best['percent']:
                best = {'cmd': key, 'percent': score}
    return best

async def filter_cmd(raw: str) -> str:
    """Удаляет служебные слова и возвращает «чистую» команду."""
    text = raw.lower()
    for stop in VA_TBR:
        text = text.replace(stop, '').strip()
    return text

def _matches_activation(word: str) -> bool:
    """Проверяет, похоже ли слово на ключевое «джарвис».

    Используем fuzzy‑сравнение, чтобы распознавать фразы вроде
    «дарвис», «джарвис» и т.п.
    """
    return any(fuzz.ratio(word, alias) >= ACTIVATION_CONFIDENCE for alias in VA_ALIAS)


def extract_cmd(text: str) -> str:
    """Возвращает часть фразы после слова-активатора.

    Если первое слово похоже на «джарвис», отрезаем его и возвращаем
    остаток в качестве команды.
    """
    text = text.lower().strip()
    if not text:
        return ""
    words = text.split()
    if _matches_activation(words[0]):
        return " ".join(words[1:]).strip()
    return ""

def is_stop_cmd(text: str) -> bool:
    """Проверяет, произнесена ли команда «стоп» после активации."""
    return extract_cmd(text) == 'стоп'


def contains_stop(text: str) -> bool:
    """Определяет, встречается ли слово «стоп» в произнесённой фразе.

    Функция предназначена для использования во время длительной озвучки,
    когда распознавание может не уловить слово активации («джарвис»).
    Мы разбиваем фразу на отдельные слова и сравниваем их с искомым словом
    "стоп" по метрике сходства, чтобы реагировать даже при неточных
    распознаваниях вроде "стопа" или "стоп" с лишними символами.
    """
    words = text.lower().split()
    for word in words:
        # Дополнительно реагируем на незавершённые фрагменты
        # вроде "сто" или "ст", которые могут появляться в
        # промежуточных результатах распознавания.
        if word.startswith("ст"):
            if word.startswith("сто") or fuzz.ratio(word, "стоп") >= 75:
                return True
        elif fuzz.ratio(word, "стоп") >= 80:
            return True
    return False

async def va_respond(voice: str):
    """Основная реакция ассистента на распознанный текст."""
    log.info("Распознано: %s", voice)
    cmd = extract_cmd(voice)
    if not cmd:
        return False
    # Сначала пробуем скиллы на исходной команде
    # (нормализация используется только для сопоставления внутри router)
    if await asyncio.to_thread(handle_utterance, cmd):
        return True
    raw = await filter_cmd(cmd)
    raw_norm = normalize(raw)
    cmd_info = await recognize_cmd(raw_norm)
    log.debug("Cmd match: %s", cmd_info)
    if not cmd_info['cmd'] or cmd_info['percent'] < CMD_CONFIDENCE_THRESHOLD:
        # можно озвучивать "Команда не распознана" при необходимости
        return False
    return await execute_cmd(cmd_info['cmd'], voice)

# ────────────────────────── MAIN LOOP ────────────────────────────

async def main(_conn=None):
    """Инициализация и основной цикл ассистента."""
    global VA_CMD_LIST

    # 1. Конфигурация и загрузка скиллов
    VA_CMD_LIST = yaml.safe_load(open('commands.yaml', 'rt', encoding='utf-8'))
    VA_CMD_LIST = {k: [normalize(v) for v in variants]
                   for k, variants in VA_CMD_LIST.items()}
    cfg = configparser.ConfigParser()
    cfg.read('config.ini', encoding='utf-8')
    mic_idx = cfg.getint('MIC', 'microphone_index')
    suggestion_interval = cfg.getint('SUGGESTIONS', 'interval_sec', fallback=60)
    # Загружаем структуру конфигурации (``core.config``) для передачи
    # параметров в отдельные подсистемы.
    app_cfg = load_config()
    setup_event_logging()  # логируем все события в БД

    # Идентификатор владельца берём из конфигурации и используем при
    # создании/завершении сессии в базе данных.
    owner_id = str(app_cfg.user.telegram_user_id)
    # Отдельный логгер для событий присутствия пользователя.
    presence_log = configure_logging("presence.session")
    # В ``session_id`` храним активную сессию (если она есть).
    session_id: int | None = None

    def _on_presence(event: Event) -> None:
        """Обработчик события ``presence.update``."""

        nonlocal session_id
        try:
            if event.attrs.get("present"):
                # Пользователь появился: открываем новую сессию
                if session_id is None:
                    session_id = start_session(owner_id)
                    presence_log.info("session started", extra={"session_id": session_id})
                else:
                    # Сессия уже открыта — фиксируем некорректное состояние
                    presence_log.error(
                        "session already active", extra={"session_id": session_id}
                    )
            else:
                # Пользователь исчез: завершаем сессию, если она была
                if session_id is not None:
                    end_session(session_id)
                    presence_log.info("session ended", extra={"session_id": session_id})
                    session_id = None
                else:
                    presence_log.error("no active session to end")
        except Exception:
            # Любые ошибки логируем, чтобы не потерять информацию
            presence_log.exception("error handling presence.update")

    # Подписываем обработчик на события обновления присутствия
    subscribe("presence.update", _on_presence)

    init_driver('serial')          # канал вывода информации
    EmotionDisplayDriver()         # мост: эмоции → выбранный драйвер дисплея
    EmotionSoundDriver()           # звуки при смене эмоций
    load_all()                     # начальная загрузка плагинов
    EmotionManager(30).start()     # запускаем управление эмоциями
    start_skill_reloader()         # включаем горячую перезагрузку

    # --- Инициализация детектора присутствия ---------------------------
    # Если в конфигурации включено распознавание присутствия, создаём
    # объект ``PresenceDetector`` с параметрами камеры и порогами, взятыми
    # из ``AppConfig``. Детектор запускается в отдельном потоке, чтобы не
    # блокировать основной event loop.
    if app_cfg.presence.enabled:
        detector = PresenceDetector(
            camera_index=app_cfg.presence.camera_index,
            frame_interval_ms=app_cfg.presence.frame_interval_ms,
            absent_after_sec=app_cfg.intel.absent_after_sec,
        )
        threading.Thread(target=detector.run, daemon=True).start()

    # --- Проактивная политика и движок ---------------------------------
    # ``Policy`` определяет канал доставки подсказок, ``ProactiveEngine``
    # подписывается на события брокера и отправляет уведомления согласно
    # решению политики.
    policy = Policy(PolicyConfig())  # пока используем значения по умолчанию
    ProactiveEngine(policy)

    # --- Планировщик генерации подсказок -------------------------------
    # С заданным интервалом вызываем ``analysis.suggestions.generate``.
    # Функция синхронная, поэтому выполняем её в отдельной корутине.
    async def suggestion_scheduler() -> None:
        while True:
            try:
                analysis_suggestions.generate()
            except Exception:
                log.exception("suggestion generation failed")
            await asyncio.sleep(suggestion_interval)

    asyncio.create_task(suggestion_scheduler())  # запускаем планировщик
    asyncio.create_task(schedule_daily_aggregation())  # ежедневный расчёт агрегатов

    # 2. Распознавание речи (Vosk)
    model = vosk.Model('models/model_small')
    kaldi = vosk.KaldiRecognizer(model, 16000)
    recorder = PvRecorder(device_index=mic_idx, frame_length=512)
    recorder.start()

    # 3. Приветственное сообщение (синхронно, чтобы не потерялось)
    await asyncio.to_thread(
        working_tts.working_tts,
        "Джарвис запущен и готов к работе",
        preset="neutral",
    )

    # 4. Отдельная задача для обработки событий GUI/дисплея
    async def gui_loop():
        drv = get_driver()
        while True:
            try:
                # ``process_events`` иногда подвисает при проблемах с последовательным
                # портом дисплея (тайм-ауты записи). Выполняем её в отдельном потоке,
                # чтобы не блокировать главный event loop и распознавание речи.
                await asyncio.to_thread(drv.process_events)
            except Exception:
                log.exception("GUI driver failure")
            await asyncio.sleep(0.05)
    asyncio.create_task(gui_loop())

    log.info("Говорите команды, начиная с 'джарвис'")

    async def process_command(text: str) -> None:
        """Выполняет распознанную команду в отдельной задаче."""
        trace_id = new_trace_id()
        TRACE_ID.set(trace_id)
        log.info("[CMD] %s", text)
        publish(Event(kind="user_query_started", attrs={"text": text}))
        await va_respond(text)
        publish(Event(kind="user_query_ended", attrs={"text": text}))

    while True:
        # Читаем с микрофона в отдельном потоке, чтобы не блокировать event loop
        raw_data = await asyncio.to_thread(recorder.read)
        pcm_arr = array.array('h', raw_data)
        pcm = pcm_arr.tobytes()

        if kaldi.AcceptWaveform(pcm):
            result = json.loads(kaldi.Result()).get('text', '')
            if not result:
                kaldi.Reset()
                continue
            log.info("Услышано: %s", result)  # логируем каждую распознанную фразу
            if working_tts.is_playing:
                # Во время озвучивания реагируем на «джарвис стоп» и просто «стоп»
                if is_stop_cmd(result) or contains_stop(result):
                    working_tts.stop_speaking()
                    stop_mgr.trigger()
                kaldi.Reset()
                continue
            cmd = extract_cmd(result)  # есть слово активации с небольшой погрешностью
            if cmd:
                publish(Event(kind="speech.recognized", attrs={"text": result}))
                asyncio.create_task(process_command(result))
            kaldi.Reset()
        else:
            part = json.loads(kaldi.PartialResult()).get('partial', '')
            if part:
                log.debug("Промежуточно услышано: %s", part)
                # Проверяем, не произносится ли команда «стоп»
                if working_tts.is_playing and (
                    is_stop_cmd(part) or contains_stop(part)
                ):
                    working_tts.stop_speaking()
                    stop_mgr.trigger()
                    kaldi.Reset()

# ────────────────────────── ENTRYPOINT ───────────────────────────

def start_jarvis(conn):
    asyncio.run(main(conn))

if __name__ == '__main__':
    # ─── Демонстрация новых утилит из пакета core ────────────────
    from core.config import load_config
    from core.events import Event, publish, subscribe
    from core.logging_json import configure_logging

    # Загружаем конфигурацию из config.ini
    cfg = load_config()
    # Настраиваем JSON‑логирование для компонента "start"
    demo_log = configure_logging("start")

    def _on_demo(event: Event) -> None:
        """Простейший обработчик события для демонстрации pub/sub."""

        demo_log.info("received event", extra={"event": event.kind, "attrs": event.attrs})

    # Подписываемся на события типа "demo" и публикуем тестовое событие
    subscribe("demo", _on_demo)
    publish(Event("demo", {"user": cfg.user.name}))

    # Запускаем основное приложение в отдельном процессе
    parent_conn, _ = Pipe()
    p = Process(target=start_jarvis, args=(parent_conn,), daemon=True)
    p.start()
    p.join()
