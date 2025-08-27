import pytest

from memory import db, writer, reader


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Создаёт временную БД для изоляции тестов."""
    test_db = tmp_path / "test.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    yield


def test_feedback_crud_operations(temp_db):
    """Проверяем создание, чтение, обновление и удаление отзывов."""
    suggestion_id = writer.add_suggestion("выпей воды")
    feedback_id = writer.add_suggestion_feedback(suggestion_id, "хорошо", True)

    # Чтение — должен вернуться один отзыв
    rows = reader.get_suggestion_feedback(suggestion_id)
    assert len(rows) == 1
    assert rows[0]["id"] == feedback_id
    assert rows[0]["accepted"] == 1

    # Обновление отзывов напрямую через SQL
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE suggestion_feedback SET response_text=?, accepted=? WHERE id=?",
            ("позже", 0, feedback_id),
        )

    updated = reader.get_suggestion_feedback(suggestion_id)
    assert updated[0]["response_text"] == "позже"
    assert updated[0]["accepted"] == 0

    # Удаление записи
    with db.get_connection() as conn:
        conn.execute("DELETE FROM suggestion_feedback WHERE id=?", (feedback_id,))

    assert reader.get_suggestion_feedback(suggestion_id) == []


def test_feedback_stats(temp_db):
    """Проверяем агрегаты по принятым и отклонённым подсказкам."""
    # На чистой БД статистика должна быть нулевой
    assert reader.get_feedback_stats() == {"accepted": 0, "rejected": 0}

    s1 = writer.add_suggestion("зарядка")
    s2 = writer.add_suggestion("позвони другу")

    writer.add_suggestion_feedback(s1, "сделаю", True)
    writer.add_suggestion_feedback(s2, "не сейчас", False)

    # Подсказка без отзывов должна возвращать пустой список
    s3 = writer.add_suggestion("прочитай книгу")
    assert reader.get_suggestion_feedback(s3) == []

    assert reader.get_feedback_stats() == {"accepted": 1, "rejected": 1}
