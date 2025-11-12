from __future__ import annotations

"""Основная точка входа голосового ассистента Jarvis.

Запускает подсистемы распознавания речи, вывод на дисплей и обработку
пользовательских команд.  Файл старается оставаться как можно более
простым, поэтому отдельные функции вынесены в модули ``app``,
``jarvis_skills``, ``emotion`` и др.
"""

import asyncio
import configparser
import json
import signal
import sys
import threading
from collections import deque
from typing import Any

from display import DisplayItem, init_driver, DisplayDriver
from core.logging_json import TRACE_ID, configure_logging, new_trace_id
from core import stop as stop_mgr
from emotion import sounds
from behavior.tree import create_behavior_tree
from py_trees.trees import BehaviourTree

# ────────────────────────── LOGGING ──────────────────────────────
log = configure_logging("app")

# Глобальные объекты для управления Telegram-слушателем
tg_stop_event = threading.Event()
tg_task: asyncio.Task | None = None
# Флаг, предотвращающий повторную обработку сигнала завершения.
_shutdown_flag = threading.Event()

# ────────────────────────── SIGNALS ──────────────────────────────

def _shutdown(signum: int, frame: Any):
    """Корректное завершение по Ctrl‑C/SIGTERM."""

    # Предотвращаем повторный запуск логики остановки, если сигнал пришёл
    # несколько раз подряд.
    if _shutdown_flag.is_set():
        log.debug("Игнорируем повторный сигнал %s", signum)
        return
    _shutdown_flag.set()

    log.info("Получен сигнал %s, завершаюсь…", signum)
    # Просим Telegram-слушатель остановиться
    tg_stop_event.set()
    if tg_task is not None:
        log.info("Останавливаю Telegram-слушатель")
        tg_task.cancel()
    # Останавливаем все зарегистрированные подсистемы (проактивные потоки
    # и другие обработчики), чтобы выход был максимально быстрым.
    log.info("Останавливаю фоновые подсистемы")
    stop_mgr.trigger()
    # Сообщаем в лог о завершении и немедленно выходим через ``SystemExit``.
    # Такой подход не вмешивается в текущий ``event loop`` и исключает
    # ошибку ``RuntimeError: Event loop stopped before Future completed``.
    log.info("Ассистент завершил работу по запросу пользователя")
    log.debug("Завершение приложения через SystemExit")
    raise SystemExit(0)

signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ────────────────────────── MAIN LOOP ────────────────────────────


def init_display_from_config(cfg: configparser.ConfigParser) -> DisplayDriver:
    """Инициализировать драйвер дисплея на основе ``config.ini``.

    Параметр ``[DISPLAY] driver`` принимает значения:
      * ``none``   — полностью отключить вывод (по умолчанию);
      * ``console`` — отрисовка в терминале;
      * ``serial``  — работа с реальным устройством M5Stack.
    Если драйвер поддерживает метод ``wait_ready`` (например, Serial‑мост
    M5), он будет вызван для проверки готовности устройства.
    """

    # Получаем имя драйвера, по умолчанию отключаем вывод для чистых логов
    driver_name = cfg.get("DISPLAY", "driver", fallback="none")
    log.info("Инициализация дисплея через драйвер '%s'", driver_name)
    driver = init_driver(driver_name)

    # Некоторые драйверы (Serial) требуют подтверждения готовности
    if hasattr(driver, "wait_ready") and not driver.wait_ready():  # pragma: no cover - проверка специфична для железа
        raise RuntimeError("display not ready")

    return driver


def start_behavior_tree(interval: float = 1.0) -> BehaviourTree:
    """Запустить поведенческое дерево и тикать его периодически."""

    # Создаём дерево поведения Jarvis на основе py_trees
    tree = create_behavior_tree()

    async def _ticker() -> None:
        """Циклически вызываем ``tick`` с указанным интервалом."""
        while True:
            log.debug("tick поведенческого дерева")
            tree.tick()
            await asyncio.sleep(interval)

    # Запускаем асинхронную задачу тикера
    ticker = asyncio.create_task(_ticker())

    def _stop_tree() -> bool:
        """Остановить тикер и само дерево при завершении приложения."""

        log.info("Останавливаю поведенческое дерево")
        ticker.cancel()
        try:
            tree.stop()
        except Exception:  # pragma: no cover - защита от редких ошибок
            log.exception("ошибка остановки дерева")
        return True

    # Регистрируем обработчик остановки, чтобы корректно завершить дерево
    stop_mgr.register(_stop_tree)
    log.info("Поведенческое дерево запущено")
    return tree

async def main() -> None:
    """Инициализация и основной цикл ассистента."""

    # 0. Загружаем конфиг и инициализируем дисплей как можно раньше,
    # чтобы возможные ошибки были показаны до старта тяжёлых подсистем.
    cfg = configparser.ConfigParser()
    cfg.read("config.ini", encoding="utf-8")
    try:
        driver = init_display_from_config(cfg)
    except Exception:
        # Если дисплей не доступен, озвучиваем проблему и завершаем работу.
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
    # Модули зрения: детектор присутствия и сканер камеры
    from sensors.vision import PresenceDetector, IdleScanner
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
        _matches_activation,
    )
    from app.presence_session import setup_presence_session
    from app.gui import gui_loop
    from app.scheduler import start_background_tasks
    # Реалтайм-визуализация истории настроения
    from analysis.mood_visualizer import watch_mood_history
    import vosk
    import yaml
    from audio.robot_stream import RobotAudioStream, RobotStreamClosed

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
            show_window=app_cfg.presence.show_window,
            frame_rotation=app_cfg.presence.frame_rotation,
        )
        # Запускаем детектор в отдельном потоке. Внутри используется OpenCV,
        # поэтому при отсутствии библиотеки или камеры модуль просто
        # выводит предупреждение и завершает поток.
        threading.Thread(target=detector.run, daemon=True).start()

        # При отсутствии человека в кадре запускаем ``IdleScanner``, который
        # плавно осматривает помещение и управляет сервоприводами камеры.
        # Ширина/высота кадра подобраны под стандартный формат 320x240 px,
        # при необходимости их можно скорректировать в конфигурации.
        idle_scanner = IdleScanner(frame_width=320.0, frame_height=240.0)
        stop_mgr.register(idle_scanner.stop)
        log.debug("IdleScanner активирован для автосканирования камеры")

    # --- Проактивная политика и движок ---------------------------------
    # ``Policy`` определяет канал доставки подсказок, ``ProactiveEngine``
    # подписывается на события брокера и отправляет уведомления согласно
    # решению политики.
    policy = Policy(PolicyConfig())  # пока используем значения по умолчанию
    ProactiveEngine(policy)

    # Запускаем фоновые задачи анализа и плейбука
    start_background_tasks()

    # При необходимости запускаем отдельный поток наблюдения за настроением,
    # чтобы видеть динамику valence/arousal в реальном времени. Опция
    # включается в ``config.ini`` в разделе [ANALYSIS].
    if cfg.getboolean("ANALYSIS", "watch_mood", fallback=False):
        threading.Thread(target=watch_mood_history, daemon=True).start()
        log.info("Запущен монитор настроения")

    # ── Поведенческое дерево ─────────────────────────────────────
    # После инициализации подсистем формируем дерево и запускаем
    # его тикер, чтобы ассистент реагировал на окружение.
    start_behavior_tree()

    # --- Telegram listener -------------------------------------------------
    global tg_task
    if app_cfg.telegram.token:
        from notifiers.telegram_listener import launch as launch_telegram_listener

        log.info("Запускаю Telegram-слушатель")
        tg_task = asyncio.create_task(
            launch_telegram_listener(stop_event=tg_stop_event)
        )

    # 2. Распознавание речи (Vosk)
    expected_sample_rate = cfg.getint("AUDIO", "sample_rate", fallback=16000)
    model = vosk.Model('models/model_small')
    kaldi = vosk.KaldiRecognizer(model, expected_sample_rate)

    working_tts.set_local_playback_enabled(
        cfg.getboolean("ROBOT_AUDIO", "local_playback", fallback=False)
    )

    robot_audio_endpoint = cfg.get("ROBOT_AUDIO", "endpoint", fallback="ws://127.0.0.1:8765/")
    if not robot_audio_endpoint:
        raise RuntimeError("Не задан endpoint WebSocket для аудиопотока робота")
    robot_subprotocol = cfg.get("ROBOT_AUDIO", "subprotocol", fallback="").strip() or None
    robot_auth = cfg.get("ROBOT_AUDIO", "authorization", fallback="").strip() or None
    ping_interval = cfg.getfloat("ROBOT_AUDIO", "ping_interval", fallback=10.0)
    ping_timeout = cfg.getfloat("ROBOT_AUDIO", "ping_timeout", fallback=5.0)

    audio_stream = RobotAudioStream(
        endpoint=robot_audio_endpoint,
        queue_max=cfg.getint("AUDIO", "queue_max", fallback=200),
        expected_sample_rate=expected_sample_rate,
        expected_channels=2,
        subprotocol=robot_subprotocol,
        authorization=robot_auth,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
    )
    await audio_stream.start()
    stop_mgr.register(audio_stream.stop)
    working_tts.register_stream_listener(audio_stream.forward_tts_chunk)

    def _detach_tts() -> bool:
        """Отвязывает поток TTS от робота при остановке приложения."""

        working_tts.unregister_stream_listener(audio_stream.forward_tts_chunk)
        return True

    stop_mgr.register(_detach_tts)
    log.info(
        "WebSocket-приёмник аудио готов",
        extra={"attrs": {"endpoint": robot_audio_endpoint}},
    )

    # Кольцевой буфер на ~1.5 секунды аудио.
    # Размер maxlen вычисляется после получения первого кадра, потому что
    # длина frame_samples теоретически может отличаться от 512.
    pcm_buffer: deque[bytes] = deque(maxlen=1)
    buffer_limit_frames: int | None = None
    # Флаг, что слово активации уже было найдено и буфер «прокручен».
    # Позволяет избежать многократных повторных распознаваний, когда
    # ``PartialResult`` продолжает содержать «джарвис» несколько итераций подряд.
    activated = False

    # 3. Приветственный звук (синхронно, чтобы не потерялся)
    await asyncio.to_thread(sounds.play_effect, "WAKE")
    driver.draw(DisplayItem(kind="mode", payload="run"))

    asyncio.create_task(gui_loop())

    log.info("Говорите команды, начиная с 'джарвис'")

    async def process_command(text: str) -> None:
        """Выполняет распознанную команду в отдельной задаче."""
        trace_id = new_trace_id()
        TRACE_ID.set(trace_id)
        # Логируем входящую команду вместе с trace_id для последующего трекинга.
        log.info("[CMD] %s", text, extra={"ctx": {"trace_id": trace_id}})
        publish(Event(kind="user_query_started", attrs={"text": text, "trace_id": trace_id}))
        try:
            handled = await va_respond(text)
        except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
            log.exception("command error: %s", exc)
            publish(
                Event(
                    kind="dialog.failure",
                    attrs={"text": text, "error": str(exc), "trace_id": trace_id},
                )
            )
        else:
            kind = "dialog.success" if handled else "dialog.failure"
            publish(Event(kind=kind, attrs={"text": text, "trace_id": trace_id}))
        finally:
            publish(Event(kind="user_query_ended", attrs={"text": text, "trace_id": trace_id}))

    while True:
        try:
            frame = await audio_stream.read()
        except RobotStreamClosed:
            log.error("Аудиопоток робота завершён, останавливаю распознавание")
            break

        pcm = frame.pcm_mono
        if buffer_limit_frames is None:
            buffer_limit_frames = max(
                1,
                int(round(1.5 * frame.sample_rate / frame.frame_samples)),
            )
            new_buffer: deque[bytes] = deque(maxlen=buffer_limit_frames)
            new_buffer.extend(pcm_buffer)
            pcm_buffer = new_buffer
            buffer_seconds = buffer_limit_frames * frame.frame_samples / frame.sample_rate
            log.info(
                "Размер кольцевого буфера настроен",
                extra={
                    "attrs": {
                        "frames": buffer_limit_frames,
                        "seconds": round(buffer_seconds, 3),
                        "frame_samples": frame.frame_samples,
                    }
                },
            )

        log.debug(
            "Получен аудиокадр",
            extra={
                "attrs": {
                    "sequence": frame.sequence,
                    "timestamp_us": frame.timestamp_us,
                    "buffer": len(pcm_buffer),
                }
            },
        )
        if kaldi.AcceptWaveform(pcm):
            # Фраза завершена: собираем финальный текст.
            result = json.loads(kaldi.Result()).get('text', '')
            if not result:
                kaldi.Reset()
                pcm_buffer.clear()
                activated = False
                continue
            if activated and not result.startswith("джарвис"):
                # Иногда Vosk отбрасывает первое слово — возвращаем его вручную.
                log.debug("Слово активации отсутствует в финальном тексте — добавляю")
                result = f"джарвис {result}".strip()
            log.info("Услышано: %s", result)  # логируем каждую распознанную фразу
            if working_tts.is_playing:
                # Во время озвучивания реагируем на «джарвис стоп» и просто «стоп»
                if is_stop_cmd(result) or contains_stop(result):
                    working_tts.stop_speaking()
                    stop_mgr.trigger()
                kaldi.Reset()
                pcm_buffer.clear()
                activated = False
                continue
            cmd = extract_cmd(result)  # есть слово активации с небольшой погрешностью
            if cmd:
                publish(Event(kind="speech.recognized", attrs={"text": result}))
                asyncio.create_task(process_command(result))
            pcm_buffer.clear()
            kaldi.Reset()
            activated = False
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
                    pcm_buffer.clear()
                else:
                    # Проверяем, не появилось ли слово активации в промежуточном тексте
                    if (not activated) and any(
                        _matches_activation(w) for w in part.split()
                    ):
                        log.info("Обнаружено слово активации в потоке: %s", part)
                        log.debug(
                            "Размер буфера перед повторным распознаванием: %d",
                            len(pcm_buffer),
                        )
                        # Сбрасываем распознаватель и повторно «проигрываем»
                        # накопленные кадры, чтобы не потерять начало слова
                        kaldi.Reset()
                        for old_pcm in pcm_buffer:
                            _ = kaldi.AcceptWaveform(old_pcm)
                        _ = kaldi.AcceptWaveform(pcm)
                        log.info("Повторное распознавание выполнено")
                        pcm_buffer.clear()
                        activated = True

        # Добавляем текущий кадр в кольцевой буфер и выводим его размер
        pcm_buffer.append(pcm)
        log.debug("Размер кольцевого буфера: %d", len(pcm_buffer))

if __name__ == "__main__":
    asyncio.run(main())
    if _shutdown_flag.is_set():
        # Пользователь запросил остановку (Ctrl+C). Дополнительный лог
        # помогает отследить завершение приложения.
        log.info("Ассистент завершил работу по запросу пользователя")
