"""Central visual theme: flat clinical chrome, stylesheet and small helpers.

Design language: light & flat. White surfaces, hairline borders, generous
whitespace, neutral monochrome chrome with a *single* restrained black/gray
accent used only for interaction (active tab, primary action, focus). The clinical
red / amber / green (``PALETTE`` / ``STATUS_META`` from the report module) is
reserved strictly for result *data* — status text, table cells, chart wedges —
so the screen reads the same as the printed PDF.
"""

import os
import sys

# Make src/ importable so we share the report's palette/metadata.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
try:
    from generate_report import PALETTE, STATUS_META  # noqa: E402
except Exception:  # pragma: no cover - fallback if src not importable
    PALETTE = {
        "validated": "#b2182b", "candidate": "#ef8a47", "potential": "#fde08a",
        "nomarker": "#1a9850", "notassessed": "#9e9e9e", "ok": "#1a9850",
        "low": "#fde08a", "no": "#9e9e9e", "known": "#b2182b",
        "novel": "#5e4fa2", "band": "#1f3b57", "rowalt": "#f6f8fb",
        "grid": "#cfd8e3", "text": "#1a1a1a", "muted": "#5a6472",
    }
    STATUS_META = {}

# Pin the *on-screen* data palette to the clinical colours, independent of the
# PDF report's swatches. ``generate_report.PALETTE`` is greyscale (the settled
# monochrome report), and the report runs in its own subprocess that re-imports
# ``generate_report`` — so it keeps that greyscale. The GUI's ``widgets``,
# ``charts``, ``dashboard`` and ``surveillance`` all import the *name*
# ``PALETTE`` from here, so this GUI-local copy keeps the results colour-coded
# (red / amber / green / purple) on screen. This rebind must run immediately
# after the import above, before any consumer imports ``theme``.
_GUI_PALETTE_OVERRIDES = {
    "validated":   "#b2182b",   # red    — resistant / validated marker
    "candidate":   "#ef8a47",   # orange — candidate marker
    "potential":   "#e0a82e",   # readable amber (pale report swatch was #fde08a)
    "nomarker":    "#1a9850",   # green  — no resistance marker
    "notassessed": "#6b7280",   # neutral grey — absence of data
    "ok":          "#1a9850",   # green  — adequate coverage
    "low":         "#e0a82e",   # amber  — low coverage
    "no":          "#6b7280",   # neutral grey — no coverage
    "known":       "#b2182b",   # red    — known resistance marker (lollipop)
    "novel":       "#5e4fa2",   # purple — uncharacterised / novel
    "band":        "#1f3b57",   # navy   — chart header band
    "rowalt":      "#f6f8fb",   # pale row-stripe tint
    "grid":        "#cfd8e3",   # chart backbone / grid line
}
PALETTE = dict(PALETTE)
PALETTE.update(_GUI_PALETTE_OVERRIDES)

# -- Chrome tokens (neutral monochrome + one accent) ---------------------
# Two complete token sets: the default light theme and a near-black dark
# theme. Pick at launch with ``PF_THEME=dark`` (or ``light``); anything else
# falls back to light. The data ``PALETTE`` (below) and the PDF report are
# deliberately left on their light/clinical values so results read the same
# on screen, in dark mode and on the printed page.
_LIGHT = {
    "PAGE": "#ffffff",
    "SURFACE": "#ffffff",
    "SUBTLE": "#fafbfc",          # faint fill for header rows / tracks
    "BORDER": "#d1d5db",          # hairline
    "BORDER_STRONG": "#9ca3af",
    "HEADING": "#0f172a",
    "TEXT": "#374151",
    "MUTED": "#6b7280",
    "FAINT": "#9ca3af",
    "ACCENT": "#1f2937",          # the single restrained accent (near-black gray)
    "ACCENT_DARK": "#111827",
    "NEUTRAL_BAR": "#475569",     # slate, for progress/gauges
    "ON_ACCENT": "#ffffff",       # text/icons painted over an accent fill
    "DISABLED": "#b8bfc8",        # disabled button text
    "DANGER_TEXT": "#b91c1c",     # destructive action text
    "DANGER_BG": "#fef2f2",       # destructive hover wash
    "DANGER_BORDER": "#fecaca",
    "ACCENT_WASH": "#f3f4f6",     # selected tab/row tint (neutral gray)
    "TRACK": "#eef0f3",           # progress-bar groove
    "SCROLL_HANDLE": "#d1d5db",
    "GRID": "#c7ccd4",            # chart backbone / grid line
    "CHART_TRACK": "#eef1f5",     # empty / no-value chart fill
}

_DARK = {
    "PAGE": "#000000",            # true black page
    "SURFACE": "#0a0a0b",         # raised cards / sidebars (barely lifted off black)
    "SUBTLE": "#101012",          # header rows / faint fills
    "BORDER": "#232326",          # hairline (kept visible for card separation)
    "BORDER_STRONG": "#3a3a40",
    "HEADING": "#f4f4f5",
    "TEXT": "#d4d4d8",
    "MUTED": "#9ca3af",
    "FAINT": "#71717a",
    "ACCENT": "#d4d4d8",          # light gray accent (holds on black)
    "ACCENT_DARK": "#a1a1aa",
    "NEUTRAL_BAR": "#64748b",
    "ON_ACCENT": "#000000",       # black text/icons over the light accent
    "DISABLED": "#52525b",
    "DANGER_TEXT": "#f87171",     # brighter red for dark ground
    "DANGER_BG": "#2a1517",
    "DANGER_BORDER": "#7f1d1d",
    "ACCENT_WASH": "#18181b",     # neutral selected tint (near-black)
    "TRACK": "#101012",
    "SCROLL_HANDLE": "#3a3a40",
    "GRID": "#232326",
    "CHART_TRACK": "#101012",
}

def detect_system_mode():
    """Best-effort read of the OS light/dark preference.

    Returns ``"dark"`` or ``"light"``. Every probe is wrapped so a missing
    tool, unusual desktop or locked-down environment simply falls back to
    ``"light"`` rather than raising at import time. Runs before any
    QApplication exists, so it shells out to the platform's own setting rather
    than reading a Qt palette.
    """
    import subprocess
    try:
        if sys.platform == "darwin":
            # `defaults read -g AppleInterfaceStyle` prints "Dark" only when the
            # user is in dark mode; the key is absent (non-zero exit) otherwise.
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=2)
            return "dark" if "dark" in out.stdout.strip().lower() else "light"
        if sys.platform.startswith("win"):
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            # AppsUseLightTheme == 0 means apps should render dark.
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return "light" if val else "dark"
        # Linux / other: ask the GNOME/freedesktop setting if present.
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=2)
        if "dark" in out.stdout.strip().lower():
            return "dark"
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
            capture_output=True, text=True, timeout=2)
        if "dark" in out.stdout.strip().lower():
            return "dark"
    except Exception:
        pass
    return "light"


# An explicit ``PF_THEME`` still wins; otherwise follow the OS preference.
_env_mode = os.environ.get("PF_THEME", "").strip().lower()
MODE = _env_mode if _env_mode in ("light", "dark") else detect_system_mode()


def _bind_tokens(tokens):
    """Rebind the module-level colour names to one token set."""
    g = globals()
    for name, value in tokens.items():
        g[name] = value


_bind_tokens(_DARK if MODE == "dark" else _LIGHT)

FONT_STACK = '"Helvetica Neue", "Segoe UI", Arial, sans-serif'
MONO_STACK = '"SF Mono", "JetBrains Mono", Menlo, Consolas, monospace'


def _calendar_icon_path():
    """Paint a small calendar glyph to a cached PNG and return its path.

    Rendered with QImage (not QPixmap) so it works at import time, before any
    QApplication exists — the path is then referenced from the QDateEdit
    drop-down in ``APP_QSS`` so the date field clearly reads as a date picker.
    Returns "" on any failure, leaving the default drop-down arrow.
    """
    try:
        import tempfile

        from PyQt5.QtCore import QRectF, Qt
        from PyQt5.QtGui import QImage, QPainter, QPen, QColor

        px = 16
        img = QImage(px, px, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(MUTED))
        pen.setWidthF(1.3)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(2.0, 3.5, 12.0, 10.5), 1.6, 1.6)  # body
        p.drawLine(2, 6, 14, 6)                                    # header rule
        p.drawLine(5, 2, 5, 4)                                     # binding posts
        p.drawLine(11, 2, 11, 4)
        p.setBrush(QColor(MUTED))
        p.setPen(Qt.NoPen)
        for cx in (5.0, 8.0, 11.0):                               # day dots
            for cy in (9.0, 11.5):
                p.drawEllipse(QRectF(cx - 0.7, cy - 0.7, 1.4, 1.4))
        p.end()
        path = os.path.join(tempfile.gettempdir(), "pf_calendar_icon.png")
        img.save(path)
        return path.replace("\\", "/")
    except Exception:
        return ""


_CAL_ICON = _calendar_icon_path()
_CAL_DROPDOWN = ("image: url(%s);" % _CAL_ICON) if _CAL_ICON else ""


def _tree_arrow_path(direction):
    """Paint a small outline disclosure chevron to a cached PNG; return its path.

    ``direction`` is ``"right"`` (collapsed) or ``"down"`` (expanded). Drawn as a
    thin stroked chevron (``>`` / ``⌄``) rather than a filled triangle, in the
    active theme's foreground (``TEXT``) and cached under a mode-tagged name, so
    the arrow tracks the app theme instead of Qt's native macOS branch indicator
    -- which is drawn in the *system* appearance colour and so goes invisible
    against a light tree when the OS is in dark mode (and vice-versa).
    Returns "" on any failure, leaving the native indicator in place.
    """
    try:
        import tempfile

        from PyQt5.QtCore import QPointF, Qt
        from PyQt5.QtGui import QImage, QPainter, QColor, QPen, QPolygonF

        px = 16
        img = QImage(px, px, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(TEXT))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if direction == "down":   # expanded: chevron pointing down (⌄)
            pts = [QPointF(4.5, 6.5), QPointF(8.0, 10.0), QPointF(11.5, 6.5)]
        else:                     # collapsed: chevron pointing right (>)
            pts = [QPointF(6.5, 4.5), QPointF(10.0, 8.0), QPointF(6.5, 11.5)]
        p.drawPolyline(QPolygonF(pts))
        p.end()
        path = os.path.join(
            tempfile.gettempdir(),
            "pf_tree_arrow_%s_%s.png" % (direction, MODE))
        img.save(path)
        return path.replace("\\", "/")
    except Exception:
        return ""


def _data_tree_branch_qss():
    """QSS replacing #DataTree disclosure triangles with theme-coloured arrows.

    Empty string if the arrows can't be painted (no QApplication yet / paint
    error), leaving Qt's native branch indicator untouched.
    """
    closed = _tree_arrow_path("right")
    opened = _tree_arrow_path("down")
    if not closed or not opened:
        return ""
    return (
        "#DataTree::branch:has-children:!has-siblings:closed,\n"
        "#DataTree::branch:closed:has-children:has-siblings {\n"
        "    border-image: none; image: url(%s); }\n"
        "#DataTree::branch:open:has-children:!has-siblings,\n"
        "#DataTree::branch:open:has-children:has-siblings {\n"
        "    border-image: none; image: url(%s); }\n" % (closed, opened))

_QSS_TEMPLATE = """
* {{ font-family: {font}; }}
QWidget {{ background: {page}; color: {text}; font-size: 13px; }}
QLabel {{ background: transparent; }}

/* -- Top bar (used by the in-app report viewer) -- */
#TopBar {{ background: {surface}; border-bottom: 1px solid {border}; }}

/* -- Left sidebar navigation -- */
#Sidebar {{ background: {surface}; border-right: 1px solid {border}; }}
#SidebarFoot {{ color: {faint}; font-size: 11px; letter-spacing: 0.3px; }}
#NavItem {{
    text-align: left; background: transparent; border: none;
    border-radius: 3px; padding: 0 14px; color: {muted};
    font-size: 14px; font-weight: 500; outline: none;
}}
#NavItem:hover {{ background: {subtle}; color: {heading}; }}
#NavItem:checked {{
    background: transparent; color: {heading}; font-weight: 700;
    border-left: 3px solid {accent};
}}

/* Hierarchical nav tree (Results expands into its sections).
   The active row reads as a left accent bar + bold dark text — no fill, no
   blue wash. We keep selection out of the decoration column
   (show-decoration-selected: 0) and force the view's selection fill
   transparent so the default blue highlight never paints the indent/branch. */
#NavTree {{
    background: transparent; border: none; outline: none;
    show-decoration-selected: 0;
    selection-background-color: transparent; selection-color: {heading};
}}
#NavTree::item {{
    height: 33px; border-radius: 0; padding-left: 6px;
    color: {muted}; font-size: 13.5px; font-weight: 500;
    border-left: 3px solid transparent;
}}
#NavTree[rail="true"]::item {{ padding-left: 15px; border-left: none; }}
#NavTree::item:hover {{ color: {heading}; }}
#NavTree::item:selected {{
    background: transparent; color: {heading}; font-weight: 700;
    border-left: 3px solid {accent};
}}
/* Native (animated) expand arrows for parent rows; keep selection out of the
   branch column so the blue highlight never paints the indent. */
#NavTree::branch {{ background: transparent; }}
#NavTree::branch:selected {{ background: transparent; }}

/* Collapsible data trees (drug/gene/mutation groups): replace the native macOS
   disclosure triangle -- drawn in the system-appearance colour, so invisible on
   a light tree when the OS is in dark mode -- with a theme-coloured arrow. */
{data_tree_branch}

/* -- Dialogs / popups -- */
QDialog {{ background: {page}; }}
QDialog QLabel {{ color: {text}; }}
#DialogTitle {{ font-size: 16px; font-weight: 700; color: {heading}; }}
#DialogHint {{ color: {muted}; font-size: 12px; }}
#FormSection {{ font-size: 11px; font-weight: 700; color: {faint};
                letter-spacing: 0.7px; }}
QMessageBox {{ background: {surface}; }}
QMessageBox QLabel {{ color: {text}; font-size: 13px; }}
QInputDialog {{ background: {surface}; }}
QInputDialog QLabel {{ color: {text}; }}

/* -- Page headers -- */
#PageTitle {{ font-size: 18px; font-weight: 700; color: {heading}; }}
#PageHint {{ color: {muted}; font-size: 12px; }}
#SectionTitle {{ font-size: 12px; font-weight: 700; color: {muted};
                 letter-spacing: 0.6px; }}
#FieldCaption {{ font-size: 10.5px; font-weight: 600; color: {muted};
                 letter-spacing: 0.4px; text-transform: uppercase; }}

/* -- Cards / containers -- */
#Card {{ background: {surface}; border: 1px solid {border}; border-radius: 3px; }}
#CardTitle {{ font-size: 12px; font-weight: 700; color: {muted};
              letter-spacing: 0.6px; }}

/* -- Buttons (flat, hairline) -- */
QPushButton {{
    background: {surface}; border: 1px solid {border_strong};
    border-radius: 3px; padding: 7px 13px; color: {text}; font-size: 13px;
}}
QPushButton:hover {{ background: {subtle}; border-color: {faint}; }}
QPushButton:disabled {{ color: {disabled}; border-color: {border}; }}
QPushButton#Primary {{
    background: {accent}; border: 1px solid {accent}; color: {on_accent};
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background: {accent_dark}; border-color: {accent_dark}; }}
QPushButton#Danger {{ color: {danger_text}; border-color: {border_strong}; }}
QPushButton#Danger:hover {{ background: {danger_bg}; border-color: {danger_border}; }}
QPushButton#Ghost {{ border: none; background: transparent; color: {accent};
                     padding: 4px 6px; }}
QPushButton#Ghost:hover {{ color: {accent_dark}; }}

/* -- Inputs -- */
QLineEdit, QComboBox, QSpinBox, QDateEdit {{
    background: {surface}; border: 1px solid {border_strong};
    border-radius: 3px; padding: 6px 8px;
    selection-background-color: {accent}; selection-color: {on_accent};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDateEdit:focus {{
    border: 1px solid {accent};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
/* Editable combo (e.g. the District field): its embedded line edit is itself a
   QLineEdit, so the rule above would paint a second border/background inside
   the combo frame and double the padding. Flatten it so the selected text sits
   cleanly inside the one combo box. */
QComboBox QLineEdit {{
    border: none; border-radius: 0; padding: 0; background: transparent;
    selection-background-color: {accent}; selection-color: {on_accent};
}}
/* Drop-down list: roomy, clearly highlighted rows so the keyboard/mouse
   cursor position is obvious and long entries stay fully readable. */
QComboBox QAbstractItemView {{
    background: {surface}; border: 1px solid {border_strong};
    border-radius: 3px; padding: 4px; outline: none;
    selection-background-color: {accent}; selection-color: {on_accent};
}}
QComboBox QAbstractItemView::item {{
    min-height: 26px; padding: 2px 10px; color: {text}; border-radius: 3px;
}}
QComboBox QAbstractItemView::item:hover {{
    background: {subtle}; color: {heading};
}}
QComboBox QAbstractItemView::item:selected {{
    background: {accent}; color: {on_accent};
}}
QDateEdit::drop-down {{
    border: none; width: 24px; subcontrol-position: center right;
    {cal_dropdown}
}}

/* Calendar popup for QDateEdit: flat surface, hairline border, accent today. */
QCalendarWidget QWidget {{ background: {surface}; }}
QCalendarWidget QToolButton {{
    background: transparent; color: {heading}; font-weight: 600;
    border: none; padding: 4px 8px;
}}
QCalendarWidget QToolButton:hover {{ background: {subtle}; border-radius: 3px; }}
QCalendarWidget QMenu {{ background: {surface}; border: 1px solid {border}; }}
QCalendarWidget #qt_calendar_navigationbar {{
    background: {subtle}; border-bottom: 1px solid {border};
}}
QCalendarWidget QAbstractItemView {{
    background: {surface}; color: {text}; outline: none;
    selection-background-color: {accent}; selection-color: {on_accent};
}}
QCalendarWidget QAbstractItemView:disabled {{ color: {faint}; }}

/* Spin boxes read as clean flat numeric fields. The native up/down steppers
   render as fragile detached stubs across platforms, so we drop them entirely
   (value is typed) to keep the flat clinical look consistent with the inputs. */
QSpinBox {{ qproperty-buttonSymbols: NoButtons; }}

/* -- Inner tabs (dashboard): flat segmented pills, no pane border -- */
QTabWidget::pane {{ border: none; top: 8px; background: transparent; }}
QTabBar {{ qproperty-drawBase: 0; background: transparent; }}
QTabBar::tab {{
    background: transparent; padding: 7px 15px; margin-right: 6px;
    color: {muted}; border: none; border-radius: 3px; font-size: 13px;
}}
QTabBar::tab:hover {{ background: {subtle}; color: {heading}; }}
QTabBar::tab:selected {{ background: {accent_wash}; color: {accent};
                         font-weight: 600; }}

/* -- Tables (data-forward) -- */
QTableWidget {{
    background: {surface}; border: 1px solid {border}; border-radius: 3px;
    gridline-color: {subtle}; alternate-background-color: {subtle};
}}
QHeaderView::section {{
    background: {subtle}; color: {text}; padding: 8px 10px; border: none;
    border-bottom: 1px solid {border}; font-weight: 600; font-size: 11px;
}}
QTableWidget::item {{ padding: 6px 8px; border: none; }}
QTableWidget::item:selected {{ background: {accent_wash}; color: {text}; }}
QTableCornerButton::section {{ background: {subtle}; border: none; }}

/* -- Lists -- */
QListWidget {{ background: transparent; border: none; }}
QListWidget::item {{ border-bottom: 1px solid {border}; }}
QListWidget::item:selected {{ background: {accent_wash}; color: {text}; }}

/* -- Trends run-sample popup (click a trend dot) -- */
#SamplePicker {{ background: {subtle}; border: 1px solid {border};
                 border-radius: 3px; padding: 2px; outline: none; }}
#SamplePicker::item {{ padding: 6px 9px; border-bottom: 1px solid {border};
                       border-radius: 3px; font-weight: 500; }}
#SamplePicker::item:hover {{ background: {surface}; }}
#SamplePicker::item:selected {{ background: {accent_wash}; color: {heading};
                                font-weight: 600; }}
#SampleMeta {{ background: transparent; font-size: 12px; }}

/* -- Progress + groups (flat, monochrome) -- */
QProgressBar {{
    border: none; background: {track}; border-radius: 4px; height: 8px;
    text-align: center; color: {muted}; font-size: 10px;
}}
QProgressBar::chunk {{ background: {neutral_bar}; border-radius: 4px; }}
QGroupBox {{ border: none; margin-top: 6px; font-weight: 600; color: {heading}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 0; padding: 0 0 4px 0; }}

/* -- Scrollbars -- */
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {scroll_handle}; border-radius: 4px;
                               min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {scroll_handle}; border-radius: 4px;
                                 min-width: 28px; }}
"""


def _build_qss():
    """Render ``_QSS_TEMPLATE`` against the currently-bound colour tokens."""
    return _QSS_TEMPLATE.format(
        font=FONT_STACK, page=PAGE, surface=SURFACE, subtle=SUBTLE,
        border=BORDER, border_strong=BORDER_STRONG, heading=HEADING, text=TEXT,
        muted=MUTED, faint=FAINT, accent=ACCENT, accent_dark=ACCENT_DARK,
        neutral_bar=NEUTRAL_BAR, cal_dropdown=_CAL_DROPDOWN,
        data_tree_branch=_data_tree_branch_qss(),
        on_accent=ON_ACCENT, disabled=DISABLED, danger_text=DANGER_TEXT,
        danger_bg=DANGER_BG, danger_border=DANGER_BORDER,
        accent_wash=ACCENT_WASH, track=TRACK, scroll_handle=SCROLL_HANDLE)


APP_QSS = _build_qss()


def set_mode(mode):
    """Switch the active theme in place (``"light"`` / ``"dark"``).

    Rebinds the module colour tokens and rebuilds ``APP_QSS``. Callers re-apply
    ``APP_QSS`` to the QApplication and repaint the custom-painted charts.
    Returns the new ``APP_QSS``.
    """
    global MODE, APP_QSS
    MODE = "dark" if str(mode).lower() == "dark" else "light"
    _bind_tokens(_DARK if MODE == "dark" else _LIGHT)
    APP_QSS = _build_qss()
    return APP_QSS
