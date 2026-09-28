"""Checks for the competition submission, its validator and the generated notebook cell."""
import ast
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_g4_{name}", HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class Submission(unittest.TestCase):
    def setUp(self):
        self.vs = _load("validate_submission")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sub = self.tmp / "submission"
        shutil.copytree(HERE / "submission", self.sub)

    def test_shipped_submission_is_valid(self):
        self.assertEqual(self.vs.validate(HERE / "submission"), [])

    def test_zip_has_agent_yaml_at_root(self):
        z = self.vs.pack(self.sub, self.tmp / "s.zip")
        names = zipfile.ZipFile(z).namelist()
        self.assertIn("agent.yaml", names)
        self.assertIn("prompts/system.md", names)

    def test_catches_common_mistakes(self):
        prompt = self.sub / "prompts" / "system.md"
        prompt.write_text(prompt.read_text() + "\nSee {repo_name} and {hints}. Fine: {hints?} {problem_description} {\"a\": 1}\n")
        agent = self.sub / "agent.yaml"
        agent.write_text(agent.read_text().replace("gemma-4-31b-it-qat-w4a16-ct", "gemma-4-12b-it")
                         .replace("  - get_status\n", "  - get_status\n  - browse_web\n"))
        (self.sub / "configs" / "sampling.yaml").write_text("max_output_tokens: 40000\ntools: []\n")
        (self.sub / "eval_config.yaml").write_text("max_tool_calls: 10\n")
        (self.sub / "weights.bin").write_bytes(b"x")
        errors = "\n".join(self.vs.validate(self.sub))
        for needle in ("{repo_name}", "{hints}", "model must be", "unknown tool 'browse_web'",
                       "max_output_tokens", "generate_content_config.tools", "`evaluation:`",
                       "weights.bin"):
            self.assertIn(needle, errors)
        self.assertNotIn("{hints?}", errors)
        self.assertNotIn("problem_description}", errors)

    def test_missing_include_and_adapter(self):
        agent = self.sub / "agent.yaml"
        agent.write_text(agent.read_text().replace("prompts/system.md", "prompts/nope.md")
                         + "adapter: main_lora\n")
        errors = "\n".join(self.vs.validate(self.sub))
        self.assertIn("include not found", errors)
        self.assertIn("adapter 'main_lora'", errors)

    def test_generated_cell_is_current_and_reproduces_files(self):
        build = _load("build_cells")
        cell = HERE / "notebook" / "cell2_write_submission.py"
        self.assertEqual(cell.read_text(), build.render(), "run build_cells.py after editing submission/")
        src = cell.read_text().replace('Path("/kaggle/working")', repr(str(self.tmp / "work")).join(("Path(", ")")))
        (self.tmp / "work").mkdir()
        out = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        for p in (HERE / "submission").rglob("*"):
            if p.is_file() and p.name != ".gitkeep":
                rel = p.relative_to(HERE / "submission")
                self.assertEqual((self.tmp / "work" / "submission" / rel).read_text(), p.read_text())
        self.assertIn("agent.yaml", zipfile.ZipFile(self.tmp / "work" / "submission.zip").namelist())


class NotebookCells(unittest.TestCase):
    def test_cells_parse(self):
        for p in sorted((HERE / "notebook").glob("*.py")):
            ast.parse(p.read_text(), str(p))


if __name__ == "__main__":
    unittest.main()
