from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

# OpenCV и MediaPipe могут отсутствовать в среде тестов, поэтому
# импортируем их с защитой. В реальном запуске ассистента эти пакеты
# должны быть установлены (см. requirements.txt).
try:  # pragma: no cover - тесты не используют реальную камеру
    import cv2
    import mediapipe as mp
except Exception:  # pylint: disable=broad-except
    cv2 = None  # type: ignore
    mp = None  # type: ignore

from core.events import Event, publish
from core.logging_json import configure_logging
from core.metrics import inc_metric, set_metric

# Попытка импортировать драйвер дисплея для отправки команд поворота.
# Сам драйвер инициализируется позже (в start.py через ``init_driver``),
# поэтому здесь только импортируем ``get_driver`` и ``DisplayItem`` без
# вызова ``get_driver()`` во время импорта модуля. Иначе драйвер
# зафиксируется на реализации по умолчанию (например, ``windows``) и
# дальнейший вызов ``init_driver('serial')`` не подействует.  В тестах
# драйвера может не быть вовсе.
try:  # pragma: no cover - отсутствует в тестах
    from display import get_driver, DisplayItem
except Exception as exc:  # pylint: disable=broad-except
    get_driver = None  # type: ignore
    DisplayItem = None  # type: ignore
    _tmp_log = configure_logging(__name__)
    _tmp_log.warning("Display driver isn't available: %s", exc)

# Драйвер будем получать лениво в ``_send_track``/``_clear_track``.
_driver = None

# Логгер модуля. Включайте DEBUG для более подробной диагностики.
log = configure_logging(__name__)

# Время последней отправки track-команды
_last_sent_ms: int | None = None
# Был ли ранее активен трекинг — чтобы остановить сервоприводы при потере цели
_tracking_active = False

# Настройки визуализации/обработки (аналогично skill/face_tracker)
DEBUG_GUI = True  # показывать окно OpenCV
ROTATE_90 = True
ROTATE_CODE = cv2.ROTATE_90_COUNTERCLOCKWISE if cv2 else 0  # pragma: no cover
FOV_DEG_X = 38.0
FOV_DEG_Y = 62.0
SMOOTH_ALPHA = 0.0
POSE_MIN_VIS = 0.80


@dataclass
class PresenceState:
    """Структура для хранения состояния присутствия."""

    # Флаг "человек в кадре" после применения EMA и гистерезиса
    present: bool = False
    # Текущее сглаженное значение confidence (0..1)
    confidence: float = 0.0
    # Время последнего обнаружения лица (в секундах, `time.monotonic()`)
    last_seen: float = 0.0
    # Тип последнего обнаруженного объекта ("face" или "head")
    last_kind: str | None = None


class PresenceDetector:
    """Класс, выполняющий детекцию лица по кадрам веб‑камеры.

    Алгоритм:

    1. Захватываем кадры через OpenCV с указанной периодичностью.
    2. Выполняем детекцию лица каскадом Хаара.
    3. Обновляем `confidence` методом **экспоненциального сглаживания** (EMA).
    4. Применяем **гистерезис**: разные пороги для появления и исчезновения.
    5. По смене состояния публикуем событие ``presence.update`` и пишем лог.
    """

    def __init__(
        self,
        camera_index: int,
        frame_interval_ms: int,
        absent_after_sec: float,
        alpha: float = 0.2,
        present_th: float = 0.6,
        absent_th: float = 0.4,
    ) -> None:
        # Индекс камеры из секции [PRESENCE] конфигурации
        self.camera_index = camera_index
        # Интервал между кадрами в миллисекундах
        self.frame_interval_ms = frame_interval_ms
        # Сколько секунд считать пользователя отсутствующим
        self.absent_after_sec = absent_after_sec
        # Коэффициент EMA (0..1): чем больше, тем быстрее реагируем
        self.alpha = alpha
        # Порог для смены состояния на "присутствует"
        self.present_th = present_th
        # Порог для смены состояния на "отсутствует"
        self.absent_th = absent_th

        # Текущее состояние
        self.state = PresenceState()
        # Объект камеры OpenCV (инициализируется лениво)
        self._cap: Optional[cv2.VideoCapture] = None
        # Отслеживание времени непрерывного присутствия
        self._session_start: float | None = None
        set_metric("presence.active_seconds", 0)

    # ------------------------------------------------------------------
    def _ensure_camera(self) -> bool:
        """Ленивая инициализация камеры.

        Возвращает ``True``, если устройство успешно открыто.
        """

        if self._cap is None:
            self._cap = cv2.VideoCapture(self.camera_index)
        if not self._cap.isOpened():
            log.error("Cannot open camera %s", self.camera_index)
            return False
        return True


    def _update_state(self, kind: str | None) -> None:
        """Обновляет состояние на основе результата детекции.

        Здесь происходит EMA-сглаживание, гистерезис и публикация событий.
        """

        detected = kind is not None
        # Превращаем детекцию в числовое значение для EMA
        value = 1.0 if detected else 0.0
        self.state.confidence = (
            self.alpha * value + (1 - self.alpha) * self.state.confidence
        )
        now = time.monotonic()
        if detected:
            # Запоминаем момент последнего обнаружения человека
            self.state.last_seen = now
            self.state.last_kind = kind

        previous = self.state.present
        if previous:
            # Человек был в кадре, проверяем условие на исчезновение
            if (
                self.state.confidence < self.absent_th
                and now - self.state.last_seen > self.absent_after_sec
            ):
                self.state.present = False
                if self._session_start is not None:
                    inc_metric("presence.active_seconds", now - self._session_start)
                    self._session_start = None
        else:
            # Человек отсутствовал, проверяем условие на появление
            if self.state.confidence > self.present_th:
                self.state.present = True
                self.state.last_seen = now
                self._session_start = now

        # При смене состояния публикуем событие и логируем его
        if self.state.present != previous:
            log.info(
                "Presence %s (confidence=%.2f kind=%s)",
                "detected" if self.state.present else "lost",
                self.state.confidence,
                self.state.last_kind,
            )
            attrs = {
                "present": self.state.present,
                "confidence": self.state.confidence,
            }
            if self.state.last_kind:
                attrs["kind"] = self.state.last_kind
            publish(Event(kind="presence.update", attrs=attrs))
        else:
            # Если состояние не изменилось — выводим отладочный лог
            log.debug(
                "confidence=%.2f detected=%s kind=%s",
                self.state.confidence,
                detected,
                kind,
            )

    # ------------------------------------------------------------------
    def run(self) -> None:
        """Главный цикл захвата и обработки кадров.

        Использует MediaPipe для поиска лица. Если лицо не найдено, пытается
        оценить положение головы по ключевым точкам Pose. Положение центра лица
        преобразуется в углы yaw/pitch и отправляется драйверу дисплея для
        поворота камеры. Параллельно ведётся учёт присутствия и публикация
        события ``presence.update``.
        """

        if cv2 is None or mp is None:  # pragma: no cover - защита в тестах
            log.error("OpenCV/MediaPipe не установлены — PresenceDetector не работает")
            return

        if not self._ensure_camera():
            return

        # Инициализация MediaPipe
        mp_face = mp.solutions.face_detection.FaceDetection(
            model_selection=1,
            min_detection_confidence=0.55,
        )
        mp_pose = mp.solutions.pose.Pose(
            static_image_mode=False,
            model_complexity=0,
            enable_segmentation=False,
            min_detection_confidence=0.5,
        )

        dt_target = self.frame_interval_ms / 1000.0
        yaw_prev = pitch_prev = 0.0
        fov_x, fov_y = (FOV_DEG_Y, FOV_DEG_X) if ROTATE_90 else (FOV_DEG_X, FOV_DEG_Y)

        try:
            while True:
                loop_t0 = time.time()
                ret, frame_bgr = self._cap.read()
                if not ret:
                    log.warning("Failed to capture frame")
                    time.sleep(dt_target)
                    continue

                if ROTATE_90:
                    frame_bgr = cv2.rotate(frame_bgr, ROTATE_CODE)
                frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

                kind = None
                x_rel = y_rel = w_rel = h_rel = 0.0

                # 1) Face Detection
                face_det = mp_face.process(frame_rgb).detections
                if face_det:
                    face = max(face_det, key=lambda d: d.score[0])
                    rel_bb = face.location_data.relative_bounding_box
                    x_rel, y_rel, w_rel, h_rel = (
                        rel_bb.xmin,
                        rel_bb.ymin,
                        rel_bb.width,
                        rel_bb.height,
                    )
                    kind = "face"

                # 2) Fallback → Pose
                if kind is None:
                    result = mp_pose.process(frame_rgb)
                    pose = result.pose_landmarks
                    if pose:
                        vis = POSE_MIN_VIS
                        lms = pose.landmark
                        xs = [
                            lms[mp.solutions.pose.PoseLandmark.LEFT_EAR].x,
                            lms[mp.solutions.pose.PoseLandmark.RIGHT_EAR].x,
                            lms[mp.solutions.pose.PoseLandmark.NOSE].x,
                        ]
                        ys = [
                            lms[mp.solutions.pose.PoseLandmark.LEFT_EAR].y,
                            lms[mp.solutions.pose.PoseLandmark.RIGHT_EAR].y,
                            lms[mp.solutions.pose.PoseLandmark.NOSE].y,
                        ]
                        visibilities = [
                            lms[mp.solutions.pose.PoseLandmark.LEFT_EAR].visibility,
                            lms[mp.solutions.pose.PoseLandmark.RIGHT_EAR].visibility,
                            lms[mp.solutions.pose.PoseLandmark.NOSE].visibility,
                        ]
                        xs_vis = [x for x, v in zip(xs, visibilities) if v > vis]
                        ys_vis = [y for y, v in zip(ys, visibilities) if v > vis]
                        if len(xs_vis) >= 2:
                            x_rel = min(xs_vis)
                            y_rel = min(ys_vis)
                            w_rel = max(xs_vis) - x_rel
                            h_rel = max(ys_vis) - y_rel
                            kind = "head"

                detected = kind is not None

                if detected:
                    fh, fw = frame_bgr.shape[:2]
                    cx = (x_rel + w_rel / 2) * fw
                    cy = (y_rel + h_rel / 2) * fh

                    # Перевод в углы относительно центра кадра
                    yaw = (cx - fw / 2.0) / fw * fov_x
                    pitch = (cy - fh / 2.0) / fh * fov_y
                    yaw = yaw_prev + SMOOTH_ALPHA * (yaw - yaw_prev)
                    pitch = pitch_prev + SMOOTH_ALPHA * (pitch - pitch_prev)
                    yaw_prev, pitch_prev = yaw, pitch

                    now_ms = int(time.time() * 1000)
                    global _last_sent_ms  # pylint: disable=global-statement
                    dt_ms = 0 if _last_sent_ms is None else now_ms - _last_sent_ms
                    _last_sent_ms = now_ms

                    dx_px = float(cx - fw / 2.0)
                    dy_px = float(cy - fh / 2.0)
                    _update_track(True, dx_px, dy_px, dt_ms)

                    if DEBUG_GUI:
                        color = (0, 255, 0) if kind == "face" else (0, 165, 255)
                        pt1 = (int(x_rel * fw), int(y_rel * fh))
                        pt2 = (int((x_rel + w_rel) * fw), int((y_rel + h_rel) * fh))
                        cv2.rectangle(frame_bgr, pt1, pt2, color, 2)
                        cv2.putText(
                            frame_bgr,
                            f"{kind}:{yaw:+.1f},{pitch:+.1f}",
                            (pt1[0], max(15, pt1[1] - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.5,
                            color,
                            1,
                            lineType=cv2.LINE_AA,
                        )
                else:
                    log.debug("no face/pose detected")
                    _update_track(False, 0.0, 0.0, 0)

                self._update_state(kind)

                if DEBUG_GUI:
                    cv2.imshow("Jarvis-View", frame_bgr)
                    if cv2.waitKey(1) & 0xFF == 27:
                        log.info("Esc pressed → stopping detector")
                        break

                # Поддерживаем заданный FPS
                dt = time.time() - loop_t0
                if dt < dt_target:
                    time.sleep(dt_target - dt)
        finally:
            if self._cap is not None:
                self._cap.release()
            if cv2 is not None:
                cv2.destroyAllWindows()
            _update_track(False, 0.0, 0.0, 0)


def _send_track(dx_px: float, dy_px: float, dt_ms: int) -> None:
    """Отправить ошибку положения для сервоприводов на M5.

    Формат совместим с display-driver, используемым в skill/face_tracker.
    Если драйвер недоступен, функция ничего не делает.
    """

    global _driver  # pylint: disable=global-statement
    if _driver is None and get_driver:
        try:
            _driver = get_driver()
        except Exception as exc:  # pylint: disable=broad-except
            log.debug("driver not ready: %s", exc)
            return
    if not _driver:
        log.debug("driver missing: track dx=%+.1f dy=%+.1f", dx_px, dy_px)
        return
    try:
        pkt = DisplayItem(
            kind="track",
            payload={"dx_px": round(dx_px, 1), "dy_px": round(dy_px, 1), "dt_ms": int(dt_ms)},
        )
        _driver.draw(pkt)
    except Exception as exc:  # pylint: disable=broad-except
        log.exception("Ошибка отправки WS(track): %s", exc)


def _update_track(detected: bool, dx_px: float, dy_px: float, dt_ms: int) -> None:
    """Отправить координаты слежения или остановить сервоприводы."""

    global _tracking_active  # pylint: disable=global-statement
    if detected:
        _send_track(dx_px, dy_px, dt_ms)
        _tracking_active = True
    elif _tracking_active:
        _clear_track()
        _tracking_active = False


def _clear_track() -> None:
    """Сбросить последнюю команду слежения на дисплее."""

    global _last_sent_ms, _driver  # pylint: disable=global-statement
    _last_sent_ms = None
    if _driver is None and get_driver:
        try:
            _driver = get_driver()
        except Exception as exc:  # pylint: disable=broad-except
            log.debug("driver not ready: %s", exc)
            return
    if not _driver:
        return
    try:
        _driver.draw(DisplayItem(kind="track", payload=None))
        log.debug("WS → track cleared")
    except Exception as exc:  # pylint: disable=broad-except
        log.exception("Ошибка отправки WS(track clear): %s", exc)


__all__ = ["PresenceDetector"]
