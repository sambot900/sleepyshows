"""Exercise Auto-Show settings and responsive controls with real Qt widgets."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_playback_regressions import APP
import main
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QDialog, QPushButton, QSpinBox


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

    def close_window(self):
        self.window.close()
        for timer in self.window.findChildren(QTimer):
            timer.stop()
        self.window.deleteLater()
        APP.processEvents()

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
        self.assertTrue(button.isVisible())

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
