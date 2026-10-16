"""Per-show trim, resume and normal episode transition regressions."""
import os
from types import MethodType
from unittest.mock import Mock, patch
import unittest

from test_playback_regressions import APP
import test_playback_regressions as playback_tests
from main import MainWindow


class TrimPlaybackTests(unittest.TestCase):
    def setUp(self):
        playback_tests.IsolatedPlaylistTest.setUp(self)
        self.window.current_playlist_filename = os.path.join(self.tmp.name, 'King of the Hill.json')
        self.window._settings = {'show_trim': {
            'King of the Hill': {'start_seconds': 45, 'end_seconds': 60},
            'Futurama': {'start_seconds': 20, 'end_seconds': 30},
            'Birdman (2014)': {'start_seconds': 90, 'end_seconds': 120},
        }}

    def test_every_episode_starts_after_intro_in_all_shuffle_modes(self):
        for mode in ('off', 'standard', 'season'):
            self.pm.set_shuffle_mode(mode)
            for index in range(2):
                with self.subTest(mode=mode, index=index):
                    self.window.play_index(index, bypass_bump_gate=True)
                    self.window.player.play.assert_called_with(
                        self.pm._episode_path_for_index(index), start_position=45.0, end_trim=60)

    def test_each_show_uses_its_own_values_and_movies_are_excluded(self):
        for title, expected in (('Futurama', (20, 30)), ('Birdman (2014)', (0, 0)),
                                ('Unknown movie', (0, 0)), ('', (0, 0))):
            with self.subTest(title=title):
                self.window.current_playlist_filename = os.path.join(self.tmp.name, title + '.json')
                self.assertEqual(self.window._episode_trim(0), expected)

    def test_interludes_and_bumps_have_no_trim_and_legacy_episodes_do(self):
        self.pm.current_playlist.extend([
            {'type': 'interstitial', 'path': 'commercial.mkv'},
            {'type': 'bump', 'video': 'bump.mkv'},
            'legacy-episode.mkv',
        ])
        self.assertEqual(self.window._episode_trim(24), (0, 0))
        self.assertEqual(self.window._episode_trim(25), (0, 0))
        self.window.play_index(24, bypass_bump_gate=True)
        self.window.player.play.assert_called_with('commercial.mkv', start_position=0.0)
        self.window.play_index(26, bypass_bump_gate=True)
        self.window.player.play.assert_called_with('legacy-episode.mkv', start_position=45.0, end_trim=60)

    def test_chronological_resume_preserves_progress_and_stays_past_intro(self):
        for position, expected in ((10, 45.0), (46, 45.0), (600, 597.0)):
            with self.subTest(position=position):
                self.window._play_index_last_mono = 0.0
                self.window._apply_resume_state({
                    'current_index': 0, 'time_pos_s': position, 'duration_s': 1200,
                })
                self.window.player.play.assert_called_with(
                    self.pm._episode_path_for_index(0), start_position=expected, end_trim=60)

    def test_resume_inside_trimmed_tail_starts_next_episode_after_its_intro(self):
        self.window._apply_resume_state({
            'current_index': 0, 'time_pos_s': 1140, 'duration_s': 1200,
        })
        self.window.player.play.assert_called_with(
            self.pm._episode_path_for_index(1), start_position=45.0, end_trim=60)
        self.assertEqual(self.window._episode_resume_start(0, 1139, 1200), (0, 1136))
        self.assertEqual(self.window._episode_resume_start(23, 1140, 1200), (-1, 0.0))

    def test_unknown_resume_duration_honors_trimmed_tail_after_load(self):
        self.window._apply_resume_state({'current_index': 0, 'time_pos_s': 1140})
        self.window.update_duration(1200)
        APP.processEvents()
        self.window.player.play.assert_called_with(
            self.pm._episode_path_for_index(1), start_position=45.0, end_trim=60)

    def test_fresh_intro_skip_is_not_mistaken_for_completed_resume(self):
        self.window._settings['show_trim']['King of the Hill']['end_seconds'] = 0
        self.window.play_index(0, bypass_bump_gate=True)
        self.window.update_duration(60)
        APP.processEvents()
        self.assertEqual(self.pm.current_index, 0)
        self.assertEqual(self.window.player.play.call_count, 1)

    def test_active_end_is_unchanged_until_next_load_and_excludes_prerolls(self):
        self.window.play_index(0, bypass_bump_gate=True)
        self.window._settings['show_trim']['King of the Hill']['end_seconds'] = 0
        self.assertEqual(self.window._episode_playback_end(1200), 1140)
        self.window._pending_bump_item = {'type': 'bump'}
        self.assertEqual(self.window._episode_playback_end(1200), 1200)
        self.window._pending_bump_item = None
        self.window.play_index(1, bypass_bump_gate=True)
        self.assertEqual(self.window._episode_playback_end(1200), 1200)

    def test_completed_trimmed_tail_is_not_penalized_as_a_manual_skip(self):
        self.window.play_index(0, bypass_bump_gate=True)
        self.window._last_time_pos = 1140
        self.window.total_duration = 1200
        self.window._maybe_record_episode_skip()
        key = self.pm._norm_path_key(self.pm._episode_path_for_index(0))
        self.assertEqual(self.pm.episode_exposure_scores[key], 100)

    def test_watchdog_uses_trimmed_end_on_windows_and_linux(self):
        self.window.on_playback_finished = Mock()
        self.window.missing_media_recovery = Mock(is_waiting=True)
        self.window.player.mpv.eof_reached = False
        self.window.player.is_paused = True
        for platform in ('win32', 'linux'):
            with self.subTest(platform=platform), patch('main.sys.platform', platform):
                self.window._play_index_last_mono = 0.0
                self.window.play_index(0, bypass_bump_gate=True)
                self.window._play_start_monotonic -= 3
                self.window._last_time_pos = 1140
                self.window.total_duration = 1200
                MainWindow._check_playback_end(self.window)
                APP.processEvents()
                self.window.on_playback_finished.assert_called_once()
                self.window.on_playback_finished.reset_mock()

    def test_corrupt_settings_are_safe(self):
        for value in (None, [], 'invalid', {'King of the Hill': None},
                      {'King of the Hill': {'start_seconds': float('nan'), 'end_seconds': {}}}):
            with self.subTest(value=value):
                self.window._settings['show_trim'] = value
                self.assertEqual(self.window._episode_trim(0), (0, 0))

    def test_deferred_trim_completion_is_cancelled_by_navigation_and_stop(self):
        self.window.on_playback_finished = MethodType(MainWindow.on_playback_finished, self.window)
        self.window.play_next = Mock()
        self.window.player.mpv.idle_active = True
        self.window.play_index(0, bypass_bump_gate=True)
        with patch('main.QTimer.singleShot') as timer:
            self.window.on_playback_finished()
            callback = timer.call_args.args[1]
        self.window.play_index(1, bypass_bump_gate=True)
        callback()
        self.window.play_next.assert_not_called()

        with patch('main.QTimer.singleShot') as timer:
            self.window.on_playback_finished()
            callback = timer.call_args.args[1]
        self.window._pause_sleep_countdown = Mock()
        self.window.missing_media_recovery = Mock()
        MainWindow.stop_playback(self.window)
        callback()
        self.window.play_next.assert_not_called()


if __name__ == '__main__':
    unittest.main()
