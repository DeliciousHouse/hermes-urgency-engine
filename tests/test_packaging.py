"""protects=shared Hermes environment; fails_when=plugin becomes a buildable workspace member
or ships a top-level `scripts` package; why_new=multi-profile installs; seam=pyproject.toml;
no_claim=host PM internals beyond its documented 'installs nothing' rule.
"""
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_pyproject_installs_nothing_into_the_shared_environment(self):
        # Hermes documents a pyproject with no [build-system], dependencies, extras or groups
        # as "installs nothing": no workspace member, so two profiles cannot collide on its name.
        document = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
        project = document['project']
        self.assertNotIn('build-system', document)
        self.assertIsNot(document.get('tool', {}).get('uv', {}).get('package'), True)
        self.assertEqual(project.get('dependencies', []), [])
        self.assertFalse(any(project.get('optional-dependencies', {}).values()))
        self.assertFalse(any(document.get('dependency-groups', {}).values()))

    def test_scripts_is_launcher_files_not_a_package(self):
        # A regular top-level `scripts` package would shadow the host's namespace package.
        packages = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['tool']['setuptools']['packages']
        self.assertEqual(packages, ['urgency_engine'])
        self.assertFalse((ROOT / 'scripts' / '__init__.py').exists())
        self.assertTrue((ROOT / 'scripts' / 'watchdog.py').is_file())


    def test_readme_discloses_machine_side_effects(self):
        # Catalog rule 13: the README itself names files written outside plugin data,
        # background jobs and spawned processes. Each disclosed name must match the code.
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        runtime = (ROOT / 'urgency_engine' / 'runtime.py').read_text(encoding='utf-8')
        launcher = (ROOT / 'scripts' / 'watchdog.py').read_text(encoding='utf-8')
        for name, source in (("'hermes-urgency-engine-watchdog.py'", runtime), ("'hermes_cli.main'", launcher),
                             ("'plugins', 'list', '--json'", launcher), ("'urgency', 'watchdog', 'tick'", launcher)):
            with self.subTest(name=name):
                self.assertIn(name, source)
                self.assertIn(name.strip("'").replace("', '", ' '), readme)
        for disclosure in ('no-agent Hermes cron job that runs every minute', 'kanban ...', 'no third-party network calls'):
            self.assertIn(disclosure, readme)

if __name__ == '__main__':
    unittest.main()
