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
from types import MethodType
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PySide6.QtWidgets import QApplication
from player_backend import MpvPlayer

APP = QApplication.instance() or QApplication([])

from main import MainWindow
from playlist_manager import PlaylistManager
from test_playback_regressions import PlaybackHarness


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

    def trim_window(self, start=4, end=54):
        with patch('playlist_manager.get_local_playlists_dir', return_value=self.tmp.name):
            pm = PlaylistManager()
        pm.current_playlist = [{'type': 'video', 'path': path} for path in self.paths[:2]]
        window = PlaybackHarness(pm)
        window.player = self.player
        window.current_playlist_filename = str(Path(self.tmp.name, 'King of the Hill.json'))
        window._settings = {'show_trim': {
            'King of the Hill': {'start_seconds': start, 'end_seconds': end},
        }}
        window.play_next = MethodType(MainWindow.play_next, window)
        window.on_playback_finished = MethodType(MainWindow.on_playback_finished, window)
        window.on_mpv_end_file_reason = MethodType(MainWindow.on_mpv_end_file_reason, window)
        self.player.durationChanged.connect(window.update_duration)
        self.player.positionChanged.connect(lambda pos: setattr(window, '_last_time_pos', pos))
        self.player.playbackFinished.connect(window.on_playback_finished)
        self.player.endFileReason.connect(window.on_mpv_end_file_reason)
        return window

    def test_trim_starts_after_intro_ends_early_and_advances_normally(self):
        window = self.trim_window()
        pm = window.playlist_manager
        positions = []
        self.player.positionChanged.connect(lambda pos: positions.append((pm.current_index, pos)))
        window.play_index(0, bypass_bump_gate=True)
        self.wait_for(lambda: self.player.mpv.path == self.paths[0]
                      and self.player.mpv.time_pos is not None)
        self.assertGreaterEqual(self.player.mpv.time_pos, 4)
        self.assertLess(self.player.mpv.time_pos, 6)
        self.wait_for(lambda: pm.current_index == 1 and self.player.mpv.path == self.paths[1]
                      and self.player.mpv.time_pos is not None)
        self.player.pause()
        first_positions = [pos for index, pos in positions if index == 0]
        self.assertTrue(first_positions)
        self.assertLessEqual(max(first_positions), 6.2)
        self.assertGreaterEqual(self.player.mpv.time_pos, 4)
        key = pm._norm_path_key(self.paths[0])
        self.assertEqual(pm.episode_exposure_scores[key], 100)
        self.assertEqual(pm.episode_play_counts[key], 1)
        # A following movie/interlude/bump load must have its full duration.
        self.check_start(self.paths[2], 55)

    def test_fully_trimmed_episode_advances_instead_of_stalling_in_eof_guard(self):
        window = self.trim_window(start=59, end=10)
        window.play_index(0, bypass_bump_gate=True)
        self.wait_for(lambda: window.playlist_manager.current_index == 1)


if __name__ == "__main__":
    print(f"Testing real mpv with {sys.platform}, Python {sys.version.split()[0]}")
    unittest.main()
