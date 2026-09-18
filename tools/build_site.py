#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only

"""Assemble the dependency-free Iris website for local preview or Pages."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "site"
SITE_DIAL_ASSETS = (
    SOURCE / "assets" / "iris-dial-site.gif",
    SOURCE / "assets" / "iris-dial-site-light.gif",
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

    for asset in SITE_DIAL_ASSETS:
        if not asset.is_file():
            raise SystemExit(
                f"missing website dial asset: {asset}; run "
                "tools/render_readme_gif.py --site --both"
            )
    shutil.copytree(SOURCE, output)
    (output / ".nojekyll").touch()
    print(f"Built Iris website in {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()
