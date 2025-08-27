"""Тесты медленного сканирования комнаты при отсутствии лица."""

from sensors.vision import presence


def test_scan_update_speed_and_direction():
    """Проверяем скорость смещения и смену направления на краях."""
    pos, direction, hold, dx = presence._scan_update(
        0.0, 1, now=0.0, hold_until=0.0, dt_sec=1.0
    )
    assert pos == presence.SCAN_SPEED_PX_PER_SEC
    assert direction == 1
    assert hold == 0.0
    assert dx == presence.SCAN_SPEED_PX_PER_SEC

    # Достигаем правого края и убеждаемся, что направление меняется на обратное
    pos, direction, hold, dx = presence._scan_update(
        presence.SCAN_H_RANGE_PX, 1, now=1.0, hold_until=0.5, dt_sec=0.5
    )
    assert pos == presence.SCAN_H_RANGE_PX
    assert direction == -1
    assert hold == 1.0 + presence.SCAN_HOLD_SEC
    assert dx == 0.0

    # После выдержки серва должна начать движение в обратную сторону
    pos, direction, hold, dx = presence._scan_update(
        pos,
        direction,
        now=hold,
        hold_until=hold,
        dt_sec=1.0,
    )
    assert direction == -1
    assert dx < 0
