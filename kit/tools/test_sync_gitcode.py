#!/usr/bin/env python3
"""Check mirrors with actual Git repositories and a fake release service."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import sync_gitcode as sync


class Source:
    def __init__(self):
        self.package, self.icon = b'published package', b'published icon'
        self.catalog = {'schema': 2, 'versionPolicy': 'app-json', 'components': [
            {'id': 'example', 'kind': 'app', 'version': '1.2.3',
             'artifact': {'name': 'example@1.2.3.tgz', 'size': len(self.package),
                          'sha256': sync.publish.digest(self.package), 'url': 'https://github.com/package'},
             'icon': 'https://github.com/icon', 'iconSha256': sync.publish.digest(self.icon)}]}
        self.data = {'package': self.package, 'icon': self.icon, 'snapshot': self.catalog_bytes(),
                     'entry': self.catalog_bytes()}
        def asset(name, key):
            return {'id': key, 'name': name, 'url': 'https://api.github.com/' + key,
                    'browser_download_url': 'https://github.com/' + key, 'size': len(self.data[key])}
        self.list = [
            {'tag_name': 'catalog', 'published_at': '2026-01-02', 'name': 'Store source', 'body': '',
             'assets': [asset('catalog.json', 'entry')]},
            {'tag_name': 'cn-build-1-1', 'published_at': '2026-01-01', 'name': 'Apps', 'body': '',
             'assets': [asset('example@1.2.3.tgz', 'package'), asset('example.png', 'icon'),
                        asset('catalog.json', 'snapshot')]}]

    def releases(self):
        return copy.deepcopy(self.list)

    def asset_bytes(self, asset):
        return self.data[asset['url'].rsplit('/', 1)[1]]

    def catalog_bytes(self):
        return sync.publish.json_bytes(self.catalog)


class Target:
    host, repository = 'gitcode.com', 'owner/repo'

    def __init__(self):
        self.uploaded, self.metadata = {}, []

    def ensure_release(self, release):
        self.metadata.append(release)

    def upload_asset(self, tag, name, data, replace=False):
        self.uploaded[(tag, name)] = data
        return 'https://api.gitcode.com/' + tag + '/' + name


class Refs:
    def __init__(self):
        self.previous, self.published = None, []

    def ref_sha(self, tag):
        return 'a' * 40

    def entry_state(self):
        return ('b' * 40 if self.previous else None), self.previous

    def publish_entry(self, data, old):
        self.published.append((data, old))


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source, self.target, self.refs = Source(), Target(), Refs()

    def test_catalog_rebinding_changes_only_urls(self):
        original = self.source.catalog_bytes()
        result = json.loads(sync.rewrite_catalog(original, {
            'https://github.com/package': 'https://api.gitcode.com/package',
            'https://github.com/icon': 'https://api.gitcode.com/icon'}))
        expected = copy.deepcopy(self.source.catalog)
        expected['components'][0]['artifact']['url'] = 'https://api.gitcode.com/package'
        expected['components'][0]['icon'] = 'https://api.gitcode.com/icon'
        self.assertEqual(result, expected)
        self.assertEqual(self.source.catalog_bytes(), original)
        with self.assertRaises(KeyError):
            sync.rewrite_catalog(original, {})

    def add_history(self):
        self.source.data['history'] = b'historical release'
        self.source.list.append({'tag_name': 'old', 'published_at': '2025-01-01', 'name': 'Old', 'body': '',
                                 'assets': [{'id': 'history', 'name': 'old.tgz', 'size': 18,
                                             'url': 'https://api.github.com/history',
                                             'browser_download_url': 'https://github.com/history'}]})

    def test_automatic_sync_does_not_backfill_history(self):
        self.add_history()
        with patch.object(sync, 'verify_catalog'), patch.object(sync, 'checked_download'), \
                patch.object(self.source, 'asset_bytes', wraps=self.source.asset_bytes) as download:
            sync.sync_releases(self.source, self.target, self.refs, self.folder)
        self.assertNotIn(('old', 'old.tgz'), self.target.uploaded)
        self.assertFalse(any(c.args[0]['id'] == 'history' for c in download.call_args_list))

    def test_explicit_backfill_mirrors_historical_releases(self):
        self.add_history()
        with patch.object(sync, 'verify_catalog'), patch.object(sync, 'checked_download'):
            sync.sync_releases(self.source, self.target, self.refs, self.folder, all_releases=True)
        self.assertEqual(self.target.uploaded[('old', 'old.tgz')], b'historical release')

    def test_published_bytes_are_mirrored_and_entry_is_last(self):
        with patch.object(sync, 'verify_catalog') as verify, patch.object(sync, 'checked_download') as check:
            sync.sync_releases(self.source, self.target, self.refs, self.folder)
        self.assertEqual(self.target.uploaded[('cn-build-1-1', 'example@1.2.3.tgz')], self.source.package)
        self.assertEqual(self.target.uploaded[('cn-build-1-1', 'example.png')], self.source.icon)
        output = self.target.uploaded[('catalog', 'catalog.json')]
        self.assertEqual(self.refs.published, [(output, None)])
        verify.assert_called_with(output)
        check.assert_called_with('https://api.gitcode.com/api/v5/repos/owner/repo/raw/catalog.json?ref=store-catalog',
                                 len(output), sync.publish.digest(output))
        self.assertEqual(self.target.metadata[0]['target_commitish'], 'a' * 40)

    def test_corrupt_source_stops_before_target_mutation(self):
        self.source.data['package'] = b'corrupt'
        with self.assertRaisesRegex(ValueError, 'differs from catalog'):
            sync.sync_releases(self.source, self.target, self.refs, self.folder)
        self.assertFalse(self.target.metadata)
        self.assertFalse(self.refs.published)

    def test_failed_target_download_keeps_previous_entry(self):
        with patch.object(sync, 'verify_catalog', side_effect=ValueError('corrupt target')), \
                self.assertRaises(ValueError):
            sync.sync_releases(self.source, self.target, self.refs, self.folder)
        self.assertNotIn(('catalog', 'catalog.json'), self.target.uploaded)
        self.assertFalse(self.refs.published)

    def test_source_changes_during_sync_keeps_previous_entry(self):
        with patch.object(self.source, 'catalog_bytes', return_value=b'new release'), \
                self.assertRaisesRegex(ValueError, 'changed during sync'):
            sync.sync_releases(self.source, self.target, self.refs, self.folder)
        self.assertFalse(self.refs.published)
        self.assertNotIn(('catalog', 'catalog.json'), self.target.uploaded)

    def test_older_versions_cannot_replace_current_entry(self):
        previous = copy.deepcopy(self.source.catalog)
        previous['components'][0]['version'] = '1.2.4'
        self.refs.previous = sync.publish.json_bytes(previous)
        with self.assertRaisesRegex(ValueError, 'older version'):
            sync.sync_releases(self.source, self.target, self.refs, self.folder)
        self.assertFalse(self.target.metadata)
        self.assertFalse(self.refs.published)

    def test_identical_entry_does_not_create_another_commit(self):
        with patch.object(sync, 'verify_catalog'), patch.object(sync, 'checked_download'):
            sync.sync_releases(self.source, self.target, self.refs, self.folder)
            self.refs.previous = self.refs.published.pop()[0]
            sync.sync_releases(self.source, self.target, self.refs, self.folder)
        self.assertFalse(self.refs.published)

    def test_existing_packages_are_verified_and_skipped(self):
        target = sync.GitCode('owner/repo', 'secret')
        with patch.object(target, 'release', return_value={'assets': [{'name': 'app.tgz'}]}), \
                patch.object(target, 'call') as call, patch.object(sync, 'checked_download') as check:
            url = target.upload_asset('v1', 'app.tgz', b'package')
        call.assert_not_called()
        check.assert_called_once_with(url, 7, sync.publish.digest(b'package'))

    def test_release_inventory_is_read_once_for_multiple_attachments(self):
        target = sync.GitCode('owner/repo', 'secret')
        with patch.object(target, 'release', return_value={'assets': [{'name': 'a.tgz'}, {'name': 'b.tgz'}]}) as release, \
                patch.object(target, 'call'), patch.object(sync, 'checked_download'):
            target.ensure_release({'tag_name': 'v1', 'name': 'Version 1', 'body': ''})
            target.upload_asset('v1', 'a.tgz', b'a')
            target.upload_asset('v1', 'b.tgz', b'b')
        release.assert_called_once_with('v1')

    def test_rate_rejection_waits_before_retrying_a_mutation(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, limit):
                return b'{}'
        error = HTTPError('https://api.gitcode.com/test', 429, 'rate limit', {'Retry-After': '30'}, None)
        with patch.object(sync, '_api_next', 0), patch.object(sync, 'API_INTERVAL', 0), \
                patch.object(sync, 'urlopen', side_effect=[error, Response()]) as open_url, \
                patch.object(sync.time, 'sleep') as sleep:
            self.assertEqual(sync.request_bytes('https://api.gitcode.com/test', 'POST', b'{}'), b'{}')
        self.assertEqual(open_url.call_count, 2)
        sleep.assert_called_once_with(30)

    def test_conflicting_package_is_never_overwritten(self):
        target = sync.GitCode('owner/repo', 'secret')
        with patch.object(target, 'release', return_value={'assets': [{'name': 'app.tgz', 'id': 7}]}), \
                patch.object(target, 'call') as call, \
                patch.object(sync, 'checked_download', side_effect=ValueError('hash mismatch')), \
                self.assertRaisesRegex(ValueError, 'Refusing to replace'):
            target.upload_asset('v1', 'app.tgz', b'package')
        call.assert_not_called()

    def test_catalog_replacement_uses_attachment_id_and_unsigned_public_url(self):
        target = sync.GitCode('owner/repo', 'secret')
        upload = {'url': 'https://storage.example/upload?signature=test', 'headers': {'x-obs-acl': 'public-read'}}
        with patch.object(target, 'release', return_value={'assets': [{'name': 'catalog.json', 'id': 7}]}), \
                patch.object(target, 'call', side_effect=[None, upload]) as call, \
                patch.object(sync, 'checked_download', side_effect=[ValueError('old'), b'new']), \
                patch.object(sync, 'request_bytes') as request:
            url = target.upload_asset('catalog', 'catalog.json', b'new', replace=True)
        self.assertEqual(call.call_args_list[0].args, ('/repos/owner/repo/releases/catalog/attach_files/7', 'DELETE'))
        request.assert_called_once_with(upload['url'], 'PUT', b'new', upload['headers'], timeout=900)
        self.assertNotIn('signature', url)
        self.assertTrue(url.endswith('/catalog/attach_files/catalog.json/download'))

    def test_interrupted_upload_retries_only_after_confirming_absence(self):
        target = sync.GitCode('owner/repo', 'secret')
        upload = {'url': 'https://storage.example/upload', 'headers': {}}
        with patch.object(target, 'release', return_value={'assets': []}), \
                patch.object(target, 'call', return_value=upload) as call, \
                patch.object(sync, 'request_bytes', side_effect=[RuntimeError('timeout'), b'']) as request, \
                patch.object(sync, 'checked_download', side_effect=[sync.ApiError(404, '/download'), b'package']):
            target.upload_asset('v1', 'app.tgz', b'package')
        self.assertEqual(request.call_count, 2)
        self.assertEqual(call.call_count, 2)

    def test_lost_upload_response_does_not_reupload_valid_attachment(self):
        target = sync.GitCode('owner/repo', 'secret')
        upload = {'url': 'https://storage.example/upload', 'headers': {}}
        with patch.object(target, 'release', return_value={'assets': []}), \
                patch.object(target, 'call', return_value=upload) as call, \
                patch.object(sync, 'request_bytes', side_effect=RuntimeError('lost response')) as request, \
                patch.object(sync, 'checked_download', return_value=b'package'):
            target.upload_asset('v1', 'app.tgz', b'package')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(call.call_count, 1)

    def test_transport_credentials_do_not_follow_redirects(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, limit):
                return b'{}'
        with patch.object(sync, 'urlopen', return_value=Response()) as open_url:
            sync.request_bytes('https://api.gitcode.com/test', token='secret')
        request = open_url.call_args.args[0]
        self.assertNotIn('Authorization', request.headers)
        self.assertEqual(request.unredirected_hdrs['Authorization'], 'Bearer secret')

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', *args], cwd=cwd, stderr=subprocess.DEVNULL).decode().strip()

    def test_git_mirror_and_generated_branch_with_real_repositories(self):
        source, target, work = self.folder / 'source.git', self.folder / 'target.git', self.folder / 'work'
        self.git('init', '--bare', str(source))
        self.git('init', '--bare', str(target))
        self.git('init', '-b', 'main', str(work))
        (work / 'README').write_text('source', encoding='utf-8')
        self.git('add', 'README', cwd=work)
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'Source', cwd=work)
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'tag', '-a', 'v1', '-m', 'Version 1', cwd=work)
        self.git('push', str(source), 'main', 'v1', cwd=work)
        refs = sync.GitRefs(self.folder / 'mirror.git', 'source/repo', 'target/repo', '', '', 'gitcode.com', 'owner')
        refs.source_url, refs.target_url = str(source), str(target)
        refs.mirror()
        sha = self.git('rev-parse', 'HEAD', cwd=work)
        self.assertEqual(refs.ref_sha('v1'), sha)
        self.assertEqual(self.git('--git-dir=' + str(target), 'rev-parse', 'main'), sha)
        self.assertEqual(refs.entry_state(), (None, None))
        data = self.source.catalog_bytes()
        refs.publish_entry(data, None)
        old, previous = refs.entry_state()
        self.assertEqual(previous, data)
        refs.mirror()
        self.assertEqual(refs.entry_state(), (old, data))
        with self.assertRaises(RuntimeError):
            refs.publish_entry(b'outdated writer', None)
        refs.publish_entry(b'updated', old)
        self.assertEqual(refs.entry_state()[1], b'updated')


if __name__ == '__main__':
    unittest.main()
