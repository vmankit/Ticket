"""Parser for e-tickets this app generated itself.

Re-uploading a previously generated ticket (to reissue or correct it) is a
normal workflow, and the generic heuristic parser used for OTA tickets does
badly on our own layout. Because we control that layout exactly, it can be
read from a handful of stable anchors instead of guessed at.
"""

import re

OWN_BRANDS = ("BHARAT HORIZON TRAVELS", "ANKIT TRAVELS")

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

# "Fri, 22 May 2026" / "22 May 2026" / "Thu, 21 May" (year supplied separately)
DATE_RE = re.compile(
    r"(?:[A-Z][a-z]{2},?\s+)?(\d{1,2})\s+([A-Z][a-z]{2})[a-z]*\.?(?:\s+(\d{4}))?")
TIME_RE = re.compile(r"\b(\d{1,2}):([0-5]\d)\s*([AP]M)\b", re.IGNORECASE)
MONEY_RE = r"(?:INR|[$€£])?\s*(-?[\d,]+\.\d{2})"


OCR_MAX_PAGES = 3
OCR_DPI = 300


def ocr_available():
    """True when both the Python bindings and the tesseract binary are present."""
    try:
        import pymupdf  # noqa: F401
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def ocr_pdf_bytes(data, max_pages=OCR_MAX_PAGES, dpi=OCR_DPI):
    """Read a scanned PDF by rendering its pages and running OCR over them.

    Returns "" when OCR is unavailable, so callers can fall back to telling the
    user the file is a scan rather than failing outright. Only the first few
    pages are read: tickets are one or two pages and OCR costs seconds each.
    """
    try:
        import io

        import pymupdf
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""

    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            pages = []
            for page in list(document)[:max_pages]:
                pixmap = page.get_pixmap(dpi=dpi)
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                pages.append(pytesseract.image_to_string(image))
        return "\n".join(pages).strip()
    except Exception:
        return ""


def normalize_text(text):
    """Fold typographic punctuation to ASCII.

    Agency PDFs commonly use U+2010 and friends, so "QP‐1502" and
    "19‐May‐2026" never match patterns written with an ASCII hyphen.
    """
    if not text:
        return ""
    for source, target in (
        ("‐", "-"), ("‑", "-"), ("‒", "-"), ("–", "-"),
        ("—", "-"), ("―", "-"), ("−", "-"),
        ("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'),
        (" ", " "), (" ", " "), (" ", " "),
        ("₹", "INR "), ("ﬁ", "-"),
    ):
        text = text.replace(source, target)
    return text


def looks_like_own_ticket(text_upper):
    return "E-TICKET" in text_upper and any(b in text_upper for b in OWN_BRANDS)


def _to_iso(day, month_abbr, year):
    month = MONTHS.get(month_abbr.upper()[:3])
    if not month or not year:
        return ""
    try:
        return f"{int(year):04d}-{month:02d}-{int(day):02d}"
    except (TypeError, ValueError):
        return ""


def _find_date(chunk, fallback_year=""):
    """Read the first date in `chunk`, tolerating a year split onto the next line."""
    match = DATE_RE.search(chunk)
    if not match:
        return ""
    day, month, year = match.group(1), match.group(2), match.group(3)
    if not year:
        trailing = re.search(r"\b(20\d{2})\b", chunk[match.end():])
        year = trailing.group(1) if trailing else fallback_year
    return _to_iso(day, month, year)


def _to_24h(hour, minute, meridiem):
    hour = int(hour)
    if meridiem.upper() == "PM" and hour != 12:
        hour += 12
    elif meridiem.upper() == "AM" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute}"


def _section(lines, start_marker, *end_markers):
    """Lines between a start marker and the first following end marker."""
    start = next((i for i, l in enumerate(lines) if start_marker in l.upper()), None)
    if start is None:
        return []
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if any(m in lines[i].upper() for m in end_markers):
            end = i
            break
    return lines[start + 1:end]


AGENCY_FLIGHT_RE = re.compile(
    r"^([A-Z0-9]{2})\s?-?\s?(\d{2,4})\b.*?\(([A-Z]{3})\).*?\(([A-Z]{3})\)")
AGENCY_TIMES_RE = re.compile(
    r"(\d{1,2}:[0-5]\d)\s+(\d{1,2}-[A-Za-z]{3}-\d{4})\s+(\d{1,2}:[0-5]\d)\s+(\d{1,2}-[A-Za-z]{3}-\d{4})")


STACKED_FLIGHT_RE = re.compile(r"\b([A-Z0-9]{2})\s?-?\s?(\d{2,4})\b")


def parse_stacked_itinerary(text, is_airport, is_airline):
    """Read compact tickets that stack each field on its own line.

        AI AIR INDIA - AI 422 ECONOMY
        ATQ DEL
        13:10 14:15
        14 Sep 2026 14 Sep 2026

    There are no "from"/"to" labels to anchor on, so a flight-number line is
    located first and the rows beneath it supplied the route, times and date.
    `is_airport` is injected to avoid importing the airport database here.
    """
    # A booking UUID contains chunks like "-fd86-" that read as a flight number.
    cleaned = re.sub(
        r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b",
        " ", normalize_text(text))
    lines = [l.strip() for l in cleaned.splitlines() if l.strip()]
    flights = []

    for idx, line in enumerate(lines):
        upper = line.upper()
        if not re.search(r"\b(FLIGHT|ITINERARY)\b", upper) and "·" not in line and "-" not in line:
            continue
        # Require a real airline code, or a postal address ("Sector 6, HSR
        # Layout") supplies both a "flight number" and two airport codes.
        match = next((m for m in STACKED_FLIGHT_RE.finditer(upper) if is_airline(m.group(1))), None)
        if not match:
            continue
        flight_no = f"{match.group(1)} {match.group(2)}"

        window = lines[idx + 1:idx + 7]
        route = times = date = None
        for candidate in window:
            candidate_upper = candidate.upper()
            if STACKED_FLIGHT_RE.search(candidate_upper) and re.search(r"\b(FLIGHT|·)\b", candidate):
                break  # the next segment starts here
            if route is None:
                codes = [c for c in re.findall(r"\b[A-Z]{3}\b", candidate_upper) if is_airport(c)]
                if len(codes) >= 2 and codes[0] != codes[1]:
                    route = (codes[0], codes[1])
                    continue
            if times is None:
                found = re.findall(r"\b([0-2]?\d:[0-5]\d)\b", candidate)
                if len(found) >= 2:
                    times = (found[0].zfill(5), found[1].zfill(5))
                    continue
            if date is None:
                found = _find_date(candidate)
                if found:
                    date = found

        if not route:
            continue
        segment = {"flight_no": flight_no, "from_code": route[0], "to_code": route[1]}
        if times:
            segment["dep_time_raw"], segment["arr_time_raw"] = times
        if date:
            segment["date"] = date
        if not any(f["from_code"] == segment["from_code"]
                   and f["to_code"] == segment["to_code"] for f in flights):
            flights.append(segment)

    return flights


def parse_agency_ticket(text):
    """Parse an agency-issued ticket (not one of the big OTAs).

    These follow a recognisable shape - a flight row carrying both airport
    codes, with times and dates on the row beneath - even though each agency
    brands them differently, so match on that structure rather than the brand.
    """
    text = normalize_text(text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    joined = "\n".join(lines)
    upper = joined.upper()

    flights = []
    for idx, line in enumerate(lines):
        match = AGENCY_FLIGHT_RE.match(line.upper())
        if not match:
            continue
        segment = {
            "flight_no": f"{match.group(1)} {match.group(2)}",
            "from_code": match.group(3),
            "to_code": match.group(4),
        }
        window = " ".join(lines[idx:idx + 3])
        times = AGENCY_TIMES_RE.search(window)
        if times:
            segment["dep_time_raw"] = times.group(1).zfill(5)
            segment["arr_time_raw"] = times.group(3).zfill(5)
            segment["date"] = _find_date(times.group(2).replace("-", " "))
        flights.append(segment)

    if not flights:
        return None

    result = {
        "booking_platform": "", "pnr": "", "booking_id": "", "booking_date": "",
        "customer_email": "", "customer_phone": "", "base_fare": "", "taxes_fees": "",
        "total_fare": "", "flights": flights, "passengers": [],
    }

    # The airline PNR is often printed above its own label rather than after it.
    pnr = re.search(r"\b(?:AIRLINE\s+)?PNR\b\s*[:\-]?\s*([A-Z0-9]{5,8})\b", upper)
    if pnr:
        result["pnr"] = pnr.group(1)
    else:
        # The value is printed above the label, often as the last token of an
        # unrelated address line, so scan backwards for a PNR-shaped token.
        label = next((i for i, l in enumerate(lines) if l.upper().strip() in ("AIRLINE PNR", "PNR")), None)
        if label:
            for candidate in reversed(lines[max(0, label - 4):label]):
                token = re.search(r"\b([A-Z][A-Z0-9]{4,7})\s*$", candidate.strip())
                if token and re.search(r"\d", token.group(1)) and re.search(r"[A-Z]", token.group(1)):
                    result["pnr"] = token.group(1)
                    break

    reference = re.search(r"REFERENCE\s*(?:NUMBER|NO)?\.?\s*[:\-]?\s*([A-Z0-9]{5,20})", upper)
    if reference:
        result["booking_id"] = reference.group(1)

    issued = re.search(r"ISSUED\s*ON\.?\s*[:\-]?\s*([0-9A-Za-z/\- ]{6,20})", joined, re.I)
    if issued:
        result["booking_date"] = _find_date(issued.group(1)) or ""
        if not result["booking_date"]:
            slash = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", issued.group(1))
            if slash:
                result["booking_date"] = f"{slash.group(3)}-{int(slash.group(2)):02d}-{int(slash.group(1)):02d}"

    email = re.search(r"[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}", joined)
    if email:
        result["customer_email"] = email.group(0)
    phone = re.search(r"(?:PHONE|MOBILE(?:\s*NO)?)\s*[:\-]?\s*(\+?\d[\d\s-]{8,14})", joined, re.I)
    if phone:
        result["customer_phone"] = phone.group(1).strip()

    for key, pattern in (
        ("base_fare", r"BASE\s*FARE\s*" + MONEY_RE),
        ("taxes_fees", r"TAX(?:ES)?(?:\s*(?:AND|&)\s*FEES)?\s*" + MONEY_RE),
        ("total_fare", r"(?:GROSS\s*FARE|TOTAL(?:\s*(?:FARE|AMOUNT))?)\s*" + MONEY_RE),
    ):
        match = re.search(pattern, upper)
        if match:
            result[key] = match.group(1).replace(",", "")

    seen = set()
    for line in lines:
        match = re.match(
            r"^(MR|MRS|MS|MSTR|DR|MISS)\.?\s+([A-Z][A-Za-z .'\-]{2,40}?)"
            r"(?=\s+(?:ADULT|CHILD|INFANT)\b|\s*$)", line.strip(), re.I)
        if not match:
            continue
        name = re.sub(r"\s{2,}", " ", match.group(2)).strip().title()
        if len(name.split()) < 2 or name.upper() in seen:
            continue
        seen.add(name.upper())
        result["passengers"].append({"name": name, "title": match.group(1).title()})

    return result


def _parse_own_legacy(text):
    """Read the pre-redesign layout ("BOOKING SUMMARY" / "FLIGHT DETAILS").

    Tickets issued before the redesign are still in circulation and get
    re-uploaded to be reissued, so that layout has to keep working.
    """

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    joined = "\n".join(lines)

    result = {
        "booking_platform": "", "pnr": "", "booking_id": "", "booking_date": "",
        "customer_email": "", "customer_phone": "", "base_fare": "", "taxes_fees": "",
        "total_fare": "", "flights": [], "passengers": [],
    }

    # ── Booking summary: a header row followed by its values ──────────────
    for idx, line in enumerate(lines):
        if "BOOKING ID" in line.upper() and "PNR" in line.upper() and idx + 1 < len(lines):
            values = lines[idx + 1]
            match = re.match(r"^(\S+)\s+(.*?)\s+([A-Z0-9]{4,10})$", values)
            if match:
                result["booking_id"] = match.group(1)
                result["booking_date"] = _find_date(match.group(2))
                result["pnr"] = match.group(3)
            break

    if not result["pnr"]:
        pnr_match = re.search(r"\bPNR\s*/?\s*BOOKING\s*REF\b[^A-Z0-9]{0,10}([A-Z0-9]{4,10})", joined, re.I)
        if pnr_match:
            result["pnr"] = pnr_match.group(1)

    emails = re.findall(r"[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}", joined)
    # The agency's own address appears in the header; the customer's is on the
    # passenger line, so prefer the last distinct one.
    if emails:
        result["customer_email"] = emails[-1]
    pax_contact = re.search(r"PASSENGER DETAILS\s+(\+?[\d\s-]{10,})", joined)
    if pax_contact:
        result["customer_phone"] = pax_contact.group(1).strip()

    # ── Fares ─────────────────────────────────────────────────────────────
    for key, pattern in (
        ("base_fare", r"Base Fare\s+" + MONEY_RE),
        ("taxes_fees", r"Airline Taxes(?:\s*&\s*Fees)?\s+" + MONEY_RE),
        ("total_fare", r"Total Amount\s+" + MONEY_RE),
        # Label and amount are set at the same size, so they extract as one line.
        ("total_fare", r"AMOUNT\s*PAID\s+" + MONEY_RE),
    ):
        match = re.search(pattern, joined, re.I)
        if match:
            result[key] = match.group(1).replace(",", "")

    # ── Flights ───────────────────────────────────────────────────────────
    flight_lines = _section(lines, "FLIGHT DETAILS", "PASSENGER DETAILS", "FARE DETAILS")
    flight_text = "\n".join(flight_lines)
    fallback_year = ""
    year_match = re.search(r"\b(20\d{2})\b", flight_text)
    if year_match:
        fallback_year = year_match.group(1)

    # Sectors ("HYD-SHJ") list the routes in order and are the most reliable
    # pairing available, since the flight table wraps across lines.
    sectors = re.findall(r"\b([A-Z]{3})-([A-Z]{3})\b", joined)
    if not sectors:
        sectors = re.findall(r"\b([A-Z]{3})\s+fi\s+([A-Z]{3})\b", joined)

    entries = []
    for idx, line in enumerate(flight_lines):
        match = re.match(r"^([A-Z0-9]{2})\s+(\d{2,4})\b", line)
        if not match:
            continue
        # A wrapped row repeats the airline code followed by the continuation
        # of the cell above - a time, or the year of the travel date. Neither
        # is a flight number, so require the row to carry a real flight cell
        # (an airport code or a departure time) before accepting it.
        if re.match(r"^[A-Z0-9]{2}\s+\d{1,2}:\d{2}", line):
            continue
        if not re.search(r"\([A-Z]{3}\)|\d{1,2}:[0-5]\d", line):
            continue
        # The table wraps: the airport/date cell renders above the flight-number
        # row and the year below it, so look on both sides.
        window = "\n".join(flight_lines[max(0, idx - 2):idx + 2])
        times = [_to_24h(*t) for t in TIME_RE.findall(window)]
        entries.append({
            "flight_no": f"{match.group(1)} {match.group(2)}",
            "dep_time_raw": times[0] if times else "",
            "arr_time_raw": times[1] if len(times) > 1 else "",
            "date": _find_date(window, fallback_year),
        })

    for idx, entry in enumerate(entries):
        if idx < len(sectors):
            entry["from_code"], entry["to_code"] = sectors[idx]
        result["flights"].append(entry)

    # ── Passengers: "1 Mr Sunil kumar Harijan 000-2939874058 ..." ─────────
    pax_lines = _section(lines, "PASSENGER DETAILS", "FARE DETAILS", "TRAVEL CHECKLIST")
    seen = set()
    # Row shapes seen across versions: "1 Mr A B 890-123456789 ..." and, on
    # older tickets without an index or title, "JOHN DOE DEL-BOM".
    pax_re = re.compile(
        r"^(?:(\d{1,2})\s+)?(?:(MR|MRS|MS|MSTR|DR|MISS)\.?\s+)?"
        r"([A-Za-z][A-Za-z .'\-]{2,40}?)"
        r"(?=\s+\d{3}-\d{6,}|\s+[A-Z]{3}-[A-Z]{3}\b|\s*$)",
        re.I)
    NOISE = {"CHECK-IN", "HAND", "BAGGAGE", "NO PASSENGER NAME SECTOR TICKET NUMBER SEAT MEAL",
             "SELECTED", "NOT", "PIECE", "PIECES", "AIRLINE", "DEFAULT", "MEAL", "SEAT"}
    for line in pax_lines:
        if line.upper().strip() in NOISE or re.match(r"^(Flight \d|Not |Seat|Meal|Bag)", line):
            continue
        match = pax_re.match(line)
        if not match:
            continue
        name = re.sub(r"\s{2,}", " ", match.group(3)).strip()
        words = [w for w in name.split() if w]
        # Require a plausible human name, not a stray table fragment.
        if len(words) < 2 or any(w.upper() in NOISE for w in words):
            continue
        name = name.title()
        if name.upper() in seen:
            continue
        seen.add(name.upper())
        ticket_match = re.search(r"\b(\d{3}-\d{6,})\b", line)
        result["passengers"].append({
            "name": name,
            "title": (match.group(2) or "").title(),
            "ticket_no": ticket_match.group(1) if ticket_match else "",
        })

    return result


def parse_own_ticket(text):
    """Parse a ticket this app generated, in either layout."""
    if not looks_like_own_ticket((text or "").upper()):
        return None
    if re.search(r"\bITINERARY\b", text or "", re.I):
        return _parse_own_current(text)
    return _parse_own_legacy(text)


def _parse_own_current(text):
    """Return parsed fields for one of our own tickets, or None if it isn't one.

    The layout is ours, so it is read from fixed anchors rather than guessed
    at. Text extraction flattens each itinerary card into a short run of lines:

        AI AIR INDIA - AI 422 ECONOMY
        ATQ DEL
        Amritsar New Delhi
        1:10 PM 2:15 PM
        Mon, 14 Sep 2026 1h 5m Mon, 14 Sep 2026
    """
    lines = [l.strip() for l in normalize_text(text).splitlines() if l.strip()]
    joined = "\n".join(lines)

    result = {
        "booking_platform": "", "pnr": "", "booking_id": "", "booking_date": "",
        "customer_email": "", "customer_phone": "", "base_fare": "", "taxes_fees": "",
        "total_fare": "", "flights": [], "passengers": [],
    }

    # ── Header: "... PNR" then "AT 8B6E58" on the line below ─────────────
    for idx, line in enumerate(lines[:6]):
        if line.upper().rstrip().endswith("PNR") and idx + 1 < len(lines):
            tokens = re.findall(r"\b([A-Z0-9]{4,10})\b", lines[idx + 1].upper())
            if tokens:
                result["pnr"] = tokens[-1]
            break

    booking = re.search(r"Booking\s+([A-Za-z0-9][A-Za-z0-9\-]{3,})", joined)
    if booking:
        result["booking_id"] = booking.group(1)

    issued = re.search(r"Issued\s+([0-9]{1,2}\s+[A-Za-z]{3}\s+[0-9]{4})", joined)
    if issued:
        result["booking_date"] = _find_date(issued.group(1))

    # The passenger's contact details are not printed on the ticket, so they
    # cannot be recovered. The only address on the page is the agency's, and
    # returning that would quietly refill it as the customer's.

    # Only the total is printed on the ticket now; the base/tax split is
    # not there to recover. Tickets issued before that change carried the
    # breakdown, and these patterns still read it when present.
    for key, pattern in (
        ("base_fare", r"Base Fare\s+" + MONEY_RE),
        ("taxes_fees", r"Airline Taxes(?:\s*&\s*Fees)?\s+" + MONEY_RE),
        ("total_fare", r"Total Amount\s+" + MONEY_RE),
        # Label and amount are set at the same size, so they extract as one line.
        ("total_fare", r"AMOUNT\s*PAID\s+" + MONEY_RE),
    ):
        match = re.search(pattern, joined, re.I)
        if match:
            result[key] = match.group(1).replace(",", "")

    # Tickets issued before the amount was brought down to the label's size set
    # it much larger, so the two landed on separate lines. Look on either side.
    if not result["total_fare"]:
        for idx, line in enumerate(lines):
            if not re.fullmatch(r"AMOUNT\s*PAID", line.strip(), re.I):
                continue
            neighbours = lines[max(0, idx - 1):idx] + lines[idx + 1:idx + 2]
            for neighbour in neighbours:
                money = re.fullmatch(r"\s*" + MONEY_RE + r"\s*", neighbour)
                if money:
                    result["total_fare"] = money.group(1).replace(",", "")
                    break
            break

    # ── Itinerary cards ──────────────────────────────────────────────────
    header_re = re.compile(
        r"^([A-Z0-9]{2})\s+(.+?)\s+·\s+([A-Z0-9]{2}\s?\d{2,4})\b", re.I)
    route_re = re.compile(r"^([A-Z]{3})\s+([A-Z]{3})$")
    times_re = re.compile(r"^(\d{1,2}:[0-5]\d\s*[AP]M)\s+(\d{1,2}:[0-5]\d\s*[AP]M)$", re.I)

    itinerary = _section(lines, "ITINERARY", "PASSENGERS", "FARE")
    for idx, line in enumerate(itinerary):
        header = header_re.match(line)
        if not header:
            continue
        segment = {
            "flight_no": re.sub(r"\s+", " ", header.group(3)).upper(),
            "airline": header.group(2).title(),
        }
        for candidate in itinerary[idx + 1:idx + 6]:
            if header_re.match(candidate):
                break
            route = route_re.match(candidate.upper())
            if route and "from_code" not in segment:
                segment["from_code"], segment["to_code"] = route.group(1), route.group(2)
                continue
            times = times_re.match(candidate)
            if times and "dep_time_raw" not in segment:
                segment["dep_time_raw"] = _to_24h(*re.match(
                    r"(\d{1,2}):([0-5]\d)\s*([AP]M)", times.group(1), re.I).groups())
                segment["arr_time_raw"] = _to_24h(*re.match(
                    r"(\d{1,2}):([0-5]\d)\s*([AP]M)", times.group(2), re.I).groups())
                continue
            if "date" not in segment:
                found = _find_date(candidate)
                if found:
                    segment["date"] = found
        if segment.get("from_code"):
            result["flights"].append(segment)

    # ── Passenger rows ───────────────────────────────────────────────────
    pax_lines = _section(lines, "PASSENGERS", "FARE", "Issued")
    pax_re = re.compile(
        r"^(\d{1,2})\.\s+(?:(Mr|Mrs|Ms|Mstr|Dr|Miss)\.?\s+)?"
        r"([A-Za-z][A-Za-z .'\-]{2,40}?)\s+(?=[A-Z]{3}-[A-Z]{3}\b|\d{3}-\d)", re.I)
    for line in pax_lines:
        match = pax_re.match(line)
        if not match:
            continue
        name = re.sub(r"\s{2,}", " ", match.group(3)).strip().title()
        if len(name.split()) < 2:
            continue
        ticket = re.search(r"\b(\d{3}-\d{6,})\b", line)
        result["passengers"].append({
            "name": name,
            "title": (match.group(2) or "").title(),
            "ticket_no": ticket.group(1) if ticket else "",
        })

    return result


# ── Columnar agency/invoice tickets ──────────────────────────────────────
# Some agency back-offices lay the itinerary out as a real table: Flight,
# Departure, Duration, Arrival side by side, with each cell wrapping onto its
# own line. Flattened to text the columns interleave — the arrival airport
# lands above the flight number, and a passenger's surname below their row
# number — so these are read from word positions instead, where a column is
# just a range of x.

COLUMNAR_FLIGHT_HEADERS = ("Flight", "Departure", "Duration", "Arrival")
COLUMNAR_PAX_HEADERS = ("Sr No.", "PAX Type", "Passenger Name", "Gender")


def _group_word_lines(words, tolerance=2.5):
    """Group words into visual lines, since a cell's baseline can wobble."""
    lines = []
    for word in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if lines and abs(word["top"] - lines[-1][0]) <= tolerance:
            lines[-1][1].append(word)
        else:
            lines.append((word["top"], [word]))
    return [(top, sorted(ws, key=lambda w: w["x0"])) for top, ws in lines]


def _column_bounds(line_words, headers):
    """Left edge of each named column, or None if this is not that header row."""
    joined = " ".join(w["text"] for w in line_words).upper()
    if not all(h.upper() in joined for h in headers):
        return None
    bounds = []
    for header in headers:
        first = header.split()[0].upper().rstrip(".")
        match = next((w for w in line_words
                      if w["text"].upper().rstrip(".").startswith(first)), None)
        if match is None:
            return None
        bounds.append(match["x0"])
    return bounds if bounds == sorted(bounds) else None


def _column_of(word, bounds):
    for i in range(len(bounds) - 1, -1, -1):
        if word["x0"] >= bounds[i] - 1:
            return i
    return 0


def _find_table(lines, headers):
    """(index of the header line, column left edges) for the first match."""
    for i, (_top, line_words) in enumerate(lines):
        bounds = _column_bounds(line_words, headers)
        if bounds:
            return i, bounds
    return None, None


def _stop_row(line_words, markers):
    joined = " ".join(w["text"] for w in line_words).upper()
    return any(m in joined for m in markers)


def _cell_block(lines, start, bounds, stop_markers):
    """Concatenate each column's text down the table body."""
    columns = ["" for _ in bounds]
    for _top, line_words in lines[start:]:
        if _stop_row(line_words, stop_markers):
            break
        for word in line_words:
            idx = _column_of(word, bounds)
            columns[idx] = (columns[idx] + " " + word["text"]).strip()
    return columns


def _iso_from_words(chunk):
    match = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3})[a-z]*,?\s+(\d{4})", chunk)
    if not match:
        return ""
    month = MONTHS.get(match.group(2).upper()[:3])
    if not month:
        return ""
    return f"{match.group(3)}-{month:02d}-{int(match.group(1)):02d}"


def _endpoint(chunk):
    """Date, 24h time and terminal out of one side of the itinerary row."""
    out = {}
    iso = _iso_from_words(chunk)
    if iso:
        out["date"] = iso
    time = re.search(r"\b(\d{1,2}):([0-5]\d)\b", chunk)
    if time:
        out["time"] = f"{int(time.group(1)):02d}:{time.group(2)}"
    terminal = re.search(r"Terminal\s*:?\s*([A-Za-z0-9]{1,4})", chunk, re.I)
    if terminal:
        out["terminal"] = terminal.group(1)
    code = re.search(r"\(([A-Z]{3})\)", chunk)
    if code:
        out["code"] = code.group(1)
    return out


def _columnar_passengers(lines, header_idx, bounds):
    """Rows keyed on the serial number, with wrapped names pulled back in.

    A long name wraps above and below its own row number, so each name
    fragment is given to the serial number it sits closest to rather than to
    whichever line it happens to share.
    """
    body = []
    for top, line_words in lines[header_idx + 1:]:
        if _stop_row(line_words, ("OTHER DETAILS", "FARE DETAILS", "TOTAL AMOUNT")):
            break
        body.append((top, line_words))

    anchors = []
    for top, line_words in body:
        for word in line_words:
            if _column_of(word, bounds) == 0 and re.fullmatch(r"\d{1,2}", word["text"]):
                anchors.append({"top": top, "sr": int(word["text"]),
                                "name": [], "pax_type": "", "gender": ""})
                break
    if not anchors:
        return []

    for top, line_words in body:
        nearest = min(anchors, key=lambda a: abs(a["top"] - top))
        for word in line_words:
            col = _column_of(word, bounds)
            if col == 1 and not nearest["pax_type"] and word["text"].isalpha():
                nearest["pax_type"] = word["text"].title()
            elif col == 2:
                nearest["name"].append(word["text"])
            elif col == 3 and not nearest["gender"] and word["text"].isalpha():
                nearest["gender"] = word["text"].title()

    passengers = []
    for anchor in sorted(anchors, key=lambda a: a["sr"]):
        name = " ".join(anchor["name"]).strip()
        title = ""
        lead = re.match(r"^(MR|MRS|MS|MSTR|DR)\b\.?\s+", name, re.I)
        if lead:
            title = lead.group(1).title()
            title = {"Mr": "Mr", "Mrs": "Mrs", "Ms": "Ms",
                     "Mstr": "Mstr", "Dr": "Dr"}.get(title, "")
            name = name[lead.end():]
        if not name:
            continue
        entry = {"name": name.strip()}
        if title:
            entry["title"] = title
        if anchor["pax_type"] in ("Adult", "Child", "Infant"):
            entry["pax_type"] = anchor["pax_type"]
        passengers.append(entry)
    return passengers


def parse_columnar_ticket(pages_words):
    """Read a columnar agency ticket from per-page word boxes.

    Returns {} unless the itinerary table is actually found, so the caller can
    fall through to the text-based parsers for every other layout.
    """
    result = {}
    all_words = [w for page in pages_words for w in page]
    if not all_words:
        return {}
    flat = " ".join(w["text"] for w in all_words)

    lines = _group_word_lines(pages_words[0])
    header_idx, bounds = _find_table(lines, COLUMNAR_FLIGHT_HEADERS)
    if header_idx is None:
        return {}

    flight_col, dep_col, dur_col, arr_col = _cell_block(
        lines, header_idx + 1, bounds, ("PASSENGER DETAILS", "OTHER DETAILS"))

    segment = {}
    number = re.match(r"([A-Z0-9]{2})\s*-?\s*(\d{2,4})\b\s*(.*)", flight_col.upper())
    if number:
        segment["flight_no"] = f"{number.group(1)} {number.group(2)}"
        airline = flight_col[number.end(2):].strip(" -")
        if airline:
            segment["airline"] = airline.title()
    departure, arrival = _endpoint(dep_col), _endpoint(arr_col)

    # The header carries the route as "CITY (AAA) - CITY (BBB)", which is
    # cleaner than digging it out of either cell.
    route = re.search(r"\(([A-Z]{3})\)\s*-\s*[A-Za-z .]+\(([A-Z]{3})\)", flat)
    segment["from_code"] = (route.group(1) if route else departure.get("code", ""))
    segment["to_code"] = (route.group(2) if route else arrival.get("code", ""))
    if departure.get("date"):
        segment["date"] = departure["date"]
    if departure.get("time"):
        segment["dep_time_raw"] = departure["time"]
    if arrival.get("time"):
        segment["arr_time_raw"] = arrival["time"]
    if departure.get("terminal"):
        segment["from_terminal"] = departure["terminal"]
    if arrival.get("terminal"):
        segment["to_terminal"] = arrival["terminal"]

    cabin = re.search(r"(\d{1,2}\s*KG)\s*(?:HAND|CABIN)", flat, re.I)
    if cabin:
        segment["hand_bag"] = cabin.group(1).upper().replace(" ", " ")
    checkin = re.search(r"(\d{1,2}\s*KG)\s*(?:CHECK[- ]?IN)", flat, re.I)
    if checkin:
        segment["checkin_bag"] = checkin.group(1).upper()

    if segment.get("flight_no") and segment.get("from_code") and segment.get("to_code"):
        result["flights"] = [segment]

    pnr = re.search(r"Airline\s*PNR\s*:?\s*([A-Z0-9]{5,8})\b", flat, re.I)
    if pnr:
        result["pnr"] = pnr.group(1).upper()
    ref = re.search(r"Reference\s*Number\s*:?\s*([A-Z0-9]{5,14})\b", flat, re.I)
    if ref:
        result["booking_id"] = ref.group(1).upper()
    booked = re.search(r"Booking\s*Date\s*:?\s*(\d{4}-\d{2}-\d{2})", flat, re.I)
    if booked:
        result["booking_date"] = booked.group(1)
    total = re.search(r"Total\s*Amount\D{0,40}?" + MONEY_RE, flat, re.I)
    if total:
        result["total_fare"] = total.group(1).replace(",", "")
    if re.search(r"\bnon[- ]?refundable\b", flat, re.I):
        result["refund_status"] = "Non-Refundable"

    pax_idx, pax_bounds = _find_table(lines, COLUMNAR_PAX_HEADERS)
    if pax_idx is not None:
        passengers = _columnar_passengers(lines, pax_idx, pax_bounds)
        if passengers:
            result["passengers"] = passengers
    return result
