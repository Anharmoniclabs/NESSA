import tempfile
import unittest
from pathlib import Path

from agentharness.project_discovery import find_project, overview


class ProjectDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'AnharmonicStudio'
        self.project.mkdir()
        (self.project / 'pyproject.toml').write_text('[project]\nname="studio"\nversion="1.0"\n')

    def test_typo_and_spacing(self):
        self.assertEqual(find_project('can we inspect the anharmoinic studio app in my files',
                                      [self.root]), [self.project])

    def test_greeting_does_not_select_project(self):
        self.assertEqual(find_project('hey', [self.root]), [])
        self.assertEqual(find_project('I like AnharmonicStudio', [self.root]), [])

    def test_explicit_path(self):
        self.assertEqual(find_project('sure here ' + str(self.project), [self.root]), [self.project])

    def test_ambiguous_names_are_not_silently_chosen(self):
        (self.root / 'AnharmonicStudioDemo').mkdir()
        self.assertEqual(len(find_project('inspect anharmonic studio', [self.root])), 2)

    def test_overview_is_grounded(self):
        text = overview(self.project)
        self.assertIn(str(self.project), text)
        self.assertIn('pyproject.toml', text)
        self.assertIn('studio', text)
        self.assertIn('1.0', text)
