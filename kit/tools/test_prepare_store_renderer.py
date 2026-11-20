#!/usr/bin/env python3
"""Check that Store builds accept only source-matched or successfully rebuilt renderer tools."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import prepare_store_renderer as prepare


class PrepareRendererTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for directory in ('prebuilt', 'inputgate/tool', 'build'):
            (self.root / directory).mkdir(parents=True)
        for name in ('mgu-renderd', 'mgu-inputgate.so'):
            (self.root / 'prebuilt' / name).write_bytes(b'old binary')
            (self.root / 'build' / name).write_bytes(b'new binary')
        (self.root / 'inputgate/tool/mgu-inputgate.so').write_bytes(b'old binary')
        p = patch.object(prepare.make_kit, 'RENDERD', str(self.root))
        p.start()
        self.addCleanup(p.stop)

    def test_matching_binaries_need_no_docker_and_refresh_script_files(self):
        with patch.object(prepare.make_kit, 'source_hash', return_value='same'), \
                patch.object(prepare.make_kit, 'prebuilt_hash', return_value='same'), \
                patch.object(prepare.make_kit, 'renderd_files') as scripts, \
                patch.object(prepare.subprocess, 'run') as run:
            prepare.prepare_renderer()
        run.assert_not_called()
        scripts.assert_called_once_with(str(self.root / 'prebuilt'))

    def test_rebuild_checks_target_and_tests_before_updating_both_gate_copies(self):
        with patch.object(prepare.make_kit, 'source_hash', return_value='new'), \
                patch.object(prepare.make_kit, 'prebuilt_hash', return_value='old'), \
                patch.object(prepare.make_kit, 'renderd_files'), \
                patch.object(prepare.shutil, 'which', return_value='docker'), \
                patch.object(prepare.subprocess, 'run') as run:
            prepare.prepare_renderer()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(['make', 'all', 'check-target'], [cmd[-3:] for cmd in commands])
        self.assertEqual(commands[-1], ['make', 'test'])
        self.assertIn('linux/amd64', commands[0])
        for path in ('prebuilt/mgu-renderd', 'prebuilt/mgu-inputgate.so', 'inputgate/tool/mgu-inputgate.so'):
            self.assertEqual((self.root / path).read_bytes(), b'new binary')
        self.assertEqual((self.root / 'prebuilt/SOURCE_SHA256').read_text(), 'new\n')

    def test_failed_build_or_tests_leave_committed_binaries_and_stamp_untouched(self):
        for failed_command in range(4):
            with self.subTest(failed_command=failed_command), \
                    patch.object(prepare.make_kit, 'source_hash', return_value='new'), \
                    patch.object(prepare.make_kit, 'prebuilt_hash', return_value='old'), \
                    patch.object(prepare.shutil, 'which', return_value='docker'), \
                    patch.object(prepare.subprocess, 'run', side_effect=[None] * failed_command + [
                        subprocess.CalledProcessError(1, 'build')]), \
                    self.assertRaises(subprocess.CalledProcessError):
                prepare.prepare_renderer()
            self.assertEqual((self.root / 'prebuilt/mgu-renderd').read_bytes(), b'old binary')
            self.assertFalse((self.root / 'prebuilt/SOURCE_SHA256').exists())

    def test_source_mismatch_without_docker_fails_closed(self):
        with patch.object(prepare.make_kit, 'source_hash', return_value='new'), \
                patch.object(prepare.make_kit, 'prebuilt_hash', return_value='old'), \
                patch.object(prepare.shutil, 'which', return_value=None), \
                self.assertRaisesRegex(RuntimeError, 'Docker is required'):
            prepare.prepare_renderer()


if __name__ == '__main__':
    unittest.main()
