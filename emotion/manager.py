import threading
import time
from emotion.state import EmotionState, Emotion
from core.logging_json import configure_logging
from core import events as core_events

log = configure_logging("emotion.manager")


class EmotionManager:
    """Управляет сменой эмоций через глобальный event bus."""

    def __init__(self, idle_interval: float = 5.0):
        self._state = EmotionState()
        self._idle_interval = idle_interval
        self._stop_event = threading.Event()
        self._idle_thread = threading.Thread(target=self._idle_loop, daemon=True)
        self._busy_event = threading.Event()

        # Подписываемся на события
        core_events.subscribe('user_query_started', self._on_query_started)
        core_events.subscribe('user_query_ended', self._on_query_ended)
        # Будем слушать сенсорные события по мере добавления
        # core_events.subscribe('sensor_event', self._on_sensor_event)

    def start(self):
        """Опубликовать начальное состояние и запустить фоновый цикл."""
        # сразу отрисовать текущее (NEUTRAL)
        self._publish_emotion(self._state.current)
        # а затем — запустить idle-цикл
        self._idle_thread.start()

    def stop(self):
        """Остановить фоновый цикл (если нужно)."""
        self._stop_event.set()
        self._idle_thread.join()

    def _idle_loop(self):
        """Фоновый цикл: каждые idle_interval сек публикует новую эмоцию из idle-режима."""
        while not self._stop_event.is_set():
            if self._busy_event.is_set():
                time.sleep(0.1)
                continue
            next_emotion = self._state.get_next_idle()
            log.debug("Idle → %s", next_emotion.value)
            self._publish_emotion(next_emotion)
            # Ожидаем или прерываем
            waited = 0.0
            step = 0.1
            while waited < self._idle_interval:
                if self._stop_event.is_set() or self._busy_event.is_set():
                    return
                time.sleep(step)
                waited += step

    def _on_query_started(self, event: core_events.Event) -> None:
        """При начале обработки пользовательского запроса — эмоция THINKING."""
        self._busy_event.set()
        emo = self._state.get_thinking()
        log.debug("user_query_started → %s", emo.value)
        self._publish_emotion(emo)

    def _on_query_ended(self, event: core_events.Event) -> None:
        """При завершении обработки запроса — вернуться в режим простоя, но с паузой."""
        # Немного подождём, чтобы у пользователя было время заметить
        log.debug("user_query_ended → wait 1s")
        time.sleep(1)
        self._busy_event.clear()
        emo = self._state.get_next_idle()
        log.debug("Post-query idle → %s", emo.value)
        self._publish_emotion(emo)

    # Пример заготовки для расширения на сенсоры
    # def _on_sensor_event(self, sensor_type: str, value=None):
    #     """Переключить эмоцию на основе данных сенсора"""
    #     # Логика обработки разных сенсоров
    #     emo = Emotion.ANGRY if sensor_type == 'heat' and value > 50 else Emotion.NEUTRAL
    #     self._publish_emotion(emo)

    def _publish_emotion(self, emotion: Emotion):
        """Публикует событие смены эмоции."""
        log.debug("Publishing emotion_changed(%s)", emotion.value)
        core_events.publish(core_events.Event(kind='emotion_changed', attrs={'emotion': emotion}))
