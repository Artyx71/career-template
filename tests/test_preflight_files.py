"""Регрессии проверки реальных файлов и конкурентного сохранения редактором."""
import argparse
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import test_preflight as baseline
m = baseline.m


class FileTests(unittest.TestCase):
    def fixture(self, directory):
        source = baseline.PreflightTests()
        source.setUp()
        root = Path(directory)
        inputs = {"vacancy": source.vacancy, "profile": source.profile,
                  "evidence": source.evidence, "claims": source.claims}
        paths = {}
        for key, value in inputs.items():
            paths[key] = root / (key + ".json")
            paths[key].write_text(json.dumps(value, ensure_ascii=False))
        paths["resume"] = root / "resume.txt"
        paths["resume"].write_text(source.text)
        review = copy.deepcopy(source.review)
        review["hashes"] = {key: m.sha(path) for key, path in paths.items()}
        paths["review"] = root / "review.json"
        paths["review"].write_text(json.dumps(review, ensure_ascii=False))
        args = argparse.Namespace(**paths, output=root / "result")
        foundation = SimpleNamespace(extract=lambda path: path.read_text(), ALIASES=source.aliases)
        return args, foundation, source

    def test_сохранение_любого_входа_во_время_извлечения_блокирует_прохождение(self):
        for changed in ["resume", "vacancy", "profile", "evidence", "claims"]:
            with self.subTest(вход=changed), tempfile.TemporaryDirectory() as directory:
                args, foundation, source = self.fixture(directory)
                old_hash = m.sha(args.resume)

                def extract(snapshot):
                    target = getattr(args, changed)
                    target.write_bytes(target.read_bytes() + b" ")
                    return snapshot.read_text()

                foundation.extract = extract
                with contextlib.redirect_stdout(io.StringIO()):
                    code = m.run_files(args, {}, foundation)
                report = json.loads((args.output / "report.json").read_text())
                self.assertEqual(code, 3)
                self.assertEqual(report["verdict"], "BLOCKED")
                self.assertIn("inputs_changed_during_run", [f["code"] for f in report["findings"]])
                self.assertEqual(report["hashes"]["resume"], old_hash)
                self.assertEqual((args.output / "resume-extracted.txt").read_text(), source.text)

    def test_неверный_тип_чеклиста_даёт_review_без_исключения(self):
        source = baseline.PreflightTests()
        source.setUp()
        for value in [[], None, "approved"]:
            with self.subTest(чеклист=value):
                source.review["checks"] = value
                source.assert_code("review_incomplete", "REVIEW")

    def test_повреждённый_pdf_даёт_контролируемую_ошибку(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _, _ = self.fixture(directory)
            bad = Path(directory) / "bad.pdf"
            bad.write_bytes(b"%PDF-1.4\ninvalid\n")
            command = [sys.executable, str(m.ROOT / "scripts/preflight.py"),
                       "--vacancy", str(args.vacancy), "--resume", str(bad),
                       "--profile", str(args.profile), "--evidence", str(args.evidence),
                       "--claims", str(args.claims), "--output", str(args.output)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 4)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("Ошибка входов/проверки", result.stderr)
            self.assertFalse((args.output / "report.json").exists())

    def test_отсутствующий_файл_не_считается_пройденной_проверкой(self):
        with tempfile.TemporaryDirectory() as directory:
            args, foundation, _ = self.fixture(directory)
            args.resume.unlink()
            with self.assertRaises(FileNotFoundError):
                m.run_files(args, {}, foundation)

    def test_неверный_корневой_тип_профиля_отклоняется(self):
        source = baseline.PreflightTests()
        source.setUp()
        source.profile = []
        with self.assertRaisesRegex(ValueError, "JSON-объектами"):
            source.run_audit()
