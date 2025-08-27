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
