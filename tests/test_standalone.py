import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from publisher.runtime import verify_package

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_standalone', ROOT / 'scripts/build_standalone.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class StandaloneRelease(unittest.TestCase):
    def test_export_reproduces_verified_service_without_plugin_or_private_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage, repeat = Path(tmp) / 'release', Path(tmp) / 'repeat'
            builder.build(stage)
            hashes = verify_package(stage)
            self.assertTrue((stage / 'install.sh').is_file())
            self.assertTrue((stage / 'publisher/agent.py').is_file())
            for name in ('.git', '.agents', '.codex-plugin', '.mcp.json', '.app.json',
                         'plugin.json', 'skills', 'installation.json', 'settings.json', 'publisher.sqlite3'):
                self.assertFalse((stage / name).exists(), name)
            builder.build(repeat, root=stage)
            self.assertEqual(hashes, verify_package(repeat))
            with self.assertRaises(FileExistsError):
                builder.build(stage)

    def test_changed_runtime_fails_integrity_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / 'release'
            builder.build(stage)
            (stage / 'publisher/__init__.py').write_text('tampered')
            with self.assertRaises(Exception):
                verify_package(stage)
