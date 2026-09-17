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
OCR_MIN_DPI_UPSCALE = 1100   # page narrower than this in px gets enlarged
OCR_RETRY_CONF = 72          # mean confidence below this earns a second pass
OCR_MIN_WORDS = 25           # too few words also earns a second pass
OCR_PAGE_TEXT_FLOOR = 120    # embedded chars that make a page worth keeping as-is
OCR_BUDGET_SECONDS = 55      # upload requests must answer, so cap total OCR work

# Page segmentation modes tried in order. 6 ("uniform block") reads ticket
# tables far better than tesseract's default 3, which hunts for columns that
# are not there and interleaves the itinerary with the fare box.
OCR_PSM_ORDER = (6, 4, 3)


def ocr_available():
    """True when both the Python bindings and the tesseract binary are present."""
    try:
        import pymupdf  # noqa: F401
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _ocr_deps():
    import io

    import pymupdf
    import pytesseract
    from PIL import Image, ImageOps

    return io, pymupdf, pytesseract, Image, ImageOps


def _prepare(image, ImageOps):
    """Grayscale, normalise contrast and enlarge a small page.

    Scans arrive faded, over-bright or rendered small; tesseract wants roughly
    300 DPI of black text on white. autocontrast alone recovers most faded
    faxes, and upscaling rescues a phone photo saved at screen resolution.
    """
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image, cutoff=1)
    if image.width < OCR_MIN_DPI_UPSCALE:
        from PIL import Image as _I

        scale = OCR_MIN_DPI_UPSCALE / float(image.width)
        image = image.resize((int(image.width * scale), int(image.height * scale)),
                             _I.LANCZOS)
    return image


def _orient(image, pytesseract):
    """Rotate a sideways or upside-down scan upright using tesseract's OSD.

    A page fed in at 90 degrees OCRs to noise, so this is the difference
    between reading the ticket and rejecting it.
    """
    try:
        osd = pytesseract.image_to_osd(image, config="--psm 0")
    except Exception:
        return image
    match = re.search(r"Rotate:\s*(\d+)", osd)
    if not match:
        return image
    degrees = int(match.group(1)) % 360
    if degrees:
        image = image.rotate(-degrees, expand=True, fillcolor=255)
    return image


def _deskew(image):
    """Straighten a page scanned a couple of degrees off square.

    Rows of a table stop lining up once the page tilts, and the column parser
    reads coordinates, so a small rotation matters more here than it would for
    plain text. The angle chosen is the one whose horizontal projection has the
    sharpest peaks - text rows are darkest when they are level. Resizing to a
    single column makes PIL compute each row's mean in C, which keeps the
    search to a few milliseconds instead of a per-pixel loop in Python.
    """
    try:
        from PIL import Image as _I
    except ImportError:
        return image
    small = image.resize((max(image.width // 4, 1), max(image.height // 4, 1)), _I.BILINEAR)
    best_angle, best_score = 0.0, None
    for tenths in range(-30, 31, 5):
        angle = tenths / 10.0
        test = small.rotate(angle, expand=False, fillcolor=255) if angle else small
        profile = list(test.resize((1, test.height), _I.BILINEAR).getdata())
        score = sum((profile[i + 1] - profile[i]) ** 2 for i in range(len(profile) - 1))
        if best_score is None or score > best_score:
            best_angle, best_score = angle, score
    # Below half a degree the rotation costs more in resampling blur than it
    # recovers in alignment.
    if abs(best_angle) >= 0.5:
        image = image.rotate(best_angle, expand=True, fillcolor=255)
    return image


# Tesseract reads these interchangeably in short uppercase codes, where there
# is no surrounding word for its language model to lean on.
_CONFUSIONS = str.maketrans({"O": "0", "D": "0", "I": "1", "L": "1",
                             "S": "5", "B": "8", "Z": "2", "G": "6"})
_UNCONFUSE = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B"})


def _fix_codes(text, is_airport=None, is_airline=None):
    """Repair OCR confusions inside airport, flight and PNR codes.

    Only tokens that sit where a code belongs are touched, so ordinary words
    and real numbers are left alone.
    """
    def flight(match):
        carrier, number = match.group(1), match.group(2)
        fixed_carrier = carrier.translate(_UNCONFUSE)
        if is_airline and not is_airline(carrier) and is_airline(fixed_carrier):
            carrier = fixed_carrier
        return carrier + number.translate(_CONFUSIONS)

    text = re.sub(r"\b([A-Z0-9]{2})[ -]?(\d[A-Z0-9]{1,4})\b", flight, text)

    def airport(match):
        code = match.group(1)
        fixed = code.translate(_UNCONFUSE)
        if is_airport and not is_airport(code) and is_airport(fixed):
            return "(" + fixed + ")"
        return match.group(0)

    return re.sub(r"\(([A-Z0-9]{3})\)", airport, text)


def _words_from_data(data, scale):
    """Turn tesseract's TSV rows into pdfplumber-shaped word boxes.

    The columnar parser works in PDF points off x0/top, so pixel coordinates
    are divided back down by the render scale.
    """
    words = []
    for i, text in enumerate(data.get("text", [])):
        text = (text or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (KeyError, ValueError, IndexError):
            conf = -1.0
        if conf < 0:
            continue
        left, top = data["left"][i] / scale, data["top"][i] / scale
        words.append({
            "text": text,
            "x0": left,
            "x1": left + data["width"][i] / scale,
            "top": top,
            "bottom": top + data["height"][i] / scale,
            "conf": conf,
        })
    return words


def _text_from_data(data):
    """Rebuild text the way tesseract itself would lay it out.

    Grouping by the block, paragraph and line numbers tesseract reports keeps
    each labelled box on its own line. Joining words purely by vertical
    position instead would run a label in one column into an unrelated value
    in the next, which is enough to stop "PNR: ..." matching at all.
    """
    lines, order = {}, []
    for i, text in enumerate(data.get("text", [])):
        text = (text or "").strip()
        if not text:
            continue
        try:
            if float(data["conf"][i]) < 0:
                continue
        except (KeyError, ValueError, IndexError):
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append((text, data["left"][i], data["width"][i], data["height"][i]))

    rendered = []
    for key in order:
        parts = []
        previous = None
        for text, left, width, height in lines[key]:
            # OCR sometimes breaks one token into pieces ("S 6BKR C"), which
            # hides a booking reference from every pattern looking for it. A
            # real space is a sizeable fraction of the text height, so a gap
            # far below that means the pieces were never separate words.
            if previous is not None and left - previous <= 0.15 * height:
                parts[-1] += text
            else:
                parts.append(text)
            previous = left + width
        rendered.append(" ".join(parts))
    return "\n".join(rendered).strip()


OCR_REFINE_CONF = 55     # below this a code-shaped word is read again, alone
OCR_REFINE_MAX = 12      # cap the re-reads so a bad page cannot run away


def _refine_codes(image, data, pytesseract):
    """Read low-confidence reference codes again, one word at a time.

    A booking reference has no surrounding language for tesseract to lean on,
    so a smudged glyph becomes a plain substitution - "S6BKRC" read as
    "SOBKRC" - and one wrong character makes the reference useless. Those
    words are recognisable by their low confidence, and re-reading just that
    box, enlarged and in single-word mode, usually settles it. The rest of the
    page is left alone, so this costs a fraction of a second.
    """
    refined = 0
    for i, text in enumerate(data.get("text", [])):
        if refined >= OCR_REFINE_MAX:
            break
        text = (text or "").strip()
        try:
            conf = float(data["conf"][i])
        except (KeyError, ValueError, IndexError):
            continue
        if conf < 0 or conf >= OCR_REFINE_CONF:
            continue
        # Code-shaped: upper case and unbroken. Requiring a digit would miss
        # exactly the case this exists for, where the digit is what was misread
        # ("S6BKRC" coming back as "SOBKRC"). Ordinary prose is left alone -
        # the language model reads it better than a single-word pass can.
        if not re.fullmatch(r"[A-Z0-9]{4,12}", text):
            continue
        best_text, best_conf = None, conf
        # How much of the surrounding page to include matters more than it
        # looks: too tight clips the glyphs, too loose drags in a neighbouring
        # rule and tesseract then reports no confidence at all for a reading
        # that is otherwise correct. Try both and keep the surest answer.
        for margin in (0.2, 0.35):
            pad = max(int(data["height"][i] * margin), 3)
            left = max(data["left"][i] - pad, 0)
            top = max(data["top"][i] - pad, 0)
            right = min(data["left"][i] + data["width"][i] + pad, image.width)
            bottom = min(data["top"][i] + data["height"][i] + pad, image.height)
            if right - left < 8 or bottom - top < 8:
                continue
            try:
                from PIL import Image as _I

                crop = image.crop((left, top, right, bottom))
                crop = crop.resize((crop.width * 3, crop.height * 3), _I.LANCZOS)
                again = pytesseract.image_to_data(
                    crop, output_type=pytesseract.Output.DICT,
                    # Upper case only: allowing both cases makes tesseract
                    # report a confidence of zero, so a correct re-read could
                    # never be told from a wrong one.
                    config="--psm 8 --oem 1 -c tessedit_char_whitelist="
                           "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
            except Exception:
                continue
            for j, candidate in enumerate(again.get("text", [])):
                candidate = (candidate or "").strip()
                if not candidate:
                    continue
                try:
                    candidate_conf = float(again["conf"][j])
                except (KeyError, ValueError, IndexError):
                    continue
                if candidate_conf > best_conf and len(candidate) == len(text):
                    best_text, best_conf = candidate, candidate_conf
        refined += 1
        if best_text and best_text != text:
            data["text"][i] = best_text
            data["conf"][i] = best_conf
    return data


def _mean_conf(words):
    scored = [w["conf"] for w in words if w["conf"] >= 0]
    return sum(scored) / len(scored) if scored else 0.0


def ocr_pdf_pages(data, max_pages=OCR_MAX_PAGES, dpi=OCR_DPI,
                  is_airport=None, is_airline=None, only_pages=None):
    """OCR a scanned PDF into per-page text *and* word boxes.

    Word boxes matter as much as the text: the columnar agency layout is only
    readable as coordinates, so without them a scan of that ticket parses to
    nothing even when every character is recognised. Returns a list of
    {"text", "words", "conf"} dicts, empty when OCR is unavailable. Pages are
    indexed from zero; `only_pages` restricts the work to the pages a caller
    could not read itself, so a mostly digital document is not re-recognised
    from scratch.
    """
    try:
        io, pymupdf, pytesseract, Image, ImageOps = _ocr_deps()
    except ImportError:
        return []

    import time

    scale = dpi / 72.0
    deadline = time.monotonic() + OCR_BUDGET_SECONDS
    pages = []
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            for index, page in enumerate(list(document)[:max_pages]):
                if only_pages is not None and index not in only_pages:
                    continue
                # Return what has been read rather than let a long document
                # hold the request open past the proxy's patience.
                if time.monotonic() > deadline and pages:
                    break
                pixmap = page.get_pixmap(dpi=dpi)
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                image = _prepare(image, ImageOps)
                image = _orient(image, pytesseract)
                image = _deskew(image)
                page_scale = scale * (image.width / float(pixmap.width or image.width))

                best = None
                for psm in OCR_PSM_ORDER:
                    try:
                        tsv = pytesseract.image_to_data(
                            image, config=f"--psm {psm} --oem 1",
                            output_type=pytesseract.Output.DICT)
                    except Exception:
                        continue
                    words = _words_from_data(tsv, page_scale)
                    conf = _mean_conf(words)
                    if best is None or (len(words), conf) > (len(best[0]), best[1]):
                        best = (words, conf, tsv)
                    # A confident, populated read is not improved by trying the
                    # next mode, and each pass costs seconds.
                    if conf >= OCR_RETRY_CONF and len(words) >= OCR_MIN_WORDS:
                        break
                    if time.monotonic() > deadline:
                        break
                if best is None:
                    continue
                words, conf, tsv = best
                tsv = _refine_codes(image, tsv, pytesseract)
                words = _words_from_data(tsv, page_scale)
                text = _fix_codes(_text_from_data(tsv), is_airport, is_airline)
                pages.append({"index": index, "text": text,
                              "words": words, "conf": conf})
    except Exception:
        return pages
    return pages


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


# Structure of our own ticket, used when the wordmark itself is unreadable.
OWN_SECTIONS = ("BOOKING SUMMARY", "FLIGHT DETAILS", "PASSENGER DETAILS",
                "FARE DETAILS", "ITINERARY", "PASSENGERS")


def looks_like_own_ticket(text_upper):
    """True for a ticket this app produced.

    The agency name is the strong signal. The wordmark is drawn in a styled
    face and a scan of it often comes back as gibberish, so an unreadable
    "E-TICKET" falls back to the section headings the layout always prints -
    otherwise every scanned copy of our own ticket is handed to the parsers
    written for other people's formats.
    """
    headings = sum(1 for heading in OWN_SECTIONS if heading in text_upper)
    if not any(brand in text_upper for brand in OWN_BRANDS):
        # A poor scan can lose the agency name as well as the wordmark. The
        # layout still identifies itself: no other issuer in our samples prints
        # a "BOOKING SUMMARY" block, let alone beside three more of our own
        # headings.
        return "BOOKING SUMMARY" in text_upper and headings >= 3
    if any(word in text_upper for word in ("E-TICKET", "ETICKET", "E TICKET")):
        return True
    return headings >= 2


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


def _strip_lead_noise(line):
    """Drop a stray leading token left behind by OCR.

    Logos, row numbers and table rules come through as a character or two in
    front of the real content ("A QP1502 ...", "at Mr KARTIKEY ..."). Callers
    only reach for this once an anchored match has already failed, so a line
    that reads correctly is never re-interpreted.
    """
    return re.sub(r"^[A-Za-z0-9\u2022*.,:;|]{1,2}\s+(?=[A-Za-z0-9])", "", line, count=1)


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
            # OCR often prefixes a row with a stray character picked out of an
            # airline logo ("A QP1502 ..."), which defeats an anchored match.
            # Only retried once the line has already failed, so a row that
            # parses normally can never be re-read a different way.
            match = AGENCY_FLIGHT_RE.match(_strip_lead_noise(line).upper())
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
        # Match the label on its letters alone: a scan leaves specks and
        # punctuation around it, so "ue Airline PNR :" must still count.
        def _is_pnr_label(line):
            # Compare on letters alone, and only require the label to appear
            # somewhere on the line: a scan drops specks either side of it, so
            # demanding the line hold nothing else loses the label entirely.
            letters = re.sub(r"[^A-Z]", "", line.upper())
            return letters == "PNR" or "AIRLINEPNR" in letters

        label = next((i for i, l in enumerate(lines) if _is_pnr_label(l)), None)
        if label is not None:
            for candidate in reversed(lines[max(0, label - 4):label]):
                # The value shares its line with whatever the scan made of the
                # address behind it, so take the last code-shaped token rather
                # than insisting the line ends with it.
                tokens = re.findall(r"\b([A-Z][A-Z0-9]{4,7})\b", candidate)
                tokens = [t for t in tokens if re.search(r"\d", t) and re.search(r"[A-Z]", t)]
                if tokens:
                    result["pnr"] = tokens[-1]
                    break

    # The separators around "Reference Number" vary, and OCR adds its own, so
    # allow punctuation on either side of the word and never take the word
    # itself as the reference.
    reference = re.search(
        r"REFERENCE\s*[.,:\-]?\s*(?:NUMBER|NO)?\s*[.,:\-]?\s*([A-Z0-9]{5,20})", upper)
    if reference and reference.group(1) in ("NUMBER", "NO"):
        reference = None
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
        stripped = line.strip()
        pax_re = (r"^(MR|MRS|MS|MSTR|DR|MISS)\.?\s+([A-Z][A-Za-z .'\-]{2,40}?)"
                  r"(?=\s+(?:ADULT|CHILD|INFANT)\b|\s*$)")
        match = re.match(pax_re, stripped, re.I)
        if not match:
            # A scan puts a row number or a fragment of the table rule in front
            # of the title, which defeats an anchored match.
            match = re.match(pax_re, _strip_lead_noise(stripped), re.I)
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
        # "PNR" is three glyphs with no word around them, so OCR readily turns
        # it into "PNA" or "PNB". The row must still name the booking ID, which
        # keeps this from matching anything else.
        if ("BOOKING ID" in line.upper() and re.search(r"\bPN[A-Z]\b", line.upper())
                and idx + 1 < len(lines)):
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

    if not result["pnr"] or not result["booking_id"]:
        # A faint scan can lose the header row altogether, leaving the values
        # directly under "BOOKING SUMMARY". Find that row by its shape - an
        # identifier, a date, then the booking reference - rather than by the
        # labels that were supposed to sit above it.
        anchor = next((i for i, l in enumerate(lines) if "BOOKING SUMMARY" in l.upper()), None)
        if anchor is not None:
            for candidate in lines[anchor + 1:anchor + 5]:
                shape = re.match(r"^([A-Z0-9]{6,25})\s+(.*\b\d{4})\s+([A-Z0-9]{5,8})$",
                                 candidate.strip(), re.I)
                if not shape:
                    continue
                date = _find_date(shape.group(2))
                if not date:
                    continue
                result["booking_id"] = result["booking_id"] or shape.group(1)
                result["booking_date"] = result["booking_date"] or date
                result["pnr"] = result["pnr"] or shape.group(3)
                break

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
        # OCR frequently closes the gap in "6E 5936", so the space is
        # optional; the guards below still require a real flight cell.
        match = re.match(r"^([A-Z0-9]{2})\s*(\d{2,4})\b", line)
        if not match:
            continue
        # A wrapped row repeats the airline code followed by the continuation
        # of the cell above - a time, or the year of the travel date. Neither
        # is a flight number, so require the row to carry a real flight cell
        # (an airport code or a departure time) before accepting it.
        if re.match(r"^[A-Z0-9]{2}\s*\d{1,2}:\d{2}", line):
            continue
        # The cell carrying the airport code or the time renders on the line
        # above or below the flight number, and a scan does not always keep
        # them together, so accept the evidence from the wrapped row too.
        neighbourhood = "\n".join(flight_lines[max(0, idx - 2):idx + 3])
        if not re.search(r"\([A-Z]{3}\)|\d{1,2}:[0-5]\d", neighbourhood):
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

    if not entries and sectors:
        # A poor scan can lose the flight-number row entirely while the sector
        # list survives further down the page. The route is the part the user
        # most needs filled in, so take it from there and leave the rest blank
        # rather than report no itinerary at all.
        entries = [{"flight_no": "", "dep_time_raw": "", "arr_time_raw": "",
                    "date": _find_date(flight_text, fallback_year)} for _ in sectors]

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
        # Require a plausible human name, not a stray table fragment. A row of
        # two-letter specks ("ee ee ce ee") is what a scan leaves where a rule
        # crossed the page, so insist on at least one substantial word.
        if len(words) < 2 or any(w.upper() in NOISE for w in words):
            continue
        if not any(len(w) >= 3 for w in words):
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


# Words that sit in a passenger row but are never part of a name.
_NOT_NAME = {
    "ADULT", "CHILD", "INFANT", "MALE", "FEMALE", "GENDER", "STATUS", "CONFIRMED",
    "VEG", "NONVEG", "MEAL", "MEALS", "SEAT", "KG", "PIECE", "PIECES", "BAGGAGE",
    "HAND", "CHECKIN", "CHECK", "ONWARD", "RETURN", "NOT", "SELECTED", "NA",
    "PASSENGER", "PASSENGERS", "DETAILS", "TICKET", "NO", "SR", "TYPE", "PNR",
    "SECTOR", "FARE", "TOTAL", "AMOUNT", "INR", "YES", "CANCELLED", "WHEELCHAIR",
}
_TITLES = {"MR", "MRS", "MS", "MSTR", "MASTER", "DR", "MISS"}


def ocr_passenger_names(pages_words):
    """Read passenger names out of a scanned table using the word geometry.

    OCR damages the edges of a name cell more than the name itself: it invents
    a token from the rule beside it ("Harijan HSER |"), mangles the word that
    should end the row ("adult" -> "adutt"), or drops the separator a text
    pattern was relying on. Matching on where the words sit avoids all of it -
    the gap between two columns is several times wider than the space between
    two words, so the name is the run of words that stays close to its title.
    """
    names = []
    seen = set()
    for words in pages_words or []:
        lines = _group_word_lines(words, tolerance=3.0)
        texts = [" ".join(w["text"] for w in line).upper() for _top, line in lines]
        # Confine the search to the passenger table. The notes further down the
        # page are prose, and a sentence opening with a title word otherwise
        # reads as a passenger.
        start = next((i for i, t in enumerate(texts)
                      if any(word in t for word in ("PASSENGER", "TRAVELLER", "TRAVELER",
                                                    "GUEST", "PAX"))), None)
        if start is None:
            continue
        # Start looking for the end of the table a couple of lines in: the row
        # right under the header is the first passenger, and a stop word
        # landing there would close the section before reading anyone.
        stop = next((i for i, t in enumerate(texts[start + 2:], start + 2)
                     if any(end in t for end in ("FARE DETAILS", "PAYMENT DETAILS",
                                                 "ADDITIONAL INFORMATION", "TRAVEL CHECKLIST",
                                                 "TERMS", "IMPORTANT"))), len(lines))
        window = lines[start:stop]
        for line_index, (_top, line) in enumerate(window):
            for i, word in enumerate(line):
                token = re.sub(r"[^A-Za-z]", "", word["text"]).upper()
                if token not in _TITLES or word["bottom"] - word["top"] < 3.0:
                    continue
                height = max(word["bottom"] - word["top"], 1.0)
                parts, previous, column_right = [], word, None
                for nxt in line[i + 1:]:
                    # Scanning throws off specks - a stray comma or bracket a
                    # couple of pixels tall - and OCR reports them as words
                    # sitting inside the name. Stepping over them matters:
                    # treating one as the end of the cell truncates the name to
                    # its first word.
                    if nxt["bottom"] - nxt["top"] < 0.5 * height:
                        continue
                    text = nxt["text"].strip(".,:;|()[]")
                    if not text:
                        continue
                    # A space inside a cell is a fraction of the text height;
                    # anything approaching the height itself is a column break.
                    if nxt["x0"] - previous["x1"] > 1.5 * height:
                        column_right = nxt["x0"]
                        break
                    if not re.fullmatch(r"[A-Za-z][A-Za-z.'\-]*", text) or len(text) < 2:
                        column_right = nxt["x0"]
                        break
                    if text.upper() in _NOT_NAME:
                        column_right = nxt["x0"]
                        break
                    parts.append(text)
                    previous = nxt
                    if len(parts) >= 5:
                        break
                # A long name wraps inside its cell, so the rest of it sits on
                # the next line under the same column. Take only words that
                # start within that column's span.
                # Only a name cut off after its first word is worth chasing onto
                # the next line. Anything longer already reads as a full name,
                # and continuing it is how a city from the row below ends up
                # welded to a passenger.
                if len(parts) == 1:
                    # The cell runs to wherever the next column starts, not to
                    # the end of the longest word above, or the tail of a
                    # wrapped name is read as belonging to the column beside it.
                    left_edge = word["x0"]
                    right_edge = (column_right - 2 if column_right is not None
                                  else previous["x1"] + 3 * height)
                    # Scanning drops a line of specks between the two halves of
                    # a wrapped cell, so look past a line that yields nothing.
                    for next_top, following in window[line_index + 1:line_index + 3]:
                        # The rest of a wrapped cell sits directly underneath;
                        # a line further down belongs to another row.
                        if next_top - _top > 2.5 * height:
                            break
                        taken = 0
                        for nxt in following:
                            if nxt["x0"] < left_edge - 2 or nxt["x0"] > right_edge:
                                continue
                            if nxt["bottom"] - nxt["top"] < 0.5 * height:
                                continue
                            text = nxt["text"].strip(".,:;|()[]")
                            if (not re.fullmatch(r"[A-Za-z][A-Za-z.'\-]*", text)
                                    or len(text) < 2 or text.upper() in _NOT_NAME):
                                break
                            parts.append(text)
                            taken += 1
                            if len(parts) >= 5:
                                break
                        if taken:
                            break

                if len(parts) >= 2:
                    name = " ".join(parts).title()
                    if name.upper() not in seen:
                        seen.add(name.upper())
                        names.append({"name": name, "title": word["text"].strip(".").title()})
                break
    return names
