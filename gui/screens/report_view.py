"""In-app PDF report viewer.

Renders ``resistance_report.pdf`` page-by-page with PyMuPDF (``fitz``) into a
scrollable column of images, so a past run's report can be read inside the app.
This avoids QtWebEngine, whose Chromium back-end is unreliable (it segfaults on
construction) in the conda PyQt5 build we ship against. Importing this module
requires PyMuPDF; callers fall back to opening the PDF externally if it fails.
"""

import os
import subprocess
import sys

import fitz
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QVBoxLayout, QWidget)

from .. import theme

# Display width (in px) of a page at zoom 1.0; zoom buttons scale around it.
_BASE_WIDTH = 820
_ZOOM_MIN, _ZOOM_MAX, _ZOOM_STEP = 0.6, 2.4, 0.2


def _open_external(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", path])


class ReportViewer(QDialog):
    """Modal viewer that rasterises a PDF and shows its pages in a scroll area."""

    def __init__(self, pdf_path, parent=None):
        super().__init__(parent)
        self._pdf_path = pdf_path
        self._doc = fitz.open(pdf_path)
        self._zoom = 1.0
        self.setWindowTitle("Resistance report")
        self.resize(940, 1080)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._build_bar())

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea { background: #eef0f3; }")
        self._pages_host = QWidget()
        self._pages_host.setStyleSheet("background: #eef0f3;")
        self._pages_lay = QVBoxLayout(self._pages_host)
        self._pages_lay.setContentsMargins(24, 24, 24, 24)
        self._pages_lay.setSpacing(18)
        self._pages_lay.setAlignment(Qt.AlignHCenter)
        self._scroll.setWidget(self._pages_host)
        lay.addWidget(self._scroll, 1)

        self._page_labels = []
        self._render_pages()

    # -- chrome ----------------------------------------------------------
    def _build_bar(self):
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(52)
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 0, 16, 0)
        row.setSpacing(8)

        title = QLabel("%s  \u00b7  %d pages"
                       % (os.path.basename(self._pdf_path), self._doc.page_count))
        title.setStyleSheet(
            "font-size:13px; font-weight:600; color:%s;" % theme.HEADING)
        row.addWidget(title)
        row.addStretch(1)

        zoom_out = QPushButton("\u2212")
        zoom_out.setFixedWidth(34)
        zoom_out.setCursor(Qt.PointingHandCursor)
        zoom_out.clicked.connect(lambda: self._set_zoom(self._zoom - _ZOOM_STEP))
        zoom_in = QPushButton("+")
        zoom_in.setFixedWidth(34)
        zoom_in.setCursor(Qt.PointingHandCursor)
        zoom_in.clicked.connect(lambda: self._set_zoom(self._zoom + _ZOOM_STEP))
        row.addWidget(zoom_out)
        row.addWidget(zoom_in)

        open_btn = QPushButton("Open externally")
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.clicked.connect(lambda: _open_external(self._pdf_path))
        close_btn = QPushButton("Close")
        close_btn.setObjectName("Primary")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        row.addWidget(open_btn)
        row.addWidget(close_btn)
        return bar

    # -- rendering -------------------------------------------------------
    def _render_pages(self):
        """(Re)render every page at the current zoom into the page column."""
        for lbl in self._page_labels:
            lbl.setParent(None)
        self._page_labels = []
        target_w = int(_BASE_WIDTH * self._zoom)
        for i in range(self._doc.page_count):
            page = self._doc.load_page(i)
            zoom = target_w / page.rect.width
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            img = QImage(pix.samples, pix.width, pix.height,
                         pix.stride, QImage.Format_RGB888)
            lbl = QLabel()
            lbl.setPixmap(QPixmap.fromImage(img.copy()))
            lbl.setFixedSize(pix.width, pix.height)
            # A hairline frame gives each page a printed-sheet feel.
            lbl.setStyleSheet(
                "background:#ffffff; border:1px solid %s;" % theme.BORDER_STRONG)
            self._pages_lay.addWidget(lbl, 0, Qt.AlignHCenter)
            self._page_labels.append(lbl)

    def _set_zoom(self, zoom):
        zoom = max(_ZOOM_MIN, min(_ZOOM_MAX, round(zoom, 2)))
        if zoom == self._zoom:
            return
        self._zoom = zoom
        self._render_pages()

    def closeEvent(self, event):
        self._doc.close()
        super().closeEvent(event)
