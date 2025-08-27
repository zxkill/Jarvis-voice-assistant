"""Простая детекция присутствия пользователя по факту обнаружения лица.

Модуль не занимается управлением сервоприводами — эту задачу выполняет
`face_tracker`.  Здесь лишь оценивается вероятность присутствия и при
смене состояния публикуется событие ``presence.update``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from core.events import Event, publish
from core.logging_json import configure_logging
from .face_tracker import FaceTracker

# Попытка импортировать OpenCV. В тестовой среде или на устройствах без
# установленной библиотеки импорт может завершиться ошибкой — в таком случае
# модуль всё равно должен загрузиться, но функциональность детекции по камере
# будет недоступна.
try:  # pragma: no cover - в тестах OpenCV может отсутствовать
    import cv2  # type: ignore
except Exception as exc:  # pylint: disable=broad-except
    cv2 = None  # type: ignore
    logging.getLogger(__name__).warning("OpenCV недоступен: %s", exc)

log = configure_logging(__name__)


@dataclass
class PresenceState:
    """Состояние присутствия в кадре."""

    present: bool = False
    confidence: float = 0.0
    last_seen: float = 0.0
    last_kind: Optional[str] = None


class PresenceDetector:
    """EMA-для оценки присутствия лица и запуск ``FaceTracker``.

    Класс объединяет две задачи:

    * статистическая оценка присутствия пользователя в кадре на основе
      экспоненциального сглаживания (EMA);
    * при необходимости — автономный цикл обработки кадров с камеры для
      детекции лица при помощи OpenCV.
    """

    def __init__(
        self,
        alpha: float = 0.6,
        threshold: float = 0.5,
        tracker: Optional[FaceTracker] = None,
        camera_index: Optional[int] = None,
        frame_interval_ms: int = 500,
        absent_after_sec: int = 5,
        show_window: bool = True,
        window_size: Tuple[int, int] = (800, 600),
        frame_rotation: int = 270,
        scale_factor: float = 1.1,
        min_neighbors: int = 8,
        min_size: Tuple[int, int] = (80, 80),
        require_eyes: bool = True,
    ) -> None:
        """Создать объект детектора присутствия.

        :param alpha: коэффициент сглаживания для EMA
        :param threshold: порог уверенности, при превышении которого лицо считается найденным
        :param tracker: внешний трекер поворота камеры
        :param camera_index: индекс веб‑камеры; ``None`` отключает работу с камерой
        :param frame_interval_ms: интервал между кадрами при автономной работе
        :param absent_after_sec: сколько секунд отсутствия лица считать уходом пользователя
        :param window_size: размер окна отладки ``(ширина, высота)``
        :param frame_rotation: поворот кадра по часовой стрелке (0/90/180/270 градусов)
            по умолчанию 270°, что удобно при боковом креплении камеры
        :param scale_factor: коэффициент масштабирования каскада; больше — меньше ложных срабатываний
        :param min_neighbors: сколько соседних прямоугольников требуется для принятия объекта за лицо
        :param min_size: минимальный размер лица ``(ширина, высота)`` в пикселях
        :param require_eyes: дополнительно проверять наличие глаз, чтобы отсеять посторонние объекты
        """

        # Параметры сглаживания
        self.alpha = alpha
        self.threshold = threshold
        # Текущее состояние
        self.state = PresenceState()
        # Трекер лица, отвечающий за поворот камеры
        self.tracker = tracker or FaceTracker()

        # Параметры работы с камерой
        self.camera_index = camera_index
        self.frame_interval_ms = frame_interval_ms
        self.absent_after_sec = absent_after_sec
        # Нужно ли выводить отладочное окно с изображением камеры
        # По умолчанию включено, так как это помогает понять, работает
        # ли детекция.  В тестовой среде и при работе без дисплея
        # параметр можно отключить.
        self.show_window = show_window
        self.window_size = window_size  # хотим видеть кадр крупнее для удобной отладки

        if frame_rotation not in (0, 90, 180, 270):
            raise ValueError("frame_rotation must be 0, 90, 180 or 270 degrees")
        # фиксированный поворот помогает, если камера установлена боком;
        # по умолчанию выбираем 270°, так как камера чаще всего повернута влево
        self.frame_rotation = frame_rotation
        # Параметры каскадного детектора лица
        if scale_factor <= 1.0:
            raise ValueError("scale_factor must be > 1.0")
        if min_neighbors < 0:
            raise ValueError("min_neighbors must be >= 0")
        if min_size[0] <= 0 or min_size[1] <= 0:
            raise ValueError("min_size must contain positive values")
        self.scale_factor = scale_factor
        self.min_neighbors = min_neighbors
        self.min_size = min_size
        self.require_eyes = require_eyes
        # Событие для остановки фонового потока
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def update(self, detected: bool, dx_px: float = 0.0, dy_px: float = 0.0, dt_ms: int = 0) -> None:
        """Обновить внутреннее состояние на основе результата детекции.

        :param detected: найдено ли лицо на кадре
        :param dx_px: смещение по горизонтали от центра кадра
        :param dy_px: смещение по вертикали
        :param dt_ms: время с прошлого кадра; пробрасывается в трекер
        """

        # Переводим факт обнаружения в числовое значение и сглаживаем
        value = 1.0 if detected else 0.0
        self.state.confidence = self.state.confidence * (1 - self.alpha) + value * self.alpha

        if detected:
            # Запоминаем время и тип последнего объекта
            self.state.last_seen = time.monotonic()
            self.state.last_kind = "face"

        previous = self.state.present
        self.state.present = self.state.confidence >= self.threshold

        if self.state.present != previous:
            # Публикуем событие при смене состояния
            publish(
                Event(
                    kind="presence.update",
                    attrs={"present": self.state.present, "confidence": self.state.confidence},
                )
            )
            log.info(
                "Presence %s (confidence=%.2f)",
                "detected" if self.state.present else "lost",
                self.state.confidence,
            )
        else:
            log.debug("confidence=%.2f detected=%s", self.state.confidence, detected)

        # Передаём данные в трекер — он сам решит, нужно ли двигать камеру
        if detected:
            self.tracker.update(True, dx_px, dy_px, dt_ms)
        else:
            self.tracker.update(False, 0.0, 0.0, dt_ms)

    # ------------------------------------------------------------------
    def run(self) -> None:
        """Запустить автономный цикл обработки кадров с камеры.

        Если библиотека OpenCV недоступна либо не задан индекс камеры, метод
        завершается сразу, оставляя систему работоспособной без детекции
        присутствия.
        """

        if self.camera_index is None:
            log.warning("Индекс камеры не задан — детектор присутствия не запущен")
            return
        if cv2 is None:
            log.error("OpenCV не установлен — детектор присутствия недоступен")
            return

        log.info(
            "Запуск детектора присутствия (camera_index=%s, interval=%d ms, window=%s, rotation=%d, scale=%.2f, neighbors=%d, min_size=%s, require_eyes=%s)",
            self.camera_index,
            self.frame_interval_ms,
            self.show_window,
            self.frame_rotation,
            self.scale_factor,
            self.min_neighbors,
            self.min_size,
            self.require_eyes,
        )

        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():  # pragma: no cover - зависит от окружения
            log.error("Не удалось открыть камеру %s", self.camera_index)
            return

        classifier = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        eye_classifier = None
        if self.require_eyes:
            eye_classifier = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_eye.xml"
            )
            if eye_classifier.empty():  # pragma: no cover - зависит от установки OpenCV
                log.warning("Не удалось загрузить каскад глаз, проверка отключена")
                eye_classifier = None
        # Если требуется, создаём отдельное окно для отладки нужного размера
        if self.show_window:
            cv2.namedWindow("jarvis_presence", cv2.WINDOW_NORMAL)
            cv2.resizeWindow("jarvis_presence", *self.window_size)

        last_ts = time.monotonic()
        while not self._stop.is_set():
            ret, frame = cap.read()
            if not ret:
                log.debug("Кадр не получен")
                time.sleep(self.frame_interval_ms / 1000)
                continue
            # Поворачиваем кадр при необходимости: это упрощает работу с повернутой камерой
            if self.frame_rotation:
                rotate_flag = {
                    90: cv2.ROTATE_90_CLOCKWISE,
                    180: cv2.ROTATE_180,
                    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
                }[self.frame_rotation]
                frame = cv2.rotate(frame, rotate_flag)

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = classifier.detectMultiScale(
                gray,
                self.scale_factor,
                self.min_neighbors,
                minSize=self.min_size,
            )
            validated = []
            for (x, y, w, h) in faces:
                if eye_classifier is not None:
                    roi = gray[y : y + h, x : x + w]
                    eyes = eye_classifier.detectMultiScale(roi)
                    if len(eyes) < 2:
                        log.debug("face rejected: eyes not found at x=%d y=%d w=%d h=%d", x, y, w, h)
                        continue
                validated.append((x, y, w, h))
            faces = validated
            detected = len(faces) > 0

            dx_px = dy_px = 0.0
            if detected:
                x, y, w, h = faces[0]
                # Смещение центра лица относительно центра кадра в пикселях.
                # Положительное ``dx_px`` означает, что лицо правее центра,
                # положительное ``dy_px`` — выше центра.
                dx_px = (x + w / 2) - frame.shape[1] / 2
                dy_px = (y + h / 2) - frame.shape[0] / 2
                log.debug(
                    "face accepted at x=%d y=%d w=%d h=%d dx=%.1f dy=%.1f",
                    x,
                    y,
                    w,
                    h,
                    dx_px,
                    dy_px,
                )

            now = time.monotonic()
            dt_ms = int((now - last_ts) * 1000)
            last_ts = now

            self.update(detected, dx_px, dy_px, dt_ms)

            if self.show_window:
                # Рисуем прямоугольник вокруг лица и выводим на экран
                if detected:
                    x, y, w, h = faces[0]
                    cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
                cv2.imshow("jarvis_presence", frame)
                # waitKey необходим для обновления окна; 1 мс достаточно
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    log.info("Остановлено пользователем через 'q'")
                    break

            # Если лицо исчезло и длительное время не возвращается, явно
            # сигнализируем об отсутствии пользователя.
            if (
                not detected
                and self.state.present
                and now - self.state.last_seen > self.absent_after_sec
            ):
                log.debug("пользователь отсутствует более %d с", self.absent_after_sec)
                self.update(False, 0.0, 0.0, dt_ms)

            time.sleep(self.frame_interval_ms / 1000)

        cap.release()
        if self.show_window:
            cv2.destroyWindow("jarvis_presence")
        log.info("Детектор присутствия остановлен")

    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Остановить автономный цикл ``run``."""

        self._stop.set()


__all__ = ["PresenceDetector", "PresenceState"]
