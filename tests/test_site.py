# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        self.tags.append((tag, values))
        if values.get("id"):
            self.ids.add(values["id"])

    def handle_data(self, data: str) -> None:
        self.text.append(data)


class WebsiteContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html_path = SITE / "index.html"
        self.css_path = SITE / "styles.css"
        self.js_path = SITE / "app.js"
        self.assertTrue(self.html_path.is_file(), "site/index.html is missing")
        self.assertTrue(self.css_path.is_file(), "site/styles.css is missing")
        self.assertTrue(self.js_path.is_file(), "site/app.js is missing")
        self.html = self.html_path.read_text(encoding="utf-8")
        self.css = self.css_path.read_text(encoding="utf-8")
        self.js = self.js_path.read_text(encoding="utf-8")
        self.parser = PageParser()
        self.parser.feed(self.html)

    def test_metadata_and_product_contract(self) -> None:
        self.assertIn("<title>Iris — Face authentication for Linux</title>", self.html)
        self.assertIn('rel="canonical" href="https://roryjmahoney.github.io/Iris/"', self.html)
        self.assertIn('property="og:image"', self.html)
        self.assertIn('name="theme-color"', self.html)
        text = " ".join(self.parser.text)
        for phrase in (
            "Look at your laptop. You’re in.",
            "Ubuntu 26.04",
            "GNOME 50",
            "Password fallback",
            "AGPL-3.0",
        ):
            self.assertIn(phrase, text)

    def test_semantic_and_accessibility_contract(self) -> None:
        tags = [tag for tag, _attrs in self.parser.tags]
        for required in ("header", "nav", "main", "footer", "h1"):
            self.assertIn(required, tags)
        for section in ("features", "architecture", "security", "install", "faq"):
            self.assertIn(section, self.parser.ids)
        self.assertRegex(self.html, r'class="skip-link"[^>]+href="#main"')
        self.assertIn("aria-live=\"polite\"", self.html)
        self.assertIn(":focus-visible", self.css)
        self.assertIn("prefers-reduced-motion: reduce", self.css)
        self.assertNotRegex(self.html, r"\son[a-z]+=", "inline event handlers are forbidden")

    def test_runtime_assets_are_local_and_resolve(self) -> None:
        refs: list[str] = []
        for tag, attrs in self.parser.tags:
            if tag in {"script", "img", "source"} and attrs.get("src"):
                refs.append(attrs["src"])
            if tag == "link" and attrs.get("href") and attrs.get("rel") != "canonical":
                refs.append(attrs["href"])
        self.assertGreaterEqual(len(refs), 4)
        for ref in refs:
            parsed = urlparse(ref)
            self.assertFalse(parsed.scheme or parsed.netloc, f"runtime asset is external: {ref}")
            target = (SITE / parsed.path).resolve()
            self.assertTrue(target.is_relative_to(SITE.resolve()), f"asset escapes site/: {ref}")
            if parsed.path.startswith("assets/iris-dial"):
                target = ROOT / "docs" / "assets" / Path(parsed.path).name
            self.assertTrue(target.is_file(), f"missing local asset: {ref}")
        lowered = (self.html + self.js).lower()
        for forbidden in ("googletagmanager", "google-analytics", "fonts.googleapis", "unpkg.com", "jsdelivr"):
            self.assertNotIn(forbidden, lowered)

    def test_safe_installation_language(self) -> None:
        self.assertIn("sudo ./install.sh", self.html)
        self.assertNotRegex(self.html, r"curl[^\n<|]*\|\s*(?:sudo\s+)?(?:sh|bash)")
        for warning in ("convenience factor", "strong password", "Security model"):
            self.assertIn(warning, self.html)

    def test_javascript_parses(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        subprocess.run([node, "--check", str(self.js_path)], check=True, capture_output=True, text=True)

    def test_reproducible_build(self) -> None:
        builder = ROOT / "tools" / "build_site.py"
        self.assertTrue(builder.is_file(), "tools/build_site.py is missing")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "public"
            subprocess.run(
                [sys.executable, str(builder), "--output", str(output)],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            for relative in (
                "index.html",
                "styles.css",
                "app.js",
                "favicon.svg",
                ".nojekyll",
                "assets/iris-dial.gif",
                "assets/iris-dial-light.gif",
            ):
                self.assertTrue((output / relative).is_file(), f"build omitted {relative}")
            self.assertEqual(
                (output / "index.html").read_bytes(),
                self.html_path.read_bytes(),
            )

    def test_pages_workflow_is_minimally_privileged_and_pinned(self) -> None:
        self.assertTrue(WORKFLOW.is_file(), ".github/workflows/pages.yml is missing")
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("contents: read", workflow)
        self.assertIn("pages: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("python3 tools/build_site.py --output _site", workflow)
        self.assertIn("python3 -m unittest -v tests.test_site", workflow)
        uses = re.findall(r"uses:\s*([^\s#]+)", workflow)
        self.assertEqual(len(uses), 4)
        for action in uses:
            self.assertRegex(action, r"^actions/[a-z-]+@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()

