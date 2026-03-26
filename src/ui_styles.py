DARK_THEME = """
QMainWindow {
    background-color: #2b2b2b;
    color: #e0e0e0;
}
QWidget {
    background-color: #2b2b2b;
    color: #e0e0e0;
}
QTabWidget::pane {
    border: 1px solid #444;
    background-color: #2b2b2b;
    top: -1px;
}
QTabBar::tab {
    background-color: #333333;
    color: #aaaaaa;
    border: 1px solid #444;
    border-bottom: none;
    padding: 6px 16px;
    margin-right: 2px;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}
QTabBar::tab:hover {
    background-color: #3a3a5a;
    color: #e0e0e0;
}
QTabBar::tab:selected {
    background-color: #0e1a77;
    color: #ffffff;
    border-color: #0e1a77;
}
QTreeWidget {
    background-color: #333333;
    color: #ffffff;
    border: 1px solid #444;
    outline: 0;
}
QTreeWidget::item:hover {
    background-color: #3a3a5a;
}
QTreeWidget::item:selected {
    background-color: #0e1a77;
    color: #ffffff;
}
QListWidget {
    background-color: #333333;
    color: #ffffff;
    border: 1px solid #444;
    outline: 0;
}
QListWidget::item:hover {
    background-color: #3a3a5a;
}
QListWidget::item:selected {
    background-color: #0e1a77;
    color: #ffffff;
}
QPushButton {
    background-color: #444444;
    color: white;
    border: 1px solid #555;
    padding: 5px;
    border-radius: 3px;
}
QPushButton:hover {
    background-color: #555555;
}
QPushButton:pressed {
    background-color: #222222;
}
QLabel {
    color: #e0e0e0;
}
QSlider::groove:horizontal {
    border: 1px solid #444;
    height: 8px;
    background: #333;
    margin: 2px 0;
}
QSlider::handle:horizontal {
    background: #5cacf2;
    border: 1px solid #5cacf2;
    width: 14px;
    height: 14px;
    margin: -4px 0;
    border-radius: 7px;
}
"""
