#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only

"""Assemble the dependency-free Iris website for local preview or Pages."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "site"
DIAL_ASSETS = (
    ROOT / "docs" / "assets" / "iris-dial.gif",
    ROOT / "docs" / "assets" / "iris-dial-light.gif",
)


def build(output: Path) -> None:
    output = output.expanduser().resolve()
    protected = {ROOT.resolve(), SOURCE.resolve(), (ROOT / "docs").resolve()}
    if output in protected:
        raise SystemExit(f"refusing to replace protected directory: {output}")
    if output.exists():
        if output.is_symlink() or not output.is_dir():
            raise SystemExit(f"refusing to replace non-directory output: {output}")
        shutil.rmtree(output)

    shutil.copytree(SOURCE, output)
    assets = output / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for asset in DIAL_ASSETS:
        if not asset.is_file():
            raise SystemExit(f"missing canonical dial asset: {asset}")
        shutil.copy2(asset, assets / asset.name)
    (output / ".nojekyll").touch()
    print(f"Built Iris website in {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()

