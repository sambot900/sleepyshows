"""Exercise settings and responsive player controls with real Qt widgets."""
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_playback_regressions import APP
import main
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox, QDialog, QMenu, QPushButton, QSpinBox


class PlayerUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="Sleepy Shows UI ")
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "XDG_CONFIG_HOME": self.tmp.name,
            "APPDATA": self.tmp.name,
            "LOCALAPPDATA": self.tmp.name,
        }))
        for module in ("main", "playlist_manager"):
            self.stack.enter_context(patch(f"{module}.get_local_playlists_dir", return_value=self.tmp.name))
        self.stack.enter_context(patch("main.get_local_bumps_scripts_dir", return_value=self.tmp.name))
        for method in ("_schedule_probe_bump_video_durations", "_sync_keep_awake", "_start_startup_ambient"):
            # Qt's singleShot callable overload cannot safely inspect Mock on
            # some PySide builds. Use an ordinary Python callable for timers.
            self.stack.enter_context(patch.object(main.MainWindow, method, lambda self: None))
        self.stack.enter_context(patch.object(main.MpvAudioPlayer, "_init_mpv"))

        def init_fake_player(player):
            player.mpv = Mock(core_idle=True, pause=True, time_pos=None, path=None,
                              track_list=[], duration=None)

        self.stack.enter_context(patch.object(main.MpvPlayer, "_init_mpv", init_fake_player))
        Path(self.tmp.name, "King of the Hill.json").write_text(json.dumps({
            "shuffle_mode": "standard", "playlist": [{"type": "video", "path": "koth.mkv"}],
        }), encoding="utf-8")
        self.window = main.MainWindow()
        self.addCleanup(self.close_window)
        self.window.show()
        APP.processEvents()

    def close_window(self, window=None):
        window = self.window if window is None else window
        window.close()
        for timer in window.findChildren(QTimer):
            timer.stop()
        window.deleteLater()
        APP.processEvents()

    def test_auto_volume_controls_sync_and_save_the_reduction_and_manual_volume(self):
        window = self.window
        controls = window.play_mode_widget
        settings = window.bumps_mode_widget
        setting = settings.chk_auto_volume
        checkbox = controls.chk_auto_volume
        reduction = settings.findChild(QSpinBox, 'auto_volume_reduction')
        self.assertEqual(reduction.value(), 20)
        self.assertIsNone(controls.findChild(QSpinBox, 'auto_volume_reduction'))

        reduction.setValue(40)
        self.assertEqual(window._load_user_settings()['auto_volume_reduction_percent'], 40)
        self.assertFalse(checkbox.isChecked())
        setting.click()
        self.assertTrue(checkbox.isChecked())
        checkbox.click()
        self.assertFalse(setting.isChecked())
        checkbox.click()
        self.assertTrue(setting.isChecked())

        controls.slider_vol.setValue(65)
        self.assertEqual(window.player.mpv.volume, 65)
        with patch('sleep_timer.time.monotonic', return_value=100.0) as monotonic:
            window.player.mpv.pause = False
            window.player.mpv.core_idle = False
            window.start_sleep_timer(10)
            monotonic.return_value += 300
            window.sleep_timer._tick()
        self.assertAlmostEqual(window.player.mpv.volume, 52)
        self.assertEqual(controls.slider_vol.value(), 65)

        window.sleep_timer.pause()
        window.close()
        reopened = main.MainWindow()
        self.addCleanup(self.close_window, reopened)
        self.assertTrue(reopened.play_mode_widget.chk_auto_volume.isChecked())
        self.assertTrue(reopened.bumps_mode_widget.chk_auto_volume.isChecked())
        self.assertEqual(reopened.bumps_mode_widget.findChild(QSpinBox, 'auto_volume_reduction').value(), 40)
        self.assertEqual(reopened.play_mode_widget.slider_vol.value(), 65)
        self.assertEqual(reopened.player.mpv.volume, 65)

    def test_auto_volume_follows_countdown_pause_manual_adjustment_and_cancel(self):
        window = self.window
        timer = window.sleep_timer
        player = window.player.mpv
        window.fx_player.mpv = Mock()
        window.fx_player.set_volume(50)
        window.set_auto_volume_enabled(True)
        window.play_mode_widget.slider_vol.setValue(60)
        player.pause = False
        player.core_idle = False

        with patch('sleep_timer.time.monotonic', return_value=100.0) as monotonic:
            window.start_sleep_timer(10)
            self.assertEqual(player.volume, 60)
            monotonic.return_value += 300
            timer._tick()
            self.assertAlmostEqual(player.volume, 54)
            self.assertAlmostEqual(window.fx_player.mpv.volume, 45)

            player.pause = True
            timer.pause()
            monotonic.return_value += 1200
            timer._tick()
            self.assertAlmostEqual(player.volume, 54)

            player.pause = False
            timer.resume_if_needed()
            window.play_mode_widget.slider_vol.setValue(70)
            self.assertAlmostEqual(player.volume, 63)
            monotonic.return_value += 150
            timer._tick()
            self.assertAlmostEqual(player.volume, 59.5)

            window.play_mode_widget.chk_auto_volume.click()
            self.assertEqual(player.volume, 70)
            self.assertEqual(window.fx_player.mpv.volume, 50)
            window.play_mode_widget.chk_auto_volume.click()
            monotonic.return_value += 75
            timer._tick()
            self.assertAlmostEqual(player.volume, 63)
            window.cancel_sleep_timer()
            self.assertEqual(player.volume, 70)
            self.assertEqual(window.fx_player.mpv.volume, 50)

            window.start_sleep_timer(1)
            monotonic.return_value += 60
            with patch.object(window.player, 'set_volume', wraps=window.player.set_volume) as volume:
                timer._tick()
                self.assertEqual([call.args[0] for call in volume.call_args_list], [56, 70])
            self.assertFalse(timer.active)

    def test_auto_volume_follows_earliest_shutoff_across_midnight_and_resets_at_expiry(self):
        window = self.window
        timer = window.sleep_timer
        player = window.player.mpv
        player.stop.side_effect = lambda: setattr(player, 'core_idle', True)
        window.set_auto_volume_enabled(True)

        with patch('sleep_timer.datetime') as clock, patch(
            'sleep_timer.time.monotonic', return_value=100.0
        ) as monotonic:
            clock.now.return_value = datetime(2026, 10, 12, 23, 30)
            window.set_auto_sleep_settings(True, '00:30')
            window.start_sleep_timer(180)
            player.pause = False
            player.core_idle = False
            timer.resume_if_needed()
            self.assertEqual(player.volume, 100)

            clock.now.return_value += timedelta(minutes=30)
            monotonic.return_value += 1800
            timer._tick()
            self.assertAlmostEqual(player.volume, 90)

            reduction = window.bumps_mode_widget.findChild(QSpinBox, 'auto_volume_reduction')
            reduction.setValue(40)
            self.assertAlmostEqual(player.volume, 80)
            reduction.setValue(0)
            self.assertAlmostEqual(player.volume, 100)
            reduction.setValue(100)
            self.assertAlmostEqual(player.volume, 50)

            clock.now.return_value += timedelta(minutes=30)
            monotonic.return_value += 1800
            with patch.object(window.player, 'set_volume', wraps=window.player.set_volume) as volume:
                timer._tick()
                self.assertEqual([call.args[0] for call in volume.call_args_list], [0, 100])
            self.assertTrue(timer.auto_active)
            self.assertTrue(player.core_idle)

            clock.now.return_value += timedelta(hours=23)
            player.core_idle = False
            timer.resume_if_needed()
            self.assertEqual(player.volume, 100)
            clock.now.return_value += timedelta(minutes=30)
            monotonic.return_value += 1800
            timer._tick()
            self.assertAlmostEqual(player.volume, 50)

    def test_settings_dialog_saves_default_and_player_toggle_only_overrides_tonight(self):
        errors = []

        def configure_dialog():
            dialog = APP.activeModalWidget()
            try:
                self.assertIsInstance(dialog, QDialog)
                self.assertEqual(dialog.windowTitle(), "Auto-Show")
                combo = dialog.findChild(QComboBox)
                combo.setCurrentIndex(combo.findData("King of the Hill"))
                dialog.findChild(QSpinBox).setValue(45)
                dialog.findChild(main.ToggleSwitch).setChecked(True)
                next(button for button in dialog.findChildren(QPushButton)
                     if button.text() == "Save").click()
            except Exception as error:
                errors.append(error)
                if dialog:
                    dialog.reject()

        QTimer.singleShot(0, configure_dialog)
        self.window.show_auto_show_dialog()
        self.assertFalse(errors, errors)
        self.assertEqual(self.window.auto_show.show_name, "King of the Hill")
        self.assertEqual(self.window.auto_show.delay_minutes, 45)
        button = self.window.play_mode_widget.btn_auto_show
        self.assertEqual(button.text(), "AUTO-SHOW\nON")
        button.click()
        self.assertFalse(self.window.auto_show.enabled)
        self.assertTrue(self.window.auto_show.default_enabled)
        self.assertEqual(button.text(), "AUTO-SHOW\nOFF")
        with open(self.window._settings_path, encoding="utf-8") as handle:
            settings = json.load(handle)
        self.assertTrue(settings["auto_show_enabled"])
        self.assertFalse(settings["auto_show_night_override"]["enabled"])

    def test_auto_show_label_fits_large_fonts_and_reappears_after_expansion(self):
        self.window.set_mode(2)
        controls = self.window.play_mode_widget
        controls.sidebar_visible = False
        controls.sidebar_container.hide()
        controls.sidebar_container.setFixedWidth(0)
        button = controls.btn_auto_show
        button.setStyleSheet("font-size: 18pt; font-weight: bold;")
        for width in (1280, 760, 1920):
            self.window.resize(width, 800)
            APP.processEvents()
            controls._update_controls_size_mode(force=True)
            APP.processEvents()
            if button.isVisible():
                self.assertGreaterEqual(button.width(), button.fontMetrics().horizontalAdvance("AUTO-SHOW") + 16)
            checkbox = controls.chk_auto_volume
            self.assertTrue(checkbox.isVisible())
            self.assertGreaterEqual(checkbox.width(), checkbox.sizeHint().width())
        self.assertTrue(button.isVisible())

    def test_skip_sections_menu_saves_a_scrubbed_range_and_deletes_it(self):
        window = self.window
        window.set_mode(2)
        path = str(Path(self.tmp.name, 'S01E01.mkv'))
        window.bumps_enabled = False
        window.playlist_manager.current_playlist = [{'type': 'video', 'path': path}]
        window.current_playlist_filename = str(Path(self.tmp.name, 'King of the Hill.json'))
        window.play_index(0, bypass_bump_gate=True, start_position=0)
        window.player.mpv.path = path
        window.player.mpv.time_pos = 10.25
        window.update_duration(100)

        with patch.object(window.player, 'set_playback_end') as playback_end, patch.object(
            window.player, 'set_playback_keep_open', return_value='no'
        ) as keep_open:
            window.play_mode_widget.btn_skip_sections.click()
            menu = APP.activePopupWidget()
            self.assertIsInstance(menu, QMenu)
            menu.actions()[0].trigger()
            menu.close()
            dialog = window._skip_section_dialog
            self.assertTrue(dialog.isVisible())
            self.assertFalse(dialog.isModal())
            self.assertTrue(window.player.mpv.pause)
            self.assertEqual(dialog.time_field.text(), '00:10.250')
            playback_end.assert_called_once_with(None)
            keep_open.assert_called_once_with('yes')

            dialog.time_field.selectAll()
            QTest.keyClicks(dialog.time_field, '00:12')
            window.update_seeker(11)
            self.assertEqual(dialog.time_field.text(), '00:12')
            dialog.confirm.click()
            self.assertEqual(dialog.time_field.text(), '00:11')

            window.seek_video(3500)
            window.player.mpv.seek.assert_called_once_with(35, reference='absolute', precision='exact')
            window.update_seeker(35)
            self.assertEqual(dialog.time_field.text(), '00:35')
            dialog.confirm.click()
            self.assertIsNone(window._skip_section_dialog)
            self.assertFalse(window.player.mpv.pause)
            keep_open.assert_called_with('no')
            key = window.playlist_manager._norm_path_key(path)
            self.assertEqual(window._load_user_settings()['skip_sections'], {key: [[12, 35]]})

            window.play_mode_widget.btn_skip_sections.click()
            menu = APP.activePopupWidget()
            submenu = menu.actions()[1].menu()
            submenu.actions()[0].trigger()
            menu.close()
            self.assertEqual(window._load_user_settings()['skip_sections'], {})
            self.assertEqual(window._active_skip_ranges, ())

    def open_trim_dialog(self, configure):
        errors = []

        def edit_dialog():
            dialog = APP.activeModalWidget()
            try:
                self.assertIsInstance(dialog, QDialog)
                self.assertEqual(dialog.windowTitle(), 'Trim')
                configure(dialog)
            except Exception as error:
                errors.append(error)
                if dialog:
                    dialog.reject()

        QTimer.singleShot(0, edit_dialog)
        self.window.show_trim_dialog()
        self.assertFalse(errors, errors)

    def test_trim_saves_multiple_shows_and_reloads_without_movie_choices(self):
        def configure(dialog):
            shows = dialog.findChild(QComboBox, 'trim_show')
            choices = {shows.itemData(i) for i in range(shows.count())}
            self.assertTrue(all(entry['name'] not in choices for entry in main.SHOW_CATALOG
                                if entry['type'] == 'movie'))
            self.assertIn('The New Scooby-Doo! Movies (1972-1973)', choices)
            start = dialog.findChild(QSpinBox, 'trim_start_seconds')
            end = dialog.findChild(QSpinBox, 'trim_end_seconds')
            shows.setCurrentIndex(shows.findData('King of the Hill'))
            start.setValue(45)
            end.setValue(60)
            shows.setCurrentIndex(shows.findData('Futurama'))
            self.assertEqual((start.value(), end.value()), (0, 0))
            start.setValue(30)
            end.setValue(25)
            shows.setCurrentIndex(shows.findData('King of the Hill'))
            self.assertEqual((start.value(), end.value()), (45, 60))
            next(button for button in dialog.findChildren(QPushButton) if button.text() == 'Save').click()

        self.open_trim_dialog(configure)
        self.window._settings = self.window._load_user_settings()
        self.assertEqual(self.window._show_trim('King of the Hill'), (45, 60))
        self.assertEqual(self.window._show_trim('Futurama'), (30, 25))

        def verify_saved(dialog):
            shows = dialog.findChild(QComboBox, 'trim_show')
            shows.setCurrentIndex(shows.findData('King of the Hill'))
            self.assertEqual(dialog.findChild(QSpinBox, 'trim_start_seconds').value(), 45)
            self.assertEqual(dialog.findChild(QSpinBox, 'trim_end_seconds').value(), 60)
            dialog.reject()

        self.open_trim_dialog(verify_saved)

    def test_trim_cancel_discards_changes_and_zero_disables_trim(self):
        self.window._settings['show_trim'] = {'King of the Hill': {'start_seconds': 45, 'end_seconds': 60}}
        self.window.current_playlist_filename = str(Path(self.tmp.name, 'King of the Hill.json'))

        def cancel_changes(dialog):
            self.assertEqual(dialog.findChild(QComboBox, 'trim_show').currentData(), 'King of the Hill')
            dialog.findChild(QSpinBox, 'trim_start_seconds').setValue(0)
            dialog.reject()

        self.open_trim_dialog(cancel_changes)
        self.assertEqual(self.window._show_trim('King of the Hill'), (45, 60))

        def disable_trim(dialog):
            for field in dialog.findChildren(QSpinBox):
                field.setValue(0)
            next(button for button in dialog.findChildren(QPushButton) if button.text() == 'Save').click()

        self.open_trim_dialog(disable_trim)
        self.assertEqual(self.window._show_trim('King of the Hill'), (0, 0))
        self.assertNotIn('King of the Hill', self.window._settings['show_trim'])


if __name__ == "__main__":
    unittest.main()
