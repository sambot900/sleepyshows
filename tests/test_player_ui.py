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
            self.stack.enter_context(patch.object(main.MainWindow, method))
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


if __name__ == "__main__":
    unittest.main()
