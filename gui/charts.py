"""Lightweight custom-painted charts (no matplotlib dependency).

Both widgets keep the same ``set_data`` signatures the dashboard already uses,
and draw with the report's semantic ``PALETTE`` so colours match the PDF.
"""

from PyQt5.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPolygonF)
from PyQt5.QtWidgets import QSizePolicy, QToolTip, QWidget

from . import geo, theme
from .widgets import PALETTE


class DonutChart(QWidget):
    """Donut of resistance status tiers with an inline legend."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._counts = []          # list of (label, value, palette_key)
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, counts):
        self._counts = [c for c in counts if c[1] > 0]
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        total = sum(c[1] for c in self._counts)

        if total == 0:
            p.setPen(QColor(theme.MUTED))
            p.drawText(rect, Qt.AlignCenter, "No data")
            return

        # donut on the left half, legend on the right
        side = min(rect.height() - 16, rect.width() * 0.5)
        d = max(80.0, side)
        ring = QRectF(8, (rect.height() - d) / 2.0, d, d)
        thickness = d * 0.26

        start = 90 * 16
        for _label, value, key in self._counts:
            span = -int(360 * 16 * value / total)
            p.setPen(QPen(QColor(theme.SURFACE), 2))
            p.setBrush(QColor(PALETTE.get(key, theme.MUTED)))
            p.drawPie(ring, start, span)
            start += span
        # punch the hole
        hole = ring.adjusted(thickness, thickness, -thickness, -thickness)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.SURFACE))
        p.drawEllipse(hole)

        # centre total
        p.setPen(QColor(theme.HEADING))
        f = QFont(); f.setPointSize(15); f.setBold(True)
        p.setFont(f)
        p.drawText(hole, Qt.AlignCenter, str(total))

        # legend
        lx = int(ring.right()) + 18
        ly = int((rect.height() - len(self._counts) * 22) / 2) + 6
        sf = QFont(); sf.setPointSize(9)
        p.setFont(sf)
        for label, value, key in self._counts:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(PALETTE.get(key, theme.MUTED)))
            p.drawRoundedRect(QRectF(lx, ly - 9, 11, 11), 2, 2)
            p.setPen(QColor(theme.TEXT))
            p.drawText(lx + 18, ly,
                       "%s  (%d)" % (label, value))
            ly += 22
        p.end()


class BarChart(QWidget):
    """Horizontal bars, e.g. resistant calls per drug."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pairs = []
        self._key = "validated"
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, pairs, palette_key="validated", title=None):
        # Rank descending so the biggest burden sits on top, whatever order the
        # caller supplied.
        self._pairs = sorted(pairs, key=lambda kv: kv[1], reverse=True)
        self._key = palette_key
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        if not self._pairs:
            p.setPen(QColor(theme.MUTED))
            p.drawText(rect, Qt.AlignCenter, "No data")
            return

        max_v = max(v for _, v in self._pairs) or 1
        label_w = 110
        pad = 8
        bar_area = rect.width() - label_w - 44
        n = len(self._pairs)
        slot = (rect.height() - pad * 2) / n
        bar_h = min(20.0, slot * 0.6)
        color = QColor(PALETTE.get(self._key, theme.ACCENT))
        light = QColor(color).lighter(178)

        f = QFont(); f.setPointSize(9)
        p.setFont(f)
        y = pad
        for label, value in self._pairs:
            cy = y + (slot - bar_h) / 2.0
            frac = value / max_v
            # label (wraps to a second line when a drug name is too long to
            # fit the fixed label column, instead of clipping off the left)
            p.setPen(QColor(theme.TEXT))
            p.drawText(QRectF(0, y, label_w - 6, slot),
                       Qt.AlignRight | Qt.AlignVCenter | Qt.TextWordWrap,
                       str(label))
            # track
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.CHART_TRACK))
            p.drawRoundedRect(QRectF(label_w, cy, bar_area, bar_h), 4, 4)
            # bar: a magnitude ramp (light -> full colour by value) so the worst
            # offender reads darkest, plus a soft left-to-right sheen.
            w = max(2.0, bar_area * frac)
            fill = _lerp_color(light, color, 0.35 + 0.65 * frac)
            grad = QLinearGradient(QPointF(label_w, 0),
                                   QPointF(label_w + w, 0))
            grad.setColorAt(0.0, fill)
            grad.setColorAt(1.0, QColor(fill).lighter(118))
            p.setBrush(grad)
            p.drawRoundedRect(QRectF(label_w, cy, w, bar_h), 4, 4)
            # value
            p.setPen(QColor(theme.HEADING))
            p.drawText(QRectF(label_w + bar_area + 6, y, 36, slot),
                       Qt.AlignLeft | Qt.AlignVCenter, str(value))
            y += slot
        p.end()


# Distinct, saturated fills cycled across residues in a stacked distribution.
STACK_COLORS = ["#e0533d", "#3aa757", "#4a86e8", "#e0a82e", "#8e5fd9",
                "#2bb2b2", "#d96fb0", "#7d8a3c", "#c0504d", "#5b9bd5",
                "#a0522d", "#20b2aa"]


class StackedBarChart(QWidget):
    """Horizontal rows grouped by a categorical class, with a bottom legend.

    Each category (e.g. a mutation ``"DHFR-TS C59R"``) gets one row whose length
    is a percentage; the pill-shaped bar is split into coloured segments by
    ``group`` (e.g. a known/novel marker class), so the colour carries a second,
    meaningful dimension beyond the row label. Rows are ordered by total
    carriage (most prevalent first), the total ``%`` is printed at each row's
    end, and hovering a segment reveals its exact share. Segment colours resolve
    from the app's semantic ``PALETTE`` when the group is a palette key (so
    known/novel match the rest of the app and follow the light/dark toggle),
    otherwise from a categorical fallback.

    ``set_data(categories, groups, data, group_labels=None)`` where
    ``categories`` is the row labels, ``groups`` the ordered legend keys,
    ``data`` a mapping ``{category: {group: percent_0_100}}``, and the optional
    ``group_labels`` maps a group key to its human-readable legend text.
    """

    def __init__(self, parent=None, x_label="", y_label="", legend_title=""):
        super().__init__(parent)
        self._categories = []
        self._groups = []
        self._data = {}
        self._group_labels = {}
        self._x_label = x_label
        self._y_label = y_label
        self._legend_title = legend_title
        self._hot = []           # [(QRectF, tooltip_html), ...] rebuilt on paint
        self.setMinimumHeight(260)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, categories, groups, data, group_labels=None):
        self._groups = list(groups)
        self._data = dict(data or {})
        # Most-prevalent loci first, so the eye lands on the biggest signal.
        self._categories = sorted(
            categories,
            key=lambda c: sum(self._data.get(c, {}).values()), reverse=True)
        self._group_labels = dict(group_labels or {})
        self._hot = []
        # One row per locus — grow so the enclosing scroll area can reach them.
        self.setMinimumHeight(max(220, len(self._categories) * 30 + 72))
        self.update()

    def _color(self, group):
        key = PALETTE.get(group)
        if key:
            return QColor(key)
        try:
            idx = self._groups.index(group)
        except ValueError:
            idx = 0
        return QColor(STACK_COLORS[idx % len(STACK_COLORS)])

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        self._hot = []           # rebuilt every paint (geometry may change)

        if not self._categories:
            p.setPen(QColor(theme.MUTED))
            p.drawText(rect, Qt.AlignCenter, "No data")
            p.end()
            return

        sf = QFont(); sf.setPointSize(8)
        vf = QFont(); vf.setPointSize(8); vf.setBold(True)
        fm = QFontMetrics(sf)

        # A left gutter for the row labels (sized to the longest locus, capped),
        # a value column on the right, and a legend strip along the bottom.
        label_w = min(150, max(70, max(
            (fm.horizontalAdvance(str(c)) for c in self._categories),
            default=70) + 10))
        left = rect.left() + label_w + 10
        right = rect.right() - 46
        top = rect.top() + 10
        bottom = rect.bottom() - 40
        plot = QRectF(left, top, max(1.0, right - left),
                      max(1.0, bottom - top))

        # Faint % gridlines (0/25/50/75/100) with ticks under the axis.
        p.setFont(sf)
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            x = plot.left() + plot.width() * frac
            p.setPen(QPen(QColor(theme.FAINT), 1, Qt.DashLine))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            if frac in (0.0, 0.5, 1.0):
                p.setPen(QColor(theme.MUTED))
                p.drawText(QRectF(x - 20, plot.bottom() + 4, 40, 12),
                           Qt.AlignCenter, "%d%%" % int(frac * 100))

        # One row per locus: label (left), pill-stacked bar, total % (right).
        n = len(self._categories)
        slot = plot.height() / n
        bar_h = min(20.0, slot * 0.62)
        for i, cat in enumerate(self._categories):
            cy = plot.top() + slot * i + (slot - bar_h) / 2.0
            segs = self._data.get(cat, {})
            total_pct = min(100.0, sum(v for v in segs.values() if v > 0))
            total_w = plot.width() * total_pct / 100.0
            radius = bar_h / 2.0

            # Locus label, right-aligned in its gutter (elided to fit).
            p.setFont(sf); p.setPen(QColor(theme.TEXT))
            lbl = fm.elidedText(str(cat), Qt.ElideRight, label_w)
            p.drawText(QRectF(rect.left(), cy - 3, label_w, bar_h + 6),
                       Qt.AlignRight | Qt.AlignVCenter, lbl)

            # Track pill behind the bar.
            p.setPen(Qt.NoPen); p.setBrush(QColor(theme.CHART_TRACK))
            p.drawRoundedRect(QRectF(plot.left(), cy, plot.width(), bar_h),
                              radius, radius)

            # Coloured segments, clipped to a pill so both ends stay round.
            # Each segment's rect is registered for a hover tooltip.
            if total_w > 0:
                clip = QPainterPath()
                clip.addRoundedRect(
                    QRectF(plot.left(), cy, total_w, bar_h), radius, radius)
                p.save(); p.setClipPath(clip)
                x0 = plot.left()
                for group in self._groups:
                    pct = segs.get(group, 0.0)
                    if pct <= 0:
                        continue
                    w = plot.width() * pct / 100.0
                    seg = QRectF(x0, cy, w, bar_h)
                    p.setBrush(self._color(group))
                    p.drawRect(seg)
                    self._hot.append(
                        (seg, self._segment_tooltip(cat, group, pct)))
                    x0 += w
                p.restore()

            # Total carriage % at the right end.
            p.setFont(vf); p.setPen(QColor(theme.HEADING))
            p.drawText(QRectF(plot.right() + 6, cy - 3, 40, bar_h + 6),
                       Qt.AlignLeft | Qt.AlignVCenter, "%d%%" % round(total_pct))

        # Legend strip along the bottom.
        p.setFont(sf)
        lx = plot.left()
        ly = rect.bottom() - 16
        if self._legend_title:
            p.setPen(QColor(theme.MUTED))
            p.drawText(QPointF(lx, ly + 9), self._legend_title)
            lx += fm.horizontalAdvance(self._legend_title) + 16
        for group in self._groups:
            p.setPen(Qt.NoPen); p.setBrush(self._color(group))
            p.drawRoundedRect(QRectF(lx, ly, 11, 11), 3, 3)
            p.setPen(QColor(theme.TEXT))
            lbl = self._group_labels.get(group, str(group))
            p.drawText(QPointF(lx + 16, ly + 9), lbl)
            lx += 16 + fm.horizontalAdvance(lbl) + 20
        p.end()

    def _segment_tooltip(self, cat, group, pct):
        """Hover card for one stacked segment: locus, marker class and share."""
        label = self._group_labels.get(group, str(group))
        return ("<b>%s</b><br>%s: %.0f%% of samples" % (cat, label, pct))

    def mouseMoveEvent(self, event):
        """Show the tooltip for the segment under the cursor (if any)."""
        pt = QPointF(event.pos())
        for rect, html in self._hot:
            if rect.contains(pt):
                QToolTip.showText(event.globalPos(), html, self)
                super().mouseMoveEvent(event)
                return
        QToolTip.hideText()
        super().mouseMoveEvent(event)


def _lerp_color(c0, c1, t):
    """Blend two QColors; t in [0,1]."""
    t = max(0.0, min(1.0, t))
    return QColor(
        int(c0.red() + (c1.red() - c0.red()) * t),
        int(c0.green() + (c1.green() - c0.green()) * t),
        int(c0.blue() + (c1.blue() - c0.blue()) * t))


def _smooth_segments(pts):
    """Catmull-Rom -> cubic Bézier control points smoothing through ``pts``.

    Returns a list of ``(ctrl1, ctrl2, end)`` triples, one per span between
    consecutive points, so a ``QPainterPath`` can ``cubicTo`` along a fluid
    curve instead of angular line segments. The endpoints are duplicated so the
    curve starts and ends exactly on the first/last point.
    """
    segs = []
    n = len(pts)
    for i in range(n - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1 = pts[i]
        p2 = pts[i + 1]
        p3 = pts[i + 2] if i + 2 < n else p2
        c1 = QPointF(p1.x() + (p2.x() - p0.x()) / 6.0,
                     p1.y() + (p2.y() - p0.y()) / 6.0)
        c2 = QPointF(p2.x() - (p3.x() - p1.x()) / 6.0,
                     p2.y() - (p3.y() - p1.y()) / 6.0)
        segs.append((c1, c2, p2))
    return segs


class TrendChart(QWidget):
    """Resistance over time: a filled area + line across time buckets.

    Same contract as the other charts — ``set_data`` then ``update``; all
    drawing in ``paintEvent``. ``points`` is a list of
    ``(bucket_label, value, n)`` where ``value`` is a prevalence fraction
    (0..1) in ``"prevalence"`` mode or a raw count in ``"count"`` mode, and
    ``n`` is the number of samples in that bucket. Dots are coloured along the
    report's prevalence ramp (``nomarker`` green -> ``validated`` red), reusing
    :func:`_lerp_color`. Paints a graceful "No data" notice when empty.
    """

    # Emitted with the point index when an interactive dot is clicked; the
    # Trends screen uses it to open a navigable per-run sample panel.
    pointClicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._points = []
        self._mode = "prevalence"
        self._hints = []         # per-point short hover hint (parallel)
        self._interactive = False
        self._hot = []           # [(x, y, r, index), ...] rebuilt each paint
        self.setMinimumHeight(160)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, points, mode="prevalence", hints=None):
        """``points`` as before; optional ``hints`` is a list of short hover
        strings (one per point). When ``hints`` are supplied the dots become
        interactive: hovering shows the hint and clicking emits ``pointClicked``
        with the point index (so a caller can open a richer panel). When
        omitted the dots stay non-interactive, so existing callers are
        unaffected."""
        self._points = list(points or [])
        self._mode = mode
        self._hints = list(hints or [])
        self._interactive = bool(hints)
        self._hot = []
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        if not self._points:
            p.setPen(QColor(theme.MUTED))
            p.drawText(rect, Qt.AlignCenter, "No data")
            p.end()
            return

        # Plot area: leave room for y labels (left) and x labels (bottom).
        left = 38
        right = 12
        top = 12
        bottom = 26
        plot = QRectF(rect.left() + left, rect.top() + top,
                      max(1.0, rect.width() - left - right),
                      max(1.0, rect.height() - top - bottom))

        if self._mode == "count":
            max_v = max((v for _, v, _ in self._points), default=0) or 1
        else:
            max_v = 1.0

        def _xy(i, value):
            n = len(self._points)
            x = plot.left() + (plot.width() * (i / (n - 1)) if n > 1
                               else plot.width() / 2.0)
            frac = (value / max_v) if max_v else 0.0
            y = plot.bottom() - plot.height() * max(0.0, min(1.0, frac))
            return QPointF(x, y)

        # Reference gridlines + y labels.
        sf = QFont(); sf.setPointSize(8)
        p.setFont(sf)
        if self._mode == "count":
            refs = [(0.0, "0"), (0.5, str(int(round(max_v / 2)))),
                    (1.0, str(int(max_v)))]
        else:
            refs = [(0.0, "0%"), (0.5, "50%"), (1.0, "100%")]
        for frac, label in refs:
            y = plot.bottom() - plot.height() * frac
            p.setPen(QPen(QColor(theme.BORDER), 1))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(rect.left(), y - 8, left - 6, 16),
                       Qt.AlignRight | Qt.AlignVCenter, label)

        pts = [_xy(i, v) for i, (_, v, _) in enumerate(self._points)]

        # Smooth the polyline (Catmull-Rom -> cubic Bézier) so the trend reads
        # as a fluid curve rather than angular segments.
        segs = _smooth_segments(pts)

        # Clip to the plot so a smooth overshoot never bleeds into the axes.
        p.save()
        p.setClipRect(plot)

        # Filled area beneath the curve, fading from the accent to transparent.
        area = QPainterPath()
        area.moveTo(QPointF(pts[0].x(), plot.bottom()))
        area.lineTo(pts[0])
        for c1, c2, end in segs:
            area.cubicTo(c1, c2, end)
        area.lineTo(QPointF(pts[-1].x(), plot.bottom()))
        area.closeSubpath()
        grad = QLinearGradient(QPointF(0, plot.top()), QPointF(0, plot.bottom()))
        top_c = QColor(theme.ACCENT); top_c.setAlpha(150)
        mid_c = QColor(theme.ACCENT); mid_c.setAlpha(60)
        bot_c = QColor(theme.ACCENT); bot_c.setAlpha(8)
        grad.setColorAt(0.0, top_c)
        grad.setColorAt(0.55, mid_c)
        grad.setColorAt(1.0, bot_c)
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawPath(area)

        # The smooth curve itself, rounded so joins never spike.
        if segs:
            line = QPainterPath()
            line.moveTo(pts[0])
            for c1, c2, end in segs:
                line.cubicTo(c1, c2, end)
            pen = QPen(QColor(theme.ACCENT), 2.5)
            pen.setJoinStyle(Qt.RoundJoin)
            pen.setCapStyle(Qt.RoundCap)
            p.setBrush(Qt.NoBrush)
            p.setPen(pen)
            p.drawPath(line)
        p.restore()

        # Dots coloured by prevalence ramp, plus x labels. When interactive,
        # dots are drawn slightly larger with a halo and registered (with their
        # point index) in ``self._hot`` for hover/click.
        self._hot = []
        lo = QColor(PALETTE.get("nomarker", "#1a9850"))
        hi = QColor(PALETTE.get("validated", "#b2182b"))
        for i, ((label, value, _n), pt) in enumerate(zip(self._points, pts)):
            t = value if self._mode != "count" else (value / max_v)
            r = 5.0 if self._interactive else 4.0
            if self._interactive:
                halo = QColor(theme.ACCENT); halo.setAlpha(60)
                p.setPen(Qt.NoPen)
                p.setBrush(halo)
                p.drawEllipse(pt, r + 3.0, r + 3.0)
                self._hot.append((pt.x(), pt.y(), r, i))
            p.setPen(QPen(QColor(theme.SURFACE), 1.4))
            p.setBrush(_lerp_color(lo, hi, t))
            p.drawEllipse(pt, r, r)
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(pt.x() - 30, plot.bottom() + 4, 60, 18),
                       Qt.AlignHCenter | Qt.AlignTop, str(label))
        p.end()

    def _hit(self, pos):
        """Index of the nearest interactive dot within reach of ``pos``, or
        ``None``."""
        best = None
        best_d2 = None
        for hx, hy, r, idx in self._hot:
            dx = pos.x() - hx
            dy = pos.y() - hy
            d2 = dx * dx + dy * dy
            reach = (max(r, 6.0) + 5.0) ** 2
            if d2 <= reach and (best_d2 is None or d2 < best_d2):
                best, best_d2 = idx, d2
        return best

    def mouseMoveEvent(self, event):
        idx = self._hit(event.pos())
        if idx is not None:
            hint = self._hints[idx] if idx < len(self._hints) else ""
            if hint:
                QToolTip.showText(event.globalPos(), hint, self)
            self.setCursor(Qt.PointingHandCursor)
        else:
            QToolTip.hideText()
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        idx = self._hit(event.pos())
        if idx is not None:
            self.pointClicked.emit(idx)
        super().mousePressEvent(event)


# Distinct, muted-but-legible fills for protein-domain rectangles, cycled in
# order within a gene (matches the MutationMapper colour language).
DOMAIN_COLORS = ["#3aa757", "#e0533d", "#4a86e8", "#e0a82e",
                 "#8e5fd9", "#2bb2b2", "#d96fb0", "#7d8a3c"]


class LollipopChart(QWidget):
    """Mutation lollipop plot in the cBioPortal MutationMapper idiom.

    One panel per gene, stacked. A grey protein backbone carries coloured
    Pfam/UniProt domain rectangles; an amino-acid position axis runs beneath
    it (0 .. true protein length). Each mutation is a lollipop — a thin stem
    rising from the backbone to a circular head whose height/size scale with
    how many samples carry that change, coloured by catalog status (known
    marker = red, uncharacterised = purple). A small "# mutations" count axis
    sits in the left gutter and the amino-acid changes are labelled vertically
    above their heads.

    ``set_data(tracks)`` where ``tracks`` is a list of
    ``(gene_label, max_pos, [(pos, label, count, status_key, samples), ...],
    domains)``. ``samples`` (a tuple of sample labels) is optional and only
    feeds the hover tooltip; ``domains`` is ``[(start, end, name), ...]`` (may
    be omitted/empty). Hovering a head shows the change, count, samples and the
    domain it falls in.
    """

    BAND_H = 150                 # vertical room per gene panel

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tracks = []
        self._hot = []           # [(x, y, r, tooltip_html), ...] for hover
        self.setMinimumHeight(240)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, tracks):
        self._tracks = list(tracks or [])
        self._hot = []
        # Grow to fit every gene so the enclosing scroll area can reach them.
        self.setMinimumHeight(
            max(240, self.BAND_H * len(self._tracks) + 34))
        self.update()

    @staticmethod
    def _unpack(track):
        """Accept 3- or 4-tuples (domains optional) for forward safety."""
        gene, max_pos, pts = track[0], track[1], track[2]
        domains = track[3] if len(track) > 3 else []
        return gene, max_pos, pts, domains

    @staticmethod
    def _unpack_pt(pt):
        """Accept 4- or 5-tuples (per-point sample list optional)."""
        pos, label, cnt, key = pt[0], pt[1], pt[2], pt[3]
        samples = pt[4] if len(pt) > 4 else ()
        return pos, label, cnt, key, samples

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        self._hot = []           # rebuilt every paint (geometry may change)

        if not self._tracks:
            p.setPen(QColor(theme.MUTED))
            p.drawText(rect, Qt.AlignCenter, "No mutations to plot")
            p.end()
            return

        self._legend(p, rect)

        left = 120           # gene label + count-axis gutter
        right = 60           # room for the "<len>aa" tick at the far right
        top = 22             # below the legend
        bx0 = rect.left() + left
        bx1 = rect.right() - right
        known = QColor(PALETTE.get("known", "#b2182b"))
        novel = QColor(PALETTE.get("novel", "#5e4fa2"))

        for ti, track in enumerate(self._tracks):
            gene, max_pos, pts, domains = self._unpack(track)
            y0 = rect.top() + top + ti * self.BAND_H
            self._draw_track(p, gene, max_pos, pts, domains,
                             bx0, bx1, y0, known, novel)
        p.end()

    def _draw_track(self, p, gene, max_pos, pts, domains,
                    bx0, bx1, y0, known, novel):
        label_space = 54     # vertical room for the rotated AA labels
        stem_max = 44        # tallest stem (most-recurrent mutation)
        labels_top = y0 + 14
        stem_top = labels_top + label_space
        backbone_y = stem_top + stem_max
        span = float(max_pos) if max_pos else 1.0
        max_count = max((pt[2] for pt in pts), default=1)

        small = QFont(); small.setPointSize(8)
        tiny = QFont(); tiny.setPointSize(7)

        def x_of(pos):
            return bx0 + (bx1 - bx0) * max(0.0, min(1.0, pos / span))

        # Gene name (left gutter, on the backbone line).
        gf = QFont(); gf.setPointSize(9); gf.setBold(True)
        p.setFont(gf); p.setPen(QColor(theme.HEADING))
        p.drawText(QRectF(bx0 - 116, backbone_y - 14, 104, 28),
                   Qt.AlignRight | Qt.AlignVCenter, str(gene))

        # Count axis: a faint vertical guide with a 0 and a peak tick, so the
        # stem heights read as "# mutations" rather than arbitrary lengths.
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(QPointF(bx0 - 8, stem_top), QPointF(bx0 - 8, backbone_y))
        p.setFont(tiny); p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(bx0 - 40, stem_top - 6, 28, 12),
                   Qt.AlignRight | Qt.AlignVCenter, str(int(max_count)))
        p.drawText(QRectF(bx0 - 40, backbone_y - 6, 28, 12),
                   Qt.AlignRight | Qt.AlignVCenter, "0")

        # Protein backbone (grey rounded bar).
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.GRID))
        p.drawRoundedRect(QRectF(bx0, backbone_y - 5, bx1 - bx0, 10), 3, 3)

        # Domain rectangles on the backbone.
        dm = QFont(); dm.setPointSize(7); dm.setBold(True)
        fm = QFontMetrics(dm)
        for i, (s, e, name) in enumerate(domains):
            rx0 = x_of(s)
            rx1 = x_of(min(e, max_pos))
            w = rx1 - rx0
            if w < 2:
                continue
            col = QColor(DOMAIN_COLORS[i % len(DOMAIN_COLORS)])
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(rx0, backbone_y - 9, w, 18), 3, 3)
            if name and w >= fm.horizontalAdvance(name) + 8:
                p.setFont(dm); p.setPen(QColor("#ffffff"))
                p.drawText(QRectF(rx0, backbone_y - 9, w, 18),
                           Qt.AlignCenter, name)

        # Position axis: 0 at the left, the true length at the right, plus a
        # couple of interior ticks for scale.
        p.setFont(tiny); p.setPen(QColor(theme.MUTED))
        axis_y = backbone_y + 11
        ticks = self._axis_ticks(int(max_pos))
        for tv in ticks:
            tx = x_of(tv)
            p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
            p.drawLine(QPointF(tx, backbone_y + 6), QPointF(tx, backbone_y + 9))
            p.setPen(QColor(theme.MUTED))
            label = ("%daa" % tv) if tv == ticks[-1] else str(tv)
            align = (Qt.AlignRight if tv == ticks[-1]
                     else (Qt.AlignLeft if tv == 0 else Qt.AlignHCenter))
            p.drawText(QRectF(tx - 30, axis_y, 60, 12), align | Qt.AlignTop,
                       label)

        # Lollipops.
        for pt in pts:
            pos, label, cnt, key, samples = self._unpack_pt(pt)
            x = x_of(pos)
            frac = cnt / max_count
            stem_h = 10 + (stem_max - 10) * frac
            head_y = backbone_y - stem_h
            radius = 3.5 + 2.5 * frac
            color = known if key == "known" else novel
            p.setPen(QPen(QColor(theme.BORDER_STRONG), 1.2))
            p.drawLine(QPointF(x, backbone_y - 5), QPointF(x, head_y))
            p.setPen(QPen(QColor(theme.SURFACE), 1.2))
            p.setBrush(color)
            p.drawEllipse(QPointF(x, head_y), radius, radius)
            # Vertical AA-change label rising above the head.
            if label:
                p.save()
                p.translate(x, head_y - radius - 3)
                p.rotate(-90)
                p.setFont(small); p.setPen(QColor(theme.TEXT))
                p.drawText(QPointF(0, 3), str(label))
                p.restore()
            # Record this head for hover tooltips.
            dom = next((nm for (s, e, nm) in domains if s <= pos <= e), None)
            self._hot.append(
                (x, head_y, radius,
                 self._tooltip_html(gene, label, cnt, key, samples, dom)))

    @staticmethod
    def _tooltip_html(gene, label, cnt, key, samples, domain):
        """Build the hover card for one mutation head."""
        status = ("Known resistance marker" if key == "known"
                  else "Uncharacterised")
        head = "%s %s" % (gene, label) if label else str(gene)
        lines = ["<b>%s</b>" % head, status]
        if domain:
            lines.append("Domain: %s" % domain)
        lines.append("Carried by %d sample%s"
                     % (cnt, "" if cnt == 1 else "s"))
        if samples:
            shown = ", ".join(samples[:8])
            if len(samples) > 8:
                shown += ", \u2026"
            lines.append("<span style='color:#6b7280'>%s</span>" % shown)
        return "<br>".join(lines)

    def mouseMoveEvent(self, event):
        """Show a tooltip for the head under the cursor (if any)."""
        pos = event.pos()
        best = None
        best_d2 = None
        for hx, hy, r, html in self._hot:
            dx = pos.x() - hx
            dy = pos.y() - hy
            d2 = dx * dx + dy * dy
            reach = (max(r, 5.0) + 4.0) ** 2
            if d2 <= reach and (best_d2 is None or d2 < best_d2):
                best, best_d2 = html, d2
        if best:
            QToolTip.showText(event.globalPos(), best, self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    @staticmethod
    def _axis_ticks(length):
        """0, the length, and 1-2 rounded interior ticks for the aa axis."""
        if length <= 1:
            return [0, max(1, length)]
        if length <= 250:
            step = 100
        elif length <= 800:
            step = 200
        else:
            step = 400
        ticks = [0]
        v = step
        while v < length - step * 0.4:
            ticks.append(v)
            v += step
        ticks.append(length)
        return ticks

    def _legend(self, p, rect):
        sf = QFont(); sf.setPointSize(8)
        p.setFont(sf)
        x = rect.left() + 120
        y = rect.top() + 4
        for label, key in (("Known marker", "known"),
                           ("Uncharacterised", "novel")):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(PALETTE.get(key, theme.MUTED)))
            p.drawEllipse(QPointF(x + 5, y + 6), 4.0, 4.0)
            p.setPen(QColor(theme.MUTED))
            p.drawText(QPointF(x + 14, y + 10), label)
            x += 16 + p.fontMetrics().horizontalAdvance(label) + 22


class CoverageChart(QWidget):
    """Per-gene sequencing-depth track (genome-browser style coverage view).

    One bar per gene = mean read depth across samples, coloured by coverage
    status (green OK / amber low / grey none) with a depth axis and a dashed
    low-coverage guide. Same ``set_data`` + ``paintEvent`` contract as the
    other charts.

    ``set_data(items, threshold=30)`` where ``items`` is a list of
    ``(gene_label, mean_depth, status_key)``.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self._threshold = 30.0
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, items, threshold=30.0):
        self._items = list(items or [])
        self._threshold = float(threshold or 0) or 30.0
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        if not self._items:
            p.setPen(QColor(theme.MUTED))
            p.drawText(rect, Qt.AlignCenter, "No coverage data")
            p.end()
            return

        left = 48
        right = 12
        top = 16
        bottom = 58
        plot = QRectF(rect.left() + left, rect.top() + top,
                      max(1.0, rect.width() - left - right),
                      max(1.0, rect.height() - top - bottom))

        max_d = max((d for _, d, _ in self._items), default=1) or 1
        max_d = max(max_d, self._threshold) * 1.12

        sf = QFont(); sf.setPointSize(8)
        p.setFont(sf)
        for frac in (0.0, 0.5, 1.0):
            y = plot.bottom() - plot.height() * frac
            p.setPen(QPen(QColor(theme.BORDER), 1))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(rect.left(), y - 8, left - 6, 16),
                       Qt.AlignRight | Qt.AlignVCenter,
                       str(int(round(max_d * frac))))

        # Low-coverage threshold guide.
        ty = plot.bottom() - plot.height() * min(1.0, self._threshold / max_d)
        p.setPen(QPen(QColor(theme.FAINT), 1, Qt.DashLine))
        p.drawLine(QPointF(plot.left(), ty), QPointF(plot.right(), ty))
        p.setPen(QColor(theme.FAINT))
        p.drawText(QRectF(plot.right() - 60, ty - 14, 58, 12),
                   Qt.AlignRight | Qt.AlignVCenter,
                   "%dx" % int(self._threshold))

        n = len(self._items)
        slot = plot.width() / n
        bw = min(48.0, slot * 0.66)
        label_f = QFont(); label_f.setPointSize(8)
        for i, (gene, depth, key) in enumerate(self._items):
            cx = plot.left() + slot * i + slot / 2.0
            h = plot.height() * min(1.0, depth / max_d)
            x = cx - bw / 2.0
            y = plot.bottom() - h
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(PALETTE.get(key, theme.ACCENT)))
            p.drawRoundedRect(QRectF(x, y, bw, h), 3, 3)
            # Depth value above the bar.
            p.setPen(QColor(theme.HEADING))
            p.setFont(sf)
            p.drawText(QRectF(cx - 30, y - 16, 60, 14),
                       Qt.AlignHCenter | Qt.AlignBottom, str(int(round(depth))))
            # Gene label, rotated to fit narrow slots.
            p.save()
            p.translate(cx, plot.bottom() + 8)
            p.rotate(35)
            p.setPen(QColor(theme.MUTED))
            p.setFont(label_f)
            p.drawText(QPointF(0, 0), str(gene))
            p.restore()
        p.end()


class GhanaMap(QWidget):
    """Choropleth of Ghana's 16 regions with GPS pins, custom-painted.

    Same contract as the other charts: ``set_data`` then ``update``; all
    drawing happens in ``paintEvent``. The bundled region GeoJSON is loaded
    once via :mod:`gui.geo`; if it is missing the widget paints a graceful
    "Map data unavailable" notice instead of failing.

    ``set_data(metric_by_region, pins, mode)``:
      * ``metric_by_region``: ``{region: value}`` — prevalence fraction
        (0..1) in ``"prevalence"`` mode, or a sample count in ``"count"``.
      * ``pins``: list of ``(lon, lat, tier_palette_key)``.
      * ``mode``: ``"prevalence"`` or ``"count"``.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._metric = {}
        self._pins = []
        self._mode = "prevalence"
        self._geojson = geo.load_regions_geojson()
        feats = self._geojson.get("features", []) if self._geojson else []
        self._bbox = geo.bbox_of(feats) if feats else None
        self.setMinimumHeight(360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, metric_by_region, pins=None, mode="prevalence"):
        self._metric = {geo.normalize_region(k) or k: v
                        for k, v in (metric_by_region or {}).items()}
        self._pins = list(pins or [])
        self._mode = mode
        self.update()

    # -- projection ------------------------------------------------------
    def _map_rect(self):
        r = self.rect()
        return QRectF(r.left() + 10, r.top() + 10,
                      r.width() - 20, r.height() - 40)

    def _project(self, lon, lat, rect):
        """Aspect-preserved equirectangular letterbox fit (y flipped)."""
        min_lon, min_lat, max_lon, max_lat = self._bbox
        span_lon = (max_lon - min_lon) or 1e-9
        span_lat = (max_lat - min_lat) or 1e-9
        # Scale to fit while preserving aspect, then centre (letterbox).
        sx = rect.width() / span_lon
        sy = rect.height() / span_lat
        s = min(sx, sy)
        draw_w = span_lon * s
        draw_h = span_lat * s
        ox = rect.left() + (rect.width() - draw_w) / 2.0
        oy = rect.top() + (rect.height() - draw_h) / 2.0
        x = ox + (lon - min_lon) * s
        y = oy + (max_lat - lat) * s   # flip: north at top
        return QPointF(x, y)

    def _region_color(self, region):
        value = self._metric.get(region)
        if self._mode == "count":
            if not value:
                return QColor(theme.CHART_TRACK)
            mx = max(self._metric.values()) or 1
            c = QColor(theme.ACCENT)
            c.setAlpha(int(40 + 180 * (value / mx)))
            return c
        # prevalence: green (no resistance) -> red (validated)
        if value is None:
            return QColor(theme.CHART_TRACK)
        lo = QColor(PALETTE.get("nomarker", "#1a9850"))
        hi = QColor(PALETTE.get("validated", "#b2182b"))
        return _lerp_color(lo, hi, value)

    # -- painting --------------------------------------------------------
    def _polygon_path(self, geom, rect):
        path = QPainterPath()
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        polys = coords if gtype == "MultiPolygon" else [coords]
        for poly in polys:
            for ring in poly:
                pts = [self._project(lon, lat, rect) for lon, lat in ring]
                if len(pts) >= 3:
                    path.addPolygon(QPolygonF(pts))
                    path.closeSubpath()
        return path

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        if not self._geojson or not self._bbox:
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "Map data unavailable")
            p.end()
            return

        rect = self._map_rect()

        # Region choropleth + borders.
        border = QPen(QColor(theme.SURFACE), 1.0)
        for f in self._geojson.get("features", []):
            region = f.get("properties", {}).get("region")
            path = self._polygon_path(f.get("geometry", {}), rect)
            p.setPen(border)
            p.setBrush(self._region_color(region))
            p.drawPath(path)

        # GPS pins coloured by tier.
        for lon, lat, key in self._pins:
            try:
                pt = self._project(float(lon), float(lat), rect)
            except (TypeError, ValueError):
                continue
            c = QColor(PALETTE.get(key, theme.ACCENT))
            p.setPen(QPen(QColor(theme.SURFACE), 1.2))
            p.setBrush(c)
            p.drawEllipse(pt, 5.0, 5.0)

        self._paint_legend(p)
        p.end()

    def _paint_legend(self, p):
        rect = self.rect()
        y = rect.bottom() - 22
        sf = QFont(); sf.setPointSize(9)
        p.setFont(sf)
        x = rect.left() + 12
        if self._mode == "count":
            p.setPen(Qt.NoPen)
            c = QColor(theme.ACCENT)
            for i in range(5):
                cc = QColor(c); cc.setAlpha(40 + i * 45)
                p.setBrush(cc)
                p.drawRect(QRectF(x + i * 16, y, 16, 10))
            p.setPen(QColor(theme.MUTED))
            p.drawText(int(x + 5 * 16 + 8), int(y + 9), "fewer \u2192 more samples")
        else:
            lo = QColor(PALETTE.get("nomarker", "#1a9850"))
            hi = QColor(PALETTE.get("validated", "#b2182b"))
            steps = 6
            p.setPen(Qt.NoPen)
            for i in range(steps):
                p.setBrush(_lerp_color(lo, hi, i / (steps - 1)))
                p.drawRect(QRectF(x + i * 16, y, 16, 10))
            p.setPen(QColor(theme.MUTED))
            p.drawText(int(x + steps * 16 + 8), int(y + 9),
                       "0% \u2192 100% resistant")


class GhanaPicker(QWidget):
    """Interactive Ghana map: click to drop a collection-site pin.

    The dropped GPS point is the sample's *collection site* — finer-grained
    than the region, so two samples from the same region can still be told
    apart on the map. Emits ``picked(lon, lat, region)`` on each click;
    ``region`` is the polygon the click landed in (``""`` if outside any).

    Mirrors :class:`GhanaMap`'s offline-GeoJSON loading and letterbox
    projection, and adds the inverse transform so screen clicks become
    lon/lat. Falls back to a "Map data unavailable" notice if the bundled
    GeoJSON is missing.
    """

    picked = pyqtSignal(float, float, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._geojson = geo.load_regions_geojson()
        feats = self._geojson.get("features", []) if self._geojson else []
        self._bbox = geo.bbox_of(feats) if feats else None
        self._point = None        # (lon, lat) of the dropped pin
        self._region = None       # canonical region highlighted under the pin
        self._xform = None        # (s, ox, oy) cached from the last paint
        self._dragging = False    # True while the pin is being dragged
        self.setMinimumSize(260, 320)
        if self._bbox:
            self.setCursor(Qt.CrossCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # -- public API ------------------------------------------------------
    def set_point(self, lon, lat, region=None):
        """Place the pin from existing lat/lon (no signal); refresh region."""
        try:
            self._point = (float(lon), float(lat))
        except (TypeError, ValueError):
            self._point = None
        if region:
            self._region = geo.normalize_region(region)
        elif self._point:
            self._region = geo.region_at(*self._point)
        else:
            self._region = None
        self.update()

    def clear_point(self):
        self._point = None
        self._region = None
        self.update()

    def point(self):
        return self._point

    # -- projection ------------------------------------------------------
    def _map_rect(self):
        r = self.rect()
        return QRectF(r.left() + 8, r.top() + 8,
                      r.width() - 16, r.height() - 16)

    def _compute_xform(self, rect):
        min_lon, min_lat, max_lon, max_lat = self._bbox
        span_lon = (max_lon - min_lon) or 1e-9
        span_lat = (max_lat - min_lat) or 1e-9
        s = min(rect.width() / span_lon, rect.height() / span_lat)
        ox = rect.left() + (rect.width() - span_lon * s) / 2.0
        oy = rect.top() + (rect.height() - span_lat * s) / 2.0
        return s, ox, oy

    def _project(self, lon, lat, s, ox, oy):
        min_lon, _min_lat, _max_lon, max_lat = self._bbox
        return QPointF(ox + (lon - min_lon) * s,
                       oy + (max_lat - lat) * s)        # flip: north at top

    def _unproject(self, x, y):
        if not self._xform or not self._bbox:
            return None
        s, ox, oy = self._xform
        min_lon, _min_lat, _max_lon, max_lat = self._bbox
        return (x - ox) / s + min_lon, max_lat - (y - oy) / s

    # -- interaction -----------------------------------------------------
    def _set_from_pos(self, pos):
        """Drop/move the pin to a widget pixel, emitting ``picked`` live."""
        ll = self._unproject(pos.x(), pos.y())
        if not ll:
            return
        lon, lat = ll
        # Clamp into the bbox so a drag past the coastline stays valid.
        min_lon, min_lat, max_lon, max_lat = self._bbox
        lon = max(min_lon, min(max_lon, lon))
        lat = max(min_lat, min(max_lat, lat))
        self._point = (lon, lat)
        self._region = geo.region_at(lon, lat)
        self.update()
        self.picked.emit(lon, lat, self._region or "")

    def mousePressEvent(self, event):
        # Press drops (or grabs) the pin; holding + moving drags it live.
        self._dragging = True
        self._set_from_pos(event.pos())

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._set_from_pos(event.pos())

    def mouseReleaseEvent(self, _event):
        self._dragging = False

    # -- painting --------------------------------------------------------
    def _path(self, geom, s, ox, oy):
        path = QPainterPath()
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        polys = coords if gtype == "MultiPolygon" else [coords]
        for poly in polys:
            for ring in poly:
                pts = [self._project(lon, lat, s, ox, oy) for lon, lat in ring]
                if len(pts) >= 3:
                    path.addPolygon(QPolygonF(pts))
                    path.closeSubpath()
        return path

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self._geojson or not self._bbox:
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "Map data unavailable")
            p.end()
            return

        rect = self._map_rect()
        s, ox, oy = self._compute_xform(rect)
        self._xform = (s, ox, oy)

        border = QPen(QColor(theme.BORDER_STRONG), 1.0)
        fill = QColor(theme.CHART_TRACK)
        sel = QColor(theme.ACCENT); sel.setAlpha(55)
        for f in self._geojson.get("features", []):
            region = f.get("properties", {}).get("region")
            path = self._path(f.get("geometry", {}), s, ox, oy)
            p.setPen(border)
            p.setBrush(sel if region and region == self._region else fill)
            p.drawPath(path)

        if self._point:
            pt = self._project(self._point[0], self._point[1], s, ox, oy)
            p.setPen(QPen(QColor(theme.ACCENT_DARK), 1.2))
            p.drawLine(QPointF(pt.x() - 9, pt.y()), QPointF(pt.x() + 9, pt.y()))
            p.drawLine(QPointF(pt.x(), pt.y() - 9), QPointF(pt.x(), pt.y() + 9))
            p.setPen(QPen(QColor(theme.SURFACE), 1.5))
            p.setBrush(QColor(PALETTE.get("validated", "#b2182b")))
            p.drawEllipse(pt, 5.0, 5.0)
        p.end()
