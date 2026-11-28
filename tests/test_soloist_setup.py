"""Spotify Connect setup against a local stand-in; never calls the network."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from cliamp_music import soloist_setup as setup
    from gi.repository import Gtk
except (ImportError, ValueError):
    setup = None

from tests.fake_cliamp import run_loop


@unittest.skipUnless(setup is not None and Gtk.init_check(), "GTK display required")
class SoloistSetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.program = self.root / "fake-soloist"
        self.program.write_text(f"#!{sys.executable}\n" + '''
from pathlib import Path
import json, os, sys, time
args = sys.argv[1:]
base = Path(args[args.index("--data-dir") + 1])
with (base / "calls").open("a") as out:
    out.write(json.dumps(args) + "\\n")
if "setup-api-key" in args:
    assert sys.stdin.readline().strip() == "test-input"
    cred = base / "credentials"
    cred.mkdir(exist_ok=True)
    (cred / "api_key").write_text("stored")
elif "--pair" in args:
    assert args[args.index("--device-name") + 1] == "ミュージック"
    if (base / "expire").exists(): sys.exit(10)
    if (base / "block").exists(): time.sleep(30)
    assert (base / "credentials/api_key").is_file()
else:
    sys.exit(1)
''')
        self.program.chmod(0o700)
        lookup = patch.object(setup.shutil, "which", return_value=None)
        lookup.start()
        self.addCleanup(lookup.stop)
        self.results = []
        self.window = setup.SoloistSetup(on_paired=lambda: self.results.append(True))
        self.addCleanup(self.window.destroy)
        self.window.present()
        self.window.executable.parent.mkdir(parents=True)
        self.window.executable.symlink_to(self.program)

    def test_key_goes_via_stdin_and_pairing_success_marks_ready(self):
        self.window.key.set_text("test-input")
        self.window.submit.emit("clicked")
        self.assertTrue(run_loop(lambda: self.window.stage == "done", 5), self.window.status.get_text())
        directory = self.window.directory
        args = [json.loads(line) for line in (directory / "calls").read_text().splitlines()]
        self.assertEqual(args[0][0], "setup-api-key")
        self.assertEqual(args[1][0], "--pair")
        self.assertNotIn("test-input", str(args))
        self.assertEqual((directory / "credentials/api_key").stat().st_mode & 0o777, 0o600)
        self.assertEqual((directory / "paired").stat().st_mode & 0o777, 0o600)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.window.key.get_text(), "")
        self.assertEqual(self.results, [True])

    def test_existing_key_reused_and_expired_build_can_retry(self):
        directory = self.window.directory
        (directory / "credentials").mkdir(parents=True)
        (directory / "credentials/api_key").touch()
        (directory / "expire").touch()
        self.window.submit.emit("clicked")
        self.assertTrue(run_loop(lambda: self.window.update_required, 5))
        self.assertFalse((directory / "paired").exists())
        self.assertIn("更新", self.window.submit.get_label())
        installer = self.root / "installer"
        installer.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(directory / 'expire')!r}).unlink()\n")
        installer.chmod(0o700)
        with patch.object(setup.shutil, "which", side_effect=lambda n: str(installer) if n == setup.INSTALLER else None):
            self.window.submit.emit("clicked")
            self.assertTrue(run_loop(lambda: self.window.stage == "done", 5), self.window.status.get_text())
        self.assertFalse(any("setup-api-key" in line for line in (directory / "calls").read_text().splitlines()))
        self.assertEqual(self.results, [True])

    def test_closing_cancels_pending_pair_without_marking_ready(self):
        directory = self.window.directory
        (directory / "credentials").mkdir(parents=True)
        (directory / "credentials/api_key").touch()
        (directory / "block").touch()
        self.window.submit.emit("clicked")
        self.assertTrue(run_loop(lambda: (directory / "calls").exists(), 4))
        process = self.window.process
        self.window.close()
        self.assertTrue(run_loop(lambda: process.poll() is not None, 4))
        self.assertFalse((directory / "paired").exists())
        self.assertEqual(self.results, [])
