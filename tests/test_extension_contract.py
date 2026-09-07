from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
EXTENSION = REPOSITORY / "extension" / "iris@local"


class ExtensionContractTests(unittest.TestCase):
    def _run(
        self,
        command: list[str],
        *,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=REPOSITORY,
            input=input_text,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )

    def _assert_success(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(
            completed.returncode,
            0,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )

    def test_javascript_files_parse_as_modules(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is unavailable; JavaScript syntax check skipped")

        for filename in ("extension.js", "prefs.js"):
            with self.subTest(filename=filename):
                self._assert_success(
                    self._run([node, "--check", os.fspath(EXTENSION / filename)])
                )

    def test_extension_imports_match_installed_shell_resource_api(self) -> None:
        gresource, node, shell_resource, major = self._shell_installation()
        self._require_declared_version(major)

        required_exports = {
            "/org/gnome/shell/extensions/extension.js": {"Extension"},
            "/org/gnome/shell/ui/main.js": {
                "componentManager",
                "layoutManager",
                "notify",
                "panel",
                "screenShield",
                "sessionMode",
            },
            "/org/gnome/shell/ui/panelMenu.js": {"Button"},
            "/org/gnome/shell/ui/popupMenu.js": {
                "PopupMenuItem",
                "PopupSeparatorMenuItem",
            },
        }
        for resource_path, expected in required_exports.items():
            with self.subTest(resource=resource_path):
                source = self._extract(gresource, shell_resource, resource_path)
                available = self._module_exports(node, source)
                self.assertEqual(expected - available, set())

    def test_preferences_import_matches_installed_resource_api(self) -> None:
        gresource, node, _shell_resource, major = self._shell_installation()
        self._require_declared_version(major)
        prefs_resource = self._preferences_resource(gresource)
        source = self._extract(
            gresource,
            prefs_resource,
            "/org/gnome/Shell/Extensions/js/extensions/prefs.js",
        )

        self.assertIn("ExtensionPreferences", self._module_exports(node, source))

    def _shell_installation(self) -> tuple[str, str, Path, int]:
        shell = shutil.which("gnome-shell")
        gresource = shutil.which("gresource")
        node = shutil.which("node")
        ldd = shutil.which("ldd")
        if shell is None or gresource is None or node is None or ldd is None:
            self.skipTest("GNOME Shell resource inspection tools are unavailable")

        acorn = self._run([node, "-e", "require('acorn')"])
        if acorn.returncode != 0:
            self.skipTest("Node acorn parser is unavailable")

        version = self._run([shell, "--version"])
        if version.returncode != 0:
            self.skipTest("installed GNOME Shell version cannot be queried")
        match = re.search(r"\b(\d+)(?:\.\d+)*\b", version.stdout)
        if match is None:
            self.skipTest(f"unrecognised GNOME Shell version: {version.stdout.strip()}")

        linked = self._run([ldd, shell])
        library_match = re.search(r"\blibshell-[^\s]+\s+=>\s+(\S+)", linked.stdout)
        if linked.returncode != 0 or library_match is None:
            self.skipTest("GNOME Shell JavaScript resource library is unavailable")
        shell_resource = Path(library_match.group(1))
        if not shell_resource.is_file():
            self.skipTest("GNOME Shell JavaScript resource library is unavailable")
        return gresource, node, shell_resource, int(match.group(1))

    def _require_declared_version(self, major: int) -> None:
        metadata = json.loads((EXTENSION / "metadata.json").read_text(encoding="utf-8"))
        if str(major) not in metadata.get("shell-version", []):
            self.skipTest(f"installed GNOME Shell {major} is not a declared target")

    def _preferences_resource(self, gresource: str) -> Path:
        expected = "/org/gnome/Shell/Extensions/js/extensions/prefs.js"
        for candidate in sorted(Path("/usr/share/gnome-shell").glob("*.gresource")):
            listing = self._run([gresource, "list", os.fspath(candidate)])
            if listing.returncode == 0 and expected in listing.stdout.splitlines():
                return candidate
        self.skipTest("GNOME Shell preferences resource is unavailable")

    def _extract(self, gresource: str, bundle: Path, resource_path: str) -> str:
        extracted = self._run(
            [gresource, "extract", os.fspath(bundle), resource_path]
        )
        self._assert_success(extracted)
        self.assertNotEqual(extracted.stdout, "", f"empty resource: {resource_path}")
        return extracted.stdout

    def _module_exports(self, node: str, source: str) -> set[str]:
        parser = r"""
const acorn = require('acorn');
const fs = require('fs');
const tree = acorn.parse(fs.readFileSync(0, 'utf8'), {
    ecmaVersion: 'latest', sourceType: 'module'
});
const names = new Set();
for (const statement of tree.body) {
    if (statement.type !== 'ExportNamedDeclaration')
        continue;
    const declaration = statement.declaration;
    if (declaration?.id?.name)
        names.add(declaration.id.name);
    if (declaration?.type === 'VariableDeclaration') {
        for (const item of declaration.declarations) {
            if (item.id.type === 'Identifier')
                names.add(item.id.name);
        }
    }
    for (const specifier of statement.specifiers ?? [])
        names.add(specifier.exported.name);
}
process.stdout.write(JSON.stringify([...names].sort()));
"""
        parsed = self._run([node, "-e", parser], input_text=source)
        self._assert_success(parsed)
        return set(json.loads(parsed.stdout))


if __name__ == "__main__":
    unittest.main()
