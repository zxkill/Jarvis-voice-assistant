"""Общие фикстуры для модулей тестирования памяти."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.append(str(Path(__file__).resolve().parents[2]))

from memory import db as memory_db


@pytest.fixture()
def dialog_db(monkeypatch, tmp_path):
    """Создаёт временную SQLite-базу для тестирования журнала диалога."""

    monkeypatch.setenv("JARVIS_DB_KEY", Fernet.generate_key().decode())
    db_file = tmp_path / "memory.sqlite3"
    monkeypatch.setattr(memory_db, "DB_PATH", db_file)
    return db_file
