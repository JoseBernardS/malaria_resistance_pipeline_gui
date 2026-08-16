"""Reusable Qt widgets for the flat clinical UI.

Chrome stays neutral/monochrome; the report's semantic ``PALETTE`` is used
only for data (status text, coverage cells, chart wedges).
"""

from PyQt5.QtCore import QEvent, QPoint, QRect, QSize, Qt
from PyQt5.QtWidgets import (QComboBox, QCompleter, QFrame, QHBoxLayout, QLabel,
                             QLayout, QProgressBar, QVBoxLayout, QWidget)

from . import theme
from .theme import PALETTE, STATUS_META  # re-exported for charts/dashboard

# Job status -> (text colour, weight). Chrome stays neutral; only completed/
# failed carry a hint of the clinical green/red since that *is* the result.
JOB_STATUS_COLORS = {
    "queued":    (theme.MUTED, "500"),
    "running":   (theme.ACCENT, "600"),
    "completed": ("#15803d", "600"),
    "failed":    ("#b91c1c", "600"),
    "stopped":   (theme.FAINT, "500"),
}

# Coverage status -> palette key (semantic, matches report)
COVERAGE_COLORS = {
    "OK": "ok",
    "LOW_COVERAGE": "low",
    "NO_COVERAGE": "no",
}


def card(title=None):
    """A white hairline-bordered card. Returns (frame, content_layout)."""
    frame = QFrame()
    frame.setObjectName("Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(16, 14, 16, 16)
    lay.setSpacing(12)
    if title:
        head = QLabel(title.upper())
        head.setObjectName("CardTitle")
        lay.addWidget(head)
    return frame, lay


def section_title(text):
    lbl = QLabel(text.upper())
    lbl.setObjectName("SectionTitle")
    return lbl


def hrule():
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet("background:%s;" % theme.BORDER)
    return line


class StatusText(QLabel):
    """A small status word coloured by its meaning (used in data tables)."""

    def __init__(self, status="", parent=None):
        super().__init__(parent)
        self.set_status(status)

    def set_status(self, status):
        color, weight = JOB_STATUS_COLORS.get(
            status, (theme.MUTED, "500"))
        self.setText(status.capitalize())
        self.setStyleSheet(
            "color:%s; font-weight:%s; font-size:12px;" % (color, weight))


class FlowLayout(QLayout):
    """A left-to-right layout that wraps items onto the next line when the
    available width runs out, so a row of figures stays fully readable in a
    narrow panel instead of clipping its labels."""

    def __init__(self, parent=None, hspacing=14, vspacing=10):
        super().__init__(parent)
        self._items = []
        self._hspace = hspacing
        self._vspace = vspacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _do_layout(self, rect, test_only):
        x, y = rect.x(), rect.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._hspace
            if next_x - self._hspace > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + self._vspace
                next_x = x + hint.width() + self._hspace
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


def fit_combo_popup(combo, extra=44):
    """Widen a combo's drop-down list so its longest item is fully readable.

    Qt sizes the popup to the field, which clips long entries (e.g. district
    names). Call after populating; safe to call again when items change.
    """
    fm = combo.view().fontMetrics()
    width = 0
    for i in range(combo.count()):
        width = max(width, fm.horizontalAdvance(combo.itemText(i)))
    if width:
        combo.view().setMinimumWidth(width + extra)


class SearchableComboBox(QComboBox):
    """Editable combo that opens its list on any click and filters as you type.

    A plain editable ``QComboBox`` only drops down when the mouse hits the
    narrow (invisible) arrow zone; clicking the text area just plants a cursor,
    so the list feels broken. Operators expect any click to reveal the choices,
    so we open the popup on mouse-press and attach a contains-match completer
    so typing narrows the options.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        comp = QCompleter(self.model(), self)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchContains)
        comp.setCompletionMode(QCompleter.PopupCompletion)
        self.setCompleter(comp)
        self.lineEdit().installEventFilter(self)

    def eventFilter(self, obj, event):
        # Open on mouse *release*, not press. For an editable combo Qt routes
        # the press to the line edit, so the combo's native click-to-open mouse
        # grab never arms and any popup shown on press is dismissed by the
        # trailing release. Showing on release means no further event can close
        # it, so the list stays open until an item is picked or focus leaves.
        if obj is self.lineEdit() and event.type() == QEvent.MouseButtonRelease:
            if (self.isEnabled() and event.button() == Qt.LeftButton
                    and not self.view().isVisible()):
                self.showPopup()
        return super().eventFilter(obj, event)


class KeyFigures(QWidget):
    """A plain strip of headline figures that wraps to stay readable.

    Each figure is its value next to a muted caption, with a thin vertical
    divider between figures — no cards, borders or accent bars, so it reads
    as a quiet summary line. In a narrow panel the figures wrap onto a second
    line instead of clipping their captions. The value keeps its semantic
    colour (red = resistant, green = no-marker). Values update in place by key.
    """

    def __init__(self, items, parent=None):
        """``items``: list of ``(key, caption, color)``."""
        super().__init__(parent)
        self._values = {}
        self._value_lbls = []
        self._captions = []
        self._dividers = []
        row = FlowLayout(self, hspacing=2, vspacing=10)
        last = len(items) - 1
        for i, (key, caption, color) in enumerate(items):
            row.addWidget(self._figure(key, caption, color, divider=i != last))

    def _figure(self, key, caption, color, divider):
        accent = color or theme.HEADING
        cell = QWidget()
        lay = QHBoxLayout(cell)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(7)
        value = QLabel("0")
        value.setStyleSheet(
            "font-size:20px; font-weight:600; color:%s;" % accent)
        caption_lbl = QLabel(caption)
        caption_lbl.setStyleSheet(
            "font-size:12px; color:%s;" % theme.MUTED)
        lay.addWidget(value)
        lay.addWidget(caption_lbl)
        lay.setAlignment(value, Qt.AlignBaseline)
        lay.setAlignment(caption_lbl, Qt.AlignBaseline)
        if divider:
            line = QFrame()
            line.setFixedWidth(1)
            line.setFixedHeight(20)
            line.setStyleSheet("background:%s;" % theme.BORDER)
            lay.addSpacing(16)
            lay.addWidget(line)
            self._dividers.append(line)
            lay.addSpacing(2)
        self._values[key] = value
        self._value_lbls.append(value)
        self._captions.append(caption_lbl)
        return cell

    def set_value(self, key, value):
        if key in self._values:
            self._values[key].setText(str(value))

    def restyle(self, colors):
        """Re-apply colours after a theme change. ``colors``: fresh value
        colours in item order (captions/dividers follow the active theme)."""
        for lbl, color in zip(self._value_lbls, colors):
            lbl.setStyleSheet(
                "font-size:20px; font-weight:600; color:%s;"
                % (color or theme.HEADING))
        for lbl in self._captions:
            lbl.setStyleSheet("font-size:12px; color:%s;" % theme.MUTED)
        for line in self._dividers:
            line.setStyleSheet("background:%s;" % theme.BORDER)


class StepRow(QWidget):
    """One pipeline step: a coloured dot, the name and an optional duration."""

    STATE_STYLE = {
        "pending": ("#cbd5e1", False),
        "running": (theme.ACCENT, True),
        "done":    ("#15803d", True),
        "error":   ("#b91c1c", True),
    }

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.state = "pending"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 5, 0, 5)
        self._dot = QLabel()
        self._dot.setFixedWidth(16)
        self._dot.setAlignment(Qt.AlignCenter)
        self._name = QLabel(name)
        self._duration = QLabel("")
        self._duration.setStyleSheet(
            "color:%s; font-size:11px;" % theme.FAINT)
        lay.addWidget(self._dot)
        lay.addWidget(self._name)
        lay.addStretch(1)
        lay.addWidget(self._duration)
        self.set_state("pending")

    def set_state(self, state):
        self.state = state
        color, bold = self.STATE_STYLE.get(state, self.STATE_STYLE["pending"])
        self._dot.setText("\u25cf")  # ●
        self._dot.setStyleSheet("color:%s; font-size:11px;" % color)
        weight = "600" if bold else "400"
        text_color = theme.HEADING if bold else theme.MUTED
        self._name.setStyleSheet(
            "color:%s; font-weight:%s; font-size:13px;" % (text_color, weight))

    def set_duration(self, text):
        self._duration.setText(text)


class ResourceGauge(QWidget):
    """A labelled slim bar for CPU / RAM / Disk usage."""

    def __init__(self, label, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(4)
        row = QHBoxLayout()
        self._label = QLabel(label)
        self._label.setStyleSheet("font-size:12px; color:%s;" % theme.MUTED)
        self._pct = QLabel("0%")
        self._pct.setStyleSheet(
            "font-size:12px; font-weight:600; color:%s;" % theme.HEADING)
        row.addWidget(self._label)
        row.addStretch(1)
        row.addWidget(self._pct)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        lay.addLayout(row)
        lay.addWidget(self._bar)

    def set_value(self, percent, suffix=""):
        pct = max(0, min(100, int(percent)))
        self._bar.setValue(pct)
        self._pct.setText(("%d%%" % pct) + (" " + suffix if suffix else ""))
