#!/usr/bin/env python3
"""Build SDK-free, fixed-language packages and a catalog for the MGU Store.

    python3 kit/tools/publish_store.py --release-tag cn-build-1-1 --repository owner/repo

The catalog release points at packages in a separate release. Updating its
catalog.json is the last publishing step, after all packages are available.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

import build
from localize import languages, localize_spec
from make_kit import prebuilt_hash, source_hash

ROOT = Path(build.ROOT)
SEMVER = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z')
ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z')
META = ('title', 'description', 'author', 'texts', 'requires', 'mguHmi',
        'optional', 'uninstallable', 'mguProvides', 'mguNeeds')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(name):
    p = PurePosixPath(name)
    if not name or '\\' in name or ':' in name or p.is_absolute() or '..' in p.parts or str(p) != name:
        raise ValueError('Unsafe package path: ' + name)
    return p


def pack(path, members):
    """Explicit modes and timestamps keep release packages independent of the build host."""
    seen = set()
    with build.gz_header_free(str(path)) as gz, tarfile.open(fileobj=gz, mode='w') as tf:
        for name, data, mode in sorted(members):
            safe_path(name)
            if name in seen:
                raise ValueError('Duplicate package path: ' + name)
            seen.add(name)
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tf.addfile(info, io.BytesIO(data))


def artifact(path, base_url):
    data = path.read_bytes()
    return {'name': path.name, 'size': len(data), 'sha256': digest(data),
            'url': base_url + '/' + quote(path.name, safe='')}


def pack_app(folder, spec, out, base_url):
    version = spec['version']
    if not SEMVER.fullmatch(version):
        raise ValueError('App version must be x.y.z: ' + spec['id'])
    src = Path(build.DIST) / folder / 'source'
    members = []
    # All identities in the package use the same Store-compatible version.
    for path in sorted(src.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(src).as_posix()
        if rel.startswith('node_modules/oap-sdk/'):
            raise ValueError('Release must not contain BMW SDK: ' + rel)
        data = path.read_bytes()
        if rel in ('package.json', 'npm-shrinkwrap.json', 'javascript/kit/app-spec.json',
                   'rhmiData/application.json', 'rhmiDataStartWidget/application.json'):
            doc = json.loads(data)
            (doc['application'] if 'application' in doc else doc)['version'] = version
            if rel == 'package.json':
                doc['mguHmi'] = spec['mguHmi']
            data = json_bytes(doc)
        members.append(('package/' + rel, data, 0o644))
    pkg = src / 'package.json'
    if not pkg.is_file() or not (src / 'rhmiData/application.json').is_file():
        raise ValueError('Not an installable app: ' + folder)
    path = out / (spec['id'] + '@' + version + '.tgz')
    pack(path, members)
    entry = {k: copy.deepcopy(spec[k]) for k in META if k in spec}
    entry.update(id=spec['id'], kind='app', version=version, uninstallable=True,
                 artifact=artifact(path, base_url))
    require_deploy(entry, '0.3.2')
    app = read_json(src / 'rhmiData/application.json')['application']
    icon = src / 'rhmiData/variantGroups/Common/images' / app['entryPoints'][0]['icon']
    data = icon.read_bytes()
    if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) > 128 * 1024:
        raise ValueError('Store icon must be a PNG of at most 128 KiB: ' + str(icon))
    name = spec['id'] + '.png'
    (out / name).write_bytes(data)
    entry.update(icon=base_url + '/' + quote(name), iconSha256=digest(data))
    return entry


def pack_tool(definition, out, base_url):
    folder = ROOT.joinpath(*safe_path(definition['path']).parts)
    manifest = read_json(folder / 'component.json')
    if manifest.get('kind') != 'tool' or manifest.get('build'):
        raise ValueError('Expected a prebuilt tool: ' + str(folder))
    members = []

    def take(source, target, mode=0o644):
        p = folder.joinpath(*safe_path(source).parts)
        data = p.read_bytes()
        members.append((target, data, mode))
        return data

    for f in manifest.get('files', []):
        safe_path(f['path'])
        if not re.fullmatch(r'[0-7]{3,4}', f['mode']):
            raise ValueError('Invalid file mode: ' + f['mode'])
        data = take(f.pop('from', f['path']), 'files/' + f['path'], int(f['mode'], 8))
        f['sha256'] = digest(data)
    for u in manifest.get('units', []):
        take(u.pop('from', u['name']), 'units/' + u['name'])
    for d in manifest.get('dropins', []):
        take(d.pop('from', d['name']), 'dropins/' + d['unit'] + '.d/' + d['name'])
    # This renderer also satisfies the controller gate's capability dependency.
    if manifest['id'] == 'idrive7-renderd':
        manifest['mguProvides'] = 'renderer'
    members.append(('component.json', json_bytes(manifest), 0o644))
    path = out / (manifest['id'] + '@' + manifest['version'] + '.tgz')
    pack(path, members)
    entry = {k: copy.deepcopy(manifest[k]) for k in META if k in manifest}
    entry.update({k: copy.deepcopy(definition[k]) for k in META if k in definition})
    if 'needs' in definition:
        entry['mguNeeds'] = definition['needs']
    entry.update(id=manifest['id'], kind='tool', version=manifest['version'], artifact=artifact(path, base_url))
    require_deploy(entry, '0.4.0' if entry.get('mguProvides') else '0.3.2')
    return entry


def require_deploy(entry, minimum):
    requires = entry.setdefault('requires', {})
    have = requires.get('mgu-deploy', '0.0.0')
    requires['mgu-deploy'] = max((have, minimum), key=lambda v: tuple(map(int, v.split('.'))))


def validate_catalog(catalog):
    entries = {}
    for c in catalog['components']:
        if not ID.fullmatch(c['id']) or not SEMVER.fullmatch(c['version']) or c['id'] in entries:
            raise ValueError('Invalid or duplicate component: ' + c['id'])
        if c.get('build'):
            raise ValueError('Release catalog must use prebuilt packages')
        entries[c['id']] = c
    for c in entries.values():
        for dep, minimum in c.get('requires', {}).items():
            if not SEMVER.fullmatch(minimum):
                raise ValueError('Invalid requirement: ' + dep)
            if dep == 'mgu-deploy':
                continue
            have = entries.get(dep)
            if not have or tuple(map(int, have['version'].split('.'))) < tuple(map(int, minimum.split('.'))):
                raise ValueError(c['id'] + ': missing requirement ' + dep + '>=' + minimum)
        for need in c.get('mguNeeds', []):
            if not any(e.get('mguProvides') == need for e in entries.values()):
                raise ValueError(c['id'] + ': missing capability ' + need)


def app_spec(path, language):
    raw = read_json(path)
    spec = localize_spec(raw, language)
    spec['version'] = raw['version']
    spec.setdefault('id', path.parent.name)
    spec.setdefault('mguHmi', ['ID7', 'ID8'])
    return spec


def build_release(release_tag, repository, language, out):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', release_tag):
        raise ValueError('Release tag must contain letters, numbers, dots, underscores or hyphens')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Repository must be owner/repo')
    if prebuilt_hash() != source_hash():
        raise ValueError('Renderer prebuilt does not match its source; run kit/tools/prepare_store_renderer.py before publishing')
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError('Release output directory must be empty: ' + str(out))
    # The workflow binds these URLs to the actual release assets before publishing the catalog.
    base_url = 'https://github.com/' + repository + '/releases/download/' + release_tag
    components = []
    for path in sorted((ROOT / 'apps').glob('*/app.json')):
        spec = app_spec(path, language)
        build.build(path.parent.name, language, without_sdk=True)
        components.append(pack_app(path.parent.name, spec, out, base_url))
    for definition in read_json(ROOT / 'mgu-repo.json')['tools']:
        components.append(pack_tool(definition, out, base_url))
    catalog = {'schema': 2, 'versionPolicy': 'app-json', 'components': components}
    validate_catalog(catalog)
    (out / 'catalog.json').write_bytes(json_bytes(catalog))
    print('Store release %s: %d components in %s' % (release_tag, len(components), out))


def bind_release(out, release, private=False):
    """Public download URLs work without a token; private assets need the authenticated API."""
    if release.get('draft') is not False:
        raise ValueError('Publish the release before binding its download URLs')
    assets = {a['name']: a for a in release['assets']}
    catalog = read_json(out / 'catalog.json')
    download_path = '/releases/download/' + quote(release['tag_name'], safe='') + '/'

    def asset_url(asset):
        if download_path not in urlsplit(asset['browser_download_url']).path:
            raise ValueError('Asset does not use the published release tag: ' + asset['name'])
        return asset['url' if private else 'browser_download_url']

    for c in catalog['components']:
        a = c['artifact']
        remote = assets[a['name']]
        if remote['size'] != a['size']:
            raise ValueError('Uploaded asset has the wrong size: ' + a['name'])
        a['url'] = asset_url(remote)
        if c.get('icon'):
            c['icon'] = asset_url(assets[c['id'] + '.png'])
    validate_catalog(catalog)
    (out / 'catalog.json').write_bytes(json_bytes(catalog))


def download_file(file):
    name, url, size, sha256 = file
    for attempt in range(3):
        try:
            req = Request(url, headers={'User-Agent': 'idrive7-apps-cn-publisher'})
            token = os.environ.get('GH_TOKEN')
            if token and urlsplit(url).hostname == 'api.github.com':
                req.add_header('Accept', 'application/octet-stream')
                # The token belongs to the API host, never its download redirect.
                req.add_unredirected_header('Authorization', 'Bearer ' + token)
            with urlopen(req, timeout=30) as response:
                data = response.read((size if size is not None else 128 * 1024) + 1)
            if (size is not None and len(data) != size) or digest(data) != sha256:
                raise ValueError('Download does not match catalog: ' + name)
            return data
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def verify_downloads(out):
    """Download every advertised file before publishing the catalog, checking its size and SHA256."""
    catalog = read_json(out / 'catalog.json')
    files = []
    for c in catalog['components']:
        a = c['artifact']
        files.append((a['name'], a['url'], a['size'], a['sha256']))
        if c.get('icon'):
            files.append((c['id'] + '.png', c['icon'], None, c['iconSha256']))

    def verify(file):
        download_file(file)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(verify, files))
    print('Verified %d published downloads (size and SHA256)' % len(files))


def check_update(out, previous):
    catalog = read_json(out / 'catalog.json')
    validate_catalog(catalog)
    old = {c['id']: c for c in previous['components']}
    old_app_versions = {c['version'] for c in previous['components'] if c['kind'] == 'app'}
    migration = (catalog.get('versionPolicy') == 'app-json' and previous.get('versionPolicy') is None and
                 len(old_app_versions) == 1 and re.fullmatch(r'1\.0\.(0|[1-9]\d*)', next(iter(old_app_versions))))
    for c in catalog['components']:
        before = old.get(c['id'])
        if not before:
            continue
        if tuple(map(int, c['version'].split('.'))) < tuple(map(int, before['version'].split('.'))):
            if not (migration and c['kind'] == 'app'):
                raise ValueError('Refusing to publish an older version of ' + c['id'])
            print('Migrating %s from build version %s to app.json version %s; an installed higher version needs reinstalling' %
                  (c['id'], before['version'], c['version']))
        if c['version'] == before['version'] and c['artifact']['sha256'] != before['artifact']['sha256']:
            raise ValueError('Package changed without a version bump: ' + c['id'])


def tool_members(data):
    members = []
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tf:
        for member in tf.getmembers():
            safe_path(member.name)
            if not member.isfile() or member.name in seen:
                raise ValueError('Invalid tool package member: ' + member.name)
            seen.add(member.name)
            members.append((member.name, tf.extractfile(member).read(), member.mode))
    if 'component.json' not in seen:
        raise ValueError('Tool package has no component.json')
    return members


def tool_content(members):
    """Compare installed files and hooks, ignoring only the package's version."""
    content = []
    for name, data, mode in sorted(members):
        if name == 'component.json':
            manifest = json.loads(data)
            del manifest['version']
            data = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode('utf-8')
        content.append((name, mode, digest(data)))
    return digest(json_bytes(content))


def reconcile_tool(out, current, before):
    source_version = current['version']
    version = lambda v: tuple(map(int, v.split('.')))
    if version(source_version) < version(before.get('sourceVersion', before['version'])):
        raise ValueError('Refusing to publish an older source version of ' + current['id'])
    current['sourceVersion'] = source_version
    if version(source_version) > version(before['version']):
        return current
    a = before['artifact']
    data = download_file((a['name'], a['url'], a['size'], a['sha256']))
    path = out / current['artifact']['name']
    members = tool_members(path.read_bytes())
    if tool_content(members) == tool_content(tool_members(data)):
        path.unlink()
        (out / a['name']).write_bytes(data)
        entry = copy.deepcopy(before)
        entry['sourceVersion'] = source_version
        print('%s %s: keeping the published tool; installed content is unchanged' % (entry['id'], entry['version']))
        return entry
    major, minor, patch = version(before['version'])
    current['version'] = '%d.%d.%d' % (major, minor, patch + 1)
    for i, (name, data, mode) in enumerate(members):
        if name == 'component.json':
            manifest = json.loads(data)
            manifest['version'] = current['version']
            members[i] = (name, json_bytes(manifest), mode)
    target = out / (current['id'] + '@' + current['version'] + '.tgz')
    pack(target, members)
    path.unlink()
    current['artifact'] = artifact(target, current['artifact']['url'].rsplit('/', 1)[0])
    print('%s: tool content changed; source version %s, release version %s' %
          (current['id'], source_version, current['version']))
    return current


def reconcile_versions(out, previous):
    """Keep unchanged app versions; version tool content independently of upstream's manifests."""
    catalog = read_json(out / 'catalog.json')
    old = {c['id']: c for c in previous['components']}
    for i, c in enumerate(catalog['components']):
        before = old.get(c['id'])
        if c['kind'] == 'tool' and before:
            catalog['components'][i] = reconcile_tool(out, c, before)
            continue
        if c['kind'] != 'app' or not before or c['version'] != before['version']:
            continue
        a = before['artifact']
        if c['artifact']['sha256'] != a['sha256']:
            print('%s %s: keeping the published package; app.json version must increase to ship changed content' %
                  (c['id'], c['version']))
            (out / a['name']).write_bytes(download_file((a['name'], a['url'], a['size'], a['sha256'])))
        if before.get('icon') and c.get('iconSha256') != before.get('iconSha256'):
            name = c['id'] + '.png'
            (out / name).write_bytes(download_file((name, before['icon'], None, before['iconSha256'])))
        catalog['components'][i] = copy.deepcopy(before)
    validate_catalog(catalog)
    path = out / 'catalog.json'
    original = path.read_bytes()
    path.write_bytes(json_bytes(catalog))
    try:
        check_update(out, previous)
    except Exception:
        path.write_bytes(original)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-tag')
    parser.add_argument('--repository')
    parser.add_argument('--language', choices=languages(), default='zh-CN')
    parser.add_argument('--out', type=Path, default=ROOT / 'dist/store-release')
    parser.add_argument('--bind-release', type=Path, help='bind an existing catalog to a GitHub release API response')
    parser.add_argument('--check-update', type=Path, help='refuse downgrades and changed packages with unchanged versions')
    parser.add_argument('--reconcile-versions', type=Path, help='keep published packages for unchanged app versions')
    parser.add_argument('--verify-downloads', action='store_true', help='download and verify every advertised package and icon')
    parser.add_argument('--private', action='store_true')
    args = parser.parse_args()
    if args.reconcile_versions:
        reconcile_versions(args.out, read_json(args.reconcile_versions))
    elif args.verify_downloads:
        verify_downloads(args.out)
    elif args.check_update:
        check_update(args.out, read_json(args.check_update))
    elif args.bind_release:
        bind_release(args.out, read_json(args.bind_release), args.private)
    elif args.release_tag and args.repository:
        build_release(args.release_tag, args.repository, args.language, args.out)
    else:
        parser.error('--release-tag and --repository are required to build')


if __name__ == '__main__':
    main()
