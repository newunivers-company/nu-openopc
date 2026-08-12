from pathlib import Path

import pytest

from scripts.verify_office_ui_bundle import verify_source_bundle


def test_committed_office_ui_bundle_is_complete() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    verify_source_bundle(
        repository_root / "opc" / "plugins" / "office_ui" / "frontend_dist"
    )


def test_bundle_rejects_eager_phaser_modulepreload(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index.js").write_text("", encoding="utf-8")
    (tmp_path / "assets" / "index.css").write_text("", encoding="utf-8")
    (tmp_path / "assets" / "phaser-heavy.js").write_text("", encoding="utf-8")
    for name in ("office-bg.png", "office-tileset-32.png"):
        (tmp_path / "assets" / name).write_bytes(b"asset")
    (tmp_path / "index.html").write_text(
        '<script src="./assets/index.js"></script>'
        '<link rel="stylesheet" href="./assets/index.css">'
        '<link rel="modulepreload" href="./assets/phaser-heavy.js">',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="eagerly loads Office-only assets"):
        verify_source_bundle(tmp_path)
