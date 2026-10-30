"""Renders the e-ticket PDF.

Deliberately restrained: near-black on white, generous whitespace, hairline
rules, and one accent only for the status. The information is the design —
large airport codes and times carry the page, so nothing else needs to shout.
"""

import io
import os
import re
from html import escape

from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import Paragraph
from PIL import Image as PILImage

PAGE_W, PAGE_H = A4
MM = 72 / 25.4

# Every position below is a millimetre measurement taken off the reference
# ticket with the page top as the origin, so the layout is fixed rather than
# the result of accumulated padding. `TicketCanvas.at` converts.
MARGIN_MM = 14.2
MARGIN = MARGIN_MM * MM
CONTENT_W = PAGE_W - 2 * MARGIN

# Header
LOGO_TOP_MM, LOGO_SIZE_MM = 12.0, 10.5
BRAND_BASE_MM = 17.0
SUB_AGENCY_BASE_MM = 21.6
PNR_LABEL_BASE_MM = 17.0
PNR_BASE_MM = 26.0
DIVIDER_BASE_MM = 29.5
META_BASE_MM = 34.0

# Itinerary
ITINERARY_BASE_MM = 42.0
SECTION_GAP_MM = 3.5      # section label baseline to the block beneath it
CARD_TOP_MM = 45.8
CARD_RADIUS_MM = 4.5
CARD_GAP_MM = 4.0

# Offsets inside a flight card, measured down from the card top
HEAD_H_MM = 12.1
BADGE_BASE_MM = 6.5
CODE_BASE_MM = 24.0
CITY_BASE_MM = 31.0
TIME_BASE_MM = 39.4
DATE_BASE_MM = 44.8
PATH_Y_MM = 35.9
PLANE_Y_MM = 29.5
ARC_LIFT_MM = 3.53
TERMINAL_TOP_MM = 47.6
TERMINAL_H_MM = 5.5
# Gaps, so a row that is not shown closes up instead of leaving a band of
# white where the reference happened to put one.
DATE_BLOCK_END_MM = 47.6          # where the date row stops
BAGGAGE_GAP_MM = 6.6              # above the baggage row
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
    ("PASSENGER", 0.3000),
    ("SECTOR", 0.1400),
    ("PNR", 0.1200),
    ("SEAT", 0.0900),
    ("MEAL", 0.1000),
    ("BARCODE", 0.2500),
)

# Amount, divider, footer
# Gaps rather than page positions, so a short card or a continuation page does
# not leave the amount stranded far below the table it belongs to.
AMOUNT_GAP_MM = 7.0         # table foot to the amount baseline
DIVIDER_GAP_MM = 5.9        # amount baseline to the divider
ISSUED_GAP_MM = 5.7         # divider to the first footer line
TERMS_LEADING_MM = 4.4
INFO_LEADING_MM = 3.3       # within one Additional Information item
INFO_ITEM_GAP_MM = 1.5      # between items
INFO_GUTTER_MM = 7.0        # between the two columns
COMPANY_GAP_MM = 7.2
FOOTER_LINE_MM = 4.3

def _register_fonts():
    """Liberation Sans if the system has it, Helvetica otherwise.

    Liberation is metrically the same as Helvetica but has real text figures
    and a tighter bold, which is what makes a document read as typeset rather
    than as a screen mock-up. It is not installed everywhere, so its absence
    has to be survivable rather than fatal.
    """
    base = "/usr/share/fonts/truetype/liberation"
    faces = (("Doc", f"{base}/LiberationSans-Regular.ttf"),
             ("Doc-Bold", f"{base}/LiberationSans-Bold.ttf"))
    try:
        for name, path in faces:
            pdfmetrics.registerFont(TTFont(name, path))
        pdfmetrics.registerFontFamily("Doc", normal="Doc", bold="Doc-Bold")
        return "Doc", "Doc-Bold"
    except Exception:
        return "Helvetica", "Helvetica-Bold"


FONT, FONT_BOLD = _register_fonts()

INK = HexColor("#111318")
BODY = HexColor("#374151")
MUTED = HexColor("#6B7280")
FAINT = HexColor("#9CA3AF")
LINE = HexColor("#E5E7EB")
WASH = HexColor("#F4F5F7")

STATUS_TINTS = {
    "Confirmed": HexColor("#E8F3EC"),
    "On Hold": HexColor("#FBF1E3"),
    "Waitlisted": HexColor("#FBF1E3"),
    "Cancelled": HexColor("#FBEAE8"),
    "Refunded": HexColor("#F1F2F4"),
}

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
    def text(self, x, y, value, size=9, font=FONT, color=BODY, align="left"):
        self.c.setFont(font, size)
        self.c.setFillColor(color)
        value = str(value or "")
        if align == "right":
            self.c.drawRightString(x, y, value)
        elif align == "center":
            self.c.drawCentredString(x, y, value)
        else:
            self.c.drawString(x, y, value)

    def tracked(self, x, y, value, size=7.2, color=FAINT, tracking=1.5, font=FONT_BOLD):
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
        self.c.setFont(FONT_BOLD, size)
        width = self.c.stringWidth(label, FONT_BOLD, size) + pad * 2
        self.rounded(x, y, width, height, height / 2, fill=fill, stroke=None)
        self.text(x + width / 2, y + height / 2 - size / 2 + 2.1, label,
                  size=size, font=FONT_BOLD, color=color, align="center")
        return width

    def paragraph(self, x, y, width, html, size=7.6, leading=11, color=MUTED):
        style = ParagraphStyle("p", fontName=FONT, fontSize=size,
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
        self.tracked(MARGIN, self.y, label, size=9, tracking=2.6)
        # Clear the label before the block below anchors, or a block that has
        # flowed past its measured position lands on top of it.
        self.y -= SECTION_GAP_MM * MM

    def save(self):
        self.c.save()


def _fmt(value, fallback="—"):
    value = (str(value or "")).strip()
    return value if value and value.lower() not in ("not selected", "n/a") else fallback


def draw_header(t, *, company, pnr, booking_id, status, show_agency=True):
    """Clean, balanced two-tier executive airline header.

    Tier 1 (Branding & Reference):
      - Left: Monogram mark [JS] + Agency Name + Unified Contact/Address line
      - Right: PNR label + Large PNR code
    Tier 2 (Metadata Strip):
      - Hairline separator rule
      - Left: E-TICKET · Booking ID
      - Right: Status badge aligned to right margin
    """
    right = PAGE_W - MARGIN
    size = LOGO_SIZE_MM * MM

    # Initials monogram
    if show_agency:
        initials = company.get("initials") or "".join(word[0] for word in re.findall(r"[A-Za-z]+", company["name"]))[:2].upper()
        t.rounded(MARGIN, t.at(LOGO_TOP_MM) - size, size, size, 2.5 * MM, fill=INK, stroke=None)
        t.text(MARGIN + size / 2, t.at(LOGO_TOP_MM) - size / 2 - 3.5, initials or "JS",
               size=11, font=FONT_BOLD, color=white, align="center")

    # The PNR block is measured before anything on the left is drawn: a
    # merged itinerary carries one PNR per segment ("ABCD12 / WXYZ34"), which
    # is wide enough to reach back over the agency's address unless the
    # address is held to whatever room the code leaves.
    pnr_text = pnr or "—"
    pnr_size = 28.0
    while pnr_size > 12 and t.c.stringWidth(pnr_text, FONT_BOLD, pnr_size) > CONTENT_W * 0.5:
        pnr_size -= 0.5
    agency_limit = right - t.c.stringWidth(pnr_text, FONT_BOLD, pnr_size) - 5.0 * MM

    wordmark = company["name"].upper() if show_agency else "E-TICKET"
    brand_x = (MARGIN + size + 3.0 * MM) if show_agency else MARGIN
    brand_size = 15.0
    room = agency_limit - brand_x
    while brand_size > 11 and t.c.stringWidth(wordmark, FONT_BOLD, brand_size) > room:
        brand_size -= 0.25
    t.text(brand_x, t.at(BRAND_BASE_MM), wordmark, size=brand_size, font=FONT_BOLD, color=INK)

    # Unified contact & address line beside the monogram
    phone = (company.get("phone") or "").strip()
    addr = (company.get("address") or "").strip()
    pincode = (company.get("pincode") or "").strip()
    if pincode and str(pincode) not in addr:
        addr = f"{addr} - {pincode}"
    if addr.endswith(", India"):
        addr = addr[:-7].strip()

    if show_agency and (phone or addr):
        sub_y = t.at(SUB_AGENCY_BASE_MM)
        cursor_x = brand_x
        if phone:
            t.text(cursor_x, sub_y, phone, size=8.5, font=FONT_BOLD, color=BODY)
            cursor_x += t.c.stringWidth(phone, FONT_BOLD, 8.5) + 3.0 * MM
            if addr:
                t.text(cursor_x - 1.5 * MM, sub_y, "·", size=9.0, font=FONT_BOLD, color=FAINT)
                cursor_x += 2.0 * MM
        if addr:
            max_w = agency_limit - cursor_x
            addr_size = 8.0
            while addr_size > 6.0 and t.c.stringWidth(addr, FONT, addr_size) > max_w:
                addr_size -= 0.2
            # Still too long at the smallest readable size: trim it rather
            # than let it run under the PNR.
            if t.c.stringWidth(addr, FONT, addr_size) > max_w:
                while addr and t.c.stringWidth(addr + "…", FONT, addr_size) > max_w:
                    addr = addr[:-1].rstrip(" ,-")
                addr += "…" if addr else ""
            if addr:
                t.text(cursor_x, sub_y, addr, size=addr_size, font=FONT, color=MUTED)

    # PNR block on the right, at the size measured above.
    pnr_label = "PNRs" if pnr and "/" in pnr else "PNR"
    label_w = t.c.stringWidth(pnr_label, FONT_BOLD, 8) + 3 * 2.6
    t.tracked(right - label_w, t.at(PNR_LABEL_BASE_MM), pnr_label, size=8, tracking=2.6)
    t.text(right, t.at(PNR_BASE_MM), pnr_text, size=pnr_size,
           font=FONT_BOLD, color=INK, align="right")

    # Hairline divider between Branding and Ticket Metadata
    bar_y = t.at(DIVIDER_BASE_MM)
    t.rule(MARGIN, bar_y, CONTENT_W, color=HexColor("#EEF0F3"), line_width=0.75)

    # Metadata strip: E-ticket · Booking ID on left, Status badge right-aligned
    meta_y = t.at(META_BASE_MM)
    if show_agency:
        t.tracked(MARGIN, meta_y, "E-TICKET", size=8.0, tracking=1.5, color=MUTED)
        et_w = t.c.stringWidth("E-TICKET", FONT_BOLD, 8.0) + 7 * 1.5
        if booking_id:
            t.text(MARGIN + et_w + 3 * MM, meta_y, f"·   Booking ID: {booking_id}", size=8.5, color=MUTED)
    else:
        booking_str = f"Booking ID: {booking_id}" if booking_id else "E-Ticket"
        t.text(MARGIN, meta_y, booking_str, size=8.8, color=MUTED)

    status_str = status.upper()
    colour = STATUS_COLORS.get(status, STATUS_COLORS["Confirmed"])
    tint = STATUS_TINTS.get(status, STATUS_TINTS["Confirmed"])
    pill_w = t.c.stringWidth(status_str, FONT_BOLD, 7.0) + 2 * (3.0 * MM)
    t.pill(right - pill_w, meta_y - 2.2, status_str,
           fill=tint, color=colour, size=7.0, pad=3.0 * MM, height=4.8 * MM)

    t.y = t.at(META_BASE_MM + 2.5)


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


def _phone_icon(t, cx, cy, size=5.5, color=FAINT):
    """Draw a clean vector telephone / call icon."""
    c = t.c
    c.saveState()
    c.translate(cx, cy)
    s = size / 10.0
    c.scale(s, s)
    c.setFillColor(color)
    c.setStrokeColor(color)
    c.setLineWidth(0.8)
    c.setLineCap(1)
    c.setLineJoin(1)
    p = c.beginPath()
    p.moveTo(-3.2, 3.8)
    p.curveTo(-1.8, 5.0, 1.8, 5.0, 3.2, 3.8)
    p.lineTo(2.0, 1.8)
    p.lineTo(0.7, 2.3)
    p.curveTo(-0.5, 0.7, -0.7, 0.5, -2.3, -0.7)
    p.lineTo(-1.8, -2.0)
    p.lineTo(-3.8, -3.2)
    p.curveTo(-5.0, -1.8, -5.0, 1.8, -3.2, 3.8)
    p.close()
    c.drawPath(p, fill=1, stroke=1)
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


def _badge_text_color(brand_color_hex):
    """Determine whether to use white or dark text for the fallback badge.

    Calculates WCAG relative luminance so any brand colour meets contrast standards.
    """
    hex_clean = (brand_color_hex or "").lstrip("#")
    if len(hex_clean) == 6:
        try:
            r = int(hex_clean[0:2], 16) / 255.0
            g = int(hex_clean[2:4], 16) / 255.0
            b = int(hex_clean[4:6], 16) / 255.0
            r_lin = r / 12.92 if r <= 0.04045 else ((r + 0.055) / 1.055) ** 2.4
            g_lin = g / 12.92 if g <= 0.04045 else ((g + 0.055) / 1.055) ** 2.4
            b_lin = b / 12.92 if b <= 0.04045 else ((b + 0.055) / 1.055) ** 2.4
            lum = 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin
            if lum > 0.179:
                return INK
        except ValueError:
            pass
    return white


def _get_airline_logo(code):
    """Safely load local airline logo PNG at static/airline_logos/<IATA>.png.

    Returns (ImageReader, orig_w, orig_h) if a valid image is present,
    or (None, 0, 0) if missing, zero-byte, corrupt, or invalid.
    Performs zero network requests.
    """
    if not code or code == "--":
        return None, 0, 0
    clean_code = str(code).strip().upper()
    logo_path = os.path.join(os.path.dirname(__file__), "static", "airline_logos", f"{clean_code}.png")
    if not os.path.isfile(logo_path):
        return None, 0, 0
    try:
        if os.path.getsize(logo_path) == 0:
            return None, 0, 0
        with PILImage.open(logo_path) as im:
            im.verify()
        with PILImage.open(logo_path) as im:
            orig_w, orig_h = im.size
            if orig_w <= 0 or orig_h <= 0:
                return None, 0, 0
        return ImageReader(logo_path), orig_w, orig_h
    except Exception:
        return None, 0, 0


def _find_airline_code(flight):
    """Determine the carrier code from flight dict, flight number or airline name."""
    try:
        from ticket_parsing import STANDARD_AIRLINES
    except ImportError:
        STANDARD_AIRLINES = {}
    code = (flight.get("airline_code") or "").strip().upper()
    if code and code != "--":
        return code[:2]
    # Try from flight_no
    fn = re.sub(r"[^A-Z0-9]", "", (flight.get("flight_no") or "").upper())
    if len(fn) >= 2 and fn[:2] in STANDARD_AIRLINES:
        return fn[:2]
    # Try reverse match airline name
    airline_name = (flight.get("airline") or "").strip().lower()
    if airline_name:
        for c, name in STANDARD_AIRLINES.items():
            if name.lower() == airline_name:
                return c
    return code[:2] if code else ""


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

    # Airline logo with safe fallback to brand-coloured circle badge
    code = _find_airline_code(flight)
    badge_base = below(BADGE_BASE_MM)
    badge_cx = MARGIN + 8.55 * MM
    badge_cy = badge_base + 2.2
    badge_box = 2 * 2.9 * MM  # 5.8mm footprint matching the 2.9mm radius circle

    reader, orig_w, orig_h = _get_airline_logo(code)
    logo_drawn = False

    if reader:
        try:
            scale = min(badge_box / orig_w, badge_box / orig_h)
            w = orig_w * scale
            h = orig_h * scale
            x = badge_cx - w / 2
            y = badge_cy - h / 2
            t.c.drawImage(reader, x, y, width=w, height=h, mask="auto", preserveAspectRatio=True)
            logo_drawn = True
        except Exception:
            logo_drawn = False

    if not logo_drawn:
        try:
            from ticket_parsing import STANDARD_AIRLINE_COLORS
            brand_color_hex = STANDARD_AIRLINE_COLORS.get(code, "#0F2742")
        except Exception:
            brand_color_hex = "#0F2742"

        t.c.setFillColor(HexColor(brand_color_hex))
        t.c.circle(badge_cx, badge_cy, 2.9 * MM, stroke=0, fill=1)
        badge_text_color = _badge_text_color(brand_color_hex)
        t.text(badge_cx, badge_base, code or "--", size=6.5, font=FONT_BOLD,
               color=badge_text_color, align="center")

    title_x = MARGIN + 13.8 * MM

    travel_class = (flight.get("class") or "Economy").upper()
    pill_h = 5.5 * MM
    pill_w = t.c.stringWidth(travel_class, FONT_BOLD, 8) + 2 * 3.2 * MM
    pill_x = PAGE_W - MARGIN - 5.6 * MM - pill_w
    t.pill(pill_x, below(BADGE_BASE_MM + 2.1), travel_class,
           size=8, pad=3.2 * MM, height=pill_h)

    airline = (flight.get("airline") or "").upper()
    title = f"{airline}  \u00b7  {flight.get('flight_no', '')}".strip(" \u00b7")
    title_size = 9.0
    available_w = pill_x - title_x - 3 * MM
    while title_size > 7.0 and t.c.stringWidth(title, FONT_BOLD, title_size) > available_w:
        title_size -= 0.5
    t.text(title_x, badge_base, title, size=title_size, font=FONT_BOLD, color=INK)

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
           size=31, font=FONT_BOLD, color=INK)
    t.text(right_x, below(CODE_BASE_MM), flight.get("to_code", ""),
           size=31, font=FONT_BOLD, color=INK, align="right")
    t.text(left_x, below(CITY_BASE_MM), flight.get("from_city") or flight.get("from_code", ""),
           size=11, color=MUTED)
    t.text(right_x, below(CITY_BASE_MM), flight.get("to_city") or flight.get("to_code", ""),
           size=11, color=MUTED, align="right")
    # 24-hour, as the footer states. Airlines publish schedules this way and
    # it cannot be misread the way "5:35" without a suffix can.
    t.text(left_x, below(TIME_BASE_MM), flight.get("dep_time_raw") or "\u2014",
           size=18, font=FONT_BOLD, color=INK)
    t.text(right_x, below(TIME_BASE_MM), flight.get("arr_time_raw") or "\u2014",
           size=18, font=FONT_BOLD, color=INK, align="right")
    t.text(left_x, below(DATE_BASE_MM), flight.get("date", ""), size=9, color=FAINT)
    t.text(right_x, below(DATE_BASE_MM), flight.get("date", ""), size=9, color=FAINT, align="right")

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
        width = t.c.stringWidth(label, FONT_BOLD, 8) + 2 * pad
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
               font=FONT_BOLD, color=INK)

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
    # Prepare barcodes: collect all unique barcode images across flights
    barcode_imgs = []
    for f in flights:
        b_path = f.get("barcode_path")
        if b_path and os.path.exists(b_path):
            try:
                pil_im = PILImage.open(b_path)
                barcode_imgs.append(pil_im)
            except Exception:
                pass

    unified_barcode_reader = None
    if len(barcode_imgs) == 1:
        buf = io.BytesIO()
        barcode_imgs[0].save(buf, format="PNG")
        buf.seek(0)
        unified_barcode_reader = ImageReader(buf)
    elif len(barcode_imgs) > 1:
        try:
            # Normalize to a common HEIGHT, not width: barcodes extracted from a
            # scanned PDF and ones generated fresh from a PNR have wildly
            # different pixel aspect ratios, so matching width alone left one
            # barcode looking squat and the other tall once stacked.
            target_h = 90
            resized = []
            for im in barcode_imgs:
                w, h = im.size
                rw = max(10, int(w * (target_h / h))) if h > 0 else 50
                resized.append(im.convert("RGB").resize((rw, target_h), PILImage.Resampling.LANCZOS))
            gap = 6
            max_w = max(im.size[0] for im in resized)
            total_h = target_h * len(resized) + gap * (len(resized) - 1)
            stacked = PILImage.new("RGB", (max_w, total_h), (255, 255, 255))
            curr_y = 0
            for im in resized:
                x = (max_w - im.size[0]) // 2
                stacked.paste(im, (x, curr_y))
                curr_y += target_h + gap
            buf = io.BytesIO()
            stacked.save(buf, format="PNG")
            buf.seek(0)
            unified_barcode_reader = ImageReader(buf)
        except Exception:
            pass

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
            "cell", fontName=FONT_BOLD if bold else FONT,
            fontSize=size, leading=size + 2.6, textColor=color)

    # Check if distinct segment PNRs exist across flights
    has_distinct_pnrs = any(f.get("segment_pnr") for f in flights)

    rows = []
    for index, pax in enumerate(passengers):
        name = " ".join(part for part in (pax.get("title"), pax.get("name")) if part)
        sectors = "<br/>".join(f"{f.get('from_code')}-{f.get('to_code')}" for f in flights) \
            if multi else f"{flights[0].get('from_code')}-{flights[0].get('to_code')}" if flights else "—"
        seats = pax.get("seats_per_segment") or []
        meals = pax.get("meals_per_segment") or []
        seat_text = "<br/>".join(_fmt(s) for s in seats) if seats else _fmt(pax.get("seat"))
        meal_text = "<br/>".join(_fmt(m) for m in meals) if meals else _fmt(pax.get("meal"))

        if has_distinct_pnrs and multi:
            pnr_list = [f.get("segment_pnr") or pax.get("pnr") or "—" for f in flights]
            pnr_text = "<br/>".join(escape(p) for p in pnr_list)
        else:
            pnr_text = escape(pax.get("pnr") or "—")

        cells = [
            (xs[0], widths[0], f"{index + 1}.&nbsp;&nbsp;{escape(name)}", True, INK, 10),
            (xs[1], widths[1], sectors, False, MUTED, 9),
            (xs[2], widths[2], pnr_text, False, BODY, 9),
            (xs[3], widths[3], seat_text, False, MUTED, 9),
            (xs[4], widths[4], meal_text, False, MUTED, 9),
        ]
        tallest = 0
        for x, width, html, bold, color, size in cells:
            _, h = Paragraph(html, cell_style(bold, size)).wrap(width - PAX_CELL_INSET_MM * MM, 200)
            tallest = max(tallest, h)

        barcode_image = unified_barcode_reader or next((f["_barcode_image"] for f in flights if f.get("_barcode_image")), None)
        rows.append((cells, max(PAX_ROW_H_MM * MM, tallest + 2 * (PAX_ROW_DY_MM - 2.9) * MM), barcode_image))

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
                width = t.c.stringWidth(label, FONT_BOLD, 7.6) + len(label) * 0.8
                t.tracked(barcode_right - width, label_y, label, size=7.6, tracking=0.8)
            else:
                t.tracked(x, label_y, label, size=7.6, tracking=0.8)

        y = top - head_h
        for row_index, (cells, row_h, barcode_image) in enumerate(chunk):
            if row_index:
                t.rule(MARGIN, y, CONTENT_W)
            base = y - PAX_ROW_DY_MM * MM
            for x, width, html, bold, color, size in cells:
                para = Paragraph(html, cell_style(bold, size, color))
                _, height = para.wrap(width - PAX_CELL_INSET_MM * MM, 200)
                para.drawOn(t.c, x, base + size * 0.72 + 0.7 * MM - height)

            # Only ever the image the agent uploaded. A code this app invents
            # scans to nothing at a gate, so an empty column is the honest
            # result when no barcode was supplied.
            if barcode_image:
                try:
                    box_h = row_h - 2 * MM
                    width, height = barcode_image.getSize()
                    draw_h = min(box_h, barcode_width * height / width)
                    draw_w = draw_h * width / height
                    t.c.drawImage(barcode_image, barcode_right - draw_w,
                                  y - (row_h + draw_h) / 2, draw_w, draw_h,
                                  preserveAspectRatio=True, mask="auto")
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


def draw_additional_info(t, items):
    """The traveller-facing notes, set in two columns.

    Measured as one block before anything is drawn: these notes are the last
    thing on the ticket, and a list that breaks halfway down a column reads as
    though the ticket were truncated.
    """
    items = [item for item in (items or []) if item and item.strip()]
    if not items:
        return

    col_w = (CONTENT_W - INFO_GUTTER_MM * MM) / 2
    half = (len(items) + 1) // 2
    columns = (items[:half], items[half:])
    style = ParagraphStyle(
        "info", fontName=FONT, fontSize=7.2,
        leading=INFO_LEADING_MM * MM, textColor=MUTED,
        leftIndent=2.9 * MM, bulletIndent=0, bulletFontSize=7.2)

    def measure(column):
        height = 0
        for text in column:
            _, item_h = Paragraph(escape(text), style, bulletText="•").wrap(col_w, 400)
            height += item_h + INFO_ITEM_GAP_MM * MM
        return height

    block_h = max(measure(column) for column in columns)
    label_h = SECTION_GAP_MM * MM + 16

    if t.y - (label_h + block_h) < MARGIN:
        t.c.showPage()
        t.y = PAGE_H - MARGIN
    t.y -= 16
    t.tracked(MARGIN, t.y, "Additional Information", size=9, tracking=2.6)
    t.y -= SECTION_GAP_MM * MM

    top = t.y
    for index, column in enumerate(columns):
        x = MARGIN + index * (col_w + INFO_GUTTER_MM * MM)
        y = top
        for text in column:
            para = Paragraph(escape(text), style, bulletText="•")
            _, item_h = para.wrap(col_w, 400)
            para.drawOn(t.c, x, y - item_h)
            y -= item_h + INFO_ITEM_GAP_MM * MM
    t.y = top - block_h


def draw_fares(t, total_str, *, remarks, gst_company, gstin, show_amount=True):
    """Just the amount paid: the ticket states what was charged, not how it
    was arrived at. The label shares the amount's baseline so the two are
    read back as a single line.

    An agent handing the ticket to the traveller rather than the payer can
    leave the amount off; the booking still records what was charged.
    """
    t.space(20 * MM)
    right = PAGE_W - MARGIN
    t.y -= AMOUNT_GAP_MM * MM

    if show_amount:
        label = "Amount Paid"
        value_w = t.c.stringWidth(total_str, FONT_BOLD, 11.5)
        label_w = t.c.stringWidth(label.upper(), FONT_BOLD, 7.6) + len(label) * 1.4
        t.tracked(right - value_w - 3.3 * MM - label_w, t.y, label, size=7.6, tracking=1.4)
        t.text(right, t.y, total_str, size=11.5, font=FONT_BOLD, color=INK, align="right")
    else:
        # Nothing on this line, so give the footer back the room it would
        # have taken rather than leaving a gap where a figure used to be.
        t.y += AMOUNT_GAP_MM * MM

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


def draw_footer(t, *, company=None, issued, contact_line="", terms="", show_agency=True):
    # Measure first: this is the last block on the page, so it only needs to
    # clear the bottom margin, not the larger gap `space()` reserves for cards.
    style = ParagraphStyle("p", fontName=FONT, fontSize=8, leading=TERMS_LEADING_MM * MM)
    _, terms_h = Paragraph(terms, style).wrap(CONTENT_W, 400)
    needed = (DIVIDER_GAP_MM + ISSUED_GAP_MM) * MM + terms_h
    if t.y - needed < MARGIN:
        t.c.showPage()
        t.y = PAGE_H - MARGIN

    t.y -= DIVIDER_GAP_MM * MM
    t.rule(MARGIN, t.y, CONTENT_W, color=HexColor("#EEF0F3"), line_width=0.75)
    t.y -= ISSUED_GAP_MM * MM
    t.text(MARGIN, t.y, issued, size=8, color=FAINT)

    t.y -= 1.7 * MM
    height = t.paragraph(MARGIN, t.y, CONTENT_W, terms, size=8, leading=TERMS_LEADING_MM * MM)
    t.y -= height
