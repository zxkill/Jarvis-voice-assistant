import time
from threading import Timer

from typing import Optional

# Локальные модули системы эмоций
from emotion.state import EmotionState, Emotion
from emotion.mood import Mood
from emotion import policy

from core.logging_json import configure_logging
from core import events as core_events

log = configure_logging("emotion.manager")

# Сколько секунд отображается эмоция удивления при пробуждении голосом
WAKE_SURPRISED_SEC = 2.0
# Через сколько секунд простоя переключать эмоции
IDLE_TIMEOUT_SEC = 60.0


class EmotionManager:
    """Управляет сменой эмоций через глобальный event bus.

    Класс держит текущее состояние эмоции и реагирует на события,
    поступающие из разных подсистем ассистента.  Все переходы
    фиксируются в логах, что упрощает отладку и анализ поведения
    ассистента.
    """

    def __init__(self) -> None:
        self._state = EmotionState()
        self._prev_emotion = self._state.current

        # Объект настроения (valence/arousal), восстанавливаемый из БД
        self._mood = Mood.load()

        # Признак текущего присутствия пользователя перед камерой
        self._present = True
        # Активен ли сейчас обработчик пользовательского запроса
        self._query_active = False
        # Время, до которого следует удерживать эмоцию "удивление"
        self._surprised_until = 0.0
        # Флаг активного воспроизведения речи
        self._speech_active = False
        # Эмоция, ожидающая публикации после окончания речи
        self._pending_emotion: Optional[Emotion] = None
        # Отложенный результат политики (для передачи после завершения речи)
        self._pending_policy: Optional[policy.PolicyResult] = None
        # Таймер смены эмоций в простое
        self._idle_timer: Optional[Timer] = None

        # Подписываемся на события глобального event bus.  Каждый обработчик
        # отвечает за конкретную ситуацию: начало/конец пользовательского
        # запроса или внешнее изменение эмоции другими компонентами.
        core_events.subscribe("user_query_started", self._on_query_started)
        core_events.subscribe("user_query_ended", self._on_query_ended)
        core_events.subscribe("emotion_changed", self._on_external_change)
        core_events.subscribe("speech.recognized", self._on_speech_recognized)
        core_events.subscribe("presence.update", self._on_presence_update)
        core_events.subscribe("speech.synthesis_started", self._on_speech_started)
        core_events.subscribe("speech.synthesis_finished", self._on_speech_finished)
        # Уровень настроения зависит от успешности диалога
        core_events.subscribe("dialog.success", self._on_dialog_success)
        core_events.subscribe("dialog.failure", self._on_dialog_failure)
        # Внешние факторы, влияющие на настроение (например, погода)
        core_events.subscribe("weather.update", self._on_weather_update)

    def start(self) -> None:
        """Опубликовать начальное состояние.

        Без вызова этой функции внешний мир не узнает, какая эмоция
        активна при запуске ассистента.
        """
        self._publish_emotion(self._state.current)
        self._reset_idle_timer()

    def stop(self) -> None:  # pragma: no cover - для совместимости API
        """Остановить таймер простоя при завершении работы."""
        self._cancel_idle_timer()

    def _on_external_change(self, event: core_events.Event) -> None:
        """Обновить локальное состояние при смене эмоции и вывести её в лог.

        Иногда эмоцию может изменить другой компонент (например, детектор
        присутствия).  Мы фиксируем такое изменение и сохраняем его в
        ``EmotionState``.
        """
        new = event.attrs["emotion"]
        prev = self._state.current
        log.info("emotion %s → %s (external)", prev.value, new.value)
        self._state.set(new)

    def _on_dialog_success(self, event: core_events.Event) -> None:
        """Реакция на успешный ответ пользователю."""
        # Поднимаем числовой уровень настроения для обратной совместимости
        self._state.raise_mood(reason="dialog success")
        # Обновляем двухмерное настроение и выбираем новую эмоцию
        self._update_mood(1.0, 1.0, reason="dialog success")

    def _on_dialog_failure(self, event: core_events.Event) -> None:
        """Реакция на ошибку или непонимание команды."""
        self._state.drop_mood(reason="dialog failure")
        # Снижаем валентность сильнее, чем возбуждение, чтобы перейти в негативную зону
        self._update_mood(-2.0, 0.5, reason="dialog failure")

    def _on_presence_update(self, event: core_events.Event) -> None:
        """Запоминаем, есть ли сейчас человек в кадре."""
        self._present = bool(event.attrs.get("present"))
        if self._present and not self._query_active:
            # Наличие пользователя немного повышает настроение
            self._update_mood(0.5, 0.2, reason="presence")
            self._reset_idle_timer()
        else:
            # Отсутствие пользователя делает Jarvis чуть грустнее
            if not self._present:
                self._update_mood(-1.0, -0.5, reason="no presence")
            self._cancel_idle_timer()

    def _on_speech_recognized(self, event: core_events.Event) -> None:
        """Пробуждение голосом при отсутствии лица в кадре."""
        if self._present:
            return
        self._surprised_until = time.monotonic() + WAKE_SURPRISED_SEC
        # После завершения команды нужно вернуться в нейтральное состояние
        self._prev_emotion = Emotion.NEUTRAL
        self._switch_emotion(Emotion.SURPRISED, "voice wakeup")
        Timer(WAKE_SURPRISED_SEC, self._after_surprise).start()

    def _after_surprise(self) -> None:
        """Перейти из "удивления" в нужную эмоцию после паузы."""
        if self._query_active:
            # Команда всё ещё выполняется → показываем THINKING
            emo = Emotion.THINKING
            self._switch_emotion(emo, "surprised timeout")
        else:
            # Команда уже завершилась → возвращаем NEUTRAL
            self._switch_emotion(Emotion.NEUTRAL, "surprised timeout")

    def _on_query_started(self, event: core_events.Event) -> None:
        """При начале обработки пользовательского запроса — эмоция THINKING.

        Запоминаем предыдущую эмоцию, чтобы по завершении вернуться к ней,
        и публикуем эмоцию ``THINKING``.
        """
        self._query_active = True
        self._cancel_idle_timer()
        now = time.monotonic()
        if now < self._surprised_until:
            # Остаёмся в "удивлении", но помним, что после завершения
            # нужно вернуться в нейтральное состояние
            self._prev_emotion = Emotion.NEUTRAL
            return
        self._prev_emotion = self._state.current
        emo = Emotion.THINKING
        self._switch_emotion(emo, "user query started")

    def _on_query_ended(self, event: core_events.Event) -> None:
        """При завершении обработки запроса — вернуться к предыдущей эмоции.

        Небольшая пауза помогает избежать мгновенного переключения, если
        следом идёт новый запрос.
        """
        self._query_active = False
        now = time.monotonic()
        if now < self._surprised_until:
            # Таймер удивления сам переключит эмоцию на нейтральную
            return
        log.debug("user_query_ended → wait 1s")
        time.sleep(1)
        self._switch_emotion(self._prev_emotion, "user query ended")
        self._reset_idle_timer()

    def _publish_emotion(self, emotion: Emotion, policy_res: Optional[policy.PolicyResult] = None) -> None:
        """Публикует событие смены эмоции.

        При активном воспроизведении речи эмоция откладывается до завершения,
        чтобы не менять параметры синтеза на лету.  Дополнительно к имени
        эмоции передаются выбранные политикой пресеты TTS и палитра звуков,
        если они доступны.
        """
        if self._speech_active:
            self._pending_emotion = emotion
            self._pending_policy = policy_res
            return

        attrs = {"emotion": emotion}
        if policy_res is not None:
            attrs.update({
                "tts_preset": policy_res.tts_preset,
                "sfx_palette": policy_res.sfx_palette,
            })

        log.debug("Publishing emotion_changed(%s)", emotion.value)
        core_events.publish(core_events.Event(kind="emotion_changed", attrs=attrs))

    def _on_speech_started(self, event: core_events.Event) -> None:
        """Отметить, что началось воспроизведение речи.

        Дополняем событие текущей эмоцией, чтобы TTS и другие подписчики
        могли учитывать состояние персонажа при воспроизведении."""
        self._speech_active = True
        event.attrs["emotion"] = self._state.current
        log.debug("speech.synthesis_started emotion=%s", self._state.current.value)

    def _on_speech_finished(self, event: core_events.Event) -> None:
        """Опубликовать отложенную эмоцию после завершения речи."""
        self._speech_active = False
        if self._pending_emotion is not None:
            emotion = self._pending_emotion
            policy_res = self._pending_policy
            self._pending_emotion = None
            self._pending_policy = None
            self._publish_emotion(emotion, policy_res)

    def _on_weather_update(self, event: core_events.Event) -> None:
        """Изменить настроение в зависимости от погоды.

        Простейшая эвристика: солнечная и тёплая погода улучшает настроение,
        дождь или снег — ухудшают.  Конкретные значения можно подобрать в
        конфигурации ``config/affect.yaml``.
        """
        condition = str(event.attrs.get("condition", "")).lower()
        temp = float(event.attrs.get("temperature", 0))
        if "rain" in condition or "snow" in condition:
            self._update_mood(-1.0, -0.5, reason=f"weather {condition}")
        elif "sun" in condition or temp >= 20:
            self._update_mood(1.0, 0.5, reason=f"weather {condition}")

    # --------------------------------------- вспомогательные методы ---

    def _policy_icon_to_emotion(self, icon: str) -> Emotion:
        """Преобразовать строковое название иконки в перечисление ``Emotion``."""
        try:
            return Emotion[icon]
        except KeyError:
            return Emotion.NEUTRAL

    def _update_mood(self, valence_delta: float, arousal_delta: float, reason: str) -> None:
        """Обновить настроение и опубликовать новую эмоцию.

        Вызывает политику выбора эмоций и сохраняет состояние в БД.
        ``reason`` используется для подробного логирования вклада события.
        """
        self._mood.update(valence_delta, arousal_delta)
        self._mood.save()
        log.info(
            "mood update (%s) valence=%+.2f arousal=%+.2f → (%.2f, %.2f)",
            reason,
            valence_delta,
            arousal_delta,
            self._mood.valence,
            self._mood.arousal,
        )
        policy_res = policy.select(self._mood.valence, self._mood.arousal)
        emotion = self._policy_icon_to_emotion(policy_res.icon)
        self._switch_emotion(emotion, reason, policy_res)

    def _switch_emotion(self, emotion: Emotion, reason: str, policy_res: Optional[policy.PolicyResult] = None) -> None:
        """Переключить эмоцию с логированием причины."""
        prev = self._state.current
        self._state.set(emotion)
        log.info("emotion %s → %s (%s)", prev.value, emotion.value, reason)
        self._publish_emotion(emotion, policy_res)

    def _on_idle_timeout(self) -> None:
        """Обработчик таймера простоя: выбрать следующую эмоцию."""
        emotion = self._state.get_next_idle()
        self._switch_emotion(emotion, "idle timer")
        self._reset_idle_timer()

    def _reset_idle_timer(self) -> None:
        """Перезапустить таймер простоя, если условия соблюдены."""
        self._cancel_idle_timer()
        if not self._present or self._query_active:
            return
        self._idle_timer = Timer(IDLE_TIMEOUT_SEC, self._on_idle_timeout)
        self._idle_timer.start()

    def _cancel_idle_timer(self) -> None:
        """Отменить таймер простоя, если он запущен."""
        if self._idle_timer is not None:
            cancel = getattr(self._idle_timer, "cancel", None)
            if callable(cancel):
                cancel()
            self._idle_timer = None

