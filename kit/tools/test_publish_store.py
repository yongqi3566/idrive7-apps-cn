#!/usr/bin/env python3
"""Exercise real SDK-free app/tool packages and the catalog's release binding, without a car or SDK."""
import copy
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import warnings
from unittest.mock import patch

import build
import publish_store as publish
from localize import localize_spec


class StoreReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / 'release'
        self.out.mkdir()
        self.dist = self.root / 'dist'
        self.sdk = self.root / 'sdk'
        self.sdk.mkdir()
        for name in ('index.js', 'package.json'):
            (self.sdk / name).write_text('PRIVATE SDK SENTINEL', encoding='utf-8')
        self.patches = patch.multiple(build, ROOT=str(self.root), DIST=str(self.dist), SDK=str(self.sdk))
        self.patches.start()
        self.addCleanup(self.patches.stop)
        warning_context = warnings.catch_warnings()
        warning_context.__enter__()
        self.addCleanup(warning_context.__exit__, None, None, None)
        warnings.filterwarnings('ignore', category=ResourceWarning, module='build')

    def test_app_is_installable_chinese_and_sdk_free(self):
        build.build('service', 'zh-CN', without_sdk=True)
        spec = localize_spec(publish.read_json(publish.ROOT / 'apps/service/app.json'), 'zh-CN')
        entry = publish.pack_app('service', spec, '1.0.7', self.out, 'https://example.com/releases/7')
        with tarfile.open(self.out / entry['artifact']['name'], 'r:gz') as tf:
            names = tf.getnames()
            self.assertFalse(any('oap-sdk' in n for n in names))
            self.assertIn('package/javascript/app.js', names)
            self.assertIn('package/rhmiData/uiDescriptions/ID7/application.xml', names)
            self.assertIn('package/rhmiData/uiDescriptions/ID8/application.xml', names)
            for name in ('package/package.json', 'package/npm-shrinkwrap.json', 'package/javascript/kit/app-spec.json'):
                self.assertEqual(json.load(tf.extractfile(name))['version'], '1.0.7')
            app = json.load(tf.extractfile('package/rhmiData/application.json'))
            self.assertEqual(app['application']['version'], '1.0.7')
            locale = json.load(tf.extractfile('package/javascript/kit/locale.json'))
            self.assertEqual(locale['language'], 'zh-CN')
            self.assertTrue(locale['strings'])
            self.assertFalse(any(b'PRIVATE SDK SENTINEL' in tf.extractfile(n).read() for n in names))
        self.assertEqual(entry['requires']['idrive7-renderd'], spec['requires']['idrive7-renderd'])
        self.assertEqual(entry['mguHmi'], ['ID7', 'ID8'])
        self.assertNotIn('build', entry)
        first = (self.out / entry['artifact']['name']).read_bytes()
        publish.pack_app('service', spec, '1.0.7', self.out, 'https://example.com/releases/7')
        self.assertEqual(first, (self.out / entry['artifact']['name']).read_bytes())

    def test_build_defaults_still_require_and_embed_sdk(self):
        build.build('service')
        version = publish.read_json(publish.ROOT / 'apps/service/app.json')['version']
        with tarfile.open(self.dist / ('service@' + version + '.tgz'), 'r:gz') as tf:
            self.assertEqual(tf.extractfile('package/node_modules/oap-sdk/index.js').read(), b'PRIVATE SDK SENTINEL')
        with patch.object(build, 'SDK', str(self.root / 'missing')):
            with self.assertRaises(SystemExit):
                build.build('service')
            build.build('service', without_sdk=True)

    def test_tools_preserve_hooks_modes_and_hashes(self):
        catalog = {'schema': 2, 'components': []}
        for definition in publish.read_json(publish.ROOT / 'mgu-repo.json')['tools']:
            entry = publish.pack_tool(definition, self.out, 'https://example.com/releases/7')
            catalog['components'].append(entry)
            with tarfile.open(self.out / entry['artifact']['name'], 'r:gz') as tf:
                manifest = json.load(tf.extractfile('component.json'))
                for f in manifest['files']:
                    member = tf.getmember('files/' + f['path'])
                    self.assertEqual(member.mode, int(f['mode'], 8))
                    self.assertEqual(publish.digest(tf.extractfile(member).read()), f['sha256'])
                for d in manifest.get('dropins', []):
                    self.assertIn('dropins/' + d['unit'] + '.d/' + d['name'], tf.getnames())
                if manifest['id'] == 'idrive7-renderd':
                    self.assertEqual(manifest['postInstall'], ['idrive7-renderd-install.sh'])
                    self.assertEqual(manifest['mguProvides'], 'renderer')
        publish.validate_catalog(catalog)
        broken = copy.deepcopy(catalog)
        broken['components'][0]['version'] = '1.0.0-zh.cn.2'
        with self.assertRaises(ValueError):
            publish.validate_catalog(broken)
        broken = copy.deepcopy(catalog)
        broken['components'] = [broken['components'][1]]
        with self.assertRaises(ValueError):
            publish.validate_catalog(broken)

    def test_public_and_private_release_binding(self):
        component = {'id': 'example', 'kind': 'app', 'version': '1.0.7',
                     'icon': 'https://example.com/example.png',
                     'artifact': {'name': 'example@1.0.7.tgz', 'size': 123, 'sha256': 'a' * 64}}
        catalog = {'schema': 2, 'components': [component]}
        assets = [{'name': name, 'size': 123, 'url': 'https://api.github.com/assets/' + str(i),
                   'browser_download_url': 'https://github.com/download/' + name}
                  for i, name in enumerate(('example@1.0.7.tgz', 'example.png'))]
        for private in (False, True):
            (self.out / 'catalog.json').write_bytes(publish.json_bytes(catalog))
            publish.bind_release(self.out, {'assets': assets}, private)
            entry = publish.read_json(self.out / 'catalog.json')['components'][0]
            key = 'url' if private else 'browser_download_url'
            self.assertEqual(entry['artifact']['url'], assets[0][key])
            self.assertEqual(entry['icon'], assets[1][key])
        assets[0]['size'] = 100
        with self.assertRaises(ValueError):
            publish.bind_release(self.out, {'assets': assets})

    def test_catalog_update_refuses_old_runs_and_unversioned_changes(self):
        c = {'id': 'example', 'kind': 'app', 'version': '1.0.7',
             'artifact': {'sha256': 'a' * 64}}
        catalog = {'schema': 2, 'components': [c]}
        (self.out / 'catalog.json').write_bytes(publish.json_bytes(catalog))
        publish.check_update(self.out, catalog)
        old = copy.deepcopy(catalog)
        old['components'][0]['version'] = '1.0.6'
        publish.check_update(self.out, old)
        old['components'][0]['version'] = '1.0.8'
        with self.assertRaises(ValueError):
            publish.check_update(self.out, old)
        old['components'][0]['version'] = '1.0.7'
        old['components'][0]['artifact']['sha256'] = 'b' * 64
        with self.assertRaises(ValueError):
            publish.check_update(self.out, old)


if __name__ == '__main__':
    unittest.main()
