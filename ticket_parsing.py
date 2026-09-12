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
