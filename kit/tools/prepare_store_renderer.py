#!/usr/bin/env python3
"""Prepare source-matched renderer tools for the Store release, without committing build output."""
from pathlib import Path
import os
import shutil
import subprocess

import make_kit


def prepare_renderer():
    root = Path(make_kit.RENDERD)
    pre = root / 'prebuilt'
    gate = root / 'inputgate/tool/mgu-inputgate.so'
    want = make_kit.source_hash()
    matched = (make_kit.prebuilt_hash() == want and
               all((pre / name).is_file() for name in ('mgu-renderd', 'mgu-inputgate.so')) and
               gate.is_file() and gate.read_bytes() == (pre / 'mgu-inputgate.so').read_bytes())
    if matched:
        print('Renderer: committed binaries match the source')
    else:
        if not shutil.which('docker'):
            raise RuntimeError('Renderer source changed; Docker is required to build the Store tools')
        platform = ['--platform', 'linux/amd64']
        user = ['-u', '%d:%d' % (os.getuid(), os.getgid())] if hasattr(os, 'getuid') else []

        def run(command):
            subprocess.run(command, cwd=root, check=True)

        run(['docker', 'build'] + platform + ['-t', 'mgu-renderd-build', 'docker'])
        container = ['docker', 'run', '--rm'] + platform + user + [
            '-v', str(root) + ':/src', '-w', '/src', 'mgu-renderd-build']
        run(container + ['make', 'clean'])
        run(container + ['make', 'all', 'check-target'])
        run(['make', 'test'])
        if make_kit.source_hash() != want:
            raise RuntimeError('Renderer source changed during the build')
        for name in ('mgu-renderd', 'mgu-inputgate.so'):
            shutil.copyfile(root / 'build' / name, pre / name)
        shutil.copyfile(pre / 'mgu-inputgate.so', gate)
        (pre / 'SOURCE_SHA256').write_text(want + '\n', encoding='utf-8')
        print('Renderer: rebuilt and tested in the upstream Ubuntu 18.04 build image')
    make_kit.renderd_files(str(pre))


if __name__ == '__main__':
    prepare_renderer()
