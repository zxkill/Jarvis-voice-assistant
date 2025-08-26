"""Проверяем, что драйвер навязывает минимальный cooldown для дыхания."""

import emotion.sounds as sounds


def test_idle_breath_min_cooldown(tmp_path, monkeypatch):
    """Значение cooldown меньше 15 минут принудительно повышается."""

    # Создаём временный манифест с заниженным интервалом в 1 секунду.
    manifest = tmp_path / "sfx.yaml"
    manifest.write_text(
        """
IDLE_BREATH:
  cooldown_ms: 1000
  files: []
""",
        encoding="utf-8",
    )

    # Подменяем путь к манифесту и загружаем эффекты заново.
    monkeypatch.setattr(sounds, "MANIFEST_PATH", manifest)
    effects = sounds._load_manifest()

    # Проверяем, что итоговый cooldown не меньше 15 минут (900 секунд).
    assert effects["IDLE_BREATH"].cooldown >= sounds.MIN_IDLE_BREATH_COOLDOWN

