import time
from threading import Timer

from emotion.state import EmotionState, Emotion
from typing import Optional
from core.logging_json import configure_logging
from core import events as core_events

log = configure_logging("emotion.manager")

# Сколько секунд отображается эмоция удивления при пробуждении голосом
WAKE_SURPRISED_SEC = 2.0


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

    def start(self) -> None:
        """Опубликовать начальное состояние.

        Без вызова этой функции внешний мир не узнает, какая эмоция
        активна при запуске ассистента.
        """
        self._publish_emotion(self._state.current)

    def stop(self) -> None:  # pragma: no cover - для совместимости API
        """Совместимость с прежним API, активных потоков нет."""
        pass

    def _on_external_change(self, event: core_events.Event) -> None:
        """Обновить локальное состояние при смене эмоции и вывести её в лог.

        Иногда эмоцию может изменить другой компонент (например, детектор
        присутствия).  Мы фиксируем такое изменение и сохраняем его в
        ``EmotionState``.
        """
        new = event.attrs["emotion"]
        prev = self._state.current
        log.info("emotion %s → %s", prev.value, new.value)
        self._state.set(new)

    def _on_presence_update(self, event: core_events.Event) -> None:
        """Запоминаем, есть ли сейчас человек в кадре."""
        self._present = bool(event.attrs.get("present"))

    def _on_speech_recognized(self, event: core_events.Event) -> None:
        """Пробуждение голосом при отсутствии лица в кадре."""
        if self._present:
            return
        self._surprised_until = time.monotonic() + WAKE_SURPRISED_SEC
        # После завершения команды нужно вернуться в нейтральное состояние
        self._prev_emotion = Emotion.NEUTRAL
        self._publish_emotion(Emotion.SURPRISED)
        Timer(WAKE_SURPRISED_SEC, self._after_surprise).start()

    def _after_surprise(self) -> None:
        """Перейти из "удивления" в нужную эмоцию после паузы."""
        if self._query_active:
            # Команда всё ещё выполняется → показываем THINKING
            emo = self._state.get_thinking()
            self._publish_emotion(emo)
        else:
            # Команда уже завершилась → возвращаем NEUTRAL
            self._publish_emotion(Emotion.NEUTRAL)

    def _on_query_started(self, event: core_events.Event) -> None:
        """При начале обработки пользовательского запроса — эмоция THINKING.

        Запоминаем предыдущую эмоцию, чтобы по завершении вернуться к ней,
        и публикуем эмоцию ``THINKING``.
        """
        self._query_active = True
        now = time.monotonic()
        if now < self._surprised_until:
            # Остаёмся в "удивлении", но помним, что после завершения
            # нужно вернуться в нейтральное состояние
            self._prev_emotion = Emotion.NEUTRAL
            return
        self._prev_emotion = self._state.current
        emo = self._state.get_thinking()
        log.debug("user_query_started → %s", emo.value)
        self._publish_emotion(emo)

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
        emo = self._state.set(self._prev_emotion)
        log.debug("user_query_ended → %s", emo.value)
        self._publish_emotion(emo)

    def _publish_emotion(self, emotion: Emotion) -> None:
        """Публикует событие смены эмоции.

        Весь обмен эмоциями между компонентами происходит через
        ``core_events``. Здесь мы формируем и отправляем соответствующий
        объект ``Event``.
        """
        if self._speech_active:
            self._pending_emotion = emotion
            return
        log.debug("Publishing emotion_changed(%s)", emotion.value)
        core_events.publish(
            core_events.Event(kind="emotion_changed", attrs={"emotion": emotion})
        )

    def _on_speech_started(self, event: core_events.Event) -> None:
        """Отметить, что началось воспроизведение речи."""
        self._speech_active = True

    def _on_speech_finished(self, event: core_events.Event) -> None:
        """Опубликовать отложенную эмоцию после завершения речи."""
        self._speech_active = False
        if self._pending_emotion is not None:
            emotion = self._pending_emotion
            self._pending_emotion = None
            self._publish_emotion(emotion)

