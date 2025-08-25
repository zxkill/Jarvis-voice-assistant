from __future__ import annotations

"""Основная точка входа голосового ассистента Jarvis.

Запускает подсистемы распознавания речи, вывод на дисплей и обработку
пользовательских команд.  Файл старается оставаться как можно более
простым, поэтому отдельные функции вынесены в модули ``app``,
``jarvis_skills``, ``emotion`` и др.
"""

import array
import asyncio
import configparser
import json
import signal
import sys
import threading
from typing import Any

from display import DisplayItem, init_driver
from core.logging_json import TRACE_ID, configure_logging, new_trace_id
from core import stop as stop_mgr

# ────────────────────────── LOGGING ──────────────────────────────
log = configure_logging("app")

# ────────────────────────── SIGNALS ──────────────────────────────

def _shutdown(signum: int, frame: Any):
    """Корректное завершение по Ctrl‑C/SIGTERM."""
    log.info("Получен сигнал %s, завершаюсь…", signum)
    try:
        loop = asyncio.get_running_loop()
        loop.stop()
    except RuntimeError:
        pass
    sys.exit(0)

signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ────────────────────────── MAIN LOOP ────────────────────────────

async def main() -> None:
    """Инициализация и основной цикл ассистента."""

    # 0. Проверяем подключение дисплея как можно раньше, чтобы
    # ошибки отображались ещё до загрузки тяжёлых подсистем.
    try:
        driver = init_driver("serial")
        if not driver.wait_ready():
            from working_tts import working_tts

            await asyncio.to_thread(
                working_tts,
                "Дисплей не подключен",
                preset="neutral",
            )
            return
    except Exception:
        from working_tts import working_tts

        await asyncio.to_thread(
            working_tts,
            "Дисплей не подключен",
            preset="neutral",
        )
        return

    driver.draw(DisplayItem(kind="mode", payload="boot"))

    from emotion.manager import EmotionManager
    from emotion.drivers import EmotionDisplayDriver
    from emotion.sounds import EmotionSoundDriver
    from jarvis_skills import load_all, start_skill_reloader
    from core.config import load_config
    from core.events import Event, publish
    from sensors.vision import PresenceDetector
    from proactive.policy import Policy, PolicyConfig
    from proactive.engine import ProactiveEngine
    from memory.event_logger import setup_event_logging
    import working_tts
    from core.nlp import normalize
    from app import command_processing
    from app.command_processing import (
        contains_stop,
        extract_cmd,
        is_stop_cmd,
        va_respond,
    )
    from app.presence_session import setup_presence_session
    from app.gui import gui_loop
    from app.scheduler import start_background_tasks
    import vosk
    import yaml
    from pvrecorder import PvRecorder

    EmotionDisplayDriver()         # мост: эмоции → выбранный драйвер дисплея
    EmotionSoundDriver()           # звуки при смене эмоций

    # 1. Конфигурация и загрузка скиллов
    command_processing.VA_CMD_LIST = yaml.safe_load(
        open("commands.yaml", "rt", encoding="utf-8")
    )
    command_processing.VA_CMD_LIST = {
        k: [normalize(v) for v in variants]
        for k, variants in command_processing.VA_CMD_LIST.items()
    }
    cfg = configparser.ConfigParser()
    cfg.read("config.ini", encoding="utf-8")
    mic_idx = cfg.getint("MIC", "microphone_index")
    suggestion_interval = cfg.getint(
        "SUGGESTIONS", "interval_sec", fallback=60
    )
    # Загружаем структуру конфигурации (``core.config``) для передачи
    # параметров в отдельные подсистемы.
    app_cfg = load_config()
    setup_event_logging()  # логируем все события в БД

    owner_id = str(app_cfg.user.telegram_user_id)
    setup_presence_session(owner_id)

    load_all()                     # начальная загрузка плагинов
    EmotionManager().start()        # запускаем управление эмоциями
    start_skill_reloader()         # включаем горячую перезагрузку

    async def _monitor_display() -> None:
        while True:
            await asyncio.sleep(1)
            if driver.disconnected.is_set():
                log.warning("Display disconnected, waiting for reconnection")
                # Даем M5 время на перезапуск и повторное рукопожатие
                reconnected = await asyncio.to_thread(driver.wait_ready, 5.0)
                if reconnected:
                    continue
                while working_tts.is_playing:
                    await asyncio.sleep(0.1)
                await asyncio.to_thread(
                    working_tts.working_tts,
                    "Дисплей был отключен, завершаю работу",
                    preset="neutral",
                )
                sys.exit(0)

    asyncio.create_task(_monitor_display())

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

    start_background_tasks(suggestion_interval)

    # 2. Распознавание речи (Vosk)
    model = vosk.Model('models/model_small')
    kaldi = vosk.KaldiRecognizer(model, 16000)
    recorder = PvRecorder(device_index=mic_idx, frame_length=512)

    # 3. Приветственное сообщение (синхронно, чтобы не потерялось)
    await asyncio.to_thread(
        working_tts.working_tts,
        "Джарвис запущен и готов к работе",
        preset="neutral",
    )
    driver.draw(DisplayItem(kind="mode", payload="run"))

    recorder.start()
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


if __name__ == "__main__":
    asyncio.run(main())
