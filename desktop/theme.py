"""Qt adapter for the normative DESIGN.md palette."""
TOKENS = {'primary': '#176B68', 'background': '#EFF3F6', 'surface': '#FFFFFF',
          'ink': '#192E40', 'muted': '#536677', 'border': '#CBD6DE', 'danger': '#AD343E', 'focus': '#176B68'}


def stylesheet():
    t = TOKENS
    return f'''
    QWidget {{ font-family: "Microsoft YaHei UI", "Noto Sans CJK SC", sans-serif; font-size: 13px; color: {t['ink']}; }}
    QMainWindow, QStackedWidget {{ background: {t['background']}; }}
    QWidget#sidebar {{ background: {t['ink']}; }}
    QLabel#brand {{ color: white; font-size: 21px; font-family: Bahnschrift; font-weight: 600; padding: 20px 12px; }}
    QLabel#sidebarNote {{ color: #D6E3ED; padding: 10px 14px; }}
    QListWidget#navigation {{ background: transparent; color: #D6E3ED; border: none; outline: none; }}
    QListWidget#navigation::item {{ padding: 14px 16px; margin: 2px 8px; border-radius: 6px; }}
    QListWidget#navigation::item:selected {{ background: {t['primary']}; color: white; }}
    QLabel#pageTitle {{ font-size: 25px; font-weight: 600; }}
    QLabel#subtitle {{ color: {t['muted']}; padding-bottom: 10px; }}
    QLabel#notice {{ background: white; border-left: 4px solid {t['primary']}; padding: 12px; }}
    QLabel#error {{ color: {t['danger']}; background: #FFF0F1; padding: 10px; }}
    QFrame#panel {{ background: white; border: 1px solid {t['border']}; border-radius: 6px; }}
    QPushButton {{ min-height: 34px; padding: 0px 13px; border: 1px solid {t['border']}; background: white; border-radius: 6px; }}
    QPushButton:hover {{ background: #E2F0EE; border-color: {t['primary']}; }}
    QPushButton:pressed {{ background: #C7E3DF; }}
    QPushButton[primary="true"] {{ background: {t['primary']}; color: white; border-color: {t['primary']}; }}
    QPushButton[danger="true"] {{ color: {t['danger']}; border-color: {t['danger']}; }}
    QPushButton:disabled {{ color: #788B99; background: #E8EEF1; }}
    QPushButton:focus, QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 2px solid {t['focus']}; }}
    QLineEdit, QComboBox, QPlainTextEdit, QSpinBox {{ background: white; border: 1px solid {t['border']}; border-radius: 6px; padding: 7px; selection-background-color: {t['primary']}; }}
    QTableWidget {{ background: white; border: 1px solid {t['border']}; gridline-color: #E3E9EE; alternate-background-color: #F6F9FA; selection-background-color: #D5EBE8; selection-color: {t['ink']}; }}
    QHeaderView::section {{ background: #E8F0F3; padding: 9px; border: none; border-bottom: 1px solid {t['border']}; font-weight: 600; }}
    QStatusBar {{ background: white; border-top: 1px solid {t['border']}; }}
    QDialog {{ background: {t['background']}; }}
    QScrollArea {{ border: none; background: transparent; }}
    QToolTip {{ background: white; color: {t['ink']}; border: 1px solid {t['border']}; padding: 6px; }}
    '''
