from __future__ import annotations

"""Простая детекция присутствия и запуск трекера лица.

Модуль захватывает кадры с веб-камеры, ищет лицо с помощью MediaPipe и
публикует событие ``presence.update`` при появлении или исчезновении
пользователя.  При наличии лица дополнительно запускается
:class:`~sensors.vision.face_tracker.FaceTracker`, который сглаживает
координаты и публикует события ``vision.face_tracker``.
"""

import time
from dataclasses import dataclass
from typing import Optional

# OpenCV и MediaPipe могут отсутствовать в среде тестов, поэтому импортируем
# их с защитой. В реальном окружении эти пакеты должны быть установлены
# (см. ``requirements.txt``).
try:  # pragma: no cover - тесты не используют реальную камеру
    import cv2
    import mediapipe as mp
except Exception:  # pylint: disable=broad-except
    cv2 = None  # type: ignore
    mp = None  # type: ignore

from core.events import Event, publish
from core.logging_json import configure_logging
from sensors.vision.face_tracker import FaceTracker
from sensors import set_active

# Логгер модуля. При включенном DEBUG выводится дополнительная диагностика.
log = configure_logging(__name__)


@dataclass
class PresenceState:
    """Текущее состояние присутствия пользователя."""

    present: bool = False  # есть ли лицо в кадре
    confidence: float = 0.0  # сглаженная уверенность (0..1)
    last_seen: float = 0.0  # время последнего обнаружения


class PresenceDetector:
    """Детектор присутствия с публикацией событий.

    Основные шаги алгоритма:

    1. Периодически захватываем кадр с камеры.
    2. Ищем лицо при помощи MediaPipe.
    3. Обновляем состояние с экспоненциальным сглаживанием (EMA).
    4. При смене состояния публикуем событие ``presence.update``.
    5. Координаты лица передаём в ``FaceTracker`` для сглаживания и
       публикации ``vision.face_tracker``.
    """

    def __init__(
        self,
        camera_index: int,
        frame_interval_ms: int,
        absent_after_sec: float,
        alpha: float = 0.2,
        present_th: float = 0.6,
        absent_th: float = 0.4,
        show_window: bool = True,
        frame_rotation: int = 270,
    ) -> None:
        # Параметры камеры
        self.camera_index = camera_index
        self.frame_interval_ms = frame_interval_ms
        self.absent_after_sec = absent_after_sec
        # Параметры EMA и гистерезиса
        self.alpha = alpha
        self.present_th = present_th
        self.absent_th = absent_th
        # Визуализация
        self.show_window = show_window
        if frame_rotation not in (0, 90, 180, 270):
            raise ValueError("frame_rotation must be 0/90/180/270")
        self.frame_rotation = frame_rotation
        if cv2 is not None:
            self._rotate_code = {
                0: None,
                90: cv2.ROTATE_90_CLOCKWISE,
                180: cv2.ROTATE_180,
                270: cv2.ROTATE_90_COUNTERCLOCKWISE,
            }[frame_rotation]
        else:  # pragma: no cover - отсутствует в тестах
            self._rotate_code = None

        log.debug(
            "PresenceDetector init: camera=%s interval_ms=%s rotation=%s window=%s",
            camera_index,
            frame_interval_ms,
            frame_rotation,
            show_window,
        )

        # Текущее состояние и трекер лица
        self.state = PresenceState()
        self.tracker = FaceTracker(alpha=0.5)
        # Объект камеры OpenCV (инициализируется лениво)
        self._cap: Optional[cv2.VideoCapture] = None  # type: ignore

    # ------------------------------------------------------------------
    def _ensure_camera(self) -> bool:
        """Ленивая инициализация камеры.

        Возвращает ``True``, если устройство успешно открыто.
        """

        if self._cap is None and cv2 is not None:
            self._cap = cv2.VideoCapture(self.camera_index)
        if self._cap is None or not self._cap.isOpened():  # pragma: no cover -
            # Защита от отсутствия камеры в тестовой среде
            log.error("Cannot open camera %s", self.camera_index)
            return False
        return True

    # ------------------------------------------------------------------
    def _update_state(self, detected: bool) -> None:
        """Обновление состояния присутствия.

        Применяет EMA и публикует событие при смене флага ``present``.
        """

        value = 1.0 if detected else 0.0
        self.state.confidence = self.alpha * value + (1 - self.alpha) * self.state.confidence
        now = time.monotonic()
        if detected:
            self.state.last_seen = now

        previous = self.state.present
        if previous:
            # Проверяем условие исчезновения
            if (
                self.state.confidence < self.absent_th
                and now - self.state.last_seen > self.absent_after_sec
            ):
                self.state.present = False
        else:
            # Проверяем условие появления
            if self.state.confidence > self.present_th:
                self.state.present = True
                self.state.last_seen = now

        if self.state.present != previous:
            log.info("Presence %s", "detected" if self.state.present else "lost")
            publish(
                Event(
                    kind="presence.update",
                    attrs={"present": self.state.present, "confidence": self.state.confidence},
                )
            )
        else:
            log.debug("Presence confidence=%.2f", self.state.confidence)

    # ------------------------------------------------------------------
    def process_detection(
        self,
        detected: bool,
        x: float | None = None,
        y: float | None = None,
        frame_width: float = 1.0,
        frame_height: float = 1.0,
    ) -> None:
        """Обрабатывает результат детекции лица.

        Вызывается из ``run`` и упрощает тестирование — можно передавать
        фиктивные координаты без обращения к камере. При необходимости
        можно указать размеры кадра для корректных track-команд.
        """

        self._update_state(detected)
        if detected and self.state.present:
            self.tracker.update((x or 0.0, y or 0.0), frame_width, frame_height)
        else:
            self.tracker.update(None)

    # ------------------------------------------------------------------
    def run(self) -> None:  # pragma: no cover - в тестах камера отсутствует
        """Главный цикл захвата и обработки кадров."""

        if cv2 is None or mp is None:
            log.error("OpenCV/MediaPipe не установлены — PresenceDetector не работает")
            return
        if not self._ensure_camera():
            return

        # Активируем индикатор камеры, требуя предварительного согласия
        try:
            set_active("camera", True)
        except PermissionError:
            log.error("Запуск камеры отклонён из-за отсутствия согласия")
            return

        mp_face = mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.55)
        dt = self.frame_interval_ms / 1000.0

        try:
            while True:
                ret, frame_bgr = self._cap.read()
                if not ret:
                    time.sleep(dt)
                    continue
                if self._rotate_code is not None:
                    frame_bgr = cv2.rotate(frame_bgr, self._rotate_code)
                frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

            detections = mp_face.process(frame_rgb).detections
            if detections:
                face = max(detections, key=lambda d: d.score[0])
                rel_bb = face.location_data.relative_bounding_box
                cx = rel_bb.xmin + rel_bb.width / 2
                cy = rel_bb.ymin + rel_bb.height / 2
                h, w = frame_bgr.shape[:2]
                self.process_detection(True, cx, cy, w, h)
            else:
                self.process_detection(False)

            if self.show_window:
                if detections:
                    h, w = frame_bgr.shape[:2]
                    x0 = int(rel_bb.xmin * w)
                    y0 = int(rel_bb.ymin * h)
                    x1 = int((rel_bb.xmin + rel_bb.width) * w)
                    y1 = int((rel_bb.ymin + rel_bb.height) * h)
                    cv2.rectangle(frame_bgr, (x0, y0), (x1, y1), (0, 255, 0), 2)
                cv2.imshow("presence", frame_bgr)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            time.sleep(dt)
        finally:
            # При завершении работы снимаем индикатор активности
            set_active("camera", False)
            self._cap.release()
            cv2.destroyAllWindows()
