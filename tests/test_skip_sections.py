"""Skip sections use the active video and share the show Trim boundaries."""

from unittest.mock import patch
import unittest

import test_playback_regressions as playback_tests
from skip_sections import format_timestamp, merge_sections, parse_timestamp, saved_sections


class SkipSectionPlaybackTests(unittest.TestCase):
    def setUp(self):
        playback_tests.IsolatedPlaylistTest.setUp(self)
        self.window.current_playlist_filename = 'King of the Hill.json'
        self.key = self.window._episode_skip_key(0)
        self.window._settings = {
            'show_trim': {'King of the Hill': {'start_seconds': 30, 'end_seconds': 10}},
            'skip_sections': {self.key: [[15, 40], [80, 95], [90, 120], [160, 195]]},
        }
        self.window.player.mpv.seeking = False
        self.pm.set_shuffle_mode('off')

    def test_overlap_extends_intro_and_ends_at_earliest_tail(self):
        window = self.window
        window.play_index(0, bypass_bump_gate=True, start_position=0)
        window.player.play.assert_called_once_with(
            self.pm._episode_path_for_index(0), start_position=40, end_trim=10)
        window.update_duration(200)
        self.assertEqual(window._episode_playback_end(200), 160)
        window.player.set_playback_end.assert_called_once_with(160)
        self.assertEqual(window._episode_resume_start(0, 163, 200), (1, 0.0))

        window.play_index(1, bypass_bump_gate=True, start_position=0)
        window.update_duration(200)
        self.assertEqual(window._episode_playback_end(200), 190)
        window.player.set_playback_end.assert_called_once()

    def test_position_callbacks_seek_once_to_the_end_of_a_merged_range(self):
        window = self.window
        window.play_index(0, bypass_bump_gate=True, start_position=83)
        window.player.play.assert_called_once_with(
            self.pm._episode_path_for_index(0), start_position=120, end_trim=10)
        window.update_duration(200)
        with patch('main.time.monotonic', return_value=100.0):
            window.player.mpv.time_pos = 127
            window._skip_section_at_playhead(91)
            window.player.seek.assert_not_called()
            window.player.mpv.time_pos = 91
            window.is_seeking = True
            window._skip_section_at_playhead(91)
            window.player.seek.assert_not_called()
            window.is_seeking = False
            window._skip_section_at_playhead(91)
            window._skip_section_at_playhead(91)
            window.player.seek.assert_called_once_with(120, exact=True)

    def test_bad_saved_ranges_are_ignored_and_long_times_round_trip(self):
        stored = {'skip_sections': {self.key: [[9, 10], [2, 4], [4, 8], [2, 4],
                                               [8, 1], [-2, 4], [1, float('nan')], None]}}
        self.assertEqual(merge_sections(saved_sections(stored, self.key)), ((2, 8), (9, 10)))
        self.assertEqual(parse_timestamp(format_timestamp(7502.5)), 7502.5)
        self.assertEqual(parse_timestamp('2:30.5'), 150.5)
        for invalid in ('', '-1', '02:90', '1:60:00', 'nan', '1:2:3:4'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_timestamp(invalid)
