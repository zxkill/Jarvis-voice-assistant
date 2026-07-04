from __future__ import annotations

"""Основная точка входа голосового ассистента Jarvis."""

import asyncio
import configparser
import json
import signal
import sys
import threading
import time
from collections import deque
from typing import Any

from audio.robot_stream import RobotAudioStream, RobotStreamClosed
from display import DisplayItem, init_driver, DisplayDriver
from core.logging_json import TRACE_ID, configure_logging, new_trace_id
from core import stop as stop_mgr
from emotion import sounds
from behavior.tree import create_behavior_tree
from py_trees.trees import BehaviourTree

log = configure_logging("app")

tg_stop_event = threading.Event()
tg_task: asyncio.Task | None = None
_shutdown_flag = threading.Event()
_main_loop: asyncio.AbstractEventLoop | None = None


def _request_loop_stop() -> None:
    """Аккуратно останавливает основной event loop."""

    global _main_loop
    if _main_loop is None:
        log.warning("Основной event loop не инициализирован, завершаю процесс через sys.exit")
        sys.exit(0)
    log.debug("Передаю команду остановки в event loop")
    _main_loop.call_soon_threadsafe(_main_loop.stop)


def _shutdown(signum: int, frame: Any):
    """Корректное завершение по Ctrl-C/SIGTERM."""

    if _shutdown_flag.is_set():
        log.debug("Игнорируем повторный сигнал %s", signum)
        return
    _shutdown_flag.set()
    log.info("Получен сигнал %s, завершаюсь…", signum)
    tg_stop_event.set()
    if tg_task is not None:
        log.info("Останавливаю Telegram-слушатель")
        tg_task.cancel()
    log.info("Останавливаю фоновые подсистемы")
    stop_mgr.trigger()
    log.info("Ассистент завершил работу по запросу пользователя")
    _request_loop_stop()


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def init_display_from_config(cfg: configparser.ConfigParser) -> DisplayDriver:
    """Инициализировать драйвер дисплея на основе ``config.ini``."""

    driver_name = cfg.get("DISPLAY", "driver", fallback="none")
    log.info("Инициализация дисплея через драйвер '%s'", driver_name)
    driver = init_driver(driver_name)
    if hasattr(driver, "wait_ready") and not driver.wait_ready():  # pragma: no cover
        raise RuntimeError("display not ready")
    return driver


def start_behavior_tree(interval: float = 1.0) -> BehaviourTree:
    """Запустить поведенческое дерево и тикать его периодически."""

    tree = create_behavior_tree()

    async def _ticker() -> None:
        while True:
            log.debug("tick поведенческого дерева")
            tree.tick()
            await asyncio.sleep(interval)

    ticker = asyncio.create_task(_ticker())
    stopped = False

    def _stop_tree() -> bool:
        nonlocal stopped
        if stopped:
            log.debug("Поведенческое дерево уже остановлено, пропускаю повторный вызов")
            return False
        stopped = True
        log.info("Останавливаю поведенческое дерево")
        ticker.cancel()
        try:
            tree.shutdown()
        except asyncio.CancelledError:
            log.debug("Тикер поведенческого дерева уже отменён")
        except Exception:
            log.exception("ошибка остановки дерева")
        return True

    stop_mgr.register(_stop_tree)
    log.info("Поведенческое дерево запущено")
    return tree


async def start_robot_audio_stream(cfg: configparser.ConfigParser) -> RobotAudioStream | None:
    """Запустить WebSocket-поток аудио робота с отказоустойчивостью."""

    robot_audio_endpoint = cfg.get("ROBOT_AUDIO", "endpoint", fallback="ws://127.0.0.1:8765/")
    robot_subprotocol = cfg.get("ROBOT_AUDIO", "subprotocol", fallback="").strip() or None
    robot_auth = cfg.get("ROBOT_AUDIO", "authorization", fallback="").strip() or None
    ping_interval = cfg.getfloat("ROBOT_AUDIO", "ping_interval", fallback=0.0)
    ping_timeout = cfg.getfloat("ROBOT_AUDIO", "ping_timeout", fallback=0.0)

    audio_stream = RobotAudioStream(
        endpoint=robot_audio_endpoint,
        queue_max=cfg.getint("AUDIO", "queue_max", fallback=200),
        expected_sample_rate=cfg.getint("AUDIO", "sample_rate", fallback=16000),
        expected_channels=2,
        subprotocol=robot_subprotocol,
        authorization=robot_auth,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
        stt_channel=cfg.get("AUDIO", "stt_channel", fallback="best"),
        tts_preroll_ms=cfg.getint("TTS", "preroll_ms", fallback=120),
        tts_stream_pace=cfg.getfloat("TTS", "stream_pace", fallback=0.96),
        tts_initial_burst_ms=cfg.getint("TTS", "initial_burst_ms", fallback=650),
    )

    try:
        await audio_stream.start()
    except OSError as exc:
        log.error(
            "Не удалось запустить WebSocket-сервер аудио робота",
            exc_info=exc,
            extra={
                "attrs": {
                    "endpoint": robot_audio_endpoint,
                    "hint": "проверьте доступность интерфейса или смените endpoint на 0.0.0.0/127.0.0.1",
                }
            },
        )
        log.info("Перехожу в режим без робота", extra={"attrs": {"endpoint": robot_audio_endpoint}})
        return None
    return audio_stream


async def main() -> None:
    """Инициализация и основной цикл ассистента."""

    global _main_loop
    _main_loop = asyncio.get_running_loop()
    log.debug("Основной event loop сохранён для управляемого завершения")

    cfg = configparser.ConfigParser()
    cfg.read("config.ini", encoding="utf-8")

    try:
        driver = init_display_from_config(cfg)
    except Exception:
        from working_tts import working_tts

        await asyncio.to_thread(working_tts, "Дисплей не подключен", preset="neutral")
        return
    driver.draw(DisplayItem(kind="mode", payload="boot"))

    from emotion.manager import EmotionManager
    from emotion.drivers import EmotionDisplayDriver
    from emotion.sounds import EmotionSoundDriver
    from jarvis_skills import load_all, start_skill_reloader
    from core.config import load_config
    from core.events import Event, publish
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
        find_activation_index,
        is_stop_cmd,
        va_respond,
        _matches_activation,
    )
    from app.presence_session import setup_presence_session
    from app.gui import gui_loop
    from app.scheduler import start_background_tasks
    from analysis.mood_visualizer import watch_mood_history
    import vosk
    import yaml

    effects_enabled = cfg.getboolean("ROBOT_AUDIO", "effects_enabled", fallback=False)
    fast_partial_enabled = cfg.getboolean("STT", "fast_partial", fallback=True)
    partial_stable_sec = cfg.getfloat("STT", "partial_stable_sec", fallback=0.35)
    # Для движения держим partial чуть дольше: это защищает фразы вида
    # «джарвис, на один метр вперёд» от преждевременного запуска по куску
    # «на один метр», пока направление ещё не произнесено.
    partial_motion_stable_sec = cfg.getfloat("STT", "partial_motion_stable_sec", fallback=0.75)
    partial_stop_stable_sec = cfg.getfloat("STT", "partial_stop_stable_sec", fallback=0.12)
    partial_min_command_chars = cfg.getint("STT", "partial_min_command_chars", fallback=3)
    ignore_after_fast_sec = cfg.getfloat("STT", "ignore_after_fast_sec", fallback=0.65)
    partial_response_guard_sec = cfg.getfloat("STT", "partial_response_guard_sec", fallback=0.12)
    # Безопасный режим: fast partial срабатывает только для очевидно завершённых
    # коротких команд. Всё сомнительное ждёт финальный результат Vosk.
    partial_fast_safe_mode = cfg.getboolean("STT", "partial_fast_safe_mode", fallback=True)

    EmotionDisplayDriver()
    if effects_enabled:
        EmotionSoundDriver()
        log.info("Звуки эмоций включены")
    else:
        log.info("Звуки эмоций отключены для быстрого голосового режима")

    command_processing.VA_CMD_LIST = yaml.safe_load(open("commands.yaml", "rt", encoding="utf-8"))
    command_processing.VA_CMD_LIST = {
        k: [normalize(v) for v in variants]
        for k, variants in command_processing.VA_CMD_LIST.items()
    }

    app_cfg = load_config()
    setup_event_logging()
    owner_id = str(app_cfg.user.telegram_user_id)
    setup_presence_session(owner_id)

    load_all()
    EmotionManager().start()
    start_skill_reloader()

    async def _monitor_display() -> None:
        while True:
            await asyncio.sleep(1)
            if driver.disconnected.is_set():
                log.warning("Display disconnected, waiting for reconnection")
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

    if app_cfg.presence.enabled:
        detector = PresenceDetector(
            camera_index=app_cfg.presence.camera_index,
            frame_interval_ms=app_cfg.presence.frame_interval_ms,
            absent_after_sec=app_cfg.intel.absent_after_sec,
            show_window=app_cfg.presence.show_window,
            frame_rotation=app_cfg.presence.frame_rotation,
        )
        threading.Thread(target=detector.run, daemon=True).start()
        idle_scanner = IdleScanner(frame_width=320.0, frame_height=240.0)
        stop_mgr.register(idle_scanner.stop)
        log.debug("IdleScanner активирован для автосканирования камеры")

    policy = Policy(PolicyConfig())
    ProactiveEngine(policy)
    start_background_tasks()

    if cfg.getboolean("ANALYSIS", "watch_mood", fallback=False):
        threading.Thread(target=watch_mood_history, daemon=True).start()
        log.info("Запущен монитор настроения")

    start_behavior_tree()

    global tg_task
    if app_cfg.telegram.token:
        from notifiers.telegram_listener import launch as launch_telegram_listener

        log.info("Запускаю Telegram-слушатель")
        tg_task = asyncio.create_task(launch_telegram_listener(stop_event=tg_stop_event))

    expected_sample_rate = cfg.getint("AUDIO", "sample_rate", fallback=16000)
    model = vosk.Model("models/model_small")
    kaldi = vosk.KaldiRecognizer(model, expected_sample_rate)
    try:
        kaldi.SetWords(False)
        kaldi.SetPartialWords(False)
    except Exception:
        pass

    working_tts.set_local_playback_enabled(cfg.getboolean("ROBOT_AUDIO", "local_playback", fallback=False))
    sounds.set_local_playback_enabled(cfg.getboolean("ROBOT_AUDIO", "local_playback", fallback=False))

    audio_stream = await start_robot_audio_stream(cfg)
    if audio_stream is not None:
        stop_mgr.register(audio_stream.stop)
        working_tts.register_stream_listener(audio_stream.forward_tts_chunk)
        if effects_enabled:
            sounds.register_stream_listener(audio_stream.forward_effect_chunk)

        def _detach_tts() -> bool:
            working_tts.unregister_stream_listener(audio_stream.forward_tts_chunk)
            return True

        stop_mgr.register(_detach_tts)

        if effects_enabled:
            def _detach_effects() -> bool:
                sounds.unregister_stream_listener(audio_stream.forward_effect_chunk)
                return True

            stop_mgr.register(_detach_effects)

        log.info("WebSocket-приёмник аудио готов", extra={"attrs": {"endpoint": audio_stream._parsed.geturl()}})

    pcm_buffer: deque[bytes] = deque(maxlen=1)
    buffer_limit_frames: int | None = None
    activated = False
    activated_at = 0.0
    last_partial_cmd = ""
    last_partial_changed_at = 0.0
    ignore_until = 0.0

    def _reset_stt_state() -> None:
        nonlocal activated, activated_at, last_partial_cmd, last_partial_changed_at
        activated = False
        activated_at = 0.0
        last_partial_cmd = ""
        last_partial_changed_at = 0.0

    def _stop_tts_only() -> None:
        # Важно: не вызываем stop_mgr.trigger() на голосовую команду «стоп».
        # Он останавливает фоновые подсистемы, а нам нужно только прервать речь.
        working_tts.stop_speaking()

    def _command_from_partial(part: str) -> str:
        cmd = extract_cmd(part)
        if cmd:
            return cmd
        if activated:
            words = part.strip().split()
            idx = find_activation_index(words)
            if idx >= 0:
                return " ".join(words[idx + 1:]).strip()
            return part.strip()
        return ""

    def _norm_partial_command(text: str) -> str:
        """Нормализует partial-команду для эвристик fast-STT."""

        return normalize(text).lower().replace("ё", "е").strip()

    def _partial_command_kind(cmd: str) -> str:
        """Определяет, можно ли запускать partial-команду до финальной паузы.

        Fast-STT хорош для коротких команд, но опасен для длинных фраз:
        Vosk может на несколько кадров зафиксировать промежуточное
        «на один метр», после чего ассистент успевает ответить
        «можете повторить?» раньше, чем пользователь произнёс «вперёд».
        Поэтому здесь есть белый список быстрых команд и отдельный режим
        ожидания для очевидно незавершённых фраз.
        """

        text = _norm_partial_command(cmd)
        if not text:
            return "wait"

        words = text.split()
        tail = words[-1] if words else ""

        stop_words = ("стоп", "стой", "останов", "хватит")
        if any(w in text for w in stop_words):
            return "stop"

        # Слова, после которых почти всегда ожидается продолжение.
        incomplete_tail_words = {
            "на", "в", "во", "к", "ко", "до", "по", "за", "через",
            "один", "одна", "одно", "два", "две", "три", "четыре", "пять",
            "метр", "метра", "метров", "сантиметр", "сантиметра", "сантиметров",
            "см", "миллиметр", "миллиметра", "миллиметров", "мм",
            "градус", "градуса", "градусов",
        }

        motion_words = (
            "вперед", "назад", "подъедь", "отъедь", "ближе",
            "налево", "влево", "лево", "направо", "вправо", "право",
            "поверни", "разверни",
        )
        has_motion_word = any(w in text for w in motion_words)

        # Если во фразе уже есть число/единица, но ещё нет направления,
        # это почти наверняка середина команды движения: «на один метр ...».
        quantity_words = (
            "метр", "метра", "метров", "сантим", "см", "миллимет", "мм",
            "градус", "градуса", "градусов",
            "один", "одна", "два", "две", "три", "четыре", "пять",
        )
        has_quantity = any(w in text for w in quantity_words) or any(ch.isdigit() for ch in text)
        if has_quantity and not has_motion_word:
            return "wait"

        if tail in incomplete_tail_words:
            return "wait"

        if has_motion_word:
            return "motion"

        instant_words = (
            "время", "час", "погода", "температура", "статистика",
            "статус робота", "робот статус", "состояние робота",
        )
        if any(w in text for w in instant_words):
            return "instant"

        return "unknown"

    def _partial_required_stable_sec(kind: str) -> float | None:
        """Возвращает время стабильности partial или None, если надо ждать финал."""

        if kind == "stop":
            return partial_stop_stable_sec
        if kind == "motion":
            return partial_motion_stable_sec
        if kind == "instant":
            return partial_stable_sec
        if partial_fast_safe_mode:
            # В безопасном режиме неизвестные длинные команды не ускоряем,
            # чтобы не получать преждевременный fallback.
            return None
        return max(partial_motion_stable_sec, partial_stable_sec)

    async def _run_command_after_guard(text: str, guard_sec: float) -> None:
        # При fast partial мы запускаем ответ ещё до финальной паузы Vosk.
        # Очень короткая задержка даёт пользователю закончить фразу, а ESP32 —
        # спокойно переключиться из режима микрофона в режим воспроизведения.
        if guard_sec > 0:
            await asyncio.sleep(guard_sec)
        await process_command(text)

    async def _dispatch_recognized(text: str, *, partial: bool = False) -> None:
        publish(Event(kind="speech.recognized", attrs={"text": text, "partial": partial}))
        guard = partial_response_guard_sec if partial else 0.0
        asyncio.create_task(_run_command_after_guard(text, guard))

    if effects_enabled:
        await asyncio.to_thread(sounds.play_effect, "WAKE")
    driver.draw(DisplayItem(kind="mode", payload="run"))
    asyncio.create_task(gui_loop())
    log.info("Говорите команды, начиная с 'джарвис'")

    async def process_command(text: str) -> None:
        """Выполняет распознанную команду в отдельной задаче."""

        trace_id = new_trace_id()
        TRACE_ID.set(trace_id)
        log.info("[CMD] %s", text, extra={"ctx": {"trace_id": trace_id}})
        publish(Event(kind="user_query_started", attrs={"text": text, "trace_id": trace_id}))
        try:
            handled = await va_respond(text)
        except Exception as exc:
            log.exception("command error: %s", exc)
            publish(Event(kind="dialog.failure", attrs={"text": text, "error": str(exc), "trace_id": trace_id}))
        else:
            kind = "dialog.success" if handled else "dialog.failure"
            publish(Event(kind=kind, attrs={"text": text, "trace_id": trace_id}))
        finally:
            publish(Event(kind="user_query_ended", attrs={"text": text, "trace_id": trace_id}))

    if audio_stream is None:
        log.warning(
            "Аудиопоток робота недоступен: остаёмся в режиме Telegram/текста",
            extra={"attrs": {"telegram_active": app_cfg.telegram.token != ""}},
        )
        while not tg_stop_event.is_set() and not _shutdown_flag.is_set():
            await asyncio.sleep(0.5)
        log.info("Остановлен режим без аудиопотока по запросу пользователя")
        return

    while True:
        try:
            frame = await audio_stream.read()
        except RobotStreamClosed:
            log.error("Аудиопоток робота завершён, останавливаю распознавание")
            break

        now_mono = time.monotonic()
        pcm = frame.pcm_mono

        if buffer_limit_frames is None:
            buffer_limit_frames = max(1, int(round(1.2 * frame.sample_rate / frame.frame_samples)))
            new_buffer: deque[bytes] = deque(maxlen=buffer_limit_frames)
            new_buffer.extend(pcm_buffer)
            pcm_buffer = new_buffer
            buffer_seconds = buffer_limit_frames * frame.frame_samples / frame.sample_rate
            log.info(
                "Размер кольцевого буфера настроен",
                extra={"attrs": {"frames": buffer_limit_frames, "seconds": round(buffer_seconds, 3), "frame_samples": frame.frame_samples}},
            )

        if now_mono < ignore_until:
            pcm_buffer.append(pcm)
            continue

        if kaldi.AcceptWaveform(pcm):
            result = json.loads(kaldi.Result()).get("text", "")
            if not result:
                kaldi.Reset()
                pcm_buffer.clear()
                _reset_stt_state()
                continue

            if activated and not extract_cmd(result):
                log.debug("Слово активации отсутствует в финальном тексте — добавляю")
                result = f"джарвис {result}".strip()

            log.info("Услышано: %s", result)

            if working_tts.is_playing:
                if is_stop_cmd(result) or contains_stop(result):
                    _stop_tts_only()
                kaldi.Reset()
                pcm_buffer.clear()
                _reset_stt_state()
                continue

            if extract_cmd(result):
                await _dispatch_recognized(result, partial=False)

            pcm_buffer.clear()
            kaldi.Reset()
            _reset_stt_state()
        else:
            part = json.loads(kaldi.PartialResult()).get("partial", "")
            if part:
                log.debug("Промежуточно услышано: %s", part)

                if working_tts.is_playing and (is_stop_cmd(part) or contains_stop(part)):
                    _stop_tts_only()
                    kaldi.Reset()
                    pcm_buffer.clear()
                    _reset_stt_state()
                else:
                    words = part.split()
                    if not activated and any(_matches_activation(w) for w in words):
                        log.info("Обнаружено слово активации в потоке: %s", part)
                        kaldi.Reset()
                        for old_pcm in pcm_buffer:
                            _ = kaldi.AcceptWaveform(old_pcm)
                        _ = kaldi.AcceptWaveform(pcm)
                        pcm_buffer.clear()
                        activated = True
                        activated_at = now_mono
                        last_partial_cmd = ""
                        last_partial_changed_at = now_mono

                    if fast_partial_enabled and activated and not working_tts.is_playing:
                        cmd_part = _command_from_partial(part)
                        if len(cmd_part) >= partial_min_command_chars:
                            kind = _partial_command_kind(cmd_part)
                            required_stable_sec = _partial_required_stable_sec(kind)

                            if required_stable_sec is None:
                                # Подробный лог нужен для отладки длинных команд:
                                # видно, что partial услышан, но намеренно ждём финал Vosk.
                                if cmd_part != last_partial_cmd:
                                    log.debug(
                                        "partial ждёт финал: %r kind=%s safe=%s",
                                        cmd_part,
                                        kind,
                                        partial_fast_safe_mode,
                                    )
                                    last_partial_cmd = cmd_part
                                    last_partial_changed_at = now_mono
                            elif cmd_part != last_partial_cmd:
                                last_partial_cmd = cmd_part
                                last_partial_changed_at = now_mono
                                log.debug(
                                    "partial-кандидат: %r kind=%s stable=%.2fs",
                                    cmd_part,
                                    kind,
                                    required_stable_sec,
                                )
                            elif (
                                now_mono - last_partial_changed_at >= required_stable_sec
                                and now_mono - activated_at >= 0.25
                            ):
                                result = f"джарвис {cmd_part}".strip()
                                log.info(
                                    "Быстро распознана команда по partial: %s (kind=%s, stable=%.2fs)",
                                    result,
                                    kind,
                                    required_stable_sec,
                                )
                                await _dispatch_recognized(result, partial=True)
                                kaldi.Reset()
                                pcm_buffer.clear()
                                _reset_stt_state()
                                ignore_until = time.monotonic() + max(
                                    ignore_after_fast_sec,
                                    partial_response_guard_sec + 0.55,
                                )
                                continue

        pcm_buffer.append(pcm)
        log.debug("Размер кольцевого буфера: %d", len(pcm_buffer))


if __name__ == "__main__":
    asyncio.run(main())
    if _shutdown_flag.is_set():
        log.info("Ассистент завершил работу по запросу пользователя")
