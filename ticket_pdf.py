"""Renders the e-ticket PDF.

Deliberately restrained: near-black on white, generous whitespace, hairline
rules, and one accent only for the status. The information is the design —
large airport codes and times carry the page, so nothing else needs to shout.
"""

import io
import re
from html import escape

from reportlab.graphics.barcode.code128 import Code128
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import Paragraph

PAGE_W, PAGE_H = A4
MM = 72 / 25.4

# Every position below is a millimetre measurement taken off the reference
# ticket with the page top as the origin, so the layout is fixed rather than
# the result of accumulated padding. `TicketCanvas.at` converts.
MARGIN_MM = 14.2
MARGIN = MARGIN_MM * MM
CONTENT_W = PAGE_W - 2 * MARGIN

# Header
LOGO_TOP_MM, LOGO_SIZE_MM = 13.6, 10.0
BRAND_BASE_MM = 20.4
BOOKING_BASE_MM = 28.3
PNR_LABEL_BASE_MM = 18.3
PNR_BASE_MM = 27.5

# Itinerary
ITINERARY_BASE_MM = 38.9
CARD_TOP_MM = 42.7
CARD_RADIUS_MM = 4.5
CARD_GAP_MM = 4.0

# Offsets inside a flight card, measured down from the card top
HEAD_H_MM = 12.1
BADGE_BASE_MM = 6.5
CODE_BASE_MM = 24.0
CITY_BASE_MM = 30.3
TIME_BASE_MM = 38.8
DATE_BASE_MM = 44.1
PATH_Y_MM = 35.9
PLANE_Y_MM = 29.5
ARC_LIFT_MM = 3.53
TERMINAL_TOP_MM = 47.0
TERMINAL_H_MM = 5.5
# Gaps, so a row that is not shown closes up instead of leaving a band of
# white where the reference happened to put one.
DATE_BLOCK_END_MM = 47.0          # where the date row stops
BAGGAGE_GAP_MM = 8.8              # above the baggage row
CARD_TAIL_MM = 5.1                # below the last row, to the card foot

# Passengers
PAX_LABEL_BASE_MM = 117.5
PAX_TABLE_TOP_MM = 121.0
PAX_HEAD_H_MM = 9.5
PAX_ROW_H_MM = 13.9
PAX_LABEL_DY_MM = 5.7       # header label baseline, below the table top
PAX_ROW_DY_MM = 7.9         # row baseline, below the row top
PAX_CELL_INSET_MM = 4.2
# Column bands as a fraction of the table width, from the reference.
PAX_COLUMNS = (
    ("PASSENGER", 0.2578),
    ("SECTOR", 0.1220),
    ("PNR", 0.1800),
    ("SEAT", 0.0850),
    ("MEAL", 0.0900),
    ("BARCODE", 0.2652),
)

# Amount, divider, footer
AMOUNT_BASE_MM = 151.6
DIVIDER_MM = 157.5
ISSUED_BASE_MM = 163.2
TERMS_LEADING_MM = 4.2
COMPANY_GAP_MM = 7.2
FOOTER_LINE_MM = 4.0

INK = HexColor("#111318")
BODY = HexColor("#374151")
MUTED = HexColor("#6B7280")
FAINT = HexColor("#9CA3AF")
LINE = HexColor("#E5E7EB")
WASH = HexColor("#F4F5F7")

STATUS_COLORS = {
    "Confirmed": HexColor("#15803D"),
    "On Hold": HexColor("#B45309"),
    "Waitlisted": HexColor("#B45309"),
    "Cancelled": HexColor("#B91C1C"),
    "Refunded": HexColor("#6B7280"),
}


class TicketCanvas:
    """Thin drawing layer over the reportlab canvas, with a running cursor."""

    def __init__(self, buffer):
        self.c = pdfcanvas.Canvas(buffer, pagesize=A4)
        self.y = PAGE_H - MARGIN

    @staticmethod
    def at(mm_from_top):
        """A y in canvas points for a millimetre measurement from the page top."""
        return PAGE_H - mm_from_top * MM

    def anchor(self, mm_from_top):
        """Place the cursor at a fixed distance from the page top.

        Content that repeats - extra flights, extra passengers - pushes what
        follows down, so the anchor only ever moves the cursor up the page to
        its measured position, never back over something already drawn.
        """
        self.y = min(self.y, self.at(mm_from_top))
        return self.y

    # ── primitives ───────────────────────────────────────────────────────
    def text(self, x, y, value, size=9, font="Helvetica", color=BODY, align="left"):
        self.c.setFont(font, size)
        self.c.setFillColor(color)
        value = str(value or "")
        if align == "right":
            self.c.drawRightString(x, y, value)
        elif align == "center":
            self.c.drawCentredString(x, y, value)
        else:
            self.c.drawString(x, y, value)

    def tracked(self, x, y, value, size=7.2, color=FAINT, tracking=1.5, font="Helvetica-Bold"):
        """Letter-spaced small caps, used for section labels."""
        self.c.setFont(font, size)
        self.c.setFillColor(color)
        cursor = x
        for char in str(value or "").upper():
            self.c.drawString(cursor, y, char)
            cursor += self.c.stringWidth(char, font, size) + tracking
        return cursor - x

    def rule(self, x, y, width, color=LINE, dash=None, line_width=0.6):
        self.c.saveState()
        self.c.setStrokeColor(color)
        self.c.setLineWidth(line_width)
        if dash:
            self.c.setDash(dash, 3)
        self.c.line(x, y, x + width, y)
        self.c.restoreState()

    def rounded(self, x, y, w, h, r=9, fill=None, stroke=LINE, line_width=0.7):
        self.c.saveState()
        if fill:
            self.c.setFillColor(fill)
        if stroke:
            self.c.setStrokeColor(stroke)
            self.c.setLineWidth(line_width)
        self.c.roundRect(x, y, w, h, r, stroke=1 if stroke else 0, fill=1 if fill else 0)
        self.c.restoreState()

    def pill(self, x, y, label, fill=INK, color=white, size=7, pad=9, height=15):
        self.c.setFont("Helvetica-Bold", size)
        width = self.c.stringWidth(label, "Helvetica-Bold", size) + pad * 2
        self.rounded(x, y, width, height, height / 2, fill=fill, stroke=None)
        self.text(x + width / 2, y + height / 2 - size / 2 + 2.1, label,
                  size=size, font="Helvetica-Bold", color=color, align="center")
        return width

    def paragraph(self, x, y, width, html, size=7.6, leading=11, color=MUTED):
        style = ParagraphStyle("p", fontName="Helvetica", fontSize=size,
                               leading=leading, textColor=color)
        para = Paragraph(html, style)
        _, height = para.wrap(width, 400)
        para.drawOn(self.c, x, y - height)
        return height

    # ── flow ─────────────────────────────────────────────────────────────
    def space(self, needed):
        """Start a new page when `needed` points no longer fit."""
        if self.y - needed < MARGIN + 30:
            self.c.showPage()
            self.y = PAGE_H - MARGIN
            return True
        return False

    def section(self, label, at=None):
        self.space(40)
        if at is not None:
            self.anchor(at)
        else:
            self.y -= 16
        self.tracked(MARGIN, self.y, label, size=8.5, tracking=1.6)

    def save(self):
        self.c.save()


def _fmt(value, fallback="—"):
    value = (str(value or "")).strip()
    return value if value and value.lower() not in ("not selected", "n/a") else fallback


def draw_header(t, *, company, pnr, booking_id, status):
    """Wordmark and booking reference on the left, PNR set large on the right."""
    right = PAGE_W - MARGIN
    size = LOGO_SIZE_MM * MM

    # Initials from the company name rather than a hardcoded pair, which was
    # still "AT" from a previous trading name.
    initials = "".join(word[0] for word in re.findall(r"[A-Za-z]+", company["name"]))[:2].upper()
    t.rounded(MARGIN, t.at(LOGO_TOP_MM) - size, size, size, 2.2 * MM, fill=INK, stroke=None)
    t.text(MARGIN + size / 2, t.at(LOGO_TOP_MM) - size / 2 - 3.4, initials or "BH",
           size=10, font="Helvetica-Bold", color=white, align="center")

    t.text(MARGIN + size + 2.2 * MM, t.at(BRAND_BASE_MM), company["name"].upper(),
           size=12.5, font="Helvetica-Bold", color=INK)

    booking_y = t.at(BOOKING_BASE_MM)
    subtitle = "E-ticket" + (f"  \u00b7  Booking {booking_id}" if booking_id else "")
    t.text(MARGIN, booking_y, subtitle, size=8, color=MUTED)

    # The status has no counterpart on the reference, so it rides the booking
    # line rather than claiming a row and shifting everything below it.
    colour = STATUS_COLORS.get(status, STATUS_COLORS["Confirmed"])
    subtitle_w = t.c.stringWidth(subtitle, "Helvetica", 8)
    t.pill(MARGIN + subtitle_w + 5 * MM, booking_y - 2.4, status.upper(),
           fill=colour, size=6.4, pad=7, height=12)

    label_w = t.c.stringWidth("PNR", "Helvetica-Bold", 8) + 3 * 1.6
    t.tracked(right - label_w, t.at(PNR_LABEL_BASE_MM), "PNR", size=8, tracking=1.6)
    t.text(right, t.at(PNR_BASE_MM), pnr or "\u2014", size=27,
           font="Helvetica-Bold", color=INK, align="right")

    t.y = t.at(PNR_BASE_MM)


# Half a top-down airliner, nose at +x; mirrored about y to draw the whole.
_PLANE_HALF = [
    (1.00, 0.00),   # nose
    (0.62, 0.06), (0.40, 0.08),
    (0.06, 0.64), (-0.15, 0.66),   # swept wing
    (-0.07, 0.09),
    (-0.52, 0.08),
    (-0.72, 0.33), (-0.84, 0.34),  # tailplane
    (-0.78, 0.06),
    (-0.94, 0.05), (-0.96, 0.00),
]


def _plane(t, cx, cy, size=11, angle=0):
    """A plane silhouette; the built-in fonts have no such character."""
    c = t.c
    c.saveState()
    c.translate(cx, cy)
    if angle:
        c.rotate(angle)
    c.scale(size, size)
    c.setFillColor(INK)
    path = c.beginPath()
    path.moveTo(*_PLANE_HALF[0])
    for x, y in _PLANE_HALF[1:]:
        path.lineTo(x, y)
    for x, y in reversed(_PLANE_HALF[:-1]):
        path.lineTo(x, -y)
    path.close()
    c.drawPath(path, fill=1, stroke=0)
    c.restoreState()


def _flight_path(t, x0, x1, y, arc_lift, plane_y, duration):
    """A dashed arc between the two airports, with the plane above its apex."""
    c = t.c
    c.saveState()
    c.setStrokeColor(LINE)
    c.setFillColor(FAINT)
    c.setLineWidth(0.9)
    c.circle(x0, y, 0.7 * MM, stroke=1, fill=0)
    c.circle(x1, y, 0.7 * MM, stroke=0, fill=1)

    span = x1 - x0
    control = arc_lift * 1.33
    c.setStrokeColor(HexColor("#CBD0D8"))
    c.setLineWidth(1.1)
    c.setDash((1.5 * MM, 1.5 * MM), 0)
    arc = c.beginPath()
    arc.moveTo(x0 + 4, y)
    arc.curveTo(x0 + span * 0.28, y + control,
                x0 + span * 0.72, y + control,
                x1 - 4, y)
    c.drawPath(arc, stroke=1, fill=0)
    c.restoreState()

    # The tangent is flat at the apex, so the plane sits level on the arc.
    _plane(t, (x0 + x1) / 2, plane_y, size=5.7 * MM)
    if duration:
        t.text((x0 + x1) / 2, y - 5.3 * MM, duration, size=8.5, color=MUTED, align="center")


def draw_flight(t, flight, index, total):
    """One itinerary card: route, times and the connecting flight path.

    Everything inside is positioned from the card top by a measured offset,
    so the card is a fixed shape rather than a box grown around its padding.
    """
    has_baggage = bool(_fmt(flight.get("checkin_bag"), "") or _fmt(flight.get("hand_bag"), ""))
    has_terminal = bool((flight.get("from_terminal") or "").strip()
                        or (flight.get("to_terminal") or "").strip())
    # The card is only as tall as the rows it actually has.
    content_end = (TERMINAL_TOP_MM + TERMINAL_H_MM) if has_terminal else DATE_BLOCK_END_MM
    baggage_base_mm = content_end + BAGGAGE_GAP_MM
    if has_baggage:
        content_end = baggage_base_mm
    card_h = (content_end + CARD_TAIL_MM) * MM

    t.space(card_h + 8 * MM)
    if index == 0:
        t.anchor(CARD_TOP_MM)
    top = t.y
    bottom = top - card_h
    radius = CARD_RADIUS_MM * MM

    def below(mm_from_card_top):
        return top - mm_from_card_top * MM

    t.rounded(MARGIN, bottom, CONTENT_W, card_h, radius, fill=white, stroke=LINE)

    # Header strip: rounded at the top, square where it meets the perforation.
    head_bottom = below(HEAD_H_MM)
    t.c.saveState()
    t.c.setFillColor(WASH)
    t.c.roundRect(MARGIN, head_bottom, CONTENT_W, HEAD_H_MM * MM, radius, stroke=0, fill=1)
    t.c.rect(MARGIN, head_bottom, CONTENT_W, HEAD_H_MM * MM / 2, stroke=0, fill=1)
    t.c.restoreState()

    # A monochrome code badge rather than the carrier's logo: it keeps the
    # ticket to one palette, and needs no third-party image to load.
    code = (flight.get("airline_code") or "")[:2].upper() or "--"
    badge_base = below(BADGE_BASE_MM)
    badge_cx = MARGIN + 8.55 * MM
    t.c.setFillColor(INK)
    t.c.circle(badge_cx, badge_base + 2.2, 2.9 * MM, stroke=0, fill=1)
    t.text(badge_cx, badge_base, code, size=6.5, font="Helvetica-Bold",
           color=white, align="center")

    airline = (flight.get("airline") or "").upper()
    title = f"{airline}  \u00b7  {flight.get('flight_no', '')}".strip(" \u00b7")
    t.text(MARGIN + 13.8 * MM, badge_base, title, size=9, font="Helvetica-Bold", color=INK)

    travel_class = (flight.get("class") or "Economy").upper()
    pill_h = 5.5 * MM
    pill_w = t.c.stringWidth(travel_class, "Helvetica-Bold", 8) + 2 * 3.2 * MM
    t.pill(PAGE_W - MARGIN - 5.6 * MM - pill_w, below(BADGE_BASE_MM + 2.1) , travel_class,
           size=8, pad=3.2 * MM, height=pill_h)

    # Perforation, with a notch punched out of each edge.
    t.rule(MARGIN + 1.5 * MM, head_bottom, CONTENT_W - 3 * MM, dash=(2.1 * MM, 1.6 * MM),
           color=HexColor("#CBD0D8"), line_width=0.9)
    for cx in (MARGIN, PAGE_W - MARGIN):
        t.c.saveState()
        t.c.setFillColor(white)
        t.c.setStrokeColor(LINE)
        t.c.setLineWidth(0.75)
        t.c.circle(cx, head_bottom, 2.4 * MM, stroke=1, fill=1)
        t.c.restoreState()

    left_x = MARGIN + 5.7 * MM
    right_x = PAGE_W - MARGIN - 5.7 * MM

    t.text(left_x, below(CODE_BASE_MM), flight.get("from_code", ""),
           size=26, font="Helvetica-Bold", color=INK)
    t.text(right_x, below(CODE_BASE_MM), flight.get("to_code", ""),
           size=26, font="Helvetica-Bold", color=INK, align="right")
    t.text(left_x, below(CITY_BASE_MM), flight.get("from_city") or flight.get("from_code", ""),
           size=10.5, color=MUTED)
    t.text(right_x, below(CITY_BASE_MM), flight.get("to_city") or flight.get("to_code", ""),
           size=10.5, color=MUTED, align="right")
    t.text(left_x, below(TIME_BASE_MM), flight.get("dep_time") or flight.get("dep_time_raw") or "\u2014",
           size=16, font="Helvetica-Bold", color=INK)
    t.text(right_x, below(TIME_BASE_MM), flight.get("arr_time") or flight.get("arr_time_raw") or "\u2014",
           size=16, font="Helvetica-Bold", color=INK, align="right")
    t.text(left_x, below(DATE_BASE_MM), flight.get("date", ""), size=8.5, color=FAINT)
    t.text(right_x, below(DATE_BASE_MM), flight.get("date", ""), size=8.5, color=FAINT, align="right")

    # Flight path: a fixed span centred on the card, as on the reference.
    _flight_path(t, PAGE_W / 2 - 21.9 * MM, PAGE_W / 2 + 21.9 * MM,
                 below(PATH_Y_MM), ARC_LIFT_MM * MM, below(PLANE_Y_MM),
                 flight.get("duration"))

    # Terminal pills sit under each side's date, where the reference puts them.
    for terminal, x_edge, align in (
        (flight.get("from_terminal"), left_x, "left"),
        (flight.get("to_terminal"), right_x, "right"),
    ):
        terminal = (terminal or "").strip()
        if not terminal:
            continue
        label = terminal if terminal.upper().startswith("TERMINAL") else f"Terminal {terminal}"
        pad = 3.0 * MM
        width = t.c.stringWidth(label, "Helvetica-Bold", 8) + 2 * pad
        x = x_edge - width if align == "right" else x_edge
        t.pill(x, below(TERMINAL_TOP_MM + TERMINAL_H_MM), label, fill=WASH, color=INK,
               size=8, pad=pad, height=TERMINAL_H_MM * MM)

    if has_baggage:
        baggage_base = below(baggage_base_mm)
        t.text(left_x, baggage_base, "Baggage", size=9, color=MUTED)
        detail = "  \u00b7  ".join(
            part for part in (
                _fmt(flight.get("checkin_bag"), "") and f"{flight['checkin_bag']} check-in",
                _fmt(flight.get("hand_bag"), "") and f"{flight['hand_bag']} cabin",
            ) if part)
        t.text(left_x + 14.5 * MM, baggage_base, detail, size=9,
               font="Helvetica-Bold", color=INK)

    t.y = bottom - CARD_GAP_MM * MM


def draw_layover(t, label):
    t.space(24)
    t.rule(MARGIN + 40, t.y + 3, CONTENT_W / 2 - 90)
    t.rule(PAGE_W / 2 + 50, t.y + 3, CONTENT_W / 2 - 90)
    t.text(PAGE_W / 2, t.y, label, size=7.6, color=MUTED, align="center")
    t.y -= 18


def draw_passengers(t, passengers, flights):
    """Passenger table, with a scannable barcode per traveller.

    One rounded card with a fixed header row and fixed row height. The rows
    are measured first only to catch a value too tall for the measured row -
    a wrapped name, or one sector per line on a multi-leg trip - which grows
    that row rather than overflowing it.
    """
    multi = len(flights) > 1
    table_w = CONTENT_W
    xs, cursor = [], MARGIN
    for _, ratio in PAX_COLUMNS:
        xs.append(cursor + PAX_CELL_INSET_MM * MM)
        cursor += table_w * ratio
    widths = [table_w * ratio for _, ratio in PAX_COLUMNS]
    barcode_right = MARGIN + table_w - PAX_CELL_INSET_MM * MM
    barcode_width = widths[-1] - 2 * PAX_CELL_INSET_MM * MM
    head_h = PAX_HEAD_H_MM * MM

    def cell_style(bold, size, color=MUTED):
        return ParagraphStyle(
            "cell", fontName="Helvetica-Bold" if bold else "Helvetica",
            fontSize=size, leading=size + 2.6, textColor=color)

    rows = []
    for index, pax in enumerate(passengers):
        name = " ".join(part for part in (pax.get("title"), pax.get("name")) if part)
        sectors = "<br/>".join(f"{f.get('from_code')}-{f.get('to_code')}" for f in flights) \
            if multi else f"{flights[0].get('from_code')}-{flights[0].get('to_code')}" if flights else "\u2014"
        seats = pax.get("seats_per_segment") or []
        meals = pax.get("meals_per_segment") or []
        seat_text = "<br/>".join(_fmt(s) for s in seats) if seats else _fmt(pax.get("seat"))
        meal_text = "<br/>".join(_fmt(m) for m in meals) if meals else _fmt(pax.get("meal"))

        cells = [
            (xs[0], widths[0], f"{index + 1}.&nbsp;&nbsp;{escape(name)}", True, INK, 9.5),
            (xs[1], widths[1], sectors, False, MUTED, 9.5),
            (xs[2], widths[2], escape(pax.get("pnr") or "\u2014"), False, MUTED, 9.5),
            (xs[3], widths[3], seat_text, False, MUTED, 9.5),
            (xs[4], widths[4], meal_text, False, MUTED, 9.5),
        ]
        tallest = 0
        for x, width, html, bold, color, size in cells:
            _, h = Paragraph(html, cell_style(bold, size)).wrap(width - PAX_CELL_INSET_MM * MM, 200)
            tallest = max(tallest, h)
        payload = re.sub(r"[^A-Za-z0-9\-]", "",
                         f"{pax.get('pnr') or 'TICKET'}-{index + 1}")[:18]
        # The measured row fits one line; anything taller sets its own height.
        rows.append((cells, max(PAX_ROW_H_MM * MM, tallest + 2 * (PAX_ROW_DY_MM - 2.9) * MM), payload))

    def draw_chunk(chunk, top):
        """One card holding as many rows as fit, header repeated on each."""
        card_h = head_h + sum(row_h for _, row_h, _ in chunk)
        bottom = top - card_h
        radius = CARD_RADIUS_MM * MM

        t.rounded(MARGIN, bottom, CONTENT_W, card_h, radius, fill=white, stroke=LINE)
        t.c.saveState()
        t.c.setFillColor(WASH)
        t.c.roundRect(MARGIN, top - head_h, CONTENT_W, head_h, radius, stroke=0, fill=1)
        t.c.rect(MARGIN, top - head_h, CONTENT_W, head_h / 2, stroke=0, fill=1)
        t.c.restoreState()
        t.rule(MARGIN, top - head_h, CONTENT_W, color=LINE, line_width=0.75)

        label_y = top - PAX_LABEL_DY_MM * MM
        for index, ((label, _ratio), x) in enumerate(zip(PAX_COLUMNS, xs)):
            if index == len(PAX_COLUMNS) - 1:  # right-aligned, kept inside the table
                width = t.c.stringWidth(label, "Helvetica-Bold", 8) + len(label) * 1.6
                t.tracked(barcode_right - width, label_y, label, size=8, tracking=1.6)
            else:
                t.tracked(x, label_y, label, size=8, tracking=1.6)

        y = top - head_h
        for row_index, (cells, row_h, payload) in enumerate(chunk):
            if row_index:
                t.rule(MARGIN, y, CONTENT_W)
            base = y - PAX_ROW_DY_MM * MM
            for x, width, html, bold, color, size in cells:
                para = Paragraph(html, cell_style(bold, size, color))
                _, height = para.wrap(width - PAX_CELL_INSET_MM * MM, 200)
                para.drawOn(t.c, x, base + size * 0.72 + 0.7 * MM - height)

            # Scale the bars to the column so the code cannot run over the meal
            # text to its left, and centre them in the row.
            try:
                bar_h = min(row_h - 4 * MM, 9 * MM)
                bar_width = 0.5
                barcode = Code128(payload, barHeight=bar_h, barWidth=bar_width, humanReadable=False)
                if barcode.width > barcode_width:
                    bar_width *= barcode_width / barcode.width
                    barcode = Code128(payload, barHeight=bar_h, barWidth=bar_width, humanReadable=False)
                barcode.drawOn(t.c, barcode_right - barcode.width, y - (row_h + bar_h) / 2)
            except Exception:
                pass

            y -= row_h
        return bottom

    # A long list would otherwise run off the foot of the page, so the card is
    # broken wherever the next row stops fitting and reopened overleaf.
    t.anchor(PAX_TABLE_TOP_MM)
    remaining = list(rows)
    while remaining:
        top = t.y
        chunk, used = [], head_h
        for row in remaining:
            if chunk and top - used - row[1] < MARGIN:
                break
            chunk.append(row)
            used += row[1]
        bottom = draw_chunk(chunk, top)
        remaining = remaining[len(chunk):]
        if remaining:
            t.c.showPage()
            t.y = PAGE_H - MARGIN
        else:
            t.y = bottom


def draw_fares(t, total_str, *, remarks, gst_company, gstin):
    """Just the amount paid: the ticket states what was charged, not how it
    was arrived at. The label shares the amount's baseline so the two are
    read back as a single line."""
    t.space(20 * MM)
    right = PAGE_W - MARGIN
    t.anchor(AMOUNT_BASE_MM)

    label = "Amount Paid"
    value_w = t.c.stringWidth(total_str, "Helvetica-Bold", 9.5)
    label_w = t.c.stringWidth(label.upper(), "Helvetica-Bold", 8) + len(label) * 1.6
    t.tracked(right - value_w - 3.3 * MM - label_w, t.y, label, size=8, tracking=1.6)
    t.text(right, t.y, total_str, size=9.5, font="Helvetica-Bold", color=INK, align="right")

    details = []
    if remarks:
        details.append(remarks)
    if gstin:
        details.append(f"GSTIN {gstin}" + (f"  \u00b7  {gst_company}" if gst_company else ""))
    if details:
        # Free text with no counterpart on the reference, so it is the one
        # block that can push the footer down; it wraps rather than running off.
        t.y -= 4.4 * MM
        t.y -= t.paragraph(MARGIN, t.y, CONTENT_W,
                           escape("  \u00b7  ".join(details)), size=7.5, leading=4.2 * MM)


def draw_footer(t, *, company, issued, contact_line, terms):
    # Measure first: this is the last block on the page, so it only needs to
    # clear the bottom margin, not the larger gap `space()` reserves for cards.
    style = ParagraphStyle("p", fontName="Helvetica", fontSize=7.5, leading=TERMS_LEADING_MM * MM)
    _, terms_h = Paragraph(terms, style).wrap(CONTENT_W, 400)
    needed = (ISSUED_BASE_MM - DIVIDER_MM) * MM + terms_h + (COMPANY_GAP_MM + 2 * FOOTER_LINE_MM) * MM
    if t.y - needed < MARGIN:
        t.c.showPage()
        t.y = PAGE_H - MARGIN
        t.anchor(DIVIDER_MM)
    else:
        t.anchor(DIVIDER_MM)

    t.rule(MARGIN, t.y, CONTENT_W, color=HexColor("#EEF0F3"), line_width=0.75)
    t.y = t.at(ISSUED_BASE_MM) if t.y == t.at(DIVIDER_MM) else t.y - (ISSUED_BASE_MM - DIVIDER_MM) * MM
    t.text(MARGIN, t.y, issued, size=7.5, color=FAINT)

    t.y -= 1.7 * MM
    height = t.paragraph(MARGIN, t.y, CONTENT_W, terms, size=7.5, leading=TERMS_LEADING_MM * MM)
    t.y -= height + (COMPANY_GAP_MM - TERMS_LEADING_MM + 2.6) * MM
    t.text(MARGIN, t.y, company["name"], size=7.5, font="Helvetica-Bold", color=BODY)
    t.y -= FOOTER_LINE_MM * MM
    t.text(MARGIN, t.y, f"{company['address']} - {company['pincode']}", size=7.5, color=FAINT)
    t.y -= FOOTER_LINE_MM * MM
    t.text(MARGIN, t.y, contact_line, size=7.5, color=FAINT)
