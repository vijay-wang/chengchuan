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
QLineEdit { background: #0c1420; border: 1px solid #293b51; border-radius: 6px;
            padding: 9px; selection-background-color: #23695e; }
QLineEdit:focus { border: 1px solid #5ad7bf; }
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
QFrame#finding { background: #132130; border: 1px solid #24364a; border-radius: 6px; }
QProgressBar { height: 4px; min-height: 4px; max-height: 4px; background: #24364a;
               border: none; border-radius: 2px; }
QProgressBar::chunk { background: #5ad7bf; border-radius: 2px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 0; }
QScrollBar::handle:vertical { background: #33475d; border-radius: 3px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QSplitter::handle { background: #203044; width: 1px; height: 1px; }
QToolTip { background: #213348; color: #dce6f0; border: 1px solid #445b72; }
"""
