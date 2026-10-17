"""Run from the project root: python -m unittest discover -s tests -v."""

import importlib.util
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "retrieve.py"
spec = importlib.util.spec_from_file_location("slides_retrieve", SCRIPT)
retrieve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retrieve)


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index_text = (ROOT / "rag/index.json").read_text(encoding="utf-8")
        cls.index = json.loads(cls.index_text)
        cls.entries = cls.index["entries"]

    def run_cli(self, *args, script=SCRIPT, cwd=None):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(script), *args],
            cwd=cwd or ROOT, capture_output=True, encoding="utf-8",
            timeout=30, check=False,
        )

    def test_markdown_file_links(self):
        for document in ROOT.rglob("*.md"):
            text = document.read_text(encoding="utf-8")
            # Example snippets are not links to real repository files.
            text = re.sub(r"(?ms)^```.*?^```[^\n]*$", "", text)
            for _, target in retrieve.LINK.findall(text):
                if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
                    continue
                target = unquote(target.split("#", 1)[0])
                with self.subTest(document=document.relative_to(ROOT), target=target):
                    resolved = (document.parent / target).resolve()
                    self.assertTrue(resolved.is_relative_to(ROOT))
                    self.assertTrue(resolved.exists(), str(resolved))

    def test_skill_names_and_directories(self):
        skills = list((ROOT / ".agents/skills").glob("*/SKILL.md"))
        self.assertEqual(len(skills), 3)
        for skill in skills:
            name = re.search(r"(?m)^name: (.+)$", skill.read_text(encoding="utf-8"))
            self.assertIsNotNone(name)
            self.assertEqual(name.group(1), skill.parent.name)
            self.assertTrue(name.group(1).startswith("slides-copilot-"))

    def test_readme_html_links_and_anchors(self):
        class ReadmeHTML(HTMLParser):
            def __init__(self):
                super().__init__()
                self.targets = []
                self.anchors = set()
                self.images = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                for key in ("name", "id"):
                    if attrs.get(key):
                        self.anchors.add(attrs[key])
                for key in ("href", "src"):
                    if attrs.get(key):
                        self.targets.append(attrs[key])
                if tag == "img":
                    self.images.append(attrs)

        parser = ReadmeHTML()
        parser.feed((ROOT / "README.md").read_text(encoding="utf-8"))
        for target in parser.targets:
            with self.subTest(target=target):
                if target.startswith("#"):
                    self.assertIn(target[1:], parser.anchors)
                elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                    path = (ROOT / unquote(target.split("#", 1)[0])).resolve()
                    self.assertTrue(path.is_relative_to(ROOT))
                    self.assertTrue(path.exists())
        for attrs in parser.images:
            self.assertTrue(attrs.get("alt"), "Every README image needs alt text")

    def test_images_exist_and_are_indexed(self):
        public_images = {p.relative_to(ROOT).as_posix() for p in (ROOT / "visuals/images").glob("*.png")}
        self.assertEqual(len(public_images), 28)
        indexed_images = {p for entry in self.entries for p in entry["images"]}
        self.assertTrue(public_images.issubset(indexed_images))
        for image in public_images:
            with self.subTest(image=image):
                with (ROOT / image).open("rb") as stream:
                    self.assertEqual(stream.read(8), b"\x89PNG\r\n\x1a\n")

    def test_indexed_paths_are_relative_and_exist(self):
        for entry in self.entries:
            paths = [entry["path"], *entry["images"], *entry["visual_notes"]]
            for value in paths:
                with self.subTest(path=value):
                    self.assertFalse(PurePosixPath(value).is_absolute())
                    self.assertFalse(PureWindowsPath(value).drive)
                    self.assertNotIn("\\", value)
                    self.assertNotIn("..", PurePosixPath(value).parts)
                    self.assertTrue((ROOT / value).is_file())
            self.assertEqual(entry["id"], f"{entry['path']}:{entry['line']}")

    def test_index_has_no_absolute_machine_paths(self):
        strings = []

        def walk(value):
            if isinstance(value, dict):
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            elif isinstance(value, str):
                strings.append(value)

        walk(self.index)
        patterns = (
            r"(?i)(?<![a-z])[a-z]:[\\/]",
            r"\\\\[^\\\s]+\\",
            r"(?i)(?:/Users/|/home/|/mnt/|/tmp/|/private/|/root/|file://)",
        )
        for value in strings:
            for pattern in patterns:
                self.assertIsNone(re.search(pattern, value), value[:200])

    def test_removed_references_and_old_brand_are_absent(self):
        old_names = ("chip-dark", "medical-blue", "Adv-PPT-Designer", "adv-ppt-")
        files = [*ROOT.rglob("*.md"), ROOT / "rag/index.json"]
        for path in files:
            text = path.read_text(encoding="utf-8")
            for name in old_names:
                self.assertNotIn(name, text, str(path))
        for name in ("chip-dark.jpg", "medical-blue.jpg"):
            self.assertFalse((ROOT / "design-library/images" / name).exists())

    def test_index_matches_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(retrieve, "INDEX", Path(folder) / "index.json"):
                rebuilt = retrieve.build_index()
        self.assertEqual(self.entries, rebuilt, "Run tools/retrieve.py --rebuild")

    def test_chinese_queries(self):
        for query in ("页面太空", "光影不自然", "文字有错字", "内容很多"):
            with self.subTest(query=query):
                results = retrieve.search(self.entries, query, 3)
                self.assertTrue(results)
                self.assertLessEqual(len(results), 3)
                self.assertEqual(
                    [r["score"] for r in results],
                    sorted((r["score"] for r in results), reverse=True),
                )
                for result in results:
                    self.assertIn("source", result)
                    self.assertIn("image_paths", result)

    def test_json_cli_from_another_directory(self):
        before = (ROOT / "rag/index.json").read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_cli("光影不自然", "--top", "2", "--json", cwd=folder)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["query"], "光影不自然")
        self.assertTrue(0 < len(payload["results"]) <= 2)
        self.assertEqual(before, (ROOT / "rag/index.json").read_bytes())

    def test_unknown_query(self):
        result = self.run_cli("zzzznohit987654321", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["results"], [])

    def test_no_arguments_reports_usage(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn("请输入设计问题", result.stderr)

    def test_missing_index_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder)
            (copy / "tools").mkdir()
            shutil.copy2(SCRIPT, copy / "tools/retrieve.py")
            # Index generation reads descriptions, not image bytes.
            for name in ("handbook", "visuals", "design-library", "prompts"):
                (copy / name).mkdir()
                for source in (ROOT / name).glob("*.md"):
                    shutil.copy2(source, copy / name / source.name)
            result = self.run_cli("光影", "--json", script=copy / "tools/retrieve.py", cwd=copy)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["results"])
            rebuilt = json.loads((copy / "rag/index.json").read_text(encoding="utf-8"))
            self.assertEqual(self.entries, rebuilt["entries"])


if __name__ == "__main__":
    unittest.main()
