import pytest

from sensors.vision.presence import PresenceDetector


def test_frame_rotation_validation():
    """Неверное значение угла поворота должно приводить к исключению."""
    with pytest.raises(ValueError):
        PresenceDetector(camera_index=0, frame_interval_ms=100, absent_after_sec=5, frame_rotation=45)


def test_default_parameters():
    """Параметры по умолчанию устанавливаются корректно."""
    det = PresenceDetector(camera_index=0, frame_interval_ms=100, absent_after_sec=5)
    assert det.frame_rotation == 270
    assert det.show_window is True


def test_run_without_cv2(monkeypatch):
    """Отсутствие зависимостей не должно приводить к ошибке выполнения."""
    monkeypatch.setattr("sensors.vision.presence.cv2", None)
    monkeypatch.setattr("sensors.vision.presence.mp", None)
    det = PresenceDetector(camera_index=0, frame_interval_ms=100, absent_after_sec=5)
    det.run()  # метод просто завершается без исключений


def test_run_auto_grants_consent(monkeypatch):
    """При отсутствии согласия модуль выдаёт его автоматически и продолжает работу."""

    # Минимальные заглушки для OpenCV и MediaPipe, чтобы метод ``run`` не завершался раньше времени.
    class _Cap:
        def isOpened(self):
            return True

        def read(self):
            # Прерываем цикл сразу после первого обращения к камере
            raise KeyboardInterrupt

        def release(self):
            pass

    class _CV2:
        ROTATE_90_CLOCKWISE = 0
        ROTATE_180 = 1
        ROTATE_90_COUNTERCLOCKWISE = 2
        COLOR_BGR2RGB = 0

        def VideoCapture(self, index):
            return _Cap()

        def destroyAllWindows(self):  # noqa: D401 - заглушка
            pass

    class _MP:
        class solutions:
            class face_detection:
                @staticmethod
                def FaceDetection(**_kwargs):
                    class _Face:
                        def process(self, _frame):
                            return type("R", (), {"detections": None})()

                    return _Face()

    monkeypatch.setattr("sensors.vision.presence.cv2", _CV2())
    monkeypatch.setattr("sensors.vision.presence.mp", _MP())

    calls = {"grant": 0, "active": 0}

    def _set_active(sensor: str, active: bool) -> None:
        calls["active"] += 1
        # Первое включение камеры имитируем без согласия
        if calls["active"] == 1:
            raise PermissionError

    def _grant(sensor: str) -> None:
        calls["grant"] += 1

    monkeypatch.setattr("sensors.vision.presence.set_active", _set_active)
    monkeypatch.setattr("sensors.vision.presence.grant_consent", _grant)

    det = PresenceDetector(camera_index=0, frame_interval_ms=100, absent_after_sec=5, show_window=False)

    # Ожидаем KeyboardInterrupt из заглушки камеры, чтобы выйти из бесконечного цикла
    with pytest.raises(KeyboardInterrupt):
        det.run()

    # Убедимся, что согласие было выдано автоматически и попытка включения камеры повторилась
    assert calls["grant"] == 1
    assert calls["active"] >= 2
