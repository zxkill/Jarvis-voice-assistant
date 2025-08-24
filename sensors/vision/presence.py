from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional
import math
import random

# OpenCV и MediaPipe могут отсутствовать в среде тестов, поэтому
# импортируем их с защитой. В реальном запуске ассистента эти пакеты
# должны быть установлены (см. requirements.txt).
try:  # pragma: no cover - тесты не используют реальную камеру
    import cv2
    import mediapipe as mp
except Exception:  # pylint: disable=broad-except
    cv2 = None  # type: ignore
    mp = None  # type: ignore

from core.events import Event, publish, subscribe
from core.logging_json import configure_logging
from core.metrics import inc_metric, set_metric
from emotion.state import Emotion

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

# Настройки визуализации и обработки
DEBUG_GUI = True  # показывать окно OpenCV
ROTATE_90 = True
ROTATE_CODE = cv2.ROTATE_90_COUNTERCLOCKWISE if cv2 else 0  # pragma: no cover
FOV_DEG_X = 38.0
FOV_DEG_Y = 62.0
SMOOTH_ALPHA = 0.0
POSE_MIN_VIS = 0.60

# Ограничения для fallback-детекции головы по Pose.  Если объект по
# площади слишком мал/велик или имеет нетипичное соотношение сторон,
# считаем его ложным и игнорируем. Помогает отбрасывать, например, спинку
# стула, попавшую в кадр.
HEAD_MIN_AREA = 0.005  # относительная площадь прямоугольника (0..1)
HEAD_MIN_ASPECT = 0.3  # минимальное соотношение ширины к высоте
HEAD_MAX_ASPECT = 3.0  # максимальное соотношение ширины к высоте

# Минимальная длительность непрерывного обнаружения лица.  Кратковременные
# «вспышки» детектора (например, из‑за шумов) игнорируются, если они
# длятся меньше этого порога.
FACE_STABLE_SEC = 1.0

# Таймауты и параметры поиска лица при его отсутствии.  После
# ``NO_FACE_SCAN_SEC`` секунд отсутствия лица камера начинает медленно
# осматривать комнату. Горизонтальный обзор выполняется на полный доступный
# диапазон ``SCAN_H_RANGE_PX`` в пикселях, вертикальный – на небольшой угол
# ``SCAN_V_RANGE_PX``. Поворот осуществляется со скоростью
# ``SCAN_SPEED_PX_PER_SEC`` с небольшими паузами ``SCAN_HOLD_SEC`` на
# крайних точках. Полный цикл обзора длится ``SCAN_TOTAL_SEC`` секунд, после
# чего ассистент «засыпает» на случайный промежуток между
# ``SLEEP_MIN_SEC`` и ``SLEEP_MAX_SEC``.
NO_FACE_SCAN_SEC = 8.0
SCAN_H_RANGE_PX = 320.0
SCAN_V_RANGE_PX = 60.0
SCAN_SPEED_PX_PER_SEC = 40.0
SCAN_HOLD_SEC = 1.0
SCAN_V_PERIOD_SEC = 10.0
SCAN_TOTAL_SEC = 30.0
SLEEP_MIN_SEC = 120.0
SLEEP_MAX_SEC = 1200.0

# Параметры отображения эмоций при обнаружении лица.  Если лицо не
# появлялось более ``FACE_ABSENT_HAPPY_SEC`` секунд, при следующем
# обнаружении ассистент кратко (``HAPPY_SHOW_SEC``) демонстрирует эмоцию
# «счастье», затем возвращается в нейтральное состояние.
FACE_ABSENT_HAPPY_SEC = 300.0
HAPPY_SHOW_SEC = 5.0


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
        # Флаг, что нужно немедленно проснуться по голосовой команде
        self._wake_requested = False
        subscribe("speech.recognized", self._on_voice_command)

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

    def _on_voice_command(self, event: Event) -> None:
        """Получили голосовую команду — просыпаемся и ищем лицо."""
        self._wake_requested = True

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
        last_face_ts = time.monotonic() - FACE_ABSENT_HAPPY_SEC
        # ``stable_start`` хранит момент начала устойчивой детекции лица,
        # чтобы отфильтровать кратковременные ложные срабатывания.
        stable_start: float | None = None

        # Текущее состояние автомата:
        #   idle     – молча ждём появления лица
        #   scanning – поворачиваем камеру в поисках лица
        #   sleeping – отдыхаем, камера неподвижна
        #   tracking – активно следим за обнаруженным лицом
        mode = "idle"

        # Параметры синусоидального сканирования
        scan_start = 0.0        # момент начала текущего цикла
        scan_pos = 0.0          # текущая горизонтальная позиция в пикселях
        scan_dir = 1            # направление движения: 1 вправо, -1 влево
        scan_hold_until = 0.0   # время, до которого нужно "задержаться" на краю

        # Таймеры сна и улыбки
        sleep_until = 0.0
        face_emotion = Emotion.NEUTRAL
        happy_until = 0.0
        global _last_sent_ms  # pylint: disable=global-statement

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
                        nose = lms[mp.solutions.pose.PoseLandmark.NOSE]
                        left_ear = lms[mp.solutions.pose.PoseLandmark.LEFT_EAR]
                        right_ear = lms[mp.solutions.pose.PoseLandmark.RIGHT_EAR]
                        left_eye = lms[mp.solutions.pose.PoseLandmark.LEFT_EYE]
                        right_eye = lms[mp.solutions.pose.PoseLandmark.RIGHT_EYE]
                        left_sh = lms[mp.solutions.pose.PoseLandmark.LEFT_SHOULDER]
                        right_sh = lms[mp.solutions.pose.PoseLandmark.RIGHT_SHOULDER]

                        visible = lambda lm: lm.visibility > vis
                        landmarks = [
                            lm
                            for lm in (nose, left_ear, right_ear, left_eye, right_eye)
                            if visible(lm)
                        ]

                        if len(landmarks) >= 2:
                            xs = [lm.x for lm in landmarks]
                            ys = [lm.y for lm in landmarks]
                            x_rel = min(xs)
                            y_rel = min(ys)
                            w_rel = max(xs) - x_rel
                            h_rel = max(ys) - y_rel
                            area = w_rel * h_rel
                            aspect = w_rel / h_rel if h_rel else 0.0

                            cond_face = visible(nose) and (
                                visible(left_eye)
                                or visible(right_eye)
                                or visible(left_ear)
                                or visible(right_ear)
                            )
                            cond_left = visible(left_ear) and (
                                (visible(left_sh) and left_ear.y < left_sh.y)
                                or visible(left_eye)
                            )
                            cond_right = visible(right_ear) and (
                                (visible(right_sh) and right_ear.y < right_sh.y)
                                or visible(right_eye)
                            )

                            if (
                                (cond_face or cond_left or cond_right)
                                and area > HEAD_MIN_AREA
                                and HEAD_MIN_ASPECT < aspect < HEAD_MAX_ASPECT
                            ):
                                kind = "head"

                now = time.monotonic()
                if self._wake_requested:
                    # Снаружи поступила голосовая команда — просыпаемся и
                    # начинаем сканировать пространство, как будто только что
                    # потеряли лицо из вида.
                    self._wake_requested = False
                    if mode in ("sleeping", "idle"):
                        mode = "scanning"
                        scan_start = now
                        scan_pos = 0.0
                        scan_dir = 1
                        scan_hold_until = now
                        last_face_ts = now
                if face_emotion == Emotion.HAPPY and now >= happy_until:
                    publish(Event(kind="emotion_changed", attrs={"emotion": Emotion.NEUTRAL}))
                    face_emotion = Emotion.NEUTRAL

                raw_detected = kind is not None
                if raw_detected:
                    if stable_start is None:
                        stable_start = now
                    detected = now - stable_start >= FACE_STABLE_SEC
                else:
                    stable_start = None
                    detected = False

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
                    dt_ms = 0 if _last_sent_ms is None else now_ms - _last_sent_ms
                    _last_sent_ms = now_ms

                    dx_px = float(cx - fw / 2.0)
                    dy_px = float(cy - fh / 2.0)
                    _update_track(True, dx_px, dy_px, dt_ms)
                    dt_since_face = now - last_face_ts
                    last_face_ts = now
                    if dt_since_face > FACE_ABSENT_HAPPY_SEC:
                        desired = Emotion.HAPPY
                        happy_until = now + HAPPY_SHOW_SEC
                    else:
                        desired = Emotion.NEUTRAL
                    if face_emotion != desired:
                        publish(Event(kind="emotion_changed", attrs={"emotion": desired}))
                        face_emotion = desired
                    if mode != "tracking":
                        mode = "tracking"

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
                    if mode == "tracking":
                        # Раньше следили за лицом, но оно исчезло – останавливаем
                        # приводы и переходим в режим ожидания.
                        _clear_track()
                        mode = "idle"

                    dt_absent = now - last_face_ts
                    if mode == "sleeping":
                        # Во сне камера неподвижна. Просыпаемся, когда истёк
                        # таймер ``sleep_until``.
                        if now >= sleep_until:
                            publish(Event(kind="emotion_changed", attrs={"emotion": Emotion.SUSPICIOUS}))
                            mode = "scanning"
                            scan_start = now
                            scan_pos = 0.0
                            scan_dir = 1
                            scan_hold_until = now
                        # во сне камера не двигается
                    elif dt_absent > NO_FACE_SCAN_SEC and mode != "scanning":
                        # Лица нет уже достаточно долго → начинаем поиск
                        publish(Event(kind="emotion_changed", attrs={"emotion": Emotion.SUSPICIOUS}))
                        mode = "scanning"
                        scan_start = now
                        scan_pos = 0.0
                        scan_dir = 1
                        scan_hold_until = now

                    if mode == "scanning":
                        # Расчёт шага и направление горизонтального сканирования
                        now_ms = int(now * 1000)
                        dt_ms = 0 if _last_sent_ms is None else (now_ms - _last_sent_ms)
                        _last_sent_ms = now_ms
                        dt_sec = dt_ms / 1000.0
                        if now >= scan_hold_until:
                            scan_pos += scan_dir * SCAN_SPEED_PX_PER_SEC * dt_sec
                            if scan_pos >= SCAN_H_RANGE_PX:
                                scan_pos = SCAN_H_RANGE_PX
                                scan_dir = -1
                                scan_hold_until = now + SCAN_HOLD_SEC
                            elif scan_pos <= -SCAN_H_RANGE_PX:
                                scan_pos = -SCAN_H_RANGE_PX
                                scan_dir = 1
                                scan_hold_until = now + SCAN_HOLD_SEC
                        # Вертикальное движение описываем синусоидой, чтобы
                        # взгляд плавно "скользил" вверх-вниз.
                        dy = SCAN_V_RANGE_PX * math.sin(2 * math.pi * (now - scan_start) / SCAN_V_PERIOD_SEC)
                        _send_track(scan_pos, dy, dt_ms)
                        if now - scan_start > SCAN_TOTAL_SEC:
                            # Полный обзор завершён → уходим спать на случайный
                            # интервал и показываем эмоцию сонливости.
                            _clear_track()
                            publish(Event(kind="emotion_changed", attrs={"emotion": Emotion.SLEEPY}))
                            mode = "sleeping"
                            sleep_dur = random.uniform(SLEEP_MIN_SEC, SLEEP_MAX_SEC)
                            sleep_until = now + sleep_dur
                            log.info("sleeping for %.1f seconds", sleep_dur)

                self._update_state(kind if detected else None)

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

    Формат совместим с display-driver, управляющим поворотом камеры.
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
