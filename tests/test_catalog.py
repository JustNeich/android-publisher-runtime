import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from publisher.catalog import prepare
from publisher.config import Config, UserError
from publisher.runtime import package_files


class Catalog(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.home = self.base / 'runtime'
        self.home.mkdir()
        self.source = self.home / 'release/app'
        self.source.mkdir(parents=True)
        self.environment = patch.dict(os.environ, {
            'ANDROID_PUBLISHER_RUNTIME': str(self.home),
            'ANDROID_PUBLISHER_DATA': str(self.base / 'data')})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.config = Config(self.source)
        self.sentinel = self.config.data / 'existing-queue.json'
        self.sentinel.write_text('[{"video":"approved","slot":12345}]')
        self.before = {p.name: p.read_bytes() for p in self.config.data.iterdir() if p.is_file()}
        self.package('first')
        (self.home / 'active.json').write_text(json.dumps({'app': str(self.source), 'data': str(self.config.data)}))

    def package(self, content):
        for folder in ('.codex-plugin', '.agents/plugins'):
            (self.source / folder).mkdir(parents=True, exist_ok=True)
        (self.source / '.codex-plugin/plugin.json').write_text(json.dumps({'name': 'android-publisher', 'version': content}))
        (self.source / '.agents/plugins/marketplace.json').write_text(json.dumps({
            'name': 'android-publisher-local', 'plugins': [{'name': 'android-publisher'}]}))
        (self.source / 'README.md').write_text(content)
        hashes = {str(p): hashlib.sha256((self.source / p).read_bytes()).hexdigest() for p in package_files(self.source)}
        (self.source / 'PACKAGE.sha256.json').write_text(json.dumps(hashes))

    def assert_data_preserved(self):
        self.assertEqual(self.before, {p.name: p.read_bytes() for p in self.config.data.iterdir() if p.is_file()})

    def test_repeat_and_source_folder_removal_preserve_queue_and_plugin_identity(self):
        first = prepare(self.config)
        repeat = prepare(self.config)
        self.assertEqual(first['plugin_id'], 'android-publisher@android-publisher-local')
        self.assertFalse(first['installed_by_this_command'])
        self.assertTrue(repeat['reused'])
        self.source.rename(self.source.with_name('moved-source'))
        self.assertEqual((Path(first['catalog_root']) / 'README.md').read_text(), 'first')
        self.assert_data_preserved()

    def test_interrupted_catalog_replace_restores_previous_source(self):
        root = Path(prepare(self.config)['catalog_root'])
        self.package('second')
        replace = os.replace
        def fail_publish(source, destination):
            if Path(destination) == root and Path(source).name.startswith('.publisher-catalog-'):
                raise OSError('injected rename interruption')
            return replace(source, destination)
        with patch('publisher.catalog.os.replace', side_effect=fail_publish):
            with self.assertRaises(OSError):
                prepare(self.config)
        self.assertEqual((root / 'README.md').read_text(), 'first')
        self.assertFalse(list(self.base.glob('.publisher-catalog-*')))
        self.assert_data_preserved()

    def test_catalog_replacement_keeps_recovery_and_refuses_unexpected_user_files(self):
        root = Path(prepare(self.config)['catalog_root'])
        self.package('second')
        result = prepare(self.config)
        self.assertTrue(result['previous_catalog_saved'])
        old = next((self.base / 'catalog-recovery').iterdir())
        self.assertEqual((old / 'README.md').read_text(), 'first')
        extra = root / 'user-notes.txt'
        extra.write_text('preserve me')
        with self.assertRaises(UserError):
            prepare(self.config)
        self.assertEqual(extra.read_text(), 'preserve me')
        self.assert_data_preserved()

    def test_matching_package_owned_by_another_installation_is_not_reused(self):
        root = Path(prepare(self.config)['catalog_root'])
        marker = root / '.publisher-catalog.json'
        owner = json.loads(marker.read_text())
        owner['installation_id'] = 'other'
        marker.write_text(json.dumps(owner))
        with self.assertRaises(UserError):
            prepare(self.config)
        self.assertEqual(json.loads(marker.read_text())['installation_id'], 'other')
        self.assert_data_preserved()
