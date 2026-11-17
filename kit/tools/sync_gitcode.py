#!/usr/bin/env python3
"""Mirror Git refs and published Store releases, rebinding catalogs to GitCode.

GitHub remains authoritative. Packages are downloaded from its published
releases, never rebuilt. The generated store-catalog branch is the last write.
"""
import argparse
import base64
from concurrent.futures import as_completed, ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

import publish_store as publish

REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
CATALOG_BRANCH = 'store-catalog'
MAX_ASSET = 200 * 1024 * 1024
API_INTERVAL = 1.1
_api_lock = threading.Lock()
_api_next = 0.0


class ApiError(RuntimeError):
    def __init__(self, status, path):
        self.status = status
        super().__init__('API HTTP %s: %s' % (status, path.split('?')[0]))


def request_bytes(url, method='GET', data=None, headers=None, token=None, limit=MAX_ASSET, timeout=120):
    """Keep API credentials off redirects, object storage and exception messages."""
    req = Request(url, data=data, method=method,
                  headers=dict({'User-Agent': 'idrive7-apps-cn-sync'}, **(headers or {})))
    if token:
        req.add_unredirected_header('Authorization', 'Bearer ' + token)
    for attempt in range(3):
        if urlsplit(url).hostname in ('api.gitcode.com', 'api.atomgit.com'):
            global _api_next
            with _api_lock:
                now = time.monotonic()
                delay = max(0, _api_next - now)
                _api_next = max(now, _api_next) + API_INTERVAL
            if delay:
                time.sleep(delay)
        retry_delay = attempt + 1
        try:
            with urlopen(req, timeout=timeout) as response:
                body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError('Response exceeds size limit')
            return body
        except HTTPError as error:
            # A 429 rejects the request. Other failed mutations may already have succeeded.
            if (method != 'GET' and error.code != 429) or error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise ApiError(error.code, urlsplit(url).path) from None
            if error.code == 429:
                after = error.headers.get('Retry-After', '60')
                retry_delay = min(60, max(1, int(after))) if after.isdigit() else 60
                print('GitCode API rate limit; waiting before retry', flush=True)
        except (OSError, URLError):
            if method != 'GET' or attempt == 2:
                raise RuntimeError('Network request failed: ' + urlsplit(url).hostname) from None
        time.sleep(retry_delay)


def checked_download(url, size, sha256):
    if urlsplit(url).scheme != 'https':
        raise ValueError('Download must use HTTPS')
    data = request_bytes(url, limit=size)
    if len(data) != size or publish.digest(data) != sha256:
        raise ValueError('Published download does not match its size and SHA256')
    return data


class Api:
    def __init__(self, base, repository, token):
        if not REPO.fullmatch(repository):
            raise ValueError('Repository must be owner/repo')
        self.base, self.repository, self.token = base, repository, token
        self.root = '/repos/' + repository

    def call(self, path, method='GET', body=None):
        data = None if body is None else publish.json_bytes(body)
        result = request_bytes(self.base + path, method, data,
                               {'Accept': 'application/json', 'Content-Type': 'application/json',
                                'Cache-Control': 'no-cache'}, self.token, limit=4 * 1024 * 1024)
        return json.loads(result) if result else None

    def release(self, tag):
        return self.call(self.root + '/releases/tags/' + quote(tag, safe=''))


class GitHub(Api):
    def __init__(self, repository, token):
        super().__init__('https://api.github.com', repository, token)

    def releases(self):
        result, page = [], 1
        while True:
            batch = self.call(self.root + '/releases?' + urlencode({'per_page': 100, 'page': page}))
            result.extend(r for r in batch if not r['draft'])
            if len(batch) < 100:
                return result
            page += 1

    def asset_bytes(self, asset):
        if asset['size'] > MAX_ASSET:
            raise ValueError('GitHub asset exceeds size limit: ' + asset['name'])
        data = request_bytes(asset['url'], headers={'Accept': 'application/octet-stream'},
                             token=self.token, limit=asset['size'])
        if len(data) != asset['size']:
            raise ValueError('Incomplete GitHub asset: ' + asset['name'])
        return data

    def catalog_bytes(self):
        assets = [a for a in self.release('catalog')['assets'] if a['name'] == 'catalog.json']
        if len(assets) != 1:
            raise ValueError('GitHub catalog release must have one catalog.json')
        return self.asset_bytes(assets[0])


class GitCode(Api):
    def __init__(self, repository, token, host='gitcode.com'):
        if host not in ('gitcode.com', 'atomgit.com'):
            raise ValueError('GitCode host must be gitcode.com or atomgit.com')
        super().__init__('https://api.' + host + '/api/v5', repository, token)
        self.host = host
        self.release_assets = {}

    def ensure_release(self, source):
        tag = source['tag_name']
        body = {'name': source['name'] or tag, 'body': source.get('body') or ''}
        if source.get('prerelease'):
            body['release_status'] = 'pre'
        try:
            existing = self.release(tag)
        except ApiError as error:
            if error.status != 404:
                raise
            body.update(tag_name=tag, target_commitish=source['target_commitish'])
            existing = self.call(self.root + '/releases', 'POST', body)
        else:
            self.call(self.root + '/releases/' + quote(tag, safe=''), 'PATCH', body)
        self.release_assets[tag] = existing['assets']

    def download_url(self, tag, name):
        # This documented endpoint is stable; OBS upload signatures must never enter a catalog.
        return (self.base + self.root + '/releases/' + quote(tag, safe='') +
                '/attach_files/' + quote(name, safe='') + '/download')

    def upload_asset(self, tag, name, data, replace=False):
        digest = publish.digest(data)
        if tag not in self.release_assets:
            self.release_assets[tag] = self.release(tag)['assets']
        existing = [a for a in self.release_assets[tag] if a['name'] == name]
        if len(existing) > 1:
            raise ValueError('Duplicate GitCode attachment: ' + name)
        url = self.download_url(tag, name)
        if existing:
            try:
                checked_download(url, len(data), digest)
                print('Already mirrored: %s/%s' % (tag, name), flush=True)
                return url
            except ValueError:
                if not replace:
                    raise ValueError('Refusing to replace a different published package: ' + name) from None
            asset = existing[0]
            if 'id' not in asset:
                raise ValueError('GitCode did not return the attachment id needed to update ' + name)
            self.call(self.root + '/releases/' + quote(tag, safe='') +
                      '/attach_files/' + quote(str(asset['id']), safe=''), 'DELETE')
        for attempt in range(2):
            upload = self.call(self.root + '/releases/' + quote(tag, safe='') + '/upload_url?' +
                               urlencode({'file_name': name}))
            if urlsplit(upload['url']).scheme != 'https':
                raise ValueError('Upload URL must use HTTPS')
            try:
                request_bytes(upload['url'], 'PUT', data, upload['headers'], timeout=900)
                break
            except ApiError:
                raise
            except RuntimeError as error:
                # A lost response can still mean a successful upload. Check before retrying.
                try:
                    checked_download(url, len(data), digest)
                    return url
                except ApiError as missing:
                    if missing.status != 404 or attempt == 1:
                        raise error
                print('Retrying interrupted upload: %s/%s' % (tag, name), flush=True)
        # The object storage callback can take a moment to publish the attachment.
        for attempt in range(5):
            try:
                checked_download(url, len(data), digest)
                print('Mirrored and verified: %s/%s' % (tag, name), flush=True)
                return url
            except ApiError as error:
                if error.status != 404 or attempt == 4:
                    raise
            time.sleep(2)


class GitRefs:
    def __init__(self, folder, source, target, source_token, target_token, host, username):
        self.folder, self.target = Path(folder), target
        self.source_url = 'https://github.com/' + source + '.git'
        self.target_url = 'https://' + host + '/' + target + '.git'
        self.source_token, self.target_token = source_token, target_token
        self.host, self.username = host, username
        self.git('init', '--bare', str(self.folder))

    def git(self, *args, host=None, input=None):
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never',
                   GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='credential.helper', GIT_CONFIG_VALUE_0='',
                   GIT_CONFIG_KEY_1='http.followRedirects', GIT_CONFIG_VALUE_1='false')
        if host:
            token = self.source_token if host == 'github.com' else self.target_token
            username = 'x-access-token' if host == 'github.com' else self.username
            auth = base64.b64encode((username + ':' + token).encode()).decode()
            env.update(GIT_CONFIG_COUNT='3', GIT_CONFIG_KEY_2='http.https://' + host + '/.extraHeader',
                       GIT_CONFIG_VALUE_2='Authorization: Basic ' + auth)
        result = subprocess.run(['git', '--git-dir=' + str(self.folder), *args], input=input,
                                capture_output=True, env=env)
        if result.returncode:
            # Git errors may include credentialed redirect URLs. Keep those out of logs.
            raise RuntimeError('Git %s failed (exit %d); check repository access, branch protection and divergent refs' %
                               (args[0], result.returncode))
        return result.stdout

    def mirror(self):
        self.git('fetch', '--no-tags', self.source_url, '+refs/heads/*:refs/sync/heads/*',
                 '+refs/tags/*:refs/sync/tags/*', host='github.com')
        refs = self.git('for-each-ref', '--format=%(refname)', 'refs/sync/').decode().splitlines()
        pushes = []
        for ref in refs:
            target = ref.replace('refs/sync/', 'refs/', 1)
            if target == 'refs/heads/' + CATALOG_BRANCH:
                raise ValueError('GitHub branch conflicts with generated ' + CATALOG_BRANCH)
            pushes.append(ref + ':' + target)
        # Ordinary pushes preserve divergent target branches/tags and never delete target refs.
        if pushes:
            self.git('push', self.target_url, *pushes, host=self.host)
        print('Mirrored %d Git branches and tags' % len(pushes), flush=True)

    def ref_sha(self, tag):
        return self.git('rev-parse', 'refs/sync/tags/' + tag + '^{commit}').decode().strip()

    def entry_state(self):
        ref = 'refs/heads/' + CATALOG_BRANCH
        rows = self.git('ls-remote', self.target_url, ref, host=self.host).decode().splitlines()
        if not rows:
            return None, None
        old = rows[0].split()[0]
        self.git('fetch', '--no-tags', self.target_url, ref, host=self.host)
        data = self.git('show', old + ':catalog.json')
        return old, data

    def publish_entry(self, catalog, old):
        blob = self.git('hash-object', '-w', '--stdin', input=catalog).decode().strip()
        tree = self.git('mktree', input=('100644 blob %s\tcatalog.json\n' % blob).encode()).decode().strip()
        args = ['-c', 'user.name=Store mirror', '-c', 'user.email=store-mirror@users.noreply.github.com',
                'commit-tree', tree, '-m', 'Update verified GitCode Store catalog']
        if old:
            args.extend(['-p', old])
        commit = self.git(*args).decode().strip()
        ref = 'refs/heads/' + CATALOG_BRANCH
        self.git('push', '--force-with-lease=' + ref + ':' + (old or ''), self.target_url,
                 commit + ':' + ref, host=self.host)


def rewrite_catalog(data, urls):
    catalog = json.loads(data)
    publish.validate_catalog(catalog)
    result = copy.deepcopy(catalog)
    for c in result['components']:
        c['artifact']['url'] = urls[c['artifact']['url']]
        if c.get('icon'):
            c['icon'] = urls[c['icon']]
    return publish.json_bytes(result)


def verify_catalog(data):
    catalog = json.loads(data)
    publish.validate_catalog(catalog)
    for c in catalog['components']:
        a = c['artifact']
        checked_download(a['url'], a['size'], a['sha256'])
        if c.get('icon'):
            icon = request_bytes(c['icon'], limit=128 * 1024)
            if publish.digest(icon) != c['iconSha256']:
                raise ValueError('Mirrored icon does not match catalog: ' + c['id'])


def sync_releases(source, target, refs, folder, all_releases=False):
    releases = source.releases()
    entry = next((r for r in releases if r['tag_name'] == 'catalog'), None)
    if entry is None:
        raise ValueError('GitHub has no published catalog release')
    entry_assets = [a for a in entry['assets'] if a['name'] == 'catalog.json']
    if len(entry_assets) != 1:
        raise ValueError('GitHub catalog release must have one catalog.json')
    source_entry = source.asset_bytes(entry_assets[0])
    if not all_releases:
        # Automatic sync follows the published source, without backfilling unrelated history.
        catalog = json.loads(source_entry)
        publish.validate_catalog(catalog)
        owners = {}
        for release in releases:
            for asset in release['assets']:
                owners[asset['url']] = owners[asset['browser_download_url']] = release['tag_name']
        needed = {'catalog'}
        for c in catalog['components']:
            needed.add(owners[c['artifact']['url']])
            if c.get('icon'):
                needed.add(owners[c['icon']])
        releases = [r for r in releases if r['tag_name'] in needed]
    # Files are copied before catalogs, including historical releases that may reference one another.
    records, known, cached = [], {}, {}
    for release in sorted(releases, key=lambda r: (r['tag_name'] == 'catalog', r['published_at'] or '')):
        for asset in release['assets']:
            path = folder / ('asset-%d' % len(records))
            key = (asset.get('digest'), asset['size'])
            if key[0] and key in cached:
                data = cached[key].read_bytes()
            else:
                data = source_entry if asset['id'] == entry_assets[0]['id'] else source.asset_bytes(asset)
                if key[0]:
                    if key[0] != 'sha256:' + publish.digest(data):
                        raise ValueError('GitHub asset digest mismatch: ' + asset['name'])
                    cached[key] = path
            path.write_bytes(data)
            record = (release, asset, path)
            records.append(record)
            known[asset['url']] = known[asset['browser_download_url']] = record
    fixed = [r for r in records if r[0]['tag_name'] == 'catalog' and r[1]['name'] == 'catalog.json']
    if len(fixed) != 1:
        raise ValueError('GitHub catalog release must have one catalog.json')
    for _, asset, path in records:
        if asset['name'] == 'catalog.json':
            doc = json.loads(path.read_bytes())
            publish.validate_catalog(doc)
            for c in doc['components']:
                artifact = c['artifact']
                record = known[artifact['url']]
                data = record[2].read_bytes()
                if len(data) != artifact['size'] or publish.digest(data) != artifact['sha256']:
                    raise ValueError('GitHub package differs from catalog: ' + c['id'])
                if c.get('icon') and publish.digest(known[c['icon']][2].read_bytes()) != c['iconSha256']:
                    raise ValueError('GitHub icon differs from catalog: ' + c['id'])
    old, previous = refs.entry_state()
    if previous:
        # Check before any target attachment is replaced, especially the fixed catalog snapshot.
        (folder / 'catalog.json').write_bytes(source_entry)
        publish.check_update(folder, json.loads(previous))
    for release in releases:
        metadata = dict(release, target_commitish=refs.ref_sha(release['tag_name']))
        target.ensure_release(metadata)
    urls = {}

    def upload(record):
        release, asset, path = record
        return asset, target.upload_asset(release['tag_name'], asset['name'], path.read_bytes())

    binaries = [r for r in records if r[1]['name'] != 'catalog.json']
    errors = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(upload, record) for record in binaries]
        for future in as_completed(futures):
            try:
                asset, url = future.result()
                urls[asset['url']] = urls[asset['browser_download_url']] = url
            except (RuntimeError, ValueError) as error:
                print('Attachment sync failed: ' + str(error), flush=True)
                errors.append(error)
    if errors:
        raise errors[0]
    # Check that the source entry is still current before changing either fixed entry.
    if source.catalog_bytes() != source_entry:
        raise ValueError('GitHub catalog changed during sync; rerun with the new release')
    output = None
    for release, asset, path in records:
        if asset['name'] != 'catalog.json':
            continue
        data = rewrite_catalog(path.read_bytes(), urls)
        verify_catalog(data)
        target.upload_asset(release['tag_name'], 'catalog.json', data, replace=True)
        if release['tag_name'] == 'catalog':
            output = data
    if source.catalog_bytes() != source_entry:
        raise ValueError('GitHub catalog changed during sync; keeping previous branch entry')
    if output != previous:
        refs.publish_entry(output, old)
    # Anonymous use is a requirement for the Store's generic HTTPS source.
    stable_url = ('https://api.' + target.host + '/api/v5/repos/' + target.repository +
                  '/raw/catalog.json?ref=' + CATALOG_BRANCH)
    checked_download(stable_url, len(output), publish.digest(output))
    print('Store HTTPS source: ' + stable_url, flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as file:
            file.write('GitCode Store HTTPS source: [%s](%s)\n' % (stable_url, stable_url))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--target', default=os.environ.get('GITCODE_REPOSITORY'))
    parser.add_argument('--host', choices=['gitcode.com', 'atomgit.com'], default=os.environ.get('GITCODE_HOST', 'gitcode.com'))
    parser.add_argument('--refs-only', action='store_true')
    parser.add_argument('--all-releases', action='store_true', help='also backfill historical releases; automatic sync only follows the current catalog')
    args = parser.parse_args()
    for name in ('source', 'target'):
        if not REPO.fullmatch(getattr(args, name) or ''):
            parser.error('--%s must be owner/repo' % name)
    gh_token, gc_token = os.environ.get('GH_TOKEN', ''), os.environ.get('GITCODE_TOKEN', '')
    if not gc_token:
        parser.error('GITCODE_TOKEN is required')
    with tempfile.TemporaryDirectory(prefix='store-gitcode-') as directory:
        folder = Path(directory)
        refs = GitRefs(folder / 'repo.git', args.source, args.target, gh_token, gc_token,
                       args.host, os.environ.get('GITCODE_USERNAME') or args.target.split('/')[0])
        refs.mirror()
        if not args.refs_only:
            sync_releases(GitHub(args.source, gh_token), GitCode(args.target, gc_token, args.host), refs, folder,
                          args.all_releases)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError) as error:
        raise SystemExit(str(error)) from None
