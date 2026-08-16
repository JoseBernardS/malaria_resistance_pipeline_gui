"""Report settings: a clinical report designer page.

Lets a lab shape the PDFs a clinician receives — organisation name, report
title, an optional logo (shown/positioned), page size, which sections appear
(treatment guidance, supporting variants, QC, coverage, collection site) and a
footer/disclaimer line. Two independent report scopes are configured side by
side in tabs — the per-sample "Sample report" and the combined "Overview
report" — each storing its own complete settings set. Settings persist via
:mod:`gui.reportcfg` (the ``app_config`` store) and are read by
``src/generate_report.py`` through per-scope JSON sidecars.

The page follows the app's flat idiom: a ``#PageTitle``/``#PageHint`` header
over ``#Card`` sections, ``#FieldCaption`` labels, and a ``#Primary`` Save with
a ``#Ghost`` preview that asks the loaded run to render the active tab's report
with the current settings.
"""

import os

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from .. import reportcfg, theme
from ..widgets import card


class _ReportForm(QWidget):
    """The full branding/layout/section form for a single report scope.

    One instance owns its own widgets, loads/saves against ``reportcfg`` for the
    given ``scope`` and exposes :meth:`save` for the hosting page's Save button.
    """

    def __init__(self, scope, parent=None):
        super().__init__(parent)
        self.scope = scope

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(0, 0, 6, 4)
        self._body.setSpacing(16)
        scroll.setWidget(body)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self._build_branding()
        self._build_layout_sections()
        self._build_footer()
        self._body.addStretch(1)

        self._load()

    # -- field helpers ---------------------------------------------------
    def _caption(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("FieldCaption")
        return lbl

    def _line(self, placeholder=""):
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setMinimumHeight(32)
        return edit

    # -- cards -----------------------------------------------------------
    def _build_branding(self):
        frame, lay = card("Branding")

        lay.addWidget(self._caption("Organisation name"))
        self.org_edit = self._line("e.g. Ghana Health Service \u2014 NMEP")
        lay.addWidget(self.org_edit)

        lay.addWidget(self._caption("Report title"))
        self.title_edit = self._line("Antimalarial Resistance \u2014 Clinical "
                                     "Report")
        lay.addWidget(self.title_edit)

        # Logo: a show toggle, a browse-to-file row with a live thumbnail, and
        # a position selector shared by the logo and the organisation name.
        self.logo_check = QCheckBox("Show a logo on the report")
        self.logo_check.stateChanged.connect(self._sync_logo_enabled)
        lay.addWidget(self.logo_check)

        logo_row = QHBoxLayout()
        logo_row.setSpacing(10)
        self._logo_preview = QLabel()
        self._logo_preview.setFixedSize(84, 44)
        self._logo_preview.setAlignment(Qt.AlignCenter)
        self._logo_preview.setStyleSheet(
            "border:1px solid %s; border-radius:4px; color:%s; font-size:10px;"
            % (theme.BORDER, theme.FAINT))
        logo_row.addWidget(self._logo_preview, 0, Qt.AlignVCenter)
        btn_col = QVBoxLayout()
        btn_col.setSpacing(4)
        self._logo_browse = QPushButton("Choose image\u2026")
        self._logo_browse.setObjectName("Ghost")
        self._logo_browse.setCursor(Qt.PointingHandCursor)
        self._logo_browse.clicked.connect(self._on_browse_logo)
        self._logo_clear = QPushButton("Remove")
        self._logo_clear.setObjectName("Ghost")
        self._logo_clear.setCursor(Qt.PointingHandCursor)
        self._logo_clear.clicked.connect(self._on_clear_logo)
        btn_col.addWidget(self._logo_browse)
        btn_col.addWidget(self._logo_clear)
        logo_row.addLayout(btn_col)
        logo_row.addStretch(1)
        lay.addLayout(logo_row)

        lay.addWidget(self._caption("Header position (logo & name)"))
        self.pos_combo = QComboBox()
        self.pos_combo.setMinimumHeight(32)
        for label, val in (("Left", "left"), ("Center", "center"),
                           ("Right", "right")):
            self.pos_combo.addItem(label, val)
        lay.addWidget(self.pos_combo)

        self._logo_path = ""     # stored path (set on browse / load)
        self._body.addWidget(frame)

    def _build_layout_sections(self):
        frame, lay = card("Page & sections")

        lay.addWidget(self._caption("Page size"))
        self.page_combo = QComboBox()
        self.page_combo.setMinimumHeight(32)
        for label, val in (("A4", "A4"), ("US Letter", "Letter")):
            self.page_combo.addItem(label, val)
        lay.addWidget(self.page_combo)

        lay.addWidget(self._caption("Resistance grid"))
        self.color_combo = QComboBox()
        self.color_combo.setMinimumHeight(32)
        for label, val in (("Colored", "color"), ("Monochrome", "mono")):
            self.color_combo.addItem(label, val)
        lay.addWidget(self.color_combo)
        grid_note = QLabel("Colored uses soft status tints with a key, matching "
                           "the on-screen overview. Monochrome prints the grid "
                           "in greyscale.")
        grid_note.setObjectName("PageHint")
        grid_note.setWordWrap(True)
        lay.addWidget(grid_note)

        lay.addWidget(self._caption("Sections to include"))
        # "&&" escapes the ampersand so Qt doesn't read it as a mnemonic.
        self.chk_treatment = QCheckBox(
            "Clinical interpretation && treatment considerations")
        self.chk_variants = QCheckBox("Supporting variants")
        self.chk_qc = QCheckBox("Sequencing quality (QC)")
        self.chk_coverage = QCheckBox("Target gene coverage")
        self.chk_site = QCheckBox("Collection site")
        for chk in (self.chk_treatment, self.chk_variants, self.chk_qc,
                    self.chk_coverage, self.chk_site):
            lay.addWidget(chk)

        self._body.addWidget(frame)

    def _build_footer(self):
        frame, lay = card("Footer")
        lay.addWidget(self._caption("Footer / disclaimer line"))
        self.footer_edit = self._line(
            "Leave blank for the default research-use notice.")
        lay.addWidget(self.footer_edit)
        note = QLabel("Shown at the foot of every page. Keep clinical "
                      "disclaimers here.")
        note.setObjectName("PageHint")
        note.setWordWrap(True)
        lay.addWidget(note)
        self._body.addWidget(frame)

    # -- logo handling ---------------------------------------------------
    def _sync_logo_enabled(self, *_):
        on = self.logo_check.isChecked()
        self._logo_browse.setEnabled(on)
        self._logo_clear.setEnabled(on)
        self._logo_preview.setEnabled(on)

    def _refresh_logo_preview(self):
        if self._logo_path and os.path.isfile(self._logo_path):
            pix = QPixmap(self._logo_path)
            if not pix.isNull():
                self._logo_preview.setPixmap(pix.scaled(
                    80, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return
        self._logo_preview.setPixmap(QPixmap())
        self._logo_preview.setText("No logo")

    def _on_browse_logo(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose logo image", "",
            "Images (*.png *.jpg *.jpeg *.gif *.bmp)")
        if not path:
            return
        self._logo_path = reportcfg.store_logo(path)
        self._refresh_logo_preview()

    def _on_clear_logo(self):
        self._logo_path = ""
        self._refresh_logo_preview()

    # -- persistence -----------------------------------------------------
    def _load(self):
        s = reportcfg.load(self.scope)
        self.org_edit.setText(s.get("org_name", ""))
        self.title_edit.setText(s.get("title", ""))
        self.logo_check.setChecked(bool(s.get("logo_show")))
        self._logo_path = s.get("logo_path", "") or ""
        self._set_combo(self.pos_combo, s.get("logo_pos", "left"))
        self._set_combo(self.page_combo, s.get("page_size", "A4"))
        self._set_combo(self.color_combo, s.get("color_mode", "color"))
        self.chk_treatment.setChecked(bool(s.get("include_treatment", True)))
        self.chk_variants.setChecked(bool(s.get("include_variants", True)))
        self.chk_qc.setChecked(bool(s.get("include_qc", True)))
        self.chk_coverage.setChecked(bool(s.get("include_coverage", True)))
        self.chk_site.setChecked(bool(s.get("include_site", True)))
        self.footer_edit.setText(s.get("footer", ""))
        self._sync_logo_enabled()
        self._refresh_logo_preview()

    def _values(self):
        return {
            "org_name": self.org_edit.text().strip(),
            "title": self.title_edit.text().strip(),
            "logo_show": self.logo_check.isChecked(),
            "logo_path": self._logo_path,
            "logo_pos": self.pos_combo.currentData(),
            "page_size": self.page_combo.currentData(),
            "color_mode": self.color_combo.currentData(),
            "include_treatment": self.chk_treatment.isChecked(),
            "include_variants": self.chk_variants.isChecked(),
            "include_qc": self.chk_qc.isChecked(),
            "include_coverage": self.chk_coverage.isChecked(),
            "include_site": self.chk_site.isChecked(),
            "footer": self.footer_edit.text().strip(),
        }

    def save(self):
        """Persist this form's values under its scope."""
        reportcfg.save(self._values(), self.scope)

    @staticmethod
    def _set_combo(combo, value):
        idx = combo.findData(value)
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    # -- theme -----------------------------------------------------------
    def restyle(self):
        """Re-apply the palette after a live theme switch."""
        self._logo_preview.setStyleSheet(
            "border:1px solid %s; border-radius:4px; color:%s; font-size:10px;"
            % (theme.BORDER, theme.FAINT))


class ReportSettingsScreen(QWidget):
    """Designer for both clinical PDFs, one independent tab per report scope."""

    # Emitted after a successful Save so the host can regenerate cached PDFs.
    saved = pyqtSignal()
    # Emitted when the user asks to preview a report; carries the active scope
    # ("sample" | "overview") so the host renders the matching PDF.
    preview_requested = pyqtSignal(str)

    # Tab index -> (scope, human label for the preview button).
    _TABS = (
        ("sample", "Sample report", "Preview sample report"),
        ("overview", "Overview report", "Preview overview report"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        header = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel("Report settings")
        title.setObjectName("PageTitle")
        hint = QLabel("Design the clinical PDFs: branding, page layout and "
                      "which sections a clinician sees. Each report is "
                      "configured independently.")
        hint.setObjectName("PageHint")
        hint.setWordWrap(True)
        col.addWidget(title)
        col.addWidget(hint)
        header.addLayout(col)
        header.addStretch(1)
        self._preview_btn = QPushButton(self._TABS[0][2])
        self._preview_btn.setObjectName("Ghost")
        self._preview_btn.setCursor(Qt.PointingHandCursor)
        self._preview_btn.clicked.connect(self._on_preview)
        header.addWidget(self._preview_btn, 0, Qt.AlignTop)
        self._save_btn = QPushButton("Save")
        self._save_btn.setObjectName("Primary")
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.clicked.connect(self._on_save)
        header.addWidget(self._save_btn, 0, Qt.AlignTop)
        root.addLayout(header)

        self._tabs = QTabWidget()
        # Widen tab pills a touch so the bold selected label never clips.
        self._tabs.tabBar().setStyleSheet(
            "QTabBar::tab { min-width: 120px; }")
        self._forms = []
        for scope, label, _btn in self._TABS:
            form = _ReportForm(scope)
            self._forms.append(form)
            self._tabs.addTab(form, label)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self._tabs, 1)

    # -- header actions --------------------------------------------------
    def _active_scope(self):
        return self._TABS[self._tabs.currentIndex()][0]

    def _on_tab_changed(self, idx):
        self._preview_btn.setText(self._TABS[idx][2])

    def _on_save(self):
        # Save both forms so either report's edits persist regardless of tab.
        for form in self._forms:
            form.save()
        self.saved.emit()

    def _on_preview(self):
        # Persist both first so the preview reflects exactly what's on screen.
        for form in self._forms:
            form.save()
        self.preview_requested.emit(self._active_scope())

    # -- theme -----------------------------------------------------------
    def restyle(self):
        """Re-apply the palette after a live theme switch."""
        for form in self._forms:
            form.restyle()
