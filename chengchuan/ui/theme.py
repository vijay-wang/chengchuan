BACKGROUND = "#0c121b"
SURFACE = "#111c29"
TEXT = "#dce6f0"
MUTED = "#8193a8"
ACCENT = "#5ad7bf"
UP = "#f07b86"
DOWN = "#52c7a3"

STYLESHEET = """
QMainWindow, QWidget { background: #0c121b; color: #dce6f0; font-size: 13px; }
QWidget#sidebar { background: #101a27; border-right: 1px solid #203044; }
QWidget#inspector { background: #101a27; border-left: 1px solid #203044; }
QLabel { background: transparent; }
QLabel#brand { font-size: 25px; font-weight: 650; letter-spacing: 4px; }
QLabel#brandEnglish { color: #5ad7bf; font-size: 10px; letter-spacing: 2px; }
QLabel#stockTitle { font-size: 24px; font-weight: 600; }
QLabel#quote { font-size: 29px; font-weight: 600; }
QLabel#sectionTitle { font-size: 16px; font-weight: 600; }
QLabel#muted, QLabel#eyebrow { color: #8193a8; }
QLabel#eyebrow { font-size: 11px; letter-spacing: 1px; }
QLabel#summary { color: #5ad7bf; font-size: 17px; font-weight: 600; }
QLabel#metricValue { font-size: 19px; font-weight: 600; }
QLabel#notice { color: #c8af73; background: #24251f; border: 1px solid #3b3c30;
               border-radius: 6px; padding: 10px; font-size: 11px; }
QLabel#status { color: #8193a8; border-top: 1px solid #203044; padding: 7px 16px; }
QLineEdit, QComboBox { background: #0c1420; border: 1px solid #293b51; border-radius: 6px;
            padding: 9px; selection-background-color: #23695e; }
QLineEdit:focus { border: 1px solid #5ad7bf; }
QComboBox QAbstractItemView { background: #142233; selection-background-color: #20374a; }
QCheckBox { spacing: 6px; color: #8193a8; }
QCheckBox::indicator { width: 14px; height: 14px; }
QPushButton { background: #172536; border: 1px solid #2a3c51; border-radius: 5px;
              padding: 7px 12px; }
QPushButton:hover { background: #22364b; border-color: #53718c; }
QPushButton:checked, QPushButton#primary { background: #193d39; color: #8ee5d1;
                                        border-color: #316f64; }
QPushButton:disabled { color: #516276; background: #111c29; border-color: #203044; }
QListWidget { background: transparent; border: none; outline: none; }
QListWidget::item { border-radius: 6px; padding: 13px 11px; margin: 3px 0; }
QListWidget::item:selected { background: #20374a; color: #e6f7f3; }
QListWidget::item:hover { background: #192c3e; }
QFrame#metric { background: #142233; border: 1px solid #233548; border-radius: 7px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 0; }
QScrollBar::handle:vertical { background: #33475d; border-radius: 3px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #101a27; height: 7px; margin: 0; border: none; }
QScrollBar::handle:horizontal { background: #33475d; border-radius: 3px; min-width: 28px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QSplitter::handle { background: #203044; width: 1px; height: 1px; }
QToolTip { background: #213348; color: #dce6f0; border: 1px solid #445b72; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: #101a27; color: #8193a8; padding: 12px 18px; border-bottom: 2px solid #203044; }
QTabBar::tab:selected { color: #5ad7bf; border-bottom: 2px solid #5ad7bf; }
QTextBrowser, QTableView { background: #101a27; color: #dce6f0;
    alternate-background-color: #152335; selection-background-color: #28465e;
    selection-color: #f2f7fc; gridline-color: #233548;
    border: 1px solid #233548; border-radius: 5px; font-size: 12px; }
QTableView::item:selected { background: #28465e; color: #f2f7fc; }
QHeaderView::section { background: #172536; color: #8193a8; border: none; padding: 5px; }
QDateEdit, QSpinBox { background: #142233; border: 1px solid #293b51; padding: 4px; }
QMenu { background: #132130; border: 1px solid #354d65; padding: 6px; }
QCalendarWidget QWidget { background: #132130; color: #dce6f0; }
QCalendarWidget QToolButton { color: #dce6f0; background: #1b3044; border: none; border-radius: 4px; padding: 6px; }
QCalendarWidget QAbstractItemView { background: #132130; color: #dce6f0; selection-background-color: #276f62; selection-color: #ffffff; outline: none; }
QWidget#settingsSidebar { background: #101a27; border-right: 1px solid #203044; }
QFrame#settingsCard { background: #111c29; border: 1px solid #24364a; border-radius: 8px; }
QFrame#settingsCard QLabel { background: transparent; border: none; }
QFrame#settingsCard QCheckBox { background: transparent; }
QWidget#settingsFooter { border-top: 1px solid #203044; }
QSlider::groove:horizontal { background: #293b51; height: 5px; border-radius: 2px; }
QSlider::handle:horizontal { background: #5ad7bf; width: 14px; margin: -5px 0; border-radius: 7px; }
QWidget#indexOverview { background: #101a27; border-bottom: 1px solid #203044; }
QPushButton#indexCard { background: #132130; border: 1px solid #24364a; border-radius: 6px; padding: 0; }
QPushButton#indexCard:hover { border-color: #53718c; background: #192c3e; }
QPushButton#indexCard:checked { border-color: #5ad7bf; background: #193631; }
QPushButton#indexCard QLabel { background: transparent; border: none; }
QFrame#quoteCell { background: #132130; border: 1px solid #24364a; border-radius: 5px; }
QFrame#quoteCell QLabel { background: transparent; border: none; }
"""
