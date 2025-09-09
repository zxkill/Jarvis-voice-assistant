import sqlite3
import threading

from memory import db


def test_get_connection_retries_on_locked(tmp_path, monkeypatch):
    """Проверяем, что функция повторяет попытку при блокировке базы."""
    # Используем временный файл вместо реальной базы
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")

    real_connect = sqlite3.connect

    class LockedConn(sqlite3.Connection):
        """Соединение, которое первый раз выбрасывает `database is locked`."""
        commit_calls = 0

        def commit(self):  # type: ignore[override]
            type(self).commit_calls += 1
            if self.commit_calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return super().commit()

    def fake_connect(path, *args, **kwargs):
        kwargs.setdefault("factory", LockedConn)
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)

    # В отдельном потоке вызываем get_connection, чтобы имитировать реальный сценарий
    result: list[sqlite3.Connection] = []

    def worker():
        with db.get_connection() as conn:
            conn.execute("SELECT 1")
            result.append(conn)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)

    assert result, "get_connection не вернул соединение"
    assert LockedConn.commit_calls >= 2, "ожидалось повторение коммита"
