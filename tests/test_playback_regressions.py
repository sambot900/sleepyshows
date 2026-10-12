"""Regression checks without mounting a media library or changing user settings.

Run: venv/bin/python -m unittest discover -s tests -v
"""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from main import MainWindow
from player_backend import MpvPlayer
from playlist_manager import PlaylistManager
from sleep_timer import SleepTimerController
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

# main.py selects X11 for the actual player; these checks don't render video.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
APP = QApplication.instance() or QApplication([])


class PlaybackHarness:
    """Exercise production navigation with fake player/UI and isolated scores."""
    for name in (
        "play_index", "_episode_resume_start", "_save_current_episode_position",
        "_maybe_record_episode_skip", "_maybe_auto_resume_on_first_play",
        "_arm_auto_resume_for_playlist", "_apply_resume_state", "_capture_resume_state",
        "_predict_default_start_index", "_norm_match_path", "update_duration",
        "skip_to_next_episode",
    ):
        locals()[name] = getattr(MainWindow, name)

    def __init__(self, pm):
        self.playlist_manager = pm
        self._episode_positions = {}
        self._pending_auto_resume_state = None
        self._pending_auto_resume_playlist = None
        self._last_time_pos = None
        self.total_duration = 0
        self._play_start_monotonic = None
        self._resume_position_for_start = None
        self._last_play_target = None
        self._last_play_source_path = None
        self._advancing_from_eof = False
        self._in_bump_playback = False
        self._pending_bump_item = None
        self._pending_next_index = None
        self.sleep_timer_active = True
        self.auto_sleep_enabled = False
        self.bumps_enabled = False
        self.video_stack = Mock()
        self.video_stack.currentIndex.return_value = 0
        self.video_container = Mock()
        self.play_mode_widget = Mock()
        self.player = Mock()
        self.player.mpv = SimpleNamespace(core_idle=True, time_pos=None, duration=None, path=None)
        self.player.play.side_effect = lambda path, **kw: setattr(self.player.mpv, "path", path)
        self._resolve_video_play_target = lambda path: path
        self._should_suppress_auto_nav_ui = lambda: False
        self.isFullScreen = lambda: False
        self.set_shuffle_mode = lambda mode, **kw: pm.set_shuffle_mode(mode)
        for name in (
            "_stop_startup_ambient", "stop_bump_playback", "_show_episode_overlay",
            "_sync_keep_awake", "_resume_sleep_countdown_if_needed",
            "_update_track_button_labels", "setWindowTitle", "show_controls",
            "_log_event", "_persist_resume_state", "update_time_label", "stop_playback",
            "_set_stop_reason",
        ):
            setattr(self, name, Mock())


class IsolatedPlaylistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder_patch = patch("playlist_manager.get_local_playlists_dir", return_value=self.tmp.name)
        self.folder_patch.start()
        self.addCleanup(self.folder_patch.stop)
        self.pm = PlaylistManager()
        self.pm.current_playlist = [
            {"type": "video", "path": os.path.join(self.tmp.name, "Season 1", f"S01E{i:02}.mkv")}
            for i in range(1, 25)
        ]
        self.window = PlaybackHarness(self.pm)

    def score(self, index):
        return self.pm.episode_exposure_scores.get(self.pm._norm_path_key(self.pm._episode_path_for_index(index)), 0.0)

    def test_rapid_skips_retain_eighty_percent_without_consuming_sleep_tiers(self):
        self.pm.set_shuffle_mode("standard")
        for index in range(18):
            self.window.play_index(index, bypass_bump_gate=True)
        self.window._maybe_record_episode_skip()
        self.assertEqual([self.score(i) for i in range(18)], [80.0] * 18)
        self.assertEqual(self.pm._session_episode_plays, 0)
        self.assertEqual(self.pm.episode_play_counts[self.pm._norm_path_key(self.pm._episode_path_for_index(17))], 1)

    def test_skips_keep_current_tier_and_episode_or_season_factor(self):
        self.pm.set_sleep_timer_active_for_exposure(True)
        for index in range(3):
            self.pm.mark_episode_started(index)
        path = self.pm._episode_path_for_index(3)
        season = self.pm._season_bucket_key_from_path(path)
        self.pm.season_exposure_factors[season] = 2.0
        self.pm.mark_episode_started(3)
        self.pm.mark_episode_skipped(3)
        self.assertEqual(self.score(3), 80.0)  # 50 * 80% * 2
        self.assertEqual(self.pm._session_episode_plays, 3)
        path = self.pm._episode_path_for_index(4)
        self.pm.episode_exposure_factors[self.pm._norm_path_key(path)] = 3.0
        self.pm.mark_episode_started(4)
        self.pm.mark_episode_skipped(4)
        self.assertEqual(self.score(4), 120.0)

    def test_skip_adjustment_is_idempotent_and_no_timer_keeps_full_tiers(self):
        for index in range(12):
            self.pm.mark_episode_started(index, sleep_timer_on=False)
            self.pm.mark_episode_skipped(index)
            self.pm.mark_episode_skipped(index)
        self.assertEqual([self.score(i) for i in range(12)], [80.0] * 12)

    def test_natural_eof_keeps_full_score_and_advances_tier(self):
        self.window.play_index(0, bypass_bump_gate=True)
        self.window._advancing_from_eof = True
        self.window.play_index(1, bypass_bump_gate=True)
        self.assertEqual(self.score(0), 100.0)
        self.assertEqual(self.pm._session_episode_plays, 2)

    def test_previous_file_metadata_cannot_hide_skip_while_loading(self):
        self.window.play_index(0, bypass_bump_gate=True)
        self.window._last_time_pos = 1200
        self.window.total_duration = 1200
        self.window.player.mpv.path = "previous-episode.mkv"
        self.window._maybe_record_episode_skip()
        self.assertEqual(self.score(0), 80.0)
        self.assertEqual(self.pm._session_episode_plays, 0)

    def test_interlude_does_not_change_previous_episode_exposure(self):
        self.window.play_index(0, bypass_bump_gate=True)
        self.window._last_play_source_path = "interlude.mkv"
        self.window._maybe_record_episode_skip()
        self.assertEqual(self.score(0), 100.0)

    def test_watchdog_flushes_final_throttled_skip(self):
        with patch("playlist_manager.time.monotonic", return_value=100.0):
            self.pm.mark_episode_started(0, sleep_timer_on=True)
            self.pm.mark_episode_skipped(0)
            self.assertTrue(self.pm._exposure_dirty)
        self.window.player = None
        with patch("playlist_manager.time.monotonic", return_value=102.0):
            MainWindow._check_playback_end(self.window)
        with open(self.pm._exposure_scores_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["episodes"][self.pm._norm_path_key(self.pm._episode_path_for_index(0))], 80.0)

    def test_scores_and_counts_survive_restart_and_reorder_queue(self):
        self.pm.set_shuffle_mode("standard")
        for index in range(12):
            self.window.play_index(index, bypass_bump_gate=True)
        self.window._maybe_record_episode_skip()
        self.pm._save_exposure_scores(force=True)
        restored = PlaylistManager()
        restored.current_playlist = self.pm.current_playlist
        restored.reset_playback_state()
        restored.set_shuffle_mode("standard")
        self.assertTrue(all(i >= 12 for i in restored.play_queue[:12]))
        self.assertEqual(restored.episode_exposure_scores, self.pm.episode_exposure_scores)
        self.assertEqual(restored.episode_play_counts, self.pm.episode_play_counts)
        with open(self.pm._exposure_scores_path, encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)["episode_history_keys"]), 12)

    def test_both_shuffle_modes_ignore_legacy_positions_and_disk_resume(self):
        for mode in ("standard", "season"):
            with self.subTest(mode=mode):
                self.pm.set_shuffle_mode(mode)
                path = self.pm._episode_path_for_index(0)
                self.window._episode_positions[path] = (1199.0, 1200.0)
                self.window._pending_auto_resume_state = {"time_pos_s": 1199.0}
                self.window._play_index_last_mono = 0.0
                self.window.play_index(0)
                self.window.player.play.assert_called_with(path, start_position=0.0)
                self.window._last_time_pos = 1199.0
                self.window.total_duration = 1200.0
                payload = self.window._capture_resume_state()
                self.assertIsNone(payload["time_pos_s"])
                self.assertIsNone(payload["duration_s"])
                self.assertEqual(payload["current_index"], 0)
                self.assertIsNone(self.window._pending_auto_resume_state)
                self.window._load_resume_state = Mock()
                self.window._arm_auto_resume_for_playlist("show.json")
                self.window._load_resume_state.assert_not_called()

    def test_chronological_resume_boundary_and_last_episode(self):
        for position, expected in ((1179.0, (0, 1176.0)), (1180.0, (1, 0.0)),
                                   (1199.0, (1, 0.0)), (1200.0, (1, 0.0)),
                                   (1300.0, (1, 0.0))):
            with self.subTest(position=position):
                self.assertEqual(self.window._episode_resume_start(0, position, 1200.0), expected)
        self.assertEqual(self.window._episode_resume_start(23, 1180, 1200), (-1, 0.0))

    def test_disk_resume_at_end_starts_next_fresh_despite_old_saved_tail(self):
        next_path = self.pm._episode_path_for_index(1)
        self.window._episode_positions[next_path] = (1199.0, 1200.0)
        self.window._apply_resume_state({
            "current_index": 0, "time_pos_s": 1180, "duration_s": 1200,
            "queue_episode_keys": [self.pm._episode_path_for_index(i) for i in range(1, 24)],
        })
        self.window.player.play.assert_called_once_with(next_path, start_position=0.0)
        self.assertEqual(self.pm.current_index, 1)
        self.assertEqual(self.pm.get_next_episode_index_episode_only(), 2)

    def test_auto_resume_of_first_episode_is_not_dropped_as_duplicate(self):
        self.window._pending_auto_resume_state = {"current_index": 0, "time_pos_s": 600, "duration_s": 1200}
        self.window.play_index(0)
        self.window.player.play.assert_called_once_with(self.pm._episode_path_for_index(0), start_position=597.0)

    def test_chronological_back_navigation_remembers_partial_position(self):
        self.window.play_index(0, bypass_bump_gate=True)
        self.window._last_time_pos = 300
        self.window.total_duration = 1200
        self.window.play_index(1, bypass_bump_gate=True)
        self.window._play_index_last_mono = 0.0
        self.window.play_index(0, bypass_bump_gate=True)
        self.window.player.play.assert_called_with(self.pm._episode_path_for_index(0), start_position=297.0)

    def test_unknown_duration_applies_completion_rule_after_load(self):
        self.window._apply_resume_state({"current_index": 0, "time_pos_s": 1180})
        self.window.update_duration(1200)
        APP.processEvents()
        self.window.player.play.assert_called_with(self.pm._episode_path_for_index(1), start_position=0.0)

    def test_delayed_duration_advance_does_not_change_new_track(self):
        self.window._apply_resume_state({"current_index": 0, "time_pos_s": 1180})
        self.window.update_duration(1200)
        self.window.play_index(2, bypass_bump_gate=True)
        APP.processEvents()
        self.assertEqual(self.pm.current_index, 2)


class BackendLoadTests(unittest.TestCase):
    def test_windows_dll_directory_is_not_confused_with_pyside_subdirectory(self):
        with tempfile.TemporaryDirectory(prefix="Sleepy Shows DLL ") as folder:
            dll_dir = os.path.join(folder, "runtime")
            qt_dir = os.path.join(dll_dir, "Lib", "site-packages", "PySide6")
            os.makedirs(qt_dir)
            Path(dll_dir, "libmpv-2.dll").touch()
            backend = SimpleNamespace(_dll_dir_handles=[])
            with patch("player_backend.sys.platform", "win32"), \
                 patch("player_backend.sys.executable", os.path.join(dll_dir, "python.exe")), \
                 patch.dict(os.environ, {"PATH": qt_dir}), \
                 patch("player_backend.os.add_dll_directory", create=True) as add_dir:
                MpvPlayer._prepare_windows_mpv_dll_search(backend)
                self.assertIn(dll_dir, os.environ["PATH"].split(os.pathsep))
                self.assertIn(qt_dir, os.environ["PATH"].split(os.pathsep))
                add_dir.assert_called_once_with(dll_dir)

    def test_windows_dll_search_preserves_existing_normalized_path_entry(self):
        with tempfile.TemporaryDirectory(prefix="Sleepy Shows DLL ") as folder:
            Path(folder, "libmpv-2.dll").touch()
            existing_path = os.path.join(folder, os.curdir)
            backend = SimpleNamespace(_dll_dir_handles=[])
            with patch("player_backend.sys.platform", "win32"), \
                 patch("player_backend.sys.executable", os.path.join(folder, "python.exe")), \
                 patch.dict(os.environ, {"PATH": existing_path}), \
                 patch("player_backend.os.add_dll_directory", create=True):
                MpvPlayer._prepare_windows_mpv_dll_search(backend)
                self.assertEqual(os.environ["PATH"], existing_path)

    def test_start_position_belongs_to_each_load_and_native_resume_is_disabled(self):
        backend = SimpleNamespace(mpv=Mock(), winId=lambda: 0, errorOccurred=Mock())
        MpvPlayer.play(backend, "a.mkv", start_position=500)
        MpvPlayer.play(backend, "b.mkv")
        self.assertEqual(backend.mpv.loadfile.call_args_list[0].kwargs,
                         {"start": 500.0, "resume_playback": "no"})
        self.assertEqual(backend.mpv.loadfile.call_args_list[1].kwargs,
                         {"start": 0.0, "resume_playback": "no"})
        backend.mpv.seek.assert_not_called()
        backend.errorOccurred.emit.assert_not_called()


class TimerWindow(QObject):
    on_sleep_timer = MainWindow.on_sleep_timer

    def __init__(self):
        super().__init__()
        self.auto_sleep_enabled = True
        self.playlist_manager = Mock()
        self.player = SimpleNamespace(mpv=SimpleNamespace(pause=True, core_idle=True))
        self.lbl_sleep_status = Mock()
        self.play_mode_widget = Mock()
        self._settings = {"auto_sleep_enabled": True, "sleep_timer_enabled": True}
        self.sleep_timer = SleepTimerController(self)
        self.sleep_timer.expired.connect(self.on_sleep_timer)
        self.expirations = 0
        self.set_mode = Mock()
        self._set_stop_reason = Mock()
        self.update_sleep_menu_state = Mock()
        self._update_sleep_timer_ui = self.sleep_timer._update_ui
        self.start_auto_sleep_timer = self.sleep_timer.start_auto
        self.cancel_sleep_timer = Mock(side_effect=self.sleep_timer.cancel)

    def stop_playback(self):
        self.expirations += 1
        self.player.mpv.pause = True
        self.sleep_timer.pause()

    def play(self):
        self.player.mpv.pause = False
        self.player.mpv.core_idle = False
        self.sleep_timer.resume_if_needed()


class NightlyTimerTests(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2026, 10, 12, 1, 30)
        self.monotonic = 100.0
        self.datetime_patch = patch("sleep_timer.datetime")
        self.fake_datetime = self.datetime_patch.start()
        self.fake_datetime.now.side_effect = lambda: self.clock
        self.addCleanup(self.datetime_patch.stop)
        self.monotonic_patch = patch("sleep_timer.time.monotonic", side_effect=lambda: self.monotonic)
        self.monotonic_patch.start()
        self.addCleanup(self.monotonic_patch.stop)
        self.window = TimerWindow()
        self.timer = self.window.sleep_timer
        self.addCleanup(self.timer.pause)
        self.timer.set_auto_target("02:00")
        self.timer.set_nightly_enabled(True)
        self.timer.start_auto()

    def test_after_two_am_countdown_then_next_night_auto(self):
        self.clock = datetime(2026, 10, 12, 2, 30)
        self.timer.start(30)
        self.window.play()
        self.monotonic += 1800
        self.clock = datetime(2026, 10, 12, 3)
        self.timer._tick()
        self.assertEqual(self.window.expirations, 1)
        self.assertTrue(self.timer.auto_active)
        self.assertEqual(self.timer._auto_next_trigger, datetime(2026, 10, 13, 2))
        self.window.cancel_sleep_timer.assert_not_called()
        self.clock = datetime(2026, 10, 12, 23)
        self.window.play()
        self.clock = datetime(2026, 10, 13, 2)
        self.timer._tick()
        self.assertEqual(self.window.expirations, 2)
        self.assertEqual(self.timer._auto_next_trigger, datetime(2026, 10, 14, 2))

    def test_nightly_shutoff_wins_over_long_countdown(self):
        self.window.play()
        self.timer.start(180)
        self.clock = datetime(2026, 10, 12, 2)
        self.monotonic += 1800
        self.timer._tick()
        self.assertEqual(self.window.expirations, 1)
        self.assertTrue(self.timer.auto_active)

    def test_player_off_does_not_disable_recurring_setting(self):
        self.window.play()
        self.timer.cancel()
        self.assertFalse(self.timer.active)
        self.assertTrue(self.timer.nightly_enabled)
        self.assertTrue(self.timer._timer.isActive())
        self.clock = datetime(2026, 10, 12, 2)
        self.timer._tick()
        self.assertEqual(self.window.expirations, 1)
        self.assertTrue(self.timer.auto_active)

    def test_player_off_after_shutoff_still_stops_next_early_morning(self):
        self.clock = datetime(2026, 10, 12, 2, 30)
        self.window.play()
        self.timer.cancel()
        self.window.player.mpv.pause = True
        self.timer.pause()
        self.clock = datetime(2026, 10, 12, 23)
        self.window.play()
        self.clock = datetime(2026, 10, 13, 2)
        self.timer._tick()
        self.assertEqual(self.window.expirations, 1)

    def test_setting_off_preserves_countdown_and_removes_nightly_shutoff(self):
        self.window.play()
        self.timer.start(180)
        self.window.auto_sleep_enabled = False
        self.timer.set_nightly_enabled(False)
        self.clock = datetime(2026, 10, 12, 2)
        self.monotonic += 1800
        self.timer._tick()
        self.assertEqual(self.window.expirations, 0)
        self.assertEqual(self.timer.remaining_minutes(), 150)

    def test_idle_missed_shutoff_rolls_to_next_night(self):
        self.clock = datetime(2026, 10, 12, 22)
        self.window.play()
        self.assertEqual(self.timer._auto_next_trigger, datetime(2026, 10, 13, 2))
        self.timer._tick()
        self.assertEqual(self.window.expirations, 0)

    def test_changing_auto_time_also_updates_schedule_during_countdown(self):
        self.window.play()
        self.timer.start(180)
        self.timer.set_auto_target("01:45")
        self.assertEqual(self.timer._auto_next_trigger, datetime(2026, 10, 12, 1, 45))
        self.clock = datetime(2026, 10, 12, 1, 45)
        self.timer._tick()
        self.assertEqual(self.window.expirations, 1)


if __name__ == "__main__":
    unittest.main()
