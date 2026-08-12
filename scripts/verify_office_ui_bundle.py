#!/usr/bin/env python3
"""Verify that the packaged Office UI contains a complete Vite bundle."""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


BUNDLE_PREFIX = PurePosixPath("opc/plugins/office_ui/frontend_dist")
REQUIRED_STATIC_ASSETS = (
    "assets/office-bg.png",
    "assets/office-tileset-32.png",
)
FORBIDDEN_EAGER_ASSET_MARKERS = ("phaser-", "PhaserGame-")
_HTML_ASSET_RE = re.compile(r"(?:src|href)=[\"']([^\"']+)[\"']")


def _local_asset_references(index_html: str) -> set[str]:
    references: set[str] = set()
    for raw_reference in _HTML_ASSET_RE.findall(index_html):
        parsed = urlsplit(raw_reference)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        normalized = parsed.path.removeprefix("./").lstrip("/")
        path = PurePosixPath(normalized)
        if ".." in path.parts:
            raise ValueError(f"Office UI index contains an unsafe asset path: {raw_reference}")
        references.add(path.as_posix())
    return references


def _validate_bundle(read_text: Callable[[str], str], names: Iterable[str]) -> None:
    available = {PurePosixPath(name).as_posix().lstrip("/") for name in names}
    if "index.html" not in available:
        raise ValueError("Office UI bundle is missing index.html")

    references = _local_asset_references(read_text("index.html"))
    if not any(reference.endswith(".js") for reference in references):
        raise ValueError("Office UI index does not reference a JavaScript entrypoint")
    if not any(reference.endswith(".css") for reference in references):
        raise ValueError("Office UI index does not reference a stylesheet")
    eager_heavy_assets = sorted(
        reference
        for reference in references
        if any(marker.lower() in reference.lower() for marker in FORBIDDEN_EAGER_ASSET_MARKERS)
    )
    if eager_heavy_assets:
        raise ValueError(
            "Office UI index eagerly loads Office-only assets: "
            + ", ".join(eager_heavy_assets)
        )

    missing = sorted(
        reference
        for reference in (*references, *REQUIRED_STATIC_ASSETS)
        if reference not in available
    )
    if missing:
        raise ValueError(f"Office UI bundle references missing assets: {', '.join(missing)}")


def verify_source_bundle(root: Path) -> None:
    root = root.resolve()
    names = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    ]
    _validate_bundle(lambda name: (root / name).read_text(encoding="utf-8"), names)


def verify_wheel_bundle(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        prefix = f"{BUNDLE_PREFIX.as_posix()}/"
        members = {
            member.removeprefix(prefix): member
            for member in archive.namelist()
            if member.startswith(prefix) and not member.endswith("/")
        }
        _validate_bundle(
            lambda name: archive.read(members[name]).decode("utf-8"),
            members,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("opc/plugins/office_ui/frontend_dist"),
        help="frontend_dist directory to validate",
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        action="append",
        default=[],
        help="built wheel to validate; may be supplied more than once",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        verify_source_bundle(args.source)
        for wheel in args.wheel:
            verify_wheel_bundle(wheel)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Office UI bundle verification failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Office UI bundle verified"
        + (f" in {len(args.wheel)} wheel(s)" if args.wheel else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
