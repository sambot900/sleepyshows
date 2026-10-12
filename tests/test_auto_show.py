"""Auto-Show timing, nightly preferences, and production playlist integration."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_playback_regressions import PlaybackHarness, APP
from auto_show import AutoShowController
from main import MainWindow
from playlist_manager import PlaylistManager
from sleep_timer import SleepTimerController
from PySide6.QtCore import QObject


class AutoShowWindow(PlaybackHarness, QObject):
    for name in (
        '_switch_to_auto_show', '_update_auto_show_ui', '_on_auto_show_state_changed',
        'set_auto_show_settings', 'toggle_auto_show_for_night', '_save_user_settings',
        'load_playlist', 'on_sleep_timer', 'start_auto_sleep_timer', 'cancel_sleep_timer',
    ):
        locals()[name] = getattr(MainWindow, name)

    def __init__(self, pm, folder, settings=None):
        QObject.__init__(self)
        PlaybackHarness.__init__(self, pm)
        self._settings = dict(settings or {})
        self._settings_path = os.path.join(folder, 'settings.json')
        self.current_playlist_filename = os.path.join(folder, 'Movie.json')
        self.playback_mode = 'web'
        self.edit_mode_widget = Mock()
        self.set_mode = Mock()
        self._show_mpv_osd_text = Mock()
        self.missing_media_recovery = Mock()
        self._load_resume_state = Mock(return_value={})
        self.lbl_sleep_status = Mock()
        self.player.mpv.pause = False
        self.player.mpv.core_idle = False
        self.sleep_timer = SleepTimerController(self)
        self.sleep_timer.expired.connect(self.on_sleep_timer)
        self._update_sleep_timer_ui = self.sleep_timer._update_ui
        self.update_sleep_menu_state = Mock()
        self.stop_playback = Mock(side_effect=self._stop)
        self.auto_show = AutoShowController(
            self, enabled=self._settings.get('auto_show_enabled', True),
            show_name=self._settings.get('auto_show_name', 'Destination'),
            delay_minutes=self._settings.get('auto_show_delay_minutes', 1),
        )
        self.auto_show.state_changed.connect(self._on_auto_show_state_changed)
        self.auto_show.switch_requested.connect(self._switch_to_auto_show)

    def _stop(self):
        self.player.mpv.pause = True
        self.player.mpv.core_idle = True
        self.sleep_timer.pause()

    def shutdown_timers(self):
        self.auto_show._timer.stop()
        self.sleep_timer.pause()


class AutoShowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = datetime(2026, 10, 12, 23)
        self.monotonic = 100.0
        for module in ('auto_show', 'sleep_timer'):
            datetime_patch = patch(f'{module}.datetime')
            fake_datetime = datetime_patch.start()
            fake_datetime.now.side_effect = lambda: self.clock
            fake_datetime.fromisoformat.side_effect = datetime.fromisoformat
            self.addCleanup(datetime_patch.stop)
        monotonic_patch = patch('auto_show.time.monotonic', side_effect=lambda: self.monotonic)
        monotonic_patch.start()
        self.addCleanup(monotonic_patch.stop)
        for module in ('main', 'playlist_manager'):
            folder_patch = patch(f'{module}.get_local_playlists_dir', return_value=self.tmp.name)
            folder_patch.start()
            self.addCleanup(folder_patch.stop)
        record_patch = patch('main._record_show_played')
        record_patch.start()
        self.addCleanup(record_patch.stop)
        self.pm = PlaylistManager()
        self.pm.current_playlist = [{'type': 'video', 'path': 'movie.mkv'}]
        self.window = AutoShowWindow(self.pm, self.tmp.name)
        self.addCleanup(self.window.shutdown_timers)
        self.controller = self.window.auto_show
        self.window.play_index(0, bypass_bump_gate=True)
        self.controller._tick()
        self.switches = Mock()
        self.controller.switch_requested.connect(self.switches)
        self.write_destination()

    def write_destination(self, mode='standard'):
        with open(os.path.join(self.tmp.name, 'Destination.json'), 'w', encoding='utf-8') as handle:
            json.dump({'shuffle_mode': mode, 'playlist': [
                {'type': 'video', 'path': 'destination-S01E01.mkv'},
                {'type': 'video', 'path': 'destination-S01E02.mkv'},
            ]}, handle)

    def advance(self, seconds):
        self.monotonic += seconds
        self.controller._tick()

    def test_switch_after_playback_delay_and_only_once_per_night(self):
        self.advance(59)
        self.switches.assert_not_called()
        self.advance(1)
        self.switches.assert_called_once_with('Destination')
        self.assertEqual(self.window.current_playlist_filename, os.path.join(self.tmp.name, 'Destination.json'))
        self.assertTrue(self.window.player.play.call_args.args[0].startswith('destination-'))
        self.advance(600)
        self.switches.assert_called_once()

    def test_pause_time_does_not_count(self):
        self.advance(20)
        self.window.player.mpv.pause = True
        self.controller._tick()
        self.advance(900)
        self.assertEqual(self.controller.elapsed_seconds, 20)
        self.window.player.mpv.pause = False
        self.controller._tick()
        self.advance(39)
        self.switches.assert_not_called()
        self.advance(1)
        self.switches.assert_called_once()

    def test_episode_changes_and_skips_do_not_restart_delay(self):
        self.advance(20)
        self.pm.current_playlist.append({'type': 'video', 'path': 'movie-part-2.mkv'})
        self.window.play_index(1, bypass_bump_gate=True)
        self.advance(39)
        self.switches.assert_not_called()
        self.advance(1)
        self.switches.assert_called_once()

    def test_bumps_interludes_idle_and_loading_do_not_count(self):
        self.advance(10)
        self.window._in_bump_playback = True
        self.controller._tick()
        self.advance(200)
        self.window._in_bump_playback = False
        self.window._last_play_source_path = 'interlude.mkv'
        self.controller._tick()
        self.advance(200)
        self.window._last_play_source_path = 'movie.mkv'
        self.window.player.mpv.core_idle = True
        self.controller._tick()
        self.advance(200)
        self.assertEqual(self.controller.elapsed_seconds, 10)
        self.switches.assert_not_called()

    def test_switch_keeps_remaining_sleep_countdown(self):
        self.window.sleep_timer.start(30)
        self.advance(60)
        self.assertTrue(self.window.sleep_timer.active)
        self.assertEqual(self.window.sleep_timer.mode, 'countdown')
        self.assertEqual(self.window.sleep_timer.remaining_ms, 29 * 60 * 1000)
        self.switches.assert_called_once()
        self.window.stop_playback.assert_not_called()

    def test_switch_preserves_movie_progress_and_skip_exposure(self):
        self.window._last_time_pos = 300
        self.window.total_duration = 7200
        self.advance(60)
        self.assertEqual(self.window._episode_positions['movie.mkv'], (300.0, 7200.0))
        self.assertEqual(self.pm.episode_exposure_scores['movie.mkv'], 80.0)

    def test_switch_keeps_nightly_sleep_target(self):
        self.window.auto_sleep_enabled = True
        self.window.sleep_timer.set_nightly_enabled(True)
        self.window.sleep_timer.start_auto()
        self.controller.refresh_boundary()
        target = self.window.sleep_timer._auto_next_trigger
        self.advance(60)
        self.assertTrue(self.window.sleep_timer.auto_active)
        self.assertEqual(self.window.sleep_timer._auto_next_trigger, target)

    def test_sleep_expiry_wins_when_both_countdowns_are_due(self):
        self.window.sleep_timer.start(1)
        self.advance(60)
        self.window.stop_playback.assert_called_once()
        self.switches.assert_not_called()
        self.assertFalse(self.controller.switched)

    def test_missing_destination_keeps_current_movie_and_attempts_once(self):
        os.remove(os.path.join(self.tmp.name, 'Destination.json'))
        self.advance(60)
        self.advance(60)
        self.assertEqual(self.window.current_playlist_filename, os.path.join(self.tmp.name, 'Movie.json'))
        self.assertEqual(self.window.player.play.call_count, 1)
        self.window.stop_playback.assert_not_called()
        self.window._show_mpv_osd_text.assert_called_once()
        self.assertTrue(self.controller.error)
        self.switches.assert_called_once()

    def test_empty_destination_keeps_current_movie(self):
        with open(os.path.join(self.tmp.name, 'Destination.json'), 'w', encoding='utf-8') as handle:
            json.dump({'playlist': []}, handle)
        self.advance(60)
        self.assertEqual(self.window.player.play.call_count, 1)
        self.assertTrue(self.controller.error)

    def test_already_playing_destination_does_not_reload(self):
        self.window.current_playlist_filename = os.path.join(self.tmp.name, 'Destination.json')
        self.advance(60)
        self.assertEqual(self.window.player.play.call_count, 1)
        self.assertTrue(self.controller.switched)

    def test_destination_chronological_mode_is_respected(self):
        self.write_destination('off')
        self.advance(60)
        self.assertEqual(self.pm.shuffle_mode, 'off')
        self.window.player.play.assert_called_with('destination-S01E01.mkv', start_position=0.0)

    def test_night_off_with_default_on_resets_at_midnight_while_idle(self):
        self.window.toggle_auto_show_for_night()
        self.assertFalse(self.controller.enabled)
        self.assertTrue(self.controller.default_enabled)
        self.window.player.mpv.pause = True
        self.clock = datetime(2026, 10, 13, 0)
        self.controller._tick()
        self.assertTrue(self.controller.enabled)
        self.assertEqual(self.controller.elapsed_seconds, 0)
        self.assertIsNone(self.controller.export_override())
        self.assertNotIn('auto_show_night_override', self.window._settings)

    def test_night_on_with_default_off_resets_to_off_at_midnight(self):
        self.window.set_auto_show_settings(False, 'Destination', 1)
        self.window.toggle_auto_show_for_night()
        self.assertTrue(self.controller.enabled)
        self.clock = datetime(2026, 10, 13, 0)
        self.controller._tick()
        self.assertFalse(self.controller.enabled)

    def test_auto_sleep_boundary_uses_setting_even_when_player_sleep_is_off(self):
        self.window.auto_sleep_enabled = True
        self.window.sleep_timer.set_auto_target('02:00')
        self.window.sleep_timer.set_nightly_enabled(True)
        self.window.sleep_timer.cancel()
        self.controller.refresh_boundary()
        self.window.toggle_auto_show_for_night()
        self.clock = datetime(2026, 10, 13, 0)
        self.controller._tick()
        self.assertFalse(self.controller.enabled)
        self.clock = datetime(2026, 10, 13, 1, 59)
        self.controller._tick()
        self.assertFalse(self.controller.enabled)
        self.clock = datetime(2026, 10, 13, 2)
        self.controller._tick()
        self.assertTrue(self.controller.enabled)
        self.assertEqual(self.controller.reset_at, datetime(2026, 10, 14, 2))
        self.switches.assert_not_called()

    def test_same_calendar_day_after_two_am_override_expires_next_morning(self):
        self.clock = datetime(2026, 10, 13, 2, 30)
        self.window.auto_sleep_enabled = True
        self.window.sleep_timer.set_auto_target('02:00')
        self.controller.refresh_boundary()
        self.window.toggle_auto_show_for_night()
        self.assertFalse(self.controller.enabled)
        self.assertEqual(self.controller.reset_at, datetime(2026, 10, 14, 2))
        self.clock = datetime(2026, 10, 13, 23)
        self.controller._tick()
        self.assertFalse(self.controller.enabled)
        self.clock = datetime(2026, 10, 14, 2)
        self.controller._tick()
        self.assertTrue(self.controller.enabled)

    def test_override_survives_restart_within_night_but_not_next_night(self):
        self.window.toggle_auto_show_for_night()
        with open(self.window._settings_path, encoding='utf-8') as handle:
            saved = json.load(handle)
        self.window._settings = saved
        restored = AutoShowController(self.window, enabled=True, show_name='Destination')
        self.addCleanup(restored._timer.stop)
        self.assertFalse(restored.enabled)
        self.clock = datetime(2026, 10, 13, 0)
        restored._tick()
        self.assertTrue(restored.enabled)
        expired = AutoShowController(self.window, enabled=True, show_name='Destination')
        self.addCleanup(expired._timer.stop)
        self.assertTrue(expired.enabled)

    def test_reset_rearms_next_night_and_does_not_restart_playback(self):
        self.advance(60)
        self.clock = datetime(2026, 10, 13, 0)
        self.controller._tick()
        self.assertFalse(self.controller.switched)
        self.assertEqual(self.window.player.play.call_count, 2)
        self.advance(60)
        self.assertEqual(self.switches.call_count, 2)
        self.assertEqual(self.window.player.play.call_count, 2)  # Target is already playing.

    def test_settings_are_persisted_and_player_toggle_preserves_default(self):
        self.window.set_auto_show_settings(True, 'Destination', 37)
        self.window.toggle_auto_show_for_night()
        with open(self.window._settings_path, encoding='utf-8') as handle:
            saved = json.load(handle)
        self.assertTrue(saved['auto_show_enabled'])
        self.assertEqual(saved['auto_show_name'], 'Destination')
        self.assertEqual(saved['auto_show_delay_minutes'], 37)
        self.assertFalse(saved['auto_show_night_override']['enabled'])
        self.window.play_mode_widget.btn_auto_show.setText.assert_called_with('AUTO-SHOW\nOFF')

    def test_changed_auto_sleep_settings_update_override_deadline(self):
        self.window.toggle_auto_show_for_night()
        self.window.auto_sleep_enabled = True
        self.window.sleep_timer.set_auto_target('03:15')
        self.controller.refresh_boundary()
        self.assertFalse(self.controller.enabled)
        self.assertEqual(self.controller.reset_at, datetime(2026, 10, 13, 3, 15))
        self.assertEqual(self.window._settings['auto_show_night_override']['until'], '2026-10-13T03:15:00')

    def test_invalid_delay_and_override_values_are_safe(self):
        self.assertEqual(AutoShowController.parse_delay('bad'), 60)
        self.assertEqual(AutoShowController.parse_delay(0), 1)
        self.window._settings['auto_show_night_override'] = {'enabled': False, 'until': 'bad'}
        restored = AutoShowController(self.window, enabled=True)
        self.addCleanup(restored._timer.stop)
        self.assertTrue(restored.enabled)


if __name__ == '__main__':
    unittest.main()
