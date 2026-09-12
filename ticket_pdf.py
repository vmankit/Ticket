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
MARGIN = 52
CONTENT_W = PAGE_W - 2 * MARGIN

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
        self.text(x + width / 2, y + height / 2 - size / 2 + 0.7, label,
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

    def section(self, label):
        self.space(40)
        self.y -= 4
        self.tracked(MARGIN, self.y, label)
        self.y -= 12

    def save(self):
        self.c.save()


def _fmt(value, fallback="—"):
    value = (str(value or "")).strip()
    return value if value and value.lower() not in ("not selected", "n/a") else fallback


def draw_header(t, *, company, pnr, booking_id, status):
    """Wordmark and booking reference on the left, PNR set large on the right."""
    top = t.y
    # Initials from the company name rather than a hardcoded pair, which was
    # still "AT" from a previous trading name.
    initials = "".join(word[0] for word in re.findall(r"[A-Za-z]+", company["name"]))[:2].upper()
    t.rounded(MARGIN, top - 30, 30, 30, 8, fill=INK, stroke=None)
    t.text(MARGIN + 15, top - 19, initials or "BH", size=11, font="Helvetica-Bold",
           color=white, align="center")

    t.text(MARGIN + 40, top - 14, company["name"].upper(), size=12.5,
           font="Helvetica-Bold", color=INK)
    subtitle = "E-ticket" + (f"  ·  Booking {booking_id}" if booking_id else "")
    t.text(MARGIN + 40, top - 26, subtitle, size=8, color=MUTED)

    right = PAGE_W - MARGIN
    label_w = t.c.stringWidth("PNR", "Helvetica-Bold", 7.2) + 3 * 1.5
    t.tracked(right - label_w, top - 8, "PNR")
    t.text(right, top - 30, pnr or "—", size=23, font="Helvetica-Bold",
           color=INK, align="right")

    t.y = top - 46
    colour = STATUS_COLORS.get(status, STATUS_COLORS["Confirmed"])
    t.pill(MARGIN, t.y - 4, status.upper(), fill=colour, size=6.6)
    t.y -= 26


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


def _flight_path(t, x0, x1, y, duration):
    """A dashed arc between the two airports, with the plane riding it."""
    lift = max(8, min(18, (x1 - x0) * 0.11))
    c = t.c
    c.saveState()
    c.setStrokeColor(LINE)
    c.setFillColor(FAINT)
    c.setLineWidth(0.9)
    c.circle(x0, y, 2.1, stroke=1, fill=0)
    c.circle(x1, y, 2.1, stroke=0, fill=1)

    span = x1 - x0
    control = lift * 1.35
    c.setStrokeColor(HexColor("#CBD0D8"))
    c.setLineWidth(1.1)
    c.setDash((2.2, 3.2), 0)
    arc = c.beginPath()
    arc.moveTo(x0 + 4, y)
    arc.curveTo(x0 + span * 0.28, y + control,
                x0 + span * 0.72, y + control,
                x1 - 4, y)
    c.drawPath(arc, stroke=1, fill=0)
    c.restoreState()

    # The tangent is flat at the apex, so the plane sits level on the arc.
    _plane(t, (x0 + x1) / 2, y + lift, size=11)
    if duration:
        t.text((x0 + x1) / 2, y - 13, duration, size=7.6, color=MUTED, align="center")


def draw_flight(t, flight, index, total):
    """One itinerary card: route, times and the connecting flight path."""
    # Body offsets measured down from the perforation, so the box is sized to
    # its contents instead of the contents being squeezed to fit the box.
    head_h = 32
    CODE_DY, TIME_DY, DATE_DY = 30, 60, 71
    has_baggage = bool(_fmt(flight.get("checkin_bag"), "") or _fmt(flight.get("hand_bag"), ""))
    body_h = (DATE_DY + 37) if has_baggage else (DATE_DY + 12)
    card_h = head_h + body_h

    t.space(card_h + 20)
    top = t.y
    bottom = top - card_h

    t.rounded(MARGIN, bottom, CONTENT_W, card_h, 10, fill=white, stroke=LINE)

    # Header strip
    t.c.saveState()
    t.c.setFillColor(WASH)
    t.c.roundRect(MARGIN, top - head_h, CONTENT_W, head_h, 10, stroke=0, fill=1)
    t.c.rect(MARGIN, top - head_h, CONTENT_W, head_h / 2, stroke=0, fill=1)
    t.c.restoreState()

    # A monochrome code badge rather than the carrier's logo: it keeps the
    # ticket to one palette, and needs no third-party image to load.
    code = (flight.get("airline_code") or "")[:2].upper() or "--"
    badge_y = top - head_h + 9
    t.c.setFillColor(INK)
    t.c.circle(MARGIN + 24, badge_y + 8, 8, stroke=0, fill=1)
    t.text(MARGIN + 24, badge_y + 5.4, code, size=6.4, font="Helvetica-Bold",
           color=white, align="center")

    airline = (flight.get("airline") or "").upper()
    title = f"{airline}  ·  {flight.get('flight_no', '')}".strip(" ·")
    t.text(MARGIN + 40, badge_y + 5, title, size=9, font="Helvetica-Bold", color=INK)

    travel_class = flight.get("class") or "Economy"
    width = t.c.stringWidth(travel_class.upper(), "Helvetica-Bold", 6.6) + 18
    t.pill(PAGE_W - MARGIN - width - 12, badge_y + 1, travel_class.upper(), size=6.6)

    # Perforation between header and body
    divider = top - head_h
    t.rule(MARGIN + 14, divider, CONTENT_W - 28, dash=(2.5, 3),
           color=HexColor("#CBD0D8"), line_width=0.9)
    for cx in (MARGIN, PAGE_W - MARGIN):
        t.c.saveState()
        t.c.setFillColor(white)
        t.c.setStrokeColor(LINE)
        t.c.setLineWidth(0.7)
        t.c.circle(cx, divider, 5, stroke=1, fill=1)
        t.c.restoreState()

    # Route
    left_x = MARGIN + 24
    right_x = PAGE_W - MARGIN - 24
    code_y = divider - CODE_DY

    t.text(left_x, code_y, flight.get("from_code", ""), size=27, font="Helvetica-Bold", color=INK)
    t.text(right_x, code_y, flight.get("to_code", ""), size=27, font="Helvetica-Bold",
           color=INK, align="right")
    t.text(left_x, code_y - 13, flight.get("from_city") or flight.get("from_code", ""),
           size=8, color=MUTED)
    t.text(right_x, code_y - 13, flight.get("to_city") or flight.get("to_code", ""),
           size=8, color=MUTED, align="right")

    time_y = divider - TIME_DY
    t.text(left_x, time_y, flight.get("dep_time") or flight.get("dep_time_raw") or "—",
           size=14, font="Helvetica-Bold", color=INK)
    t.text(right_x, time_y, flight.get("arr_time") or flight.get("arr_time_raw") or "—",
           size=14, font="Helvetica-Bold", color=INK, align="right")
    t.text(left_x, divider - DATE_DY, flight.get("date", ""), size=7.6, color=FAINT)
    t.text(right_x, divider - DATE_DY, flight.get("date", ""), size=7.6, color=FAINT, align="right")

    # Flight path
    gap_left = left_x + 100
    gap_right = right_x - 100
    path_y = time_y + 4
    if gap_right > gap_left + 40:
        centre = (gap_left + gap_right) / 2
        half = min((gap_right - gap_left) / 2, 66)
        _flight_path(t, centre - half, centre + half, path_y, flight.get("duration"))

    if has_baggage:
        baggage_y = divider - DATE_DY - 25
        t.rule(MARGIN + 20, baggage_y + 13, CONTENT_W - 40)
        t.text(left_x, baggage_y, "Baggage", size=8, color=MUTED)
        detail = "  ·  ".join(
            part for part in (
                _fmt(flight.get("checkin_bag"), "") and f"{flight['checkin_bag']} check-in",
                _fmt(flight.get("hand_bag"), "") and f"{flight['hand_bag']} cabin",
            ) if part)
        t.text(left_x + 48, baggage_y, detail, size=8, font="Helvetica-Bold", color=INK)

    t.y = bottom - 10


def draw_layover(t, label):
    t.space(24)
    t.rule(MARGIN + 40, t.y + 3, CONTENT_W / 2 - 90)
    t.rule(PAGE_W / 2 + 50, t.y + 3, CONTENT_W / 2 - 90)
    t.text(PAGE_W / 2, t.y, label, size=7.6, color=MUTED, align="center")
    t.y -= 18


def draw_passengers(t, passengers, flights):
    """Passenger table, with a scannable barcode per traveller.

    Laid out inside one rounded card, matching the itinerary above it: the
    rows are measured before anything is drawn, because the card border has
    to be stroked before the content that decides its height.
    """
    multi = len(flights) > 1
    pad = 14
    table_w = CONTENT_W - pad * 2
    columns = [
        ("PASSENGER", 0.29),
        ("SECTOR", 0.15),
        ("TICKET NO.", 0.19),
        ("SEAT", 0.10),
        ("MEAL", 0.12),
        ("BARCODE", 0.15),
    ]
    xs, cursor = [], MARGIN + pad
    for _, ratio in columns:
        xs.append(cursor)
        cursor += table_w * ratio
    barcode_right = MARGIN + pad + table_w
    barcode_width = table_w * columns[-1][1] - 6
    header_h = 24

    def cell_style(bold, size, color=MUTED):
        return ParagraphStyle(
            "cell", fontName="Helvetica-Bold" if bold else "Helvetica",
            fontSize=size, leading=size + 2.6, textColor=color)

    # Measure first: row heights decide how tall the card has to be.
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
            (xs[0], columns[0][1], f"{index + 1}.&nbsp;&nbsp;{escape(name)}", True, INK, 8.8),
            (xs[1], columns[1][1], sectors, False, MUTED, 7.8),
            (xs[2], columns[2][1], escape(pax.get("ticket_no") or "\u2014"), False, MUTED, 7.8),
            (xs[3], columns[3][1], seat_text, False, MUTED, 7.8),
            (xs[4], columns[4][1], meal_text, False, MUTED, 7.8),
        ]
        tallest = 0
        for x, ratio, html, bold, color, size in cells:
            _, h = Paragraph(html, cell_style(bold, size)).wrap(table_w * ratio - 8, 200)
            tallest = max(tallest, h)
        payload = re.sub(r"[^A-Za-z0-9\-]", "",
                         pax.get("ticket_no") or pax.get("name") or "TICKET")[:18]
        rows.append((cells, max(tallest + 16, 30), payload))

    card_h = header_h + sum(row_h for _, row_h, _ in rows)
    t.space(card_h + 16)
    top = t.y
    bottom = top - card_h

    t.rounded(MARGIN, bottom, CONTENT_W, card_h, 10, fill=white, stroke=LINE)
    t.c.saveState()
    t.c.setFillColor(WASH)
    t.c.roundRect(MARGIN, top - header_h, CONTENT_W, header_h, 10, stroke=0, fill=1)
    t.c.rect(MARGIN, top - header_h, CONTENT_W, header_h / 2, stroke=0, fill=1)
    t.c.restoreState()

    label_y = top - header_h + 9
    for index, ((label, ratio), x) in enumerate(zip(columns, xs)):
        if index == len(columns) - 1:  # right-aligned, kept inside the table
            width = t.c.stringWidth(label, "Helvetica-Bold", 6.8) + len(label) * 1.2
            t.tracked(barcode_right - width, label_y, label, size=6.8, tracking=1.2)
        else:
            t.tracked(x, label_y, label, size=6.8, tracking=1.2)

    y = top - header_h
    for row_index, (cells, row_h, payload) in enumerate(rows):
        if row_index:
            t.rule(MARGIN + pad, y, table_w)
        for x, ratio, html, bold, color, size in cells:
            para = Paragraph(html, cell_style(bold, size, color))
            _, height = para.wrap(table_w * ratio - 8, 200)
            para.drawOn(t.c, x, y - 8 - height)

        # Scale the bars to the column so the code cannot run over the meal
        # text to its left, and centre them in the row.
        try:
            bar_h = min(row_h - 12, 20)
            bar_width = 0.5
            barcode = Code128(payload, barHeight=bar_h, barWidth=bar_width, humanReadable=False)
            if barcode.width > barcode_width:
                bar_width *= barcode_width / barcode.width
                barcode = Code128(payload, barHeight=bar_h, barWidth=bar_width, humanReadable=False)
            barcode.drawOn(t.c, barcode_right - barcode.width, y - (row_h + bar_h) / 2)
        except Exception:
            pass

        y -= row_h

    t.y = bottom - 14


def draw_fares(t, total_str, *, remarks, gst_company, gstin):
    """Just the amount paid: the ticket states what was charged, not how it
    was arrived at. The label shares the amount's baseline so the two are
    read back as a single line."""
    t.space(64)
    left = MARGIN + 14
    right = PAGE_W - MARGIN - 14

    t.y -= 16

    label = "Amount Paid"
    value_w = t.c.stringWidth(total_str, "Helvetica-Bold", 13)
    label_w = t.c.stringWidth(label.upper(), "Helvetica-Bold", 7.2) + len(label) * 1.5
    t.tracked(right - value_w - 16 - label_w, t.y, label)
    t.text(right, t.y, total_str, size=13, font="Helvetica-Bold", color=INK, align="right")

    details = []
    if remarks:
        details.append(remarks)
    if gstin:
        details.append(f"GSTIN {gstin}" + (f"  \u00b7  {gst_company}" if gst_company else ""))
    if details:
        t.y -= 15
        # Free text, so it has to wrap rather than run off the page.
        t.y -= t.paragraph(left, t.y + 8, CONTENT_W - 28,
                           escape("  \u00b7  ".join(details)), size=7.6) - 8
    t.y -= 16


def draw_footer(t, *, company, issued, contact_line, terms):
    # Measure first: this is the last block on the page, so it only needs to
    # clear the bottom margin, not the larger gap `space()` reserves for cards.
    style = ParagraphStyle("p", fontName="Helvetica", fontSize=7.4, leading=10.4)
    _, terms_h = Paragraph(terms, style).wrap(CONTENT_W, 400)
    needed = 14 + 12 + terms_h + 10 + 11 + 11 + 11
    if t.y - needed < MARGIN:
        t.c.showPage()
        t.y = PAGE_H - MARGIN

    t.rule(MARGIN, t.y, CONTENT_W)
    t.y -= 14
    t.text(MARGIN, t.y, issued, size=7.4, color=FAINT)
    t.y -= 12
    height = t.paragraph(MARGIN, t.y, CONTENT_W, terms, size=7.4, leading=10.4)
    t.y -= height + 10
    t.text(MARGIN, t.y, company["name"], size=7.6, font="Helvetica-Bold", color=BODY)
    t.y -= 11
    t.text(MARGIN, t.y, f"{company['address']} - {company['pincode']}", size=7.4, color=FAINT)
    t.y -= 11
    t.text(MARGIN, t.y, contact_line, size=7.4, color=FAINT)
