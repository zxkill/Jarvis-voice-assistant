from display import get_driver, DisplayItem
from emotion.state import Emotion
from core.logging_json import configure_logging
from core import events as core_events

log = configure_logging("emotion.driver")

class EmotionDisplayDriver:
    """
    Драйвер, подписывается на смену эмоции и
    отображает её на дисплее через базовый DisplayDriver.
    """

    def __init__(self):
        # получаем базовый драйвер (Websocket, Windows, etc.)
        self._driver = get_driver()
        # подписываемся на событие
        core_events.subscribe('emotion_changed', self._on_emotion_changed)

    def _on_emotion_changed(self, event: core_events.Event):
        """
        Callback: рисуем иконку текущей эмоции.
        """
        emotion: Emotion = event.attrs["emotion"]
        log.debug("Received emotion_changed → %s", emotion.value)

        # Сначала очищаем возможный текст, оставшийся от других модулей,
        # затем отображаем выбранную эмоцию. Благодаря расширенной
        # прошивке M5Stack, эмоция ``TIRED`` теперь поддерживается
        # аппаратно и рисуется так же, как остальные.
        self._driver.draw(DisplayItem(kind="text", payload=None))
        self._driver.draw(
            DisplayItem(
                kind="emotion",
                payload=emotion.value,  # строковый ключ, например "Happy"
            )
        )
