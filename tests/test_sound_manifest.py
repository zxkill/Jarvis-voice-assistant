import textwrap
from pathlib import Path

import pytest

pytest.importorskip("yaml")

from emotion import sounds


def test_bool_keys_in_manifest(tmp_path, monkeypatch):
    manifest = textwrap.dedent(
        """
        YES:
          files: [a.wav]
        NO:
          files: [b.wav]
        """
    )
    path = tmp_path / "sfx_manifest.yaml"
    path.write_text(manifest, encoding="utf-8")
    monkeypatch.setattr(sounds, "MANIFEST_PATH", path)
    effects = sounds._load_manifest()
    assert set(effects.keys()) == {"YES", "NO"}
    assert effects["YES"].files == ["a.wav"]
    assert effects["NO"].files == ["b.wav"]
