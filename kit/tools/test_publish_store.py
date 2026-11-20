#!/usr/bin/env python3
"""Exercise real SDK-free app/tool packages and the catalog's release binding, without a car or SDK."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
import warnings
from unittest.mock import patch

import build
import publish_store as publish


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
        spec = publish.app_spec(publish.ROOT / 'apps/service/app.json', 'zh-CN')
        version = publish.read_json(publish.ROOT / 'apps/service/app.json')['version']
        self.assertEqual(spec['version'], version)
        entry = publish.pack_app('service', spec, self.out, 'https://example.com/releases/7')
        with tarfile.open(self.out / entry['artifact']['name'], 'r:gz') as tf:
            names = tf.getnames()
            self.assertFalse(any('oap-sdk' in n for n in names))
            self.assertIn('package/javascript/app.js', names)
            self.assertIn('package/rhmiData/uiDescriptions/ID7/application.xml', names)
            self.assertIn('package/rhmiData/uiDescriptions/ID8/application.xml', names)
            for name in ('package/package.json', 'package/npm-shrinkwrap.json', 'package/javascript/kit/app-spec.json'):
                self.assertEqual(json.load(tf.extractfile(name))['version'], version)
            app = json.load(tf.extractfile('package/rhmiData/application.json'))
            self.assertEqual(app['application']['version'], version)
            locale = json.load(tf.extractfile('package/javascript/kit/locale.json'))
            self.assertEqual(locale['language'], 'zh-CN')
            self.assertTrue(locale['strings'])
            self.assertFalse(any(b'PRIVATE SDK SENTINEL' in tf.extractfile(n).read() for n in names))
        self.assertEqual(entry['requires']['idrive7-renderd'], spec['requires']['idrive7-renderd'])
        self.assertEqual(entry['mguHmi'], ['ID7', 'ID8'])
        self.assertNotIn('build', entry)
        first = (self.out / entry['artifact']['name']).read_bytes()
        second = publish.pack_app('service', spec, self.out, 'https://example.com/releases/8')
        self.assertEqual(first, (self.out / entry['artifact']['name']).read_bytes())
        self.assertEqual(second['version'], entry['version'])
        self.assertNotEqual(second['artifact']['url'], entry['artifact']['url'])

    def test_each_app_uses_its_own_source_version(self):
        versions = set()
        for path in (publish.ROOT / 'apps').glob('*/app.json'):
            spec = publish.app_spec(path, 'zh-CN')
            self.assertEqual(spec['version'], publish.read_json(path)['version'])
            self.assertRegex(spec['version'], publish.SEMVER)
            versions.add(spec['version'])
        self.assertGreater(len(versions), 1)

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
                   'browser_download_url': 'https://github.com/owner/repo/releases/download/cn-1.0.7-1/' + name}
                  for i, name in enumerate(('example@1.0.7.tgz', 'example.png'))]
        release = {'draft': False, 'tag_name': 'cn-1.0.7-1', 'assets': assets}
        for private in (False, True):
            (self.out / 'catalog.json').write_bytes(publish.json_bytes(catalog))
            publish.bind_release(self.out, release, private)
            entry = publish.read_json(self.out / 'catalog.json')['components'][0]
            key = 'url' if private else 'browser_download_url'
            self.assertEqual(entry['artifact']['url'], assets[0][key])
            self.assertEqual(entry['icon'], assets[1][key])
        assets[0]['size'] = 100
        with self.assertRaises(ValueError):
            publish.bind_release(self.out, release)

    def test_draft_and_temporary_asset_urls_are_refused(self):
        component = {'id': 'example', 'kind': 'app', 'version': '1.0.7',
                     'artifact': {'name': 'example@1.0.7.tgz', 'size': 123, 'sha256': 'a' * 64}}
        (self.out / 'catalog.json').write_bytes(publish.json_bytes({'schema': 2, 'components': [component]}))
        asset = {'name': 'example@1.0.7.tgz', 'size': 123,
                 'browser_download_url': 'https://github.com/owner/repo/releases/download/untagged-123/example.tgz'}
        release = {'draft': True, 'tag_name': 'cn-1.0.7-1', 'assets': [asset]}
        with self.assertRaises(ValueError):
            publish.bind_release(self.out, release)
        release['draft'] = False
        with self.assertRaises(ValueError):
            publish.bind_release(self.out, release)

    def test_downloads_must_be_accessible_and_match_catalog(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'package')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            entry = {'id': 'example', 'kind': 'app', 'version': '1.0.7',
                     'artifact': {'name': 'example@1.0.7.tgz', 'size': 7,
                                  'url': 'http://127.0.0.1:%d/app.tgz' % server.server_port,
                                  'sha256': publish.digest(b'package')}}
            path = self.out / 'catalog.json'
            path.write_bytes(publish.json_bytes({'schema': 2, 'components': [entry]}))
            publish.verify_downloads(self.out)
            entry['artifact']['sha256'] = publish.digest(b'corrupt')
            path.write_bytes(publish.json_bytes({'schema': 2, 'components': [entry]}))
            with patch.object(publish.time, 'sleep'), self.assertRaises(ValueError):
                publish.verify_downloads(self.out)
            with patch.object(publish, 'urlopen', side_effect=OSError('404')), \
                    patch.object(publish.time, 'sleep'), self.assertRaises(OSError):
                publish.verify_downloads(self.out)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

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

    def test_app_version_migration_is_allowed_only_once(self):
        previous = {'schema': 2, 'components': [
            {'id': 'example', 'kind': 'app', 'version': '1.0.2', 'artifact': {'sha256': 'a' * 64}}]}
        catalog = copy.deepcopy(previous)
        catalog['versionPolicy'] = 'app-json'
        catalog['components'][0].update(version='0.9.6', artifact={'sha256': 'b' * 64})
        (self.out / 'catalog.json').write_bytes(publish.json_bytes(catalog))
        publish.check_update(self.out, previous)
        previous['versionPolicy'] = 'app-json'
        with self.assertRaises(ValueError):
            publish.check_update(self.out, previous)
        del previous['versionPolicy']
        previous['components'][0]['kind'] = 'tool'
        catalog['components'][0]['kind'] = 'tool'
        (self.out / 'catalog.json').write_bytes(publish.json_bytes(catalog))
        with self.assertRaises(ValueError):
            publish.check_update(self.out, previous)

    def versioned_entry(self, kind='app'):
        data = b'published package'
        icon = b'published icon'
        return {'id': 'example', 'kind': kind, 'version': '1.2.3', 'title': 'Published title',
                'icon': 'https://example.com/old/example.png', 'iconSha256': publish.digest(icon),
                'artifact': {'name': 'example@1.2.3.tgz', 'size': len(data),
                             'sha256': publish.digest(data),
                             'url': 'https://example.com/old/example@1.2.3.tgz'}}, data, icon

    def test_same_app_version_reuses_published_bytes_and_metadata(self):
        before, package, icon = self.versioned_entry()
        previous = {'schema': 2, 'versionPolicy': 'app-json', 'components': [before]}
        catalog = copy.deepcopy(previous)
        current = catalog['components'][0]
        current['title'] = 'Changed title'
        current['artifact'].update(sha256=publish.digest(b'changed runtime'), size=15)
        current['iconSha256'] = publish.digest(b'changed icon')
        (self.out / 'catalog.json').write_bytes(publish.json_bytes(catalog))
        (self.out / before['artifact']['name']).write_bytes(b'changed runtime')
        (self.out / 'example.png').write_bytes(b'changed icon')
        with patch.object(publish, 'download_file', side_effect=[package, icon]) as download:
            publish.reconcile_versions(self.out, previous)
            self.assertEqual(download.call_count, 2)
        self.assertEqual((self.out / before['artifact']['name']).read_bytes(), package)
        self.assertEqual((self.out / 'example.png').read_bytes(), icon)
        self.assertEqual(publish.read_json(self.out / 'catalog.json'), previous)
        assets = [{'name': name, 'size': len(data),
                   'browser_download_url': 'https://github.com/owner/repo/releases/download/cn-build-9-1/' + name}
                  for name, data in ((before['artifact']['name'], package), ('example.png', icon))]
        publish.bind_release(self.out, {'draft': False, 'tag_name': 'cn-build-9-1', 'assets': assets})
        rebound = publish.read_json(self.out / 'catalog.json')['components'][0]
        self.assertEqual(rebound['artifact']['url'], assets[0]['browser_download_url'])
        self.assertEqual(rebound['icon'], assets[1]['browser_download_url'])

    def test_higher_app_version_uses_new_package(self):
        before, _, _ = self.versioned_entry()
        previous = {'schema': 2, 'versionPolicy': 'app-json', 'components': [before]}
        catalog = copy.deepcopy(previous)
        catalog['components'][0].update(version='1.2.4', artifact={'sha256': 'b' * 64})
        (self.out / 'catalog.json').write_bytes(publish.json_bytes(catalog))
        with patch.object(publish, 'download_file') as download:
            publish.reconcile_versions(self.out, previous)
            download.assert_not_called()
        self.assertEqual(publish.read_json(self.out / 'catalog.json'), catalog)

    def tool_entry(self, version='1.2.3', payload=b'old binary'):
        path = self.out / ('example@' + version + '.tgz')
        publish.pack(path, [('component.json', publish.json_bytes({
            'id': 'example', 'kind': 'tool', 'version': version, 'postInstall': ['install.sh']}), 0o644),
            ('files/binary', payload, 0o755)])
        return {'id': 'example', 'kind': 'tool', 'version': version,
                'artifact': publish.artifact(path, 'https://example.com/releases/7')}, path.read_bytes()

    def test_tool_content_change_gets_a_patch_version_and_matching_manifest(self):
        before, data = self.tool_entry()
        previous = {'schema': 2, 'versionPolicy': 'app-json', 'components': [before]}
        current, _ = self.tool_entry(payload=b'new binary')
        (self.out / 'catalog.json').write_bytes(publish.json_bytes({'schema': 2, 'components': [current]}))
        with patch.object(publish, 'download_file', return_value=data):
            publish.reconcile_versions(self.out, previous)
        entry = publish.read_json(self.out / 'catalog.json')['components'][0]
        self.assertEqual(entry['version'], '1.2.4')
        self.assertEqual(entry['sourceVersion'], '1.2.3')
        self.assertFalse((self.out / 'example@1.2.3.tgz').exists())
        path = self.out / 'example@1.2.4.tgz'
        self.assertEqual(entry['artifact']['sha256'], publish.digest(path.read_bytes()))
        with tarfile.open(path, 'r:gz') as tf:
            self.assertEqual(json.load(tf.extractfile('component.json'))['version'], '1.2.4')
            self.assertEqual(tf.extractfile('files/binary').read(), b'new binary')
            self.assertEqual(tf.getmember('files/binary').mode, 0o755)
        publish.check_update(self.out, previous)

    def test_repeated_tool_build_keeps_published_version_and_exact_bytes(self):
        before, data = self.tool_entry('1.2.4')
        before['sourceVersion'] = '1.2.3'
        current, _ = self.tool_entry()
        with patch.object(publish, 'download_file', return_value=data):
            entry = publish.reconcile_tool(self.out, current, before)
        self.assertEqual(entry, before)
        self.assertEqual((self.out / entry['artifact']['name']).read_bytes(), data)
        self.assertFalse((self.out / current['artifact']['name']).exists())

    def test_upstream_tool_version_catches_up_without_replacing_same_version(self):
        before, data = self.tool_entry('1.2.4')
        before['sourceVersion'] = '1.2.3'
        current, _ = self.tool_entry('1.2.4', payload=b'new upstream binary')
        with patch.object(publish, 'download_file', return_value=data):
            entry = publish.reconcile_tool(self.out, current, before)
        self.assertEqual(entry['version'], '1.2.5')
        self.assertEqual(entry['sourceVersion'], '1.2.4')

    def test_higher_upstream_tool_version_is_preserved(self):
        before, _ = self.tool_entry()
        current, data = self.tool_entry('1.3.0')
        with patch.object(publish, 'download_file') as download:
            entry = publish.reconcile_tool(self.out, current, before)
        download.assert_not_called()
        self.assertEqual(entry['version'], '1.3.0')
        self.assertEqual((self.out / entry['artifact']['name']).read_bytes(), data)

    def test_older_upstream_tool_version_is_still_refused(self):
        before, _ = self.tool_entry('1.2.5')
        before['sourceVersion'] = '1.2.4'
        current, _ = self.tool_entry()
        with self.assertRaisesRegex(ValueError, 'older source version'):
            publish.reconcile_tool(self.out, current, before)

    def test_tool_hook_and_file_mode_changes_count_as_content_changes(self):
        _, data = self.tool_entry()
        members = publish.tool_members(data)
        fingerprint = publish.tool_content(members)
        members[1] = (members[1][0], members[1][1], 0o644)
        self.assertNotEqual(fingerprint, publish.tool_content(members))
        members = publish.tool_members(data)
        manifest = json.loads(members[0][1])
        manifest['postInstall'] = ['new-hook.sh']
        members[0] = (members[0][0], publish.json_bytes(manifest), members[0][2])
        self.assertNotEqual(fingerprint, publish.tool_content(members))

    def test_unavailable_previous_package_stops_republishing(self):
        before, _, _ = self.versioned_entry()
        previous = {'schema': 2, 'versionPolicy': 'app-json', 'components': [before]}
        catalog = copy.deepcopy(previous)
        catalog['components'][0]['artifact']['sha256'] = 'b' * 64
        original = publish.json_bytes(catalog)
        (self.out / 'catalog.json').write_bytes(original)
        with patch.object(publish, 'download_file', side_effect=OSError('missing package')), \
                self.assertRaises(OSError):
            publish.reconcile_versions(self.out, previous)
        self.assertEqual((self.out / 'catalog.json').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
