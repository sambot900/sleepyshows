"""Real mpv integration checks; run explicitly with python tests/smoke_mpv.py.

Requires python-mpv and libmpv (libmpv-2.dll on Windows). Only generated media in
a temporary directory is played. Audio/video output is disabled.
"""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PySide6.QtWidgets import QApplication
from player_backend import MpvPlayer

APP = QApplication.instance() or QApplication([])


class MpvSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="Sleepy Shows mpv ")
        self.addCleanup(self.tmp.cleanup)
        self.player = MpvPlayer()
        self.addCleanup(self.player.shutdown)
        self.assertIsNotNone(self.player.mpv, self.player._init_error)
        self.player.mpv["vo"] = "null"
        self.player.mpv["ao"] = "null"
        self.addCleanup(self.player.stop)
        self.loaded = []

        @self.player.mpv.event_callback("file-loaded")
        def file_loaded(event):
            self.loaded.append(True)

        media = Path(self.tmp.name, "Média 夜")
        media.mkdir()
        self.paths = []
        for index in range(3):
            path = str(media / f"Episode {index}.wav")
            with wave.open(path, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\0\0" * 8000 * 60)
            self.paths.append(path)

    def wait_for(self, condition):
        deadline = time.monotonic() + 10
        while not condition():
            self.assertLess(time.monotonic(), deadline, "mpv did not finish loading")
            APP.processEvents()
            time.sleep(0.01)

    def check_start(self, path, start):
        loaded_before = len(self.loaded)
        self.player.play(path, start_position=start)
        self.wait_for(lambda: len(self.loaded) > loaded_before
                      and self.player.mpv.time_pos is not None)
        self.player.pause()
        position = self.player.mpv.time_pos
        self.assertGreaterEqual(position, start - 0.1)
        self.assertLess(position, start + 2)

    def test_saved_native_resume_cannot_override_fresh_start(self):
        watch_dir = str(Path(self.tmp.name, "watch later"))
        os.mkdir(watch_dir)
        self.player.mpv["watch-later-directory"] = watch_dir
        self.check_start(self.paths[0], 25)
        self.player.mpv.write_watch_later_config()
        self.player.stop()
        self.assertTrue(os.listdir(watch_dir), "mpv did not save a resume position")
        self.check_start(self.paths[0], 0)
        for path in self.paths[1:]:
            self.check_start(path, 0)

    def test_rapid_loads_do_not_carry_position_to_final_file(self):
        self.check_start(self.paths[0], 55)
        for index in range(15):
            self.player.play(self.paths[index % 3], start_position=0)
        final_path = self.paths[2]
        self.wait_for(lambda: self.player.mpv.path == final_path
                      and self.player.mpv.time_pos is not None
                      and not self.player.mpv.core_idle)
        self.assertLess(self.player.mpv.time_pos, 2)


if __name__ == "__main__":
    print(f"Testing real mpv with {sys.platform}, Python {sys.version.split()[0]}")
    unittest.main()
