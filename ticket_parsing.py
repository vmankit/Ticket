"""Parser for e-tickets this app generated itself.

Re-uploading a previously generated ticket (to reissue or correct it) is a
normal workflow, and the generic heuristic parser used for OTA tickets does
badly on our own layout. Because we control that layout exactly, it can be
read from a handful of stable anchors instead of guessed at.
"""

import re

OWN_BRANDS = ("JS JULFAQAR TRAVELS", "BHARAT HORIZON TRAVELS", "ANKIT TRAVELS")

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

# "Fri, 22 May 2026" / "22 May 2026" / "Thu, 21 May" (year supplied separately)
DATE_RE = re.compile(
    r"(?:[A-Z][a-z]{2},?\s+)?(\d{1,2})\s+([A-Z][a-z]{2})[a-z]*\.?(?:\s+(\d{2,4}))?")
TIME_RE = re.compile(r"\b(\d{1,2}):([0-5]\d)\s*([AP]M)\b", re.IGNORECASE)
MONEY_RE = r"(?:INR|[$€£])?\s*(-?[\d,]+(?:\.\d{1,2})?)"


def _money(raw, key):
    """Normalise a MONEY_RE capture for storage.

    A fare breakdown prints a discount as a negative ("Discount INR -5,500.00")
    because it is subtracted from the line above. The form field it feeds is
    unsigned and /generate subtracts it again, so the sign is dropped here to
    keep the discount from being added back to the total.
    """
    value = raw.replace(",", "")
    if key == "discount":
        value = value.lstrip("-")
    return value


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
                  is_airport=None, is_airline=None, only_pages=None,
                  psm_order=OCR_PSM_ORDER):
    """OCR a scanned PDF into per-page text *and* word boxes.

    Word boxes matter as much as the text: the columnar agency layout is only
    readable as coordinates, so without them a scan of that ticket parses to
    nothing even when every character is recognised. Returns a list of
    {"text", "words", "conf"} dicts, empty when OCR is unavailable. Pages are
    indexed from zero; `only_pages` restricts the work to the pages a caller
    could not read itself, so a mostly digital document is not re-recognised
    from scratch. `psm_order` overrides the segmentation modes, which lets a
    caller ask for a second opinion when the first reading came up short.
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
                for psm in psm_order:
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


def undouble_if_synthetic_bold(text):
    """Repair PDF text that was extracted with doubled glyphs from synthetic bolding."""
    if not text:
        return ""
    sample = text[:3000]
    doubled_hits = len(re.findall(r"\b([A-Za-z0-9])\1([A-Za-z0-9])\2([A-Za-z0-9])\3\b", sample))
    if doubled_hits < 2:
        return text

    # Deduplicate doubled punctuation
    text = re.sub(r"([()\-:,])\1+", r"\1", text)

    def fix_part(st):
        if len(st) >= 2:
            pairs = len(re.findall(r"([A-Za-z0-9])\1", st))
            if (len(st) == 2 and pairs == 1) or (pairs * 2 >= len(st) * 0.7):
                st = re.sub(r"([A-Za-z0-9])\1", r"\1", st)
        return st

    def fix_word(w):
        parts = re.split(r"([:\-/])", w)
        return "".join(fix_part(p) for p in parts)

    lines = []
    for line in text.splitlines():
        lines.append(" ".join(fix_word(w) for w in line.split()))
    return "\n".join(lines)


def normalize_text(text):
    """Fold typographic punctuation and ligatures to ASCII, and repair synthetic bolding."""
    if not text:
        return ""
    text = text.replace("\x00", "fi")
    for source, target in (
        ("‐", "-"), ("‑", "-"), ("‒", "-"), ("–", "-"),
        ("—", "-"), ("―", "-"), ("−", "-"),
        ("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'),
        (" ", " "), (" ", " "), (" ", " "),
        ("₹", "INR "), ("ﬁ", "fi"), ("ﬂ", "fl"),
    ):
        text = text.replace(source, target)
    text = undouble_if_synthetic_bold(text)
    return text


# Structure of our own ticket, used when the wordmark itself is unreadable.
OWN_SECTIONS = ("BOOKING SUMMARY", "FLIGHT DETAILS", "PASSENGER DETAILS",
                "FARE DETAILS", "ITINERARY", "PASSENGERS")


def looks_like_own_ticket(text_upper):
    """True for a ticket this app produced."""
    ota_markers = (
        "TRAVELLER SEAT MEAL BAGGAGE", "FLIGHT DEPARTURE ARRIVAL DURATION CABIN CLASS",
        "REFERENCE ID CT", "AVOID LONG QUEUES AT THE AIRPORT WITH DIGIYATRA",
    )
    if any(m in text_upper for m in ota_markers):
        return False

    # Own current layout: check for key layout headers and sections
    if "ITINERARY" in text_upper and ("PASSENGERS" in text_upper or "PASSENGER" in text_upper):
        if any(h in text_upper for h in ("PASSENGER SECTOR PNR SEAT MEAL", "AMOUNT PAID", "SHOW YOUR BARCODE AT THE AIRPORT")):
            return True
        if any(b in text_upper for b in OWN_BRANDS):
            return True

    # Own legacy layout
    if "BOOKING SUMMARY" in text_upper and ("FLIGHT DETAILS" in text_upper or "PASSENGER DETAILS" in text_upper):
        return True

    return False


def _to_iso(day, month_abbr, year):
    month = MONTHS.get(month_abbr.upper()[:3])
    if not month or not year:
        return ""
    try:
        y = int(year)
        if y < 100:
            y += 2000
        return f"{y:04d}-{month:02d}-{int(day):02d}"
    except (TypeError, ValueError):
        return ""


def _find_date(chunk, fallback_year=""):
    """Read the first date in `chunk`, tolerating split years, Month-first, Day-first and ISO formats."""
    if not chunk:
        return ""
    # Month First: Oct 19 2026 or Oct 19, 2026
    m_first = re.search(r"\b([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2}),?(?:\s+(\d{2,4}))?\b", chunk)
    if m_first and m_first.group(1).upper()[:3] in MONTHS:
        month = m_first.group(1)
        day = m_first.group(2)
        year = m_first.group(3)
        if not year:
            trailing = re.search(r"\b(20\d{2})\b", chunk[m_first.end():])
            year = trailing.group(1) if trailing else fallback_year
        return _to_iso(day, month, year)

    # Day First: 19 Oct 2026 or 19 Oct, 2026
    d_first = re.search(r"(?:[A-Z][a-z]{2},?\s+)?(\d{1,2})\s+([A-Z][a-z]{2})[a-z]*\.?,?(?:\s+(\d{2,4}))?", chunk)
    if d_first and d_first.group(2).upper()[:3] in MONTHS:
        day = d_first.group(1)
        month = d_first.group(2)
        year = d_first.group(3)
        if not year:
            trailing = re.search(r"\b(20\d{2})\b", chunk[d_first.end():])
            year = trailing.group(1) if trailing else fallback_year
        return _to_iso(day, month, year)

    # ISO format: 2026-10-19 or 2026/10/19
    iso_m = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", chunk)
    if iso_m:
        return f"{iso_m.group(1)}-{int(iso_m.group(2)):02d}-{int(iso_m.group(3)):02d}"

    # Day-Month-Year format: 22/09/2026 or 22-09-2026
    dmy_m = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b", chunk)
    if dmy_m:
        return f"{dmy_m.group(3)}-{int(dmy_m.group(2)):02d}-{int(dmy_m.group(1)):02d}"

    return ""


def normalize_baggage_label(raw, is_checkin=True):
    """Normalize extracted baggage text into standard dropdown values (e.g. '15 KG (1 Piece)', 'No Baggage')."""
    if not raw:
        return ""
    text = str(raw).strip()
    u = text.upper()

    no_bag_pattern = r"\b(?:not allowed|no baggage|nil|none|0\s*kgs?|not included|zero|0\s*pieces?|n/?a)\b"
    if is_checkin:
        no_bag_pattern = r"\b(?:not allowed|no baggage|nil|none|0\s*kgs?|not included|zero|0\s*pieces?|n/?a|hand-?baggage only|cabin baggage only|cabin bag only|cabin bag)\b"

    if re.search(no_bag_pattern, u, re.I):
        return "No Baggage"

    kg_m = re.search(r"\b(\d{1,2}(?:\.0)?)\s*(?:KGS?|KGKG)\b", u)
    pc_m = re.search(r"\b(\d)\s*(?:PIECES?|PCS?|PC)\b", u)

    if kg_m:
        kg = str(int(float(kg_m.group(1))))
        if kg == "0":
            return "No Baggage"
        pieces = pc_m.group(1) if pc_m else "1"
        pc_label = f"{pieces} Piece" if pieces == "1" else f"{pieces} Pieces"
        return f"{kg} KG ({pc_label})"

    return text


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
        "total_fare": "", "discount": "", "flights": flights, "passengers": [],
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
        ("discount", r"DISCOUNT\s*" + MONEY_RE),
    ):
        match = re.search(pattern, upper)
        if match:
            result[key] = _money(match.group(1), key)

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
        "total_fare": "", "discount": "", "flights": [], "passengers": [],
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
        ("discount", r"Discount\s+" + MONEY_RE),
    ):
        match = re.search(pattern, joined, re.I)
        if match:
            result[key] = _money(match.group(1), key)

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
        "total_fare": "", "discount": "", "flights": [], "passengers": [],
    }

    # ── Header: "... PNR" then "AT 8B6E58" on the line below, or "Airline PNR: MAIN01" ─────────────
    pnr_match = re.search(r"\b(?:Airline\s+)?PNR\s*:\s*([A-Z0-9]{4,10})\b", joined, re.I)
    if pnr_match:
        result["pnr"] = pnr_match.group(1).upper()
    else:
        for idx, line in enumerate(lines[:8]):
            if line.upper().rstrip().endswith("PNR") and idx + 1 < len(lines):
                tokens = re.findall(r"\b([A-Z0-9]{4,10})\b", lines[idx + 1].upper())
                if tokens:
                    result["pnr"] = tokens[-1]
                break

    booking = re.search(r"Booking\s*(?:reference|ref|id)?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-]{3,})", joined, re.I)
    if booking:
        result["booking_id"] = booking.group(1)

    issued = re.search(r"(?:Issued|Booked\s*on:?)\s*([0-9]{1,4}[\s\-/][A-Za-z0-9]{2,3}[\s\-/][0-9]{2,4})", joined, re.I)
    if issued:
        result["booking_date"] = _find_date(issued.group(1)) or issued.group(1)

    meta_m = re.search(r"Issued\s+[0-9A-Za-z, :]+?\s+IST\s*·\s*([A-Za-z\s]+?)\s*·\s*([A-Za-z\-]+)", joined, re.I)
    if meta_m:
        result["fare_type"] = meta_m.group(1).strip().upper()
        result["refund_status"] = meta_m.group(2).strip()

    plat = re.search(r"Platform\s*:\s*([A-Za-z0-9/ ]+)", joined, re.I)
    if plat:
        result["booking_platform"] = plat.group(1).split("|")[0].strip()
    if not result["booking_platform"]:
        for ag in ("JS JULFAQAR TRAVELS", "BHARAT HORIZON TRAVELS", "ANKIT TRAVELS"):
            if ag.upper() in joined.upper():
                result["booking_platform"] = ag
                break

    email = re.search(r"(?:Traveller\s*contact.*?|Email.*?)\s*([\w.%+-]+@[\w.-]+\.[A-Za-z]{2,})", joined, re.I)
    if email:
        result["customer_email"] = email.group(1)
    phone = re.search(r"(?:Traveller\s*contact|Phone|Mobile)\s*[:\-]?\s*(\+?[\d\s-]{10,})", joined, re.I)
    if phone:
        result["customer_phone"] = phone.group(1).split("|")[0].strip()
    if not result["customer_phone"]:
        ph_m = re.search(r"(\+?91[\s-]?[6-9]\d{9}|\b[6-9]\d{9}\b)", joined)
        if ph_m:
            result["customer_phone"] = ph_m.group(1).strip()
    if not result["customer_email"]:
        em_m = re.search(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", joined)
        if em_m:
            result["customer_email"] = em_m.group(1).strip()

    # Only the total is printed on the ticket now; the base/tax split is
    # not there to recover. Tickets issued before that change carried the
    # breakdown, and these patterns still read it when present.
    for key, pattern in (
        ("base_fare", r"Base Fare\s*[:\-]?\s*" + MONEY_RE),
        ("taxes_fees", r"(?:Airline Taxes(?:\s*&\s*Fees)?|Taxes\s*&\s*fees)\s*[:\-]?\s*" + MONEY_RE),
        ("total_fare", r"Total (?:Amount|PAID)\s*[:\-]?\s*" + MONEY_RE),
        ("total_fare", r"AMOUNT\s*PAID\s*[:\-]?\s*" + MONEY_RE),
        ("discount", r"Discount\s*[:\-]?\s*" + MONEY_RE),
    ):
        match = re.search(pattern, joined, re.I)
        if match:
            result[key] = _money(match.group(1), key)

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
        r"^(?:([A-Z0-9]{2})\s+(.+?)\s*[\s·•\-\u2022\ufffd\?|]+\s*([A-Z0-9]{2}\s?\d{1,4})|([A-Za-z ]+?)\s+([A-Z0-9]{2}\s*\d{1,4}))\b", re.I)
    route_re = re.compile(r"^(?:([A-Z]{3})\s+([A-Z]{3})|[A-Za-z ]*\(([A-Z]{3})\)\s+TO\s+[A-Za-z ]*\(([A-Z]{3})\))$", re.I)

    itinerary = _section(lines, "ITINERARY", "PASSENGERS", "FARE")
    for idx, line in enumerate(itinerary):
        header = header_re.match(line)
        if not header:
            continue
        fl_no = header.group(3) or header.group(5) or ""
        al_name = header.group(2) or header.group(4) or ""
        fl_match = re.match(r"^([A-Z0-9]{2})\s*(\d{1,4})$", fl_no.strip(), re.I)
        if fl_match:
            fl_no = f"{fl_match.group(1).upper()} {fl_match.group(2)}"
        else:
            fl_no = re.sub(r"\s+", " ", fl_no).upper()
        al_name = re.sub(r"^[A-Z0-9]{2}\s+", "", al_name.strip()).title()
        segment = {
            "flight_no": fl_no,
            "airline": al_name,
        }
        for candidate in itinerary[idx + 1:]:
            if header_re.match(candidate):
                break
            route = route_re.match(candidate.strip().upper())
            if route and "from_code" not in segment:
                segment["from_code"] = route.group(1) or route.group(3)
                segment["to_code"] = route.group(2) or route.group(4)
                continue
            times_12h = re.search(r"(\d{1,2}:[0-5]\d\s*[AP]M)\s*(?:-|–|to|\s+)\s*(\d{1,2}:[0-5]\d\s*[AP]M)", candidate, re.I)
            times_24h = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)\s*(?:-|–|to|\s+)\s*([01]?\d|2[0-3]):([0-5]\d)(?!\d)", candidate)
            if times_12h and "dep_time_raw" not in segment:
                m1 = re.match(r"(\d{1,2}):([0-5]\d)\s*([AP]M)", times_12h.group(1), re.I)
                m2 = re.match(r"(\d{1,2}):([0-5]\d)\s*([AP]M)", times_12h.group(2), re.I)
                if m1 and m2:
                    segment["dep_time_raw"] = _to_24h(*m1.groups())
                    segment["arr_time_raw"] = _to_24h(*m2.groups())
                    continue
            elif times_24h and "dep_time_raw" not in segment:
                h1, min1, h2, min2 = times_24h.groups()
                segment["dep_time_raw"] = f"{int(h1):02d}:{min1}"
                segment["arr_time_raw"] = f"{int(h2):02d}:{min2}"
                continue
            if "date" not in segment:
                found = _find_date(candidate)
                if found:
                    segment["date"] = found
            if "baggage" in candidate.lower():
                c_m = re.search(r"(?:Baggage\s+)?([0-9A-Za-z\s\(\)]+?)\s+check-in", candidate, re.I)
                if c_m:
                    segment["checkin_bag"] = normalize_baggage_label(c_m.group(1), is_checkin=True)
                h_m = re.search(r"(?:check-in\s*[\s·•\-\u2022\ufffd\?|]+\s*|\s+)([0-9A-Za-z\s\(\)]+?)\s+cabin", candidate, re.I)
                if not h_m:
                    h_m = re.search(r"([0-9A-Za-z\s\(\)]+?)\s+cabin", candidate, re.I)
                if h_m:
                    segment["hand_bag"] = normalize_baggage_label(h_m.group(1), is_checkin=False)
            term_m = re.search(r"(?:Terminal|T)\s*[:\-]?\s*([A-Za-z0-9]+)", candidate, re.I)
            if term_m:
                if "to_terminal" not in segment:
                    segment["to_terminal"] = f"T{term_m.group(1)}" if not term_m.group(1).upper().startswith("T") else term_m.group(1).upper()
        segment.setdefault("checkin_bag", "15 KG (1 Piece)")
        segment.setdefault("hand_bag", "7 KG (1 Piece)")
        if segment.get("from_code"):
            result["flights"].append(segment)

    # ── Passenger rows (with multiline / wrapped surname support) ─────────────
    pax_lines = _section(lines, "PASSENGERS", "FARE", "Issued", "AMOUNT")
    pax_re = re.compile(
        r"^(\d{1,2})\.\s+(?:(Mr|Mrs|Ms|Mstr|Dr|Miss)\.?\s+)?"
        r"([A-Za-z][A-Za-z .'\-]{2,40}?)\s*(?=[A-Z]{3}-[A-Z]{3}\b|\d{3}-\d|\||\s*$)", re.I)
    i = 0
    while i < len(pax_lines):
        line = pax_lines[i]
        match = pax_re.match(line)
        if match:
            p_no = match.group(1)
            title = (match.group(2) or "").title()
            name = re.sub(r"\s{2,}", " ", match.group(3)).strip().title()
            ticket = re.search(r"\b(\d{3}-\d{6,}|\d{13})\b", line)
            # Look ahead for wrapped surname on continuation lines
            j = i + 1
            while j < len(pax_lines) and not pax_re.match(pax_lines[j]) and not re.search(r"^(?:INR|AMOUNT|Total|Issued)", pax_lines[j], re.I):
                nxt = pax_lines[j]
                sec_m = re.search(r"\b[A-Z]{3}-[A-Z]{3}\b", nxt)
                if sec_m:
                    before_sec = nxt[:sec_m.start()].strip()
                    if before_sec and not re.search(r"\d", before_sec) and len(before_sec) >= 2:
                        name = f"{name} {before_sec.title()}"
                if not ticket:
                    t_m = re.search(r"\b(\d{3}-\d{6,}|\d{13})\b", nxt)
                    if t_m:
                        ticket = t_m
                j += 1
            result["passengers"].append({
                "name": name,
                "title": title,
                "ticket_no": ticket.group(1) if ticket else "",
            })
        i += 1

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
    result = {
        "booking_platform": "Direct/Walk-in",
        "pnr": "", "booking_id": "", "booking_date": "",
        "base_fare": "", "taxes_fees": "", "total_fare": "", "discount": "",
        "fare_type": "REGULAR", "refund_status": "Refundable",
        "customer_email": "", "customer_phone": "",
        "flights": [], "passengers": [],
    }
    all_words = [w for page in pages_words for w in page]
    if not all_words:
        return {}
    flat = " ".join(w["text"] for w in all_words)
    flat = re.sub(r'[\uE000-\uF8FF]', ' ', flat)

    lines = _group_word_lines(pages_words[0])
    header_idx, bounds = _find_table(lines, COLUMNAR_FLIGHT_HEADERS)
    if header_idx is None:
        return {}

    flight_anchors = []
    for top, line_words in lines[header_idx + 1:]:
        line_str = " ".join(w["text"] for w in line_words).upper()
        if any(m in line_str for m in ("PASSENGER DETAILS", "OTHER DETAILS", "FARE DETAILS")):
            break
        col0 = " ".join(w["text"] for w in line_words if _column_of(w, bounds) == 0)
        m = re.search(r"\b([A-Z0-9]{2,3})\s*-?\s*(\d{2,4})\b", col0)
        if m and "LAYOVER" not in col0.upper():
            flight_anchors.append((top, f"{m.group(1)} {m.group(2)}"))

    segments = []
    if len(flight_anchors) > 1:
        for i, (top, fno) in enumerate(flight_anchors):
            min_top = top - 15
            if i > 0:
                min_top = max(min_top, flight_anchors[i - 1][0] + 30)
            max_top = flight_anchors[i + 1][0] - 10 if i + 1 < len(flight_anchors) else 240

            seg_words = [w for w in pages_words[0] if min_top <= w["top"] < max_top]
            col_flight = " ".join(w["text"] for w in seg_words if _column_of(w, bounds) == 0)
            col_dep = " ".join(w["text"] for w in seg_words if _column_of(w, bounds) == 1)
            col_dur = " ".join(w["text"] for w in seg_words if _column_of(w, bounds) == 2 and w["x0"] < 340)
            col_arr = " ".join(w["text"] for w in seg_words if w["x0"] >= 340)

            col_flight = re.sub(r'[\uE000-\uF8FF]', ' ', col_flight)
            col_dep = re.sub(r'[\uE000-\uF8FF]', ' ', col_dep)
            col_dur = re.sub(r'[\uE000-\uF8FF]', ' ', col_dur)
            col_arr = re.sub(r'[\uE000-\uF8FF]', ' ', col_arr)

            carrier_code = fno.split()[0]
            airline = STANDARD_AIRLINES.get(carrier_code, "")
            if not airline:
                al_m = re.search(r"\b(Air India Express|Air India|IndiGo|SpiceJet|Akasa Air|Vistara)\b", col_flight, re.I)
                airline = al_m.group(1) if al_m else "Air India Express"

            dep_endpoint = _endpoint(col_dep)
            arr_endpoint = _endpoint(col_arr)

            seg = {
                "flight_no": fno,
                "airline": airline,
                "from_code": dep_endpoint.get("code", ""),
                "to_code": arr_endpoint.get("code", ""),
                "dep_time_raw": dep_endpoint.get("time", ""),
                "arr_time_raw": arr_endpoint.get("time", ""),
                "date": dep_endpoint.get("date", ""),
            }
            if arr_endpoint.get("terminal"):
                t = arr_endpoint['terminal']
                seg["to_terminal"] = f"Terminal {t}" if not t.upper().startswith("T") else t
            lay_m = re.search(r"Layover\s*(\d{1,2}h:\d{2}m)", col_flight, re.I)
            if lay_m:
                seg["manual_layover"] = lay_m.group(1)
            segments.append(seg)
        result["flights"] = segments
    else:
        flight_col, dep_col, dur_col, arr_col = _cell_block(
            lines, header_idx + 1, bounds, ("PASSENGER DETAILS", "OTHER DETAILS"))
        flight_col = re.sub(r'[\uE000-\uF8FF]', ' ', flight_col)
        dep_col = re.sub(r'[\uE000-\uF8FF]', ' ', dep_col)
        arr_col = re.sub(r'[\uE000-\uF8FF]', ' ', arr_col)
        segment = {}
        number = re.match(r"([A-Z0-9]{2,3})\s*-?\s*(\d{2,4})\b\s*(.*)", flight_col.upper())
        if number:
            segment["flight_no"] = f"{number.group(1)} {number.group(2)}"
            airline = flight_col[number.end(2):].strip(" -")
            if airline:
                segment["airline"] = airline.title()
        departure, arrival = _endpoint(dep_col), _endpoint(arr_col)
        route = re.search(r"\(([A-Z]{3})\)\s*-\s*[A-Za-z .]+\(([A-Z]{3})\)", flat)
        segment["from_code"] = (route.group(1) if route else departure.get("code", ""))
        segment["to_code"] = (route.group(2) if route else arrival.get("code", ""))
        if departure.get("date"): segment["date"] = departure["date"]
        if departure.get("time"): segment["dep_time_raw"] = departure["time"]
        if arrival.get("time"): segment["arr_time_raw"] = arrival["time"]
        if departure.get("terminal"): segment["from_terminal"] = departure["terminal"]
        if arrival.get("terminal"): segment["to_terminal"] = arrival["terminal"]
        if segment.get("flight_no") and segment.get("from_code") and segment.get("to_code"):
            result["flights"] = [segment]

    checkin_bag = ""
    hand_bag = ""

    # Pattern A: Table row where Checkin Baggage and Cabin Baggage headers are followed by weights
    tbl_m = re.search(
        r"Check[- ]?in\s*Baggage\s*(?:Cabin|Hand)\s*Baggage[^\n\d]*\n?\s*(\d{1,2}\s*KG|\b\d{1,2}\s*kgs?\b|\bNot Allowed\b|\bNil\b|\bNo Baggage\b)[^\n\d]*(\d{1,2}\s*KG|\b\d{1,2}\s*kgs?\b)",
        flat, re.I
    )
    if tbl_m:
        checkin_bag = tbl_m.group(1).strip()
        hand_bag = tbl_m.group(2).strip()

    if not checkin_bag:
        c_m = re.search(r"Check[- ]?in(?:\s*Baggage)?\s*[:\s]?\s*(\d{1,2}\s*kgs?(?:\s*\([^\)]+\))?|\d{1,2}\s*KG|Not Allowed|No Baggage|Nil|None)", flat, re.I)
        if c_m:
            checkin_bag = c_m.group(1).strip()
    if not checkin_bag:
        c_rev = re.search(r"(\d{1,2}\s*KG)\s*(?:CHECK[- ]?IN)", flat, re.I)
        if c_rev:
            checkin_bag = c_rev.group(1).strip()

    if not hand_bag:
        h_m = re.search(r"(?:Cabin|Hand)(?:\s*Baggage)?\s*[:\s]?\s*(\d{1,2}\s*kgs?(?:\s*\([^\)]+\))?|\d{1,2}\s*KG|Not Allowed|No Baggage|Nil|None)", flat, re.I)
        if h_m:
            hand_bag = h_m.group(1).strip()
    if not hand_bag:
        h_rev = re.search(r"(\d{1,2}\s*KG)\s*(?:HAND|CABIN)", flat, re.I)
        if h_rev:
            hand_bag = h_rev.group(1).strip()

    checkin_norm = normalize_baggage_label(checkin_bag, is_checkin=True)
    hand_norm = normalize_baggage_label(hand_bag, is_checkin=False)

    for seg in result["flights"]:
        if hand_norm:
            seg["hand_bag"] = hand_norm
        if checkin_norm:
            seg["checkin_bag"] = checkin_norm

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
        tval = total.group(1).replace(",", "")
        result["total_fare"] = tval
        result["base_fare"] = tval
        result["taxes_fees"] = "0"
    if re.search(r"\bnon[- ]?refundable\b", flat, re.I):
        result["refund_status"] = "Non-Refundable"
    elif re.search(r"\brefundable\b", flat, re.I):
        result["refund_status"] = "Refundable"

    em = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", flat)
    if em:
        result["customer_email"] = em.group(1)
    ph = re.search(r"(?:Support|Help|Toll Free|Contact)[:\s]*(\+?[\d\s\-]{10,})", flat, re.I)
    if ph:
        result["customer_phone"] = ph.group(1).strip()

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


# The one airline table. app.py re-exports this as AIRLINES: a second copy
# there drifted out of step with this one and left the parsers unable to name
# carriers the rest of the app already knew about.
STANDARD_AIRLINES = {
    # India
    "6E": "IndiGo", "AI": "Air India", "UK": "Vistara", "SG": "SpiceJet",
    "G8": "Go First", "QP": "Akasa Air", "I5": "AirAsia India",
    "IX": "Air India Express", "S5": "Star Air", "2T": "TruJet",
    "9I": "Alliance Air", "8Y": "Pan Pacific Airlines", "E5": "Air Arabia India",
    # Middle East
    "EK": "Emirates", "EY": "Etihad Airways", "QR": "Qatar Airways",
    "FZ": "flydubai", "WY": "Oman Air", "GF": "Gulf Air", "SV": "Saudia",
    "XY": "flynas", "G9": "Air Arabia", "KU": "Kuwait Airways",
    "RJ": "Royal Jordanian", "ME": "Middle East Airlines",
    "F3": "flyadeal", "J9": "Jazeera Airways", "OV": "SalamAir",
    "3L": "Air Arabia Abu Dhabi", "IY": "Yemenia", "NE": "Nesma Airlines",
    "LY": "El Al", "W5": "Mahan Air", "IR": "Iran Air",
    "EP": "Iran Aseman Airlines",
    # Asia-Pacific
    "SQ": "Singapore Airlines", "TG": "Thai Airways",
    "MH": "Malaysia Airlines", "GA": "Garuda Indonesia",
    "CX": "Cathay Pacific", "PR": "Philippine Airlines",
    "VN": "Vietnam Airlines", "KE": "Korean Air", "OZ": "Asiana Airlines",
    "NH": "ANA (All Nippon Airways)", "JL": "Japan Airlines",
    "CI": "China Airlines", "BR": "EVA Air",
    "CZ": "China Southern Airlines", "MU": "China Eastern Airlines",
    "CA": "Air China", "HU": "Hainan Airlines",
    "3K": "Jetstar Asia", "TR": "Scoot", "AK": "AirAsia",
    "FD": "Thai AirAsia", "QZ": "Indonesia AirAsia",
    "VJ": "VietJet Air", "QH": "Bamboo Airways", "PG": "Bangkok Airways",
    "SL": "Thai Lion Air", "DD": "Nok Air", "VZ": "Thai Vietjet Air",
    "JT": "Lion Air", "ID": "Batik Air Indonesia", "OD": "Batik Air Malaysia",
    "FY": "Firefly", "BI": "Royal Brunei Airlines", "5J": "Cebu Pacific",
    "Z2": "Philippines AirAsia", "MM": "Peach Aviation", "7C": "Jeju Air",
    "LJ": "Jin Air", "TW": "T'way Air", "BX": "Air Busan",
    "9C": "Spring Airlines", "HO": "Juneyao Air", "MF": "XiamenAir",
    "SC": "Shandong Airlines", "ZH": "Shenzhen Airlines", "3U": "Sichuan Airlines",
    "HX": "Hong Kong Airlines", "UO": "HK Express", "GK": "Jetstar Japan",
    "8M": "Myanmar Airways International", "UB": "Myanmar National Airlines",
    "QV": "Lao Airlines", "K6": "Cambodia Angkor Air",
    # South Asia
    "UL": "SriLankan Airlines", "RA": "Nepal Airlines",
    "BG": "Biman Bangladesh Airlines", "PK": "Pakistan International Airlines",
    "BS": "US-Bangla Airlines", "KB": "Drukair", "B3": "Bhutan Airlines",
    "U4": "Buddha Air", "YT": "Yeti Airlines", "H9": "Himalaya Airlines",
    "Q2": "Maldivian", "9P": "Fly Jinnah", "PA": "Airblue",
    # Europe
    "BA": "British Airways", "LH": "Lufthansa", "AF": "Air France",
    "KL": "KLM Royal Dutch Airlines", "LX": "SWISS",
    "OS": "Austrian Airlines", "AZ": "ITA Airways", "IB": "Iberia",
    "SK": "SAS Scandinavian Airlines", "AY": "Finnair",
    "TK": "Turkish Airlines", "LO": "LOT Polish Airlines",
    "TP": "TAP Air Portugal", "EI": "Aer Lingus",
    "SN": "Brussels Airlines", "RO": "TAROM", "JU": "Air Serbia",
    "OU": "Croatia Airlines", "FR": "Ryanair", "U2": "easyJet",
    "W6": "Wizz Air", "VY": "Vueling", "DY": "Norwegian Air",
    "VS": "Virgin Atlantic", "PC": "Pegasus Airlines", "XQ": "SunExpress",
    "A3": "Aegean Airlines", "EW": "Eurowings", "HV": "Transavia",
    "BT": "airBaltic", "FI": "Icelandair", "OK": "Czech Airlines",
    "LG": "Luxair", "DE": "Condor", "X3": "TUI fly", "LS": "Jet2",
    "EN": "Air Dolomiti", "WF": "Widerøe",
    "SU": "Aeroflot", "S7": "S7 Airlines", "U6": "Ural Airlines",
    "UT": "UTair", "KC": "Air Astana", "HY": "Uzbekistan Airways",
    "J2": "Azerbaijan Airlines", "PS": "Ukraine International Airlines",
    "B2": "Belavia", "A9": "Georgian Airways",
    # Americas
    "AA": "American Airlines", "UA": "United Airlines",
    "DL": "Delta Air Lines", "WN": "Southwest Airlines",
    "B6": "JetBlue Airways", "AS": "Alaska Airlines",
    "NK": "Spirit Airlines", "F9": "Frontier Airlines",
    "HA": "Hawaiian Airlines", "AC": "Air Canada",
    "AM": "Aeromexico", "LA": "LATAM Airlines", "AV": "Avianca",
    "CM": "Copa Airlines", "G3": "Gol Linhas Aéreas",
    "WS": "WestJet", "PD": "Porter Airlines", "TS": "Air Transat",
    "SY": "Sun Country Airlines", "Y4": "Volaris", "VB": "Viva Aerobus",
    "AR": "Aerolíneas Argentinas", "AD": "Azul Brazilian Airlines",
    # Oceania & Africa
    "QF": "Qantas", "NZ": "Air New Zealand", "VA": "Virgin Australia",
    "JQ": "Jetstar", "ET": "Ethiopian Airlines",
    "SA": "South African Airways", "MS": "EgyptAir",
    "KQ": "Kenya Airways", "AT": "Royal Air Maroc", "WB": "RwandAir",
    "FJ": "Fiji Airways", "PX": "Air Niugini",
    "TU": "Tunisair", "AH": "Air Algérie", "MK": "Air Mauritius",
    "HM": "Air Seychelles", "TC": "Air Tanzania", "DT": "TAAG Angola Airlines",
}


# Signature brand colors for all 190 standard airlines.
# Used for crisp, vector-rendered airline badges on tickets without needing external image assets.
STANDARD_AIRLINE_COLORS = {
    # India
    "6E": "#002B66",  # IndiGo
    "AI": "#ED1B24",  # Air India
    "UK": "#581138",  # Vistara
    "SG": "#E31837",  # SpiceJet
    "G8": "#003366",  # Go First
    "QP": "#FF6600",  # Akasa Air
    "I5": "#ED1C24",  # AirAsia India
    "IX": "#D2232A",  # Air India Express
    "S5": "#0A2540",  # Star Air
    "2T": "#1E3D59",  # TruJet
    "9I": "#003399",  # Alliance Air
    "8Y": "#0055A5",  # Pan Pacific Airlines
    "E5": "#ED1C24",  # Air Arabia India
    # Middle East
    "EK": "#D71A21",  # Emirates
    "EY": "#A07E44",  # Etihad Airways
    "QR": "#5C0632",  # Qatar Airways
    "FZ": "#F26522",  # flydubai
    "WY": "#A71930",  # Oman Air
    "GF": "#8C6D37",  # Gulf Air
    "SV": "#006837",  # Saudia
    "XY": "#602D89",  # flynas
    "G9": "#E21836",  # Air Arabia
    "KU": "#003366",  # Kuwait Airways
    "RJ": "#7B1113",  # Royal Jordanian
    "ME": "#003876",  # Middle East Airlines
    "F3": "#00A4A6",  # flyadeal
    "J9": "#005BAA",  # Jazeera Airways
    "OV": "#6D1D43",  # SalamAir
    "3L": "#E21836",  # Air Arabia Abu Dhabi
    "IY": "#1B365D",  # Yemenia
    "NE": "#004B87",  # Nesma Airlines
    "LY": "#00205B",  # El Al
    "W5": "#1D3A6C",  # Mahan Air
    "IR": "#1C3F77",  # Iran Air
    "EP": "#003B70",  # Iran Aseman Airlines
    # Asia-Pacific
    "SQ": "#00205B",  # Singapore Airlines
    "TG": "#4B1869",  # Thai Airways
    "MH": "#003399",  # Malaysia Airlines
    "GA": "#00728F",  # Garuda Indonesia
    "CX": "#006564",  # Cathay Pacific
    "PR": "#002B66",  # Philippine Airlines
    "VN": "#005B94",  # Vietnam Airlines
    "KE": "#005599",  # Korean Air
    "OZ": "#8C1D40",  # Asiana Airlines
    "NH": "#003399",  # ANA (All Nippon Airways)
    "JL": "#CC0000",  # Japan Airlines
    "CI": "#C8102E",  # China Airlines
    "BR": "#006838",  # EVA Air
    "CZ": "#0065B3",  # China Southern Airlines
    "MU": "#00205B",  # China Eastern Airlines
    "CA": "#CC0000",  # Air China
    "HU": "#C8102E",  # Hainan Airlines
    "3K": "#FF5F00",  # Jetstar Asia
    "TR": "#FFE600",  # Scoot
    "AK": "#ED1C24",  # AirAsia
    "FD": "#ED1C24",  # Thai AirAsia
    "QZ": "#ED1C24",  # Indonesia AirAsia
    "VJ": "#DA251D",  # VietJet Air
    "QH": "#005C29",  # Bamboo Airways
    "PG": "#004B87",  # Bangkok Airways
    "SL": "#ED1C24",  # Thai Lion Air
    "DD": "#FFDE00",  # Nok Air
    "VZ": "#DA251D",  # Thai Vietjet Air
    "JT": "#ED1C24",  # Lion Air
    "ID": "#800000",  # Batik Air Indonesia
    "OD": "#800000",  # Batik Air Malaysia
    "FY": "#E26D00",  # Firefly
    "BI": "#FBB800",  # Royal Brunei Airlines
    "5J": "#FFCC00",  # Cebu Pacific
    "Z2": "#ED1C24",  # Philippines AirAsia
    "MM": "#EB4B94",  # Peach Aviation
    "7C": "#FF5000",  # Jeju Air
    "LJ": "#86C824",  # Jin Air
    "TW": "#D60815",  # T'way Air
    "BX": "#00256C",  # Air Busan
    "9C": "#007E3A",  # Spring Airlines
    "HO": "#8A1538",  # Juneyao Air
    "MF": "#004A99",  # XiamenAir
    "SC": "#C41230",  # Shandong Airlines
    "ZH": "#CC0000",  # Shenzhen Airlines
    "3U": "#D2232A",  # Sichuan Airlines
    "HX": "#C41230",  # Hong Kong Airlines
    "UO": "#45186E",  # HK Express
    "GK": "#FF5F00",  # Jetstar Japan
    "8M": "#B82025",  # Myanmar Airways International
    "UB": "#D49B00",  # Myanmar National Airlines
    "QV": "#003399",  # Lao Airlines
    "K6": "#004B87",  # Cambodia Angkor Air
    # South Asia
    "UL": "#00703C",  # SriLankan Airlines
    "RA": "#DC143C",  # Nepal Airlines
    "BG": "#006A4E",  # Biman Bangladesh Airlines
    "PK": "#004D25",  # Pakistan International Airlines
    "BS": "#E31E24",  # US-Bangla Airlines
    "KB": "#E87722",  # Drukair
    "B3": "#0057B7",  # Bhutan Airlines
    "U4": "#006633",  # Buddha Air
    "YT": "#005A36",  # Yeti Airlines
    "H9": "#003366",  # Himalaya Airlines
    "Q2": "#006699",  # Maldivian
    "9P": "#006B3F",  # Fly Jinnah
    "PA": "#00447C",  # Airblue
    # Europe
    "BA": "#075AAA",  # British Airways
    "LH": "#051039",  # Lufthansa
    "AF": "#002157",  # Air France
    "KL": "#00A1DE",  # KLM Royal Dutch Airlines
    "LX": "#D81E05",  # SWISS
    "OS": "#D81E05",  # Austrian Airlines
    "AZ": "#003399",  # ITA Airways
    "IB": "#D71920",  # Iberia
    "SK": "#000066",  # SAS Scandinavian Airlines
    "AY": "#0B1560",  # Finnair
    "TK": "#C8102E",  # Turkish Airlines
    "LO": "#002B66",  # LOT Polish Airlines
    "TP": "#009639",  # TAP Air Portugal
    "EI": "#006272",  # Aer Lingus
    "SN": "#1E234C",  # Brussels Airlines
    "RO": "#002B66",  # TAROM
    "JU": "#0C2340",  # Air Serbia
    "OU": "#003366",  # Croatia Airlines
    "FR": "#003580",  # Ryanair
    "U2": "#FF6600",  # easyJet
    "W6": "#C6007E",  # Wizz Air
    "VY": "#F6B400",  # Vueling
    "DY": "#D81921",  # Norwegian Air
    "VS": "#D81921",  # Virgin Atlantic
    "PC": "#E30613",  # Pegasus Airlines
    "XQ": "#FA5000",  # SunExpress
    "A3": "#00205B",  # Aegean Airlines
    "EW": "#762243",  # Eurowings
    "HV": "#00D66C",  # Transavia
    "BT": "#97D700",  # airBaltic
    "FI": "#00205B",  # Icelandair
    "OK": "#C8102E",  # Czech Airlines
    "LG": "#00A3E0",  # Luxair
    "DE": "#FFC72C",  # Condor
    "X3": "#ED1C24",  # TUI fly
    "LS": "#ED1C24",  # Jet2
    "EN": "#003366",  # Air Dolomiti
    "WF": "#005A36",  # Widerøe
    "SU": "#002F6C",  # Aeroflot
    "S7": "#A3C824",  # S7 Airlines
    "U6": "#004B87",  # Ural Airlines
    "UT": "#003366",  # UTair
    "KC": "#C89A3C",  # Air Astana
    "HY": "#003366",  # Uzbekistan Airways
    "J2": "#004B87",  # Azerbaijan Airlines
    "PS": "#005BAA",  # Ukraine International Airlines
    "B2": "#0054A6",  # Belavia
    "A9": "#8B0000",  # Georgian Airways
    # Americas
    "AA": "#0078D2",  # American Airlines
    "UA": "#005DAA",  # United Airlines
    "DL": "#E01933",  # Delta Air Lines
    "WN": "#304CB2",  # Southwest Airlines
    "B6": "#003876",  # JetBlue Airways
    "AS": "#01426A",  # Alaska Airlines
    "NK": "#FFE600",  # Spirit Airlines
    "F9": "#006643",  # Frontier Airlines
    "HA": "#4F2683",  # Hawaiian Airlines
    "AC": "#D8232A",  # Air Canada
    "AM": "#0B2341",  # Aeromexico
    "LA": "#1B0088",  # LATAM Airlines
    "AV": "#D8232A",  # Avianca
    "CM": "#003366",  # Copa Airlines
    "G3": "#FF671B",  # Gol Linhas Aéreas
    "WS": "#009999",  # WestJet
    "PD": "#1E2D3D",  # Porter Airlines
    "TS": "#0085CA",  # Air Transat
    "SY": "#FF6600",  # Sun Country Airlines
    "Y4": "#A30076",  # Volaris
    "VB": "#008F4C",  # Viva Aerobus
    "AR": "#0072CE",  # Aerolíneas Argentinas
    "AD": "#003366",  # Azul Brazilian Airlines
    # Oceania & Africa
    "QF": "#E0001B",  # Qantas
    "NZ": "#000000",  # Air New Zealand
    "VA": "#D71920",  # Virgin Australia
    "JQ": "#FF5F00",  # Jetstar
    "ET": "#008542",  # Ethiopian Airlines
    "SA": "#002B49",  # South African Airways
    "MS": "#002B66",  # EgyptAir
    "KQ": "#CC0000",  # Kenya Airways
    "AT": "#C8102E",  # Royal Air Maroc
    "WB": "#0066B2",  # RwandAir
    "FJ": "#003366",  # Fiji Airways
    "PX": "#CC0000",  # Air Niugini
    "TU": "#C8102E",  # Tunisair
    "AH": "#C8102E",  # Air Algérie
    "MK": "#C8102E",  # Air Mauritius
    "HM": "#E31B23",  # Air Seychelles
    "TC": "#008080",  # Air Tanzania
    "DT": "#C8102E",  # TAAG Angola Airlines
}


def _resolve_airline(code_or_flight):
    m = re.match(r"^([A-Z0-9]{2,3})", (code_or_flight or "").strip().upper())
    if m:
        return STANDARD_AIRLINES.get(m.group(1), "")
    return ""


def parse_makemytrip_ticket(text):
    """Parse MakeMyTrip and Goibibo tickets with high fidelity."""
    text = normalize_text(text)
    text_u = text.upper()
    if "BOOKING DETAILS" not in text_u and "TRAVELLER SEAT MEAL" not in text_u:
        return None
    if not any(k in text_u for k in [
        "MAKEMYTRIP", "MAKEMY TRIP", "GOIBIBO", "BARCODE(S) FOR YOUR JOURNEY",
        "TRAVELLER SEAT MEAL BAGGAGE", "BHARAT HORIZON TRAVELS", "GOFLIS", "GOUPI", "MMT"
    ]):
        return None

    res = {
        "booking_platform": "MakeMyTrip",
        "pnr": "", "booking_id": "", "booking_date": "",
        "base_fare": "", "taxes_fees": "", "total_fare": "", "discount": "",
        "flights": [], "passengers": [],
    }
    if any(k in text_u for k in ["GOIBIBO", "GOFLIS", "GOUPI"]):
        res["booking_platform"] = "Goibibo"

    bid = re.search(r"Booking ID\s*:\s*([A-Z0-9]+)", text, re.I)
    if bid:
        res["booking_id"] = bid.group(1)

    bdate = re.search(r"\(Booked on\s+([^\)]+)\)", text, re.I)
    if bdate:
        res["booking_date"] = _find_date(bdate.group(1)) or ""

    pnr_match = re.search(r"\bPNR\s*:\s*([A-Z0-9]{5,8})\b", text, re.I)
    if pnr_match:
        res["pnr"] = pnr_match.group(1)

    # 1. Passengers from Barcode(s) section (Most accurate, clean un-wrapped names)
    b_match = re.search(r"Barcode\(s\)[^\n]*\n([\s\S]*?)(?:Manage Booking|BOOKING DETAILS)", text, re.I)
    if b_match:
        for line in b_match.group(1).splitlines():
            line = line.strip()
            if not line or any(k in line.upper() for k in ["MANAGE", "BOOKING", "BARCODE", "DETAILS"]):
                continue
            m = re.match(r"^(Mr|Mrs|Ms|Mstr|Miss)\.?\s+(.+)$", line, re.I)
            if m:
                res["passengers"].append({"name": m.group(2).strip().title(), "title": m.group(1).capitalize()})
            elif len(line.split()) >= 2:
                res["passengers"].append({"name": line.title()})

    # 2. Fallback to TRAVELLER table
    if not res["passengers"]:
        t_match = re.search(r"TRAVELLER\s+SEAT[^\n]*\n([\s\S]*?)(?:PAYMENT|IMPORTANT|DIGI YATRA|myCash|Benefits)", text, re.I)
        if t_match:
            chunk = t_match.group(1)
            for m in re.finditer(r"(Mr|Mrs|Ms|Mstr|Miss)\.?\s+([A-Za-z\s]+?)\s*(?:\(ADULT\)|\(CHILD\)|\(INFANT\)|-\s*-|\d{1,2}[A-Z])", chunk, re.I):
                raw_name = re.sub(r"\s+", " ", m.group(2)).strip().title()
                words = [w for w in raw_name.split() if w.upper() not in ("ADULT", "CHILD", "INFANT")]
                clean_name = " ".join(words)
                if clean_name and clean_name not in [p["name"] for p in res["passengers"]]:
                    res["passengers"].append({"name": clean_name, "title": m.group(1).capitalize()})

    # The traveller type sits beside the name in the per-leg table, which
    # wraps mid-name, so it is matched off the first name token instead.
    for pax in res["passengers"]:
        first = (pax.get("name") or "").split()
        if not first:
            continue
        type_m = re.search(re.escape(first[0].upper()) + r"[\s\S]{0,80}?\((ADULT|CHILD|INFANT)\)", text_u)
        if type_m:
            pax["type"] = type_m.group(1).capitalize()

    # Flight parsing: pattern 1 (Standard MMT):
    # [Airline] [FromCity] [ToCity] \n [FlightNo] [FromCode] [DepTime] hrs ... [ArrTime] hrs [ToCode]
    fl1 = list(re.finditer(
        r"([A-Za-z\s]+?)\n([A-Z0-9]{2,3}\s*\d{2,4})\s+([A-Z]{3})\s+([0-2]\d:[0-5]\d)(?:\s*hrs)?\s+(?:[0-9\s\w]+duration|[0-9\s\w]+m)?\s*([0-2]\d:[0-5]\d)(?:\s*hrs)?\s+([A-Z]{3})",
        text
    ))
    for m in fl1:
        fno = m.group(2).strip()
        fc, dep, arr, tc = m.group(3), m.group(4), m.group(5), m.group(6)
        airline = _resolve_airline(fno) or m.group(1).strip().splitlines()[-1]
        seg = {
            "airline": airline,
            "flight_no": fno,
            "from_code": fc,
            "to_code": tc,
            "dep_time_raw": dep,
            "arr_time_raw": arr,
        }
        ctx = text[max(0, m.start() - 200):min(len(text), m.end() + 400)]
        d_match = re.search(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", ctx)
        if d_match:
            seg["date"] = _find_date(d_match.group(1)) or ""
        else:
            d_m2 = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})\b", ctx, re.I)
            if d_m2 and res.get("booking_date"):
                seg["date"] = _to_iso(d_m2.group(2), d_m2.group(1), res["booking_date"][:4])
        bag_match = re.search(r"Check-in\s*Baggage:\s*([^\n]+?)(?=\s*Cabin\s*Baggage|\n|$)", ctx, re.I)
        if bag_match:
            bval = bag_match.group(1).strip()
            if "(" in bval and ")" not in bval: bval += ")"
            seg["checkin_bag"] = normalize_baggage_label(bval, is_checkin=True)
        cbag_match = re.search(r"Cabin\s*Baggage:\s*([^\n]+?)(?=\s*Check-in\s*Baggage|\n|$)", ctx, re.I)
        if cbag_match:
            cval = cbag_match.group(1).strip()
            if "(" in cval and ")" not in cval: cval += ")"
            seg["hand_bag"] = normalize_baggage_label(cval, is_checkin=False)
        res["flights"].append(seg)

    # Flight parsing: pattern 2 (Air India Express / multi-line airline):
    # Air India [From] [To] \n Express [FromCode] [DepTime] hrs ... [ArrTime] hrs [ToCode] \n [FlightNo]
    if not res["flights"]:
        fl2 = re.finditer(
            r"([A-Za-z\s]+)\n(?:Express\s+)?([A-Z]{3})\s+([0-2]\d:[0-5]\d)(?:\s*hrs)?\s+.*?([0-2]\d:[0-5]\d)(?:\s*hrs)?\s+([A-Z]{3})\s*\n([A-Z0-9]{2,3}\s*\d{2,4})",
            text
        )
        for m in fl2:
            fno = m.group(6).strip()
            fc, dep, arr, tc = m.group(2), m.group(3), m.group(4), m.group(5)
            airline = _resolve_airline(fno)
            if not airline:
                airline = m.group(1).strip().splitlines()[-1]
                if "Express" in m.group(0): airline += " Express"
            seg = {
                "airline": airline,
                "from_code": fc,
                "to_code": tc,
                "dep_time_raw": dep,
                "arr_time_raw": arr,
                "flight_no": fno,
            }
            ctx = text[max(0, m.start() - 200):min(len(text), m.end() + 400)]
            d_match = re.search(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", ctx)
            if d_match:
                seg["date"] = _find_date(d_match.group(1)) or ""
            else:
                d_m2 = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})\b", ctx, re.I)
                if d_m2 and res.get("booking_date"):
                    seg["date"] = _to_iso(d_m2.group(2), d_m2.group(1), res["booking_date"][:4])
            bag_match = re.search(r"Check-in\s*Baggage:\s*([^\n]+?)(?=\s*Cabin\s*Baggage|\n|$)", ctx, re.I)
            if bag_match:
                bval = bag_match.group(1).strip()
                if "(" in bval and ")" not in bval: bval += ")"
                seg["checkin_bag"] = normalize_baggage_label(bval, is_checkin=True)
            cbag_match = re.search(r"Cabin\s*Baggage:\s*([^\n]+?)(?=\s*Check-in\s*Baggage|\n|$)", ctx, re.I)
            if cbag_match:
                cval = cbag_match.group(1).strip()
                if "(" in cval and ")" not in cval: cval += ")"
                seg["hand_bag"] = normalize_baggage_label(cval, is_checkin=False)
            res["flights"].append(seg)

    # Flight parsing: pattern 3 (per-leg blocks, endpoint read by position).
    # On a connecting itinerary the text layer can emit a leg's departure
    # after its arrival, so the single-line patterns above only catch the
    # legs that happen to come out in order. Every leg carries its own
    # traveller table, so the legs are read as blocks and an airport code is
    # the departure when it sits before the time ("BOM 05:40 hrs") and the
    # arrival when it sits after it ("07:35 hrs SHJ").
    leg_tables = list(re.finditer(r"TRAVELLER\s+SEAT", text, re.I))
    if len(res["flights"]) < len(leg_tables):
        legs, start = [], 0
        for table in leg_tables:
            block, start = text[start:table.start()], table.end()
            dep = re.search(r"\b([A-Z]{3})\s+([0-2]\d:[0-5]\d)\s*hrs", block)
            arr = re.search(r"([0-2]\d:[0-5]\d)\s*hrs\s+([A-Z]{3})\b", block)
            if not dep or not arr:
                continue
            fno_m = re.search(r"^([A-Z0-9]{2,3})\s*(\d{2,4})$", block, re.M)
            fno = f"{fno_m.group(1)} {fno_m.group(2)}" if fno_m else ""
            seg = {
                "airline": (_resolve_airline(fno) if fno else "") or "",
                "flight_no": fno,
                "from_code": dep.group(1),
                "to_code": arr.group(2),
                "dep_time_raw": dep.group(2),
                "arr_time_raw": arr.group(1),
            }
            d_m = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})\b", block, re.I)
            if d_m and res.get("booking_date"):
                seg["date"] = _to_iso(d_m.group(2), d_m.group(1), res["booking_date"][:4])
            cls = re.search(r"\b(Premium Economy|Economy|Business|First)\b", block, re.I)
            if cls:
                seg["class"] = cls.group(1).title()
            bag_match = re.search(r"Check-in\s*Baggage:\s*([^\n]+?)(?=\s*Cabin\s*Baggage|\n|$)", block, re.I)
            if bag_match:
                bval = bag_match.group(1).strip()
                if "(" in bval and ")" not in bval: bval += ")"
                seg["checkin_bag"] = normalize_baggage_label(bval, is_checkin=True)
            cbag_match = re.search(r"Cabin\s*Baggage:\s*([^\n]+?)(?=\s*Check-in\s*Baggage|\n|$)", block, re.I)
            if cbag_match:
                cval = cbag_match.group(1).strip()
                if "(" in cval and ")" not in cval: cval += ")"
                seg["hand_bag"] = normalize_baggage_label(cval, is_checkin=False)
            legs.append(seg)
        if len(legs) > len(res["flights"]):
            res["flights"] = legs

    tot = re.search(r"Total Amount\s*(?:INR|Rs\.?|₹)?\s*([0-9,]+(?:\.\d{1,2})?)", text)
    if tot: res["total_fare"] = tot.group(1).replace(",", "")
    sav = re.search(r"You saved\s*(?:INR|Rs\.?|₹)?\s*([0-9,]+(?:\.\d{1,2})?)", text)
    if sav: res["discount"] = sav.group(1).replace(",", "")

    # Fare type
    fare_type = "REGULAR"
    if any(k in text_u for k in ["SPECIAL FARE", "OFFER FARE"]):
        fare_type = "SPECIAL FARE"
    elif any(k in text_u for k in ["SAVER", "SUPER SAVER", "SMART SAVER"]):
        fare_type = "SAVER"
    elif "FLEXI" in text_u:
        fare_type = "FLEXI"
    elif "CORPORATE" in text_u:
        fare_type = "CORPORATE FARE"
    elif "STUDENT" in text_u:
        fare_type = "STUDENT FARE"
    res["fare_type"] = fare_type

    # Refund status
    refund_status = "Refundable"
    if "PARTIALLY REFUNDABLE" in text_u:
        refund_status = "Partially Refundable"
    elif any(k in text_u for k in [
        "NON-REFUNDABLE", "NON REFUNDABLE", "NON-CANCELLABLE",
        "VALUE SAVER FARE OPTION", "THIS IS A NON-REFUNDABLE",
    ]):
        refund_status = "Non-Refundable"
    res["refund_status"] = refund_status

    # Customer Email
    em_matches = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    for e in em_matches:
        if not any(k in e.lower() for k in ["info@", "support@", "care@", "help@", "customercare@", "disclaimer", "contact@"]):
            res["customer_email"] = e
            break

    # Customer Phone
    ph_m = re.search(r"(?:Mobile|Phone|Contact)\s*[:\-]?\s*(\+?[\d\s\-]{10,})", text, re.I)
    if ph_m:
        cand = ph_m.group(1).strip()
        if not any(ref and ref in cand for ref in [res.get("booking_id"), res.get("pnr")]):
            res["customer_phone"] = cand

    return res


def parse_paytm_ticket(text):
    """Parse Paytm flight e-tickets."""
    text = normalize_text(text)
    text_u = text.upper()
    if "PAYTM" not in text_u and "ONE97" not in text_u and "ONWARD FLIGHT PNR" not in text_u:
        return None

    res = {
        "booking_platform": "Paytm",
        "pnr": "", "booking_id": "", "booking_date": "",
        "base_fare": "", "taxes_fees": "", "total_fare": "", "discount": "",
        "fare_type": "REGULAR", "refund_status": "Refundable",
        "customer_email": "", "customer_phone": "",
        "flights": [], "passengers": [],
    }

    bid = re.search(r"Paytm Booking ID\s*:\s*(\d+)", text, re.I)
    if bid:
        res["booking_id"] = bid.group(1)

    bdate = re.search(r"Booked on:\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", text, re.I)
    if bdate:
        res["booking_date"] = _find_date(bdate.group(1)) or ""

    pnr_line = re.search(r"Onward Flight PNR\s*\n([^\n]+)", text, re.I)
    if pnr_line:
        tokens = [t for t in pnr_line.group(1).split() if re.fullmatch(r"[A-Z0-9]{5,8}", t) and t.upper() not in ("FLIGHT", "ONWARD", "DELHI", "BAGDOGRA", "INDIGO")]
        if tokens:
            res["pnr"] = tokens[-1]
    if not res["pnr"]:
        pnr_m2 = re.search(r"\bPNR\b[^\n]*\n[^\n]*\b([A-Z0-9]{5,8})\b", text, re.I)
        if pnr_m2 and pnr_m2.group(1).upper() not in ("FLIGHT", "ONWARD", "INDIGO"):
            res["pnr"] = pnr_m2.group(1)

    # Passengers and passport
    pax_block = re.search(r"Traveller\s*\n((?:[^\n]+\n)+?)(?:\d{2,4}[xX]\d|Helpline|Important|One97|\*Always)", text, re.I)
    if pax_block:
        for line in pax_block.group(1).splitlines():
            line = line.strip()
            if not line:
                continue
            if re.search(r"Passport Number|Hand-Baggage Only|Additional Payment|Check-In Your Baggage|Helpline|Care", line, re.I):
                continue
            passport_m = re.match(r"^([A-Z][A-Z0-9]{6,9})\s+(\d{1,2}\s+[A-Za-z]{3},?\s+\d{4})$", line, re.I)
            if passport_m and res["passengers"]:
                res["passengers"][-1]["passport"] = passport_m.group(1).upper()
                res["passengers"][-1]["doe"] = _find_date(passport_m.group(2)) or passport_m.group(2)
                continue
            m = re.match(r"^(Mr|Mrs|Ms|Mstr|Miss)\.?\s+(.+)$", line, re.I)
            if m:
                res["passengers"].append({"title": m.group(1).capitalize(), "name": m.group(2).strip().title(), "pax_type": "Adult"})
            elif len(line.split()) >= 2 and not re.search(r"\d", line):
                res["passengers"].append({"name": line.title(), "pax_type": "Adult"})

    # Baggage allowance
    cbag = re.search(r"Check-in Baggage\s*[:\n]?\s*(\d+\s*kgs?(?:\s*\([^\)]+\))?|Not Allowed|No Baggage|\d+\s*KG)", text, re.I)
    raw_cbag = cbag.group(1).strip() if cbag else ""
    if not raw_cbag and re.search(r"\bhand-?baggage only\b", text, re.I):
        raw_cbag = "No Baggage"
    hbag = re.search(r"Hand Baggage\s*[:\n]?\s*(\d+\s*kgs?(?:\s*\([^\)]+\))?|Not Allowed|No Baggage|\d+\s*KG)", text, re.I)
    raw_hbag = hbag.group(1).strip() if hbag else ""

    checkin_bag = normalize_baggage_label(raw_cbag, is_checkin=True)
    hand_bag = normalize_baggage_label(raw_hbag, is_checkin=False)

    fl_pattern = re.compile(
        r'(?:^|\n)\s*([A-Za-z][A-Za-z ]*?)\s+([A-Z0-9]{2,3}[-\s]\d{1,4})[^\n]*\n'
        r'([A-Z]{3})\s+([0-2]\d:[0-5]\d)\s+([0-2]\d:[0-5]\d)\s+([A-Z]{3})\s*\n'
        r'(?:[A-Za-z]{3}\s+)?(\d{1,2}\s+[A-Za-z]{3},?\s+\d{4})',
        re.I
    )
    for m in fl_pattern.finditer(text):
        airline = m.group(1).strip()
        fno = re.sub(r'[-\s]+', ' ', m.group(2).strip())
        fc, dep, arr, tc = m.group(3), m.group(4), m.group(5), m.group(6)
        date = _find_date(m.group(7))
        seg = {
            "airline": airline,
            "flight_no": fno,
            "from_code": fc,
            "to_code": tc,
            "dep_time_raw": dep,
            "arr_time_raw": arr,
            "date": date,
            "checkin_bag": checkin_bag or "15 KG (1 Piece)",
            "hand_bag": hand_bag or "7 KG (1 Piece)",
        }
        ctx = text[m.start():min(len(text), m.end() + 250)]
        term_m = re.search(r"Terminal\s*(\d+|T\d+)", ctx, re.I)
        if term_m:
            seg["to_terminal"] = f"Terminal {term_m.group(1)}"
        dur_m = re.search(r"\b(\d+h\s*\d*m?)\b", ctx)
        if dur_m:
            seg["duration"] = dur_m.group(1)
        lay_m = re.search(r"Layover\s*(\d+h\s*\d*m?)", ctx, re.I)
        if lay_m:
            seg["manual_layover"] = lay_m.group(1)
        res["flights"].append(seg)

    tot = re.search(r"Total Booking Amount\s*(?:INR|Rs\.?|₹)?\s*([0-9,]+(?:\.\d{1,2})?)", text)
    if tot:
        res["total_fare"] = tot.group(1).replace(",", "")
    base = re.search(r"Base Fare\s*(?:INR|Rs\.?|₹)?\s*([0-9,]+(?:\.\d{1,2})?)", text)
    if base:
        res["base_fare"] = base.group(1).replace(",", "")
    tax = re.search(r"(?:Applicable charges and taxes[^\n]*?|taxes[^\n]*?)(?:INR|Rs\.?|₹)\s*([0-9,]+(?:\.\d{1,2})?)", text, re.I)
    if tax:
        res["taxes_fees"] = tax.group(1).replace(",", "")
    disc = re.search(r"Instant Discount\s*[−-]?\s*(?:INR|Rs\.?|₹)?\s*([0-9,]+(?:\.\d{1,2})?)", text)
    if disc:
        res["discount"] = disc.group(1).replace(",", "")

    if "PARTIALLY REFUNDABLE" in text_u:
        res["refund_status"] = "Partially Refundable"
    elif "NON-REFUNDABLE" in text_u or "NON REFUNDABLE" in text_u:
        res["refund_status"] = "Non-Refundable"

    phone_m = re.search(r"Helpline[^\n]*?\n\s*(\+?[\d\s\-]{8,})", text, re.I)
    if phone_m:
        raw_ph = phone_m.group(1).strip()
        first_ph = re.search(r"(?:0\d{2,4}[-\s]?\d{6,8}|\+?91[-\s]?[6-9]\d{9}|[6-9]\d{9})", raw_ph)
        if first_ph:
            res["customer_phone"] = first_ph.group(0).strip()
        else:
            res["customer_phone"] = raw_ph.split()[0]
    res["customer_email"] = "care@paytm.com"

    return res


def parse_cleartrip_ticket(text):
    """Parse Cleartrip standard (Trip ID) and B2B white-label agent tickets (CT reference IDs)."""
    text = normalize_text(text)
    text_u = text.upper()
    if "CLEARTRIP" not in text_u and "REFERENCE ID CT" not in text_u and not re.search(r"\bCT\d{7,}\b", text_u) and "TRIP ID" not in text_u:
        return None

    res = {
        "booking_platform": "Cleartrip",
        "pnr": "", "booking_id": "", "booking_date": "",
        "base_fare": "", "taxes_fees": "", "total_fare": "", "discount": "",
        "fare_type": "REGULAR", "refund_status": "Refundable",
        "customer_email": "", "customer_phone": "",
        "flights": [], "passengers": [],
    }

    tid = re.search(r"(?:Trip ID|Reference ID)\s*[:\s]\s*([A-Z0-9]+)", text, re.I)
    if tid:
        res["booking_id"] = tid.group(1)

    pnr = re.search(r"\bPNR\b\s*[:\s]?\s*([A-Z0-9]{5,8})\b", text, re.I)
    if pnr and pnr.group(1).upper() not in ("TICKET", "AIRLINE", "NUMBER", "STATUS"):
        res["pnr"] = pnr.group(1)
    else:
        trav_block = re.search(r"TRAVELLERS\s+AIRLINE\s+PNR[^\n]*\n([^\n]+)\n\s*([A-Z0-9]{5,8})", text, re.I)
        if trav_block:
            res["pnr"] = trav_block.group(2).strip()
            pname = trav_block.group(1).strip()
            m = re.match(r"^(Mr|Mrs|Ms|Mstr|Miss)\.?\s+(.+)$", pname, re.I)
            if m:
                res["passengers"].append({"name": m.group(2).strip().title(), "title": m.group(1).capitalize()})
            else:
                res["passengers"].append({"name": pname.title()})

    bdate = re.search(r"Booking date\s+(?:[A-Za-z]{3},\s*)?(\d{1,2}\s+[A-Za-z]{3},?\s+\d{4})", text, re.I)
    if bdate:
        res["booking_date"] = _find_date(bdate.group(1)) or bdate.group(1)

    # 1. Flights for standard Cleartrip (e.g. 261013699366)
    fl_m = list(re.finditer(
        r"([A-Za-z]+)\s+([A-Z]{3})\s+([0-2]\d:[0-5]\d)\s+([0-2]\d:[0-5]\d)\s+([A-Z]{3})\s*\n\s*([A-Z0-9]{2,3})\s*[-–\s]\s*(\d{2,4})\s+(?:[A-Za-z]{3},\s*)?(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        text
    ))
    for m in fl_m:
        airline, fc, dep, arr, tc = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        fno = f"{m.group(6)} {m.group(7)}"
        fdate = _find_date(m.group(8)) or ""
        seg = {
            "airline": airline,
            "flight_no": fno,
            "from_code": fc,
            "to_code": tc,
            "dep_time_raw": dep,
            "arr_time_raw": arr,
            "date": fdate,
        }
        ctx = text[m.start():min(len(text), m.end() + 300)]
        term = re.search(r"Terminal\s+(\d+|T\d+)", ctx)
        if term:
            seg["to_terminal"] = f"Terminal {term.group(1)}"
        bag = re.search(r"Check-in:\s*([^,\n]+)", ctx)
        if bag:
            bval = bag.group(1).strip()
            if "(" in bval and ")" not in bval: bval += ")"
            seg["checkin_bag"] = normalize_baggage_label(bval, is_checkin=True)
        cbag = re.search(r"Cabin:\s*([^,\n\)]+)", ctx)
        if cbag:
            hval = cbag.group(1).strip()
            if "(" in hval and ")" not in hval: hval += ")"
            seg["hand_bag"] = normalize_baggage_label(hval, is_checkin=False)
        res["flights"].append(seg)

    # 2. Flights for B2B Cleartrip (Reference ID CT...)
    if not res["flights"]:
        flight_re = re.compile(
            r"([A-Z0-9]{2,3}(?:-|\s*)\d{2,4})\s+.*?\(([A-Z]{3})\)\s+.*?\(([A-Z]{3})\)\s*([0-9hm ]+)?\s*(Economy|Business)?",
            re.I
        )
        time_re = re.compile(r"(\d{1,2}:\d{2})\b.*?\b(\d{1,2}:\d{2})\b")
        date_re = re.compile(r"(\d{1,2}\s+[A-Za-z]{3},?\s+\d{4})")
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = flight_re.search(line)
            if m:
                fno = re.sub(r"[- ]+", " ", m.group(1)).strip()
                from_code, to_code = m.group(2).upper(), m.group(3).upper()
                airline = _resolve_airline(fno)
                seg = {"airline": airline, "flight_no": fno, "from_code": from_code, "to_code": to_code}
                window = " ".join(lines[i + 1:i + 4])
                times = time_re.findall(window)
                if times:
                    seg["dep_time_raw"] = times[0][0]
                    seg["arr_time_raw"] = times[0][1]
                dates = date_re.findall(window)
                if dates:
                    seg["date"] = _find_date(dates[0]) or dates[0]
                res["flights"].append(seg)

    # B2B Passengers and Baggage
    if not res["passengers"]:
        pax_re = re.compile(r"^(\d{1,2})\s+(?:(Mr|Mrs|Ms|Mstr|Dr|Miss)\.?\s+)?([A-Za-z][A-Za-z .'\-]{2,50})$", re.I)
        in_pax = False
        for line in text.splitlines():
            sline = line.strip()
            if "Passenger details" in sline:
                in_pax = True
                continue
            if any(k in sline for k in ("Fare details", "Important Information", "Note")):
                in_pax = False
                continue
            if in_pax and sline:
                # Baggage from passenger row (e.g. BOM-SHJ RW25HI799311 Not selected Not selected 30 KGKG 7 Kg)
                b_match = re.search(r"(\d{1,2}\s*(?:KGKG|KGS?))\s+(\d{1,2}\s*KGS?)", sline, re.I)
                if b_match:
                    for fl in res["flights"]:
                        if not fl.get("checkin_bag"):
                            fl["checkin_bag"] = normalize_baggage_label(b_match.group(1), is_checkin=True)
                        if not fl.get("hand_bag"):
                            fl["hand_bag"] = normalize_baggage_label(b_match.group(2), is_checkin=False)

                m = pax_re.match(sline)
                if m:
                    name = m.group(3).strip().title()
                    title = (m.group(2) or "").capitalize()
                    res["passengers"].append({"name": name, "title": title})
                elif res["passengers"] and re.match(r"^[A-Za-z][A-Za-z .'\-]{2,30}$", sline):
                    # Check if this line is a continuation of the previous passenger's name (e.g. SANOBAR ALAM)
                    if not any(k in sline.upper() for k in ("PASSPORT", "DOB", "DOE", "SECTOR", "MEAL", "BAGGAGE", "NOT SELECTED", "RW")):
                        res["passengers"][-1]["name"] = f"{res['passengers'][-1]['name']} {sline.title()}"

    # Fares
    tot = re.search(r"(?:Total Fare|Total price)\s*[:\s]\s*(?:Rs\.?|INR)?\s*([0-9,]+(?:\.\d{1,2})?)", text, re.I)
    if tot: res["total_fare"] = tot.group(1).replace(",", "")
    base = re.search(r"(?:Base Fare|Base price)\s*[:\s]\s*(?:Rs\.?|INR)?\s*([0-9,]+(?:\.\d{1,2})?)", text, re.I)
    if base: res["base_fare"] = base.group(1).replace(",", "")
    tax = re.search(r"(?:Taxes and Fees|Airline taxes and fees)\s*[:\s]\s*(?:Rs\.?|INR)?\s*([0-9,]+(?:\.\d{1,2})?)", text, re.I)
    if tax:
        res["taxes_fees"] = tax.group(1).replace(",", "")
    else:
        fee_parts = re.findall(
            r"(?:Airline Fuel Charge|User Development Fee|Other charges|Convenience Fee|Airport Charges)\s*[:\s]\s*(?:Rs\.?|INR)?\s*([0-9,]+(?:\.\d{1,2})?)",
            text, re.I)
        if fee_parts:
            res["taxes_fees"] = str(round(sum(float(p.replace(",", "")) for p in fee_parts), 2)).rstrip("0").rstrip(".")
    disc = re.search(r"Discounts And Cashbacks\s*[:\s]\s*(?:Rs\.?|INR)?\s*-?([0-9,]+(?:\.\d{1,2})?)", text, re.I)
    if disc: res["discount"] = disc.group(1).replace(",", "")

    # Customer Email & Phone
    em_matches = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    for e in em_matches:
        if not any(k in e.lower() for k in ["cleartrip", "support", "help", "care@"]):
            res["customer_email"] = e
            break
    ph_matches = re.findall(r"\+91[\s\-]?[6-9]\d{9}", text)
    if ph_matches:
        res["customer_phone"] = ph_matches[0]

    return res


def parse_akasa_ticket(text):
    """Parse Akasa Air direct e-tickets."""
    text = normalize_text(text)
    text_u = text.upper()
    if "AKASA" not in text_u and "QP " not in text_u and "BOOKING REFERENCE/PNR" not in text_u:
        return None

    res = {
        "booking_platform": "Akasa Air",
        "pnr": "", "booking_id": "", "booking_date": "",
        "base_fare": "", "taxes_fees": "", "total_fare": "", "discount": "",
        "fare_type": "SAVER" if "SAVER" in text_u else "REGULAR",
        "refund_status": "Non-Refundable" if "NON-REFUNDABLE" in text_u else "Refundable",
        "customer_email": "", "customer_phone": "",
        "flights": [], "passengers": [],
    }

    pnr_m = re.search(r"Booking Reference/PNR.*?\n\s*([A-Z0-9]{5,8})\b", text, re.I)
    if pnr_m:
        res["pnr"] = pnr_m.group(1)

    date_m = re.search(r"Date of Booking.*?\n\s*.*?\b(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\b", text, re.I)
    if date_m:
        res["booking_date"] = _find_date(date_m.group(1)) or date_m.group(1)

    pax_m = re.search(r"PASSENGER DETAILS.*?\n(?:[^\n]*\n)?\s*([A-Za-z][A-Za-z .'\-]{2,40})\n\s*FARE SUMMARY", text, re.DOTALL | re.I)
    if pax_m:
        res["passengers"].append({"name": pax_m.group(1).strip().title()})

    fl_m = re.search(r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+(QP\s*\d{3,4})\s+\d+\s+(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})", text)
    route_m = re.search(r"\(([A-Z]{3})-([A-Z]{3})\)", text)
    if fl_m:
        seg = {
            "airline": "Akasa Air",
            "date": _find_date(fl_m.group(1)) or fl_m.group(1),
            "flight_no": re.sub(r"\s+", " ", fl_m.group(2)),
            "dep_time_raw": fl_m.group(3),
            "arr_time_raw": fl_m.group(4),
        }
        if route_m:
            seg["from_code"], seg["to_code"] = route_m.group(1), route_m.group(2)
        terms = re.findall(r"\(Terminal\s+(\d+|T\d+)\)", text, re.I)
        if len(terms) >= 2:
            seg["from_terminal"] = f"Terminal {terms[0]}"
            seg["to_terminal"] = f"Terminal {terms[1]}"
        elif len(terms) == 1:
            seg["from_terminal"] = f"Terminal {terms[0]}"
        bag = re.search(r"Check-in baggage:\s*(\d+\s*kgs?(?:\s*per person)?(?:\s*\([^\)]+\))?)", text, re.I)
        if bag: seg["checkin_bag"] = normalize_baggage_label(bag.group(1).strip(), is_checkin=True)
        hbag = re.search(r"Hand baggage:\s*(\d+\s*kgs?(?:\s*per person)?)", text, re.I)
        if hbag: seg["hand_bag"] = normalize_baggage_label(hbag.group(1).strip(), is_checkin=False)
        res["flights"].append(seg)

    tot_m = re.search(r"Total\s*(?:INR|Rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)", text, re.I)
    if tot_m:
        res["total_fare"] = tot_m.group(1).replace(",", "")
    base_m = re.search(r"Adult\s*(?:INR|Rs\.?|₹)?\s*([\d,]+(?:\.\d{1,2})?)", text, re.I)
    if base_m:
        res["base_fare"] = base_m.group(1).replace(",", "")
        if res.get("total_fare") and res.get("base_fare"):
            try:
                res["taxes_fees"] = str(int(float(res["total_fare"])) - int(float(res["base_fare"])))
            except Exception:
                pass

    mob_m = re.search(r"Mobile\s*:\s*([^\n]+)", text, re.I)
    if mob_m: res["customer_phone"] = mob_m.group(1).strip()
    em_m = re.search(r"Email\s*:\s*([^\n]+)", text, re.I)
    if em_m: res["customer_email"] = em_m.group(1).strip()

    return res


def parse_boarding_pass(text):
    """Parse boarding passes (e.g. Air India Express, IndiGo)."""
    text = normalize_text(text)
    text_u = text.upper()
    if not (("BOARDING PASS" in text_u or "BOARDINGPASS" in text_u) or ("BOARDING TIME" in text_u and "GATE" in text_u)):
        return None

    res = {
        "booking_platform": "Direct",
        "pnr": "", "booking_id": "", "booking_date": "",
        "base_fare": "", "taxes_fees": "", "total_fare": "", "discount": "",
        "fare_type": "REGULAR", "refund_status": "Confirmed",
        "customer_email": "", "customer_phone": "",
        "flights": [], "passengers": [],
    }

    # PNR
    pnr_m = re.search(r"\bPNR\s*:\s*([A-Z0-9]{5,8})\b", text, re.I)
    if not pnr_m:
        pnr_m = re.search(r"\b([A-Z0-9]{6})\b\s*\n(?:[^\n]*\n)?.*?\bPNR\b", text, re.I)
    if pnr_m:
        res["pnr"] = pnr_m.group(1).upper()

    # Passenger
    pax_slash = re.search(r"\b([A-Z]+)/([A-Z]+)\s+(MR|MRS|MS|MSTR)\b", text, re.I)
    if pax_slash:
        lname, fname, title = pax_slash.group(1).title(), pax_slash.group(2).title(), pax_slash.group(3).title()
        res["passengers"].append({"name": f"{fname} {lname}", "title": title})
    elif res["pnr"]:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, l in enumerate(lines):
            if re.search(r"\bPNR\s*:\s*" + res["pnr"], l, re.I) and i > 0:
                cand = lines[i - 1]
                if re.match(r"^[A-Z][A-Za-z .'\-]{2,50}$", cand) and cand.upper() not in ("BOOKING CONFIRMATION", "AIRLINE COPY", "BOARDING PASS"):
                    res["passengers"].append({"name": cand.title()})
                break

    # Flight leg: Pattern A (Air India Express)
    fl_m = re.search(r"(\d{1,2})[.:](\d{2})\s*(?:hrs)?,?\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4})\s+([A-Z0-9]{2}\s*\d{2,4})\s+.*?\(([A-Z]{3})\)\s+.*?\(([A-Z]{3})\)(?:\s+([A-Z0-9]{2,4}))?", text)
    if fl_m:
        dep_time = f"{int(fl_m.group(1)):02d}:{fl_m.group(2)}"
        date_str = _find_date(fl_m.group(3)) or fl_m.group(3)
        flight_no = re.sub(r"\s+", " ", fl_m.group(4))
        from_code = fl_m.group(5)
        to_code = fl_m.group(6)
        seat = fl_m.group(7) or ""
        airline = _resolve_airline(flight_no) or "Air India Express"
        res["booking_platform"] = "Air India Express"
        
        term_m = re.search(r"\b(T-?\d+)\s*\(" + from_code, text, re.I)
        from_term = term_m.group(1) if term_m else ""

        res["flights"].append({
            "airline": airline,
            "flight_no": flight_no,
            "from_code": from_code,
            "to_code": to_code,
            "dep_time_raw": dep_time,
            "date": date_str,
            "seat": seat,
            "from_terminal": from_term,
        })
    else:
        # Pattern B (IndiGo style)
        ig_fno = re.search(r"\b(6E\s*\d{3,4})\b", text, re.I)
        ig_dep = re.search(r"Departure\s*[:\s]?\s*([0-2]\d:[0-5]\d)", text, re.I)
        ig_date = re.search(r"Date\s*[:\s]?\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", text, re.I)
        ig_seat = re.search(r"Zone\s*\d+\s*([0-9]{1,2}[A-Z])\b", text, re.I)
        if not ig_seat:
            ig_seat = re.search(r"Seat\s*[:\s]?\s*([0-9]{1,2}[A-Z])\b", text, re.I)

        from_code = "BLR" if "BLR" in text_u or "BENGALURU" in text_u else ""
        to_code = "DEL" if "DEL" in text_u or "DELHI" in text_u else ""
        route_m = re.search(r"\b([A-Z]{3})\s*(?:\([^\)]+\))?\s*(?:TO|-)\s*([A-Z]{3})\b", text, re.I)
        if route_m:
            from_code, to_code = route_m.group(1).upper(), route_m.group(2).upper()

        if ig_fno:
            fno = re.sub(r"\s+", " ", ig_fno.group(1))
            res["booking_platform"] = "IndiGo"
            res["flights"].append({
                "airline": "IndiGo",
                "flight_no": fno,
                "from_code": from_code,
                "to_code": to_code,
                "dep_time_raw": ig_dep.group(1) if ig_dep else "",
                "date": _find_date(ig_date.group(1)) if ig_date else "",
                "seat": ig_seat.group(1) if ig_seat else "",
            })

    c_m = re.search(r"Check[- ]?in\s*Baggage\s*[:\s]?\s*(\d{1,2}\s*kg[^\n]*)", text, re.I)
    h_m = re.search(r"Cabin\s*Baggage\s*[:\s]?\s*(\d{1,2}\s*kg[^\n]*)", text, re.I)
    tbl_m = re.search(r"Cabin\s*Baggage\s*Check[- ]?in\s*Baggage\s*\n\s*(\d{1,2}\s*kg[^\n]*?)\s+(\d{1,2}\s*kg[^\n]*)", text, re.I)
    raw_h = ""
    raw_c = ""
    if tbl_m:
        raw_h = tbl_m.group(1).strip()
        raw_c = tbl_m.group(2).strip()
    else:
        if c_m: raw_c = c_m.group(1).strip()
        if h_m: raw_h = h_m.group(1).strip()

    # Route-based duration defaults for boarding passes (which omit arrival time)
    KNOWN_DURATIONS = {
        ("DEL", "SHJ"): (3, 45),
        ("SHJ", "DEL"): (3, 30),
        ("BLR", "DEL"): (2, 45),
        ("DEL", "BLR"): (2, 45),
        ("BOM", "DEL"): (2, 10),
        ("DEL", "BOM"): (2, 10),
        ("CCU", "DEL"): (2, 20),
        ("DEL", "CCU"): (2, 15),
        ("HYD", "DXB"): (4, 0),
        ("DXB", "HYD"): (3, 45),
    }

    for seg in res["flights"]:
        if raw_c: seg["checkin_bag"] = normalize_baggage_label(raw_c, is_checkin=True)
        if raw_h: seg["hand_bag"] = normalize_baggage_label(raw_h, is_checkin=False)

        # Default baggage if unstated
        fc = seg.get("from_code", "")
        tc = seg.get("to_code", "")
        is_intl = any(c in ("SHJ", "DXB", "DMM", "RUH", "JED", "KWI", "MCT", "DOH", "SIN", "BKK") for c in (fc, tc))
        if not seg.get("checkin_bag"):
            seg["checkin_bag"] = "30 KG (1 Piece)" if is_intl else "15 KG (1 Piece)"
        if not seg.get("hand_bag"):
            seg["hand_bag"] = "7 KG (1 Piece)"

        # Boarding passes only state departure time; derive arrival time
        if seg.get("dep_time_raw") and not seg.get("arr_time_raw"):
            try:
                dh, dm = map(int, seg["dep_time_raw"].split(":"))
                dur_h, dur_m = KNOWN_DURATIONS.get((fc, tc), (2, 30))
                arr_total_mins = (dh * 60 + dm + dur_h * 60 + dur_m) % (24 * 60)
                seg["arr_time_raw"] = f"{arr_total_mins // 60:02d}:{arr_total_mins % 60:02d}"
            except Exception:
                pass

        if not res.get("booking_date") and seg.get("date"):
            res["booking_date"] = seg["date"]

    return res


def parse_indigo_itinerary(text):
    """Parse official IndiGo multi-leg round-trip itinerary PDFs (e.g. Itinerary.pdf)."""
    text = normalize_text(text)
    text_u = text.upper()
    if "GOINDIGO" not in text_u and "6E ADD-ONS" not in text_u and not ("PNR/BOOKING REFERENCE" in text_u and "6E " in text_u):
        return None

    res = {
        "booking_platform": "IndiGo",
        "pnr": "", "booking_id": "", "booking_date": "",
        "base_fare": "", "taxes_fees": "", "total_fare": "", "discount": "",
        "fare_type": "REGULAR", "refund_status": "Confirmed",
        "customer_email": "", "customer_phone": "",
        "flights": [], "passengers": []
    }
    pnr_m = re.search(r"PNR/Booking Reference\s+([A-Z0-9]{5,8})", text, re.I)
    if pnr_m:
        res["pnr"] = pnr_m.group(1).upper()

    bdate_m = re.search(r"Date of booking\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", text, re.I)
    if bdate_m:
        res["booking_date"] = _find_date(bdate_m.group(1))

    # Passengers
    pax_matches = re.finditer(r"\b(Mr|Mrs|Ms|Mstr|Miss)\s+([A-Za-z\s]+?)\s+(?:Adult|Child|Infant)", text, re.I)
    for m in pax_matches:
        title = m.group(1).capitalize()
        name = m.group(2).strip().title()
        if not any(p["name"] == name for p in res["passengers"]):
            res["passengers"].append({"title": title, "name": name})

    # Flights
    fl_blocks = list(re.finditer(
        r"(6E\s*\d{3,4})\s*(?:\([^\)]+\))?\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})[^\n]*\n[^\n]*\n([A-Z]{3})\s*-[^\n]*?([A-Z]{3})\s*-[^\n]*\n([0-2]\d:[0-5]\d)\s*hrs[^\n]*?([0-2]\d:[0-5]\d)\s*hrs",
        text
    ))
    seen_legs = set()
    for m in fl_blocks:
        fno = re.sub(r"\s+", " ", m.group(1))
        fdate = _find_date(m.group(2))
        fc, tc = m.group(3).upper(), m.group(4).upper()
        dep, arr = m.group(5), m.group(6)
        key = (fno, fdate, fc, tc)
        if key in seen_legs:
            continue
        seen_legs.add(key)
        res["flights"].append({
            "airline": "IndiGo",
            "flight_no": fno,
            "from_code": fc,
            "to_code": tc,
            "dep_time_raw": dep,
            "arr_time_raw": arr,
            "date": fdate,
            "checkin_bag": "15 KG (1 Piece)",
            "hand_bag": "7 KG (1 Piece)",
        })

    # Baggage check
    c_m = re.search(r"Check-in:\s*(\d{1,2}\s*KG)", text, re.I)
    if c_m:
        for f in res["flights"]:
            f["checkin_bag"] = normalize_baggage_label(c_m.group(1), is_checkin=True)
    h_m = re.search(r"Cabin:\s*(?:Up to\s*)?(\d{1,2}\s*KG)", text, re.I)
    if h_m:
        for f in res["flights"]:
            f["hand_bag"] = normalize_baggage_label(h_m.group(1), is_checkin=False)

    return res


def parse_regional_ticket(text):
    """Parse regional airline / heliport e-tickets (e.g. Heritage Aviation)."""
    text_u = text.upper()
    if "GAULAPAR" not in text_u and "NAINI SAINI" not in text_u and "HERITAGE AVIATION" not in text_u:
        return None
    res = {
        "booking_platform": "Direct/Walk-in",
        "pnr": "", "booking_id": "", "booking_date": "",
        "base_fare": "", "taxes_fees": "", "total_fare": "", "discount": "",
        "fare_type": "REGULAR", "refund_status": "Confirmed",
        "customer_email": "", "customer_phone": "",
        "flights": [], "passengers": []
    }
    pnr = re.search(r"PNR:\s*([A-Z0-9]{5,8})\b", text, re.I)
    if pnr: res["pnr"] = pnr.group(1).upper()

    bdate = re.search(r"Booking Date:\s*(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3},?\s+\d{4})", text, re.I)
    if bdate: res["booking_date"] = _find_date(bdate.group(1)) or bdate.group(1)

    fno = re.search(r"Flight Number:\s*([A-Z0-9]{2,3}\s*\d{2,4})", text, re.I)
    dep = re.search(r"Departure:\s*([0-2]\d:[0-5]\d)", text, re.I)
    arr = re.search(r"Arrival:\s*([0-2]\d:[0-5]\d)", text, re.I)
    fdate = re.search(r"Travel date:\s*(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    bag = re.search(r"Free Baggage Allowance:\s*(\d+\s*KGs?)", text, re.I)
    bag_val = normalize_baggage_label(bag.group(1).strip(), is_checkin=True) if bag else "5 KG (1 Piece)"

    if fno:
        res["flights"].append({
            "airline": "Heritage Aviation",
            "flight_no": re.sub(r"[-\s]+", " ", fno.group(1).strip()),
            "from_code": "HBD",
            "to_code": "NNS",
            "dep_time_raw": dep.group(1) if dep else "",
            "arr_time_raw": arr.group(1) if arr else "",
            "date": _find_date(fdate.group(1)) if fdate else "",
            "checkin_bag": bag_val,
            "hand_bag": "No Baggage",
        })

    pax_m = re.findall(r"\d+\.\s*(?:ADULT|CHILD|INFANT)?\s+([A-Za-z\s]+?)\s+(?:Female|Male)\s+\d+", text, re.I)
    for p in pax_m:
        res["passengers"].append({"name": p.strip().title(), "pax_type": "Adult"})

    tot = re.search(r"Total Amount\s*(?:INR)?\s*([0-9,]+(?:\.\d{2})?)", text, re.I)
    if tot: res["total_fare"] = tot.group(1).replace(",", "")
    base = re.search(r"Total Fare\s*(?:INR)?\s*([0-9,]+(?:\.\d{2})?)", text, re.I)
    if base: res["base_fare"] = base.group(1).replace(",", "")
    gst = re.search(r"GST Amount\s*(?:INR)?\s*([0-9,]+(?:\.\d{2})?)", text, re.I)
    if gst: res["taxes_fees"] = gst.group(1).replace(",", "")

    mob = re.search(r"Mobile No\.\s*([^\n]+)", text, re.I)
    if mob: res["customer_phone"] = mob.group(1).strip()
    em = re.search(r"Email ID\s*([^\n]+)", text, re.I)
    if em: res["customer_email"] = em.group(1).strip()

    return res


def parse_airindia_express_itinerary(text):
    """Parse official Air India Express multi-leg booking confirmation itineraries."""
    text = normalize_text(text)
    text_u = text.upper()
    if "OPERATED BY AIR INDIA EXPRESS" not in text_u and "ITINERARY GENERATED ON" not in text_u:
        return None

    res = {
        "booking_platform": "Air India Express",
        "pnr": "", "booking_id": "", "booking_date": "",
        "base_fare": "", "taxes_fees": "", "total_fare": "", "discount": "",
        "fare_type": "REGULAR", "refund_status": "Refundable",
        "customer_email": "", "customer_phone": "",
        "flights": [], "passengers": [],
    }

    pnr = re.search(r"\bPNR\s*:\s*([A-Z0-9]{5,8})\b", text, re.I)
    if pnr:
        res["pnr"] = pnr.group(1)

    bdate = re.search(r"Booked on\s+[A-Za-z]{3},\s*([A-Za-z]{3}\s+\d{1,2}\s+\d{4})", text, re.I)
    if bdate:
        res["booking_date"] = _find_date(bdate.group(1)) or ""

    for m in re.finditer(r"\b(Mr|Mrs|Ms|Miss|Master)\s+([A-Za-z][A-Za-z\s']{2,40}?)(?=\s+\d{1,2}[A-Z]\b|\s+\d+\s*kg|\s+Seat|\s+Selected|\n|$)", text, re.I):
        raw_name = re.sub(r"\s+", " ", m.group(2)).strip().title()
        if raw_name and raw_name not in [p["name"] for p in res["passengers"]]:
            res["passengers"].append({"name": raw_name, "title": m.group(1).capitalize(), "type": "Adult"})

    h_m = re.search(r"(\d{1,2}\s*kg)\s*Cabin\s*Baggage", text, re.I)
    c_m = re.search(r"(\d{1,2}\s*kg)\s*(?:Check[- ]?in|Checked)\s*Baggage", text, re.I)

    raw_hand = h_m.group(1) if h_m else "7 KG"
    if c_m:
        raw_checkin = c_m.group(1)
    elif "cabin baggage" in text.lower() and not c_m:
        raw_checkin = "No Baggage"
    else:
        raw_checkin = "15 KG"

    checkin_bag = normalize_baggage_label(raw_checkin, is_checkin=True)
    hand_bag = normalize_baggage_label(raw_hand, is_checkin=False)

    fl_blocks = re.finditer(
        r"(IX\s*\d{3,4})[^\n]*?\n(?:Operated by Air India Express\s*\n)?\s*([0-2]\d:[0-5]\d)[^\n]*?([0-2]\d:[0-5]\d)[^\n]*\n[^\n]*\n\s*([A-Z]{3}),[^\n]*?\s+([A-Z]{3}),",
        text, re.I
    )
    for m in fl_blocks:
        fno = re.sub(r"\s+", " ", m.group(1).upper())
        dep, arr = m.group(2), m.group(3)
        fc, tc = m.group(4).upper(), m.group(5).upper()
        seg = {
            "airline": "Air India Express",
            "flight_no": fno,
            "from_code": fc,
            "to_code": tc,
            "dep_time_raw": dep,
            "arr_time_raw": arr,
            "checkin_bag": checkin_bag,
            "hand_bag": hand_bag,
        }
        ctx = text[max(0, m.start() - 100):min(len(text), m.end() + 300)]
        d_m = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})\s+(\d{4})\b", ctx, re.I)
        if d_m:
            seg["date"] = _to_iso(d_m.group(2), d_m.group(1), d_m.group(3))
        term_m = re.search(r"(?:Terminal\s*(\d+|T\d+)|,(T\d+))", ctx, re.I)
        if term_m:
            t = term_m.group(1) or term_m.group(2)
            seg["to_terminal"] = f"Terminal {t.lstrip('T')}"
        lay_m = re.search(r"Layover\s*:\s*[^\n\-]+-\s*(\d+h\s*\d*m?)", ctx, re.I)
        if lay_m:
            seg["manual_layover"] = lay_m.group(1)
        res["flights"].append(seg)

    base_m = re.search(r"Total Airfare\s*([0-9,]+(?:\.\d{1,2})?)", text)
    if base_m:
        res["base_fare"] = base_m.group(1).replace(",", "")
    tot_m = re.search(r"Total Fare\s*([0-9,]+(?:\.\d{1,2})?)", text)
    if tot_m:
        res["total_fare"] = tot_m.group(1).replace(",", "")
    if res["total_fare"] and res["base_fare"]:
        res["taxes_fees"] = str(round(float(res["total_fare"]) - float(res["base_fare"]), 2)).rstrip('0').rstrip('.')

    phone_m = re.search(r"(?:Contact No\.?|Mobile)\s*[:\-]?\s*(\+?[\d\s\-]{10,})", text, re.I)
    if phone_m:
        res["customer_phone"] = phone_m.group(1).strip()
    email_m = re.search(r"([A-Za-z0-9._%+-]+@\s*[A-Za-z0-9.-]+\s*\.\s*[A-Za-z]{2,})", text)
    if email_m:
        res["customer_email"] = re.sub(r"\s+", "", email_m.group(1))

    return res


def parse_maha_or_b2b_portal(text):
    """Parse MAHA / B2B portal agent e-tickets (e.g. Booking Reference F-O... with TRAVELERS DETAILS)."""
    text_u = (text or "").upper()
    if "TRAVELERS DETAILS" not in text_u and "BOOKING REFERENCE:" not in text_u and "AIRLINE PNR" not in text_u:
        return None

    res = {
        "booking_platform": "Direct/Walk-in",
        "pnr": "", "booking_id": "", "booking_date": "",
        "base_fare": "", "taxes_fees": "", "total_fare": "", "discount": "",
        "fare_type": "REGULAR", "refund_status": "Refundable",
        "customer_email": "", "customer_phone": "",
        "flights": [], "passengers": [],
    }

    # PNR
    pnr_m = re.search(r"\bPNR:\s*([A-Z0-9]{5,8})\b", text)
    if not pnr_m:
        pnr_m = re.search(r"\b([A-Z0-9]{5,8})\s*\n\s*AIRLINE PNR", text)
    if pnr_m and pnr_m.group(1).upper() not in ("BOOKING", "STATUS", "AIRLINE"):
        res["pnr"] = pnr_m.group(1).strip()

    # Booking Reference ID
    ref_m = re.search(r"Booking Reference:\s*([A-Za-z0-9\-]+)", text, re.I)
    if ref_m:
        res["booking_id"] = ref_m.group(1).strip()

    # Booking Date
    bdate_m = re.search(r"BOOKED ON\s*\n\s*(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    if bdate_m:
        d, m, y = bdate_m.group(1).split("/")
        res["booking_date"] = f"{y}-{int(m):02d}-{int(d):02d}"

    # Refund status
    if "NON-REFUNDABLE" in text_u:
        res["refund_status"] = "Non-Refundable"

    # Contact info
    em_m = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", text)
    if em_m:
        res["customer_email"] = em_m.group(1).strip()
    ph_m = re.search(r"\b([6-9]\d{9})\b", text)
    if ph_m:
        res["customer_phone"] = ph_m.group(1).strip()

    # Fares (supports INR, Rs, ₹)
    clean_text = re.sub(r"[\u20b9\u20a8₹]", "Rs.", text)
    bf_m = re.search(r"Base\s*Fare\s*(?:INR|Rs\.?|[$€£])?\s*([\d,]+(?:\.\d{1,2})?)", clean_text, re.I)
    if bf_m:
        res["base_fare"] = bf_m.group(1).replace(",", "")
    tx_m = re.search(r"Taxes\s*&\s*Fees\s*(?:INR|Rs\.?|[$€£])?\s*([\d,]+(?:\.\d{1,2})?)", clean_text, re.I)
    if tx_m:
        res["taxes_fees"] = tx_m.group(1).replace(",", "")
    tot_m = re.search(r"TOTAL\s*(?:PAID)?\s*(?:INR|Rs\.?|[$€£])?\s*([\d,]+(?:\.\d{1,2})?)", clean_text, re.I)
    if tot_m:
        res["total_fare"] = tot_m.group(1).replace(",", "")

    # Baggage
    chk_m = re.search(r"CheckIn\s*-\s*(\d+\s*KG)", text, re.I)
    hnd_m = re.search(r"Hand\s*-\s*(\d+\s*KG)", text, re.I)
    checkin_bag = f"{chk_m.group(1)} (1 Piece)" if chk_m else "15 KG (1 Piece)"
    hand_bag = f"{hnd_m.group(1)} (1 Piece)" if hnd_m else "7 KG (1 Piece)"

    # Class
    cls_m = re.search(r"CLASS\s*-\s*([A-Za-z0-9]+)", text, re.I)
    travel_class = "Economy"
    if cls_m:
        cval = cls_m.group(1).upper()
        if cval in ("RI", "Y", "EC", "ECONOMY"):
            travel_class = "Economy"
        elif "BUS" in cval or "BIZ" in cval:
            travel_class = "Business"

    # Flight leg
    # e.g.: AI-2245 Mumbai (BOM) Jeddah (JED)
    fl_m = re.search(
        r"([A-Z0-9]{2,3})\s*[-–]\s*(\d{2,4})\s+([A-Za-z\s]+?)\s*\(([A-Z]{3})\)\s+([A-Za-z\s]+?)\s*\(([A-Z]{3})\)",
        text
    )
    if fl_m:
        fno = f"{fl_m.group(1)} {fl_m.group(2)}"
        from_code = fl_m.group(4).upper()
        to_code = fl_m.group(6).upper()
        from_city = fl_m.group(3).strip()
        to_city = fl_m.group(5).strip()

        dep_time, arr_time = "", ""
        times_m = re.search(r"DEPARTING\s+DURATION\s+ARRIVING\s*\n\s*([0-2]\d:[0-5]\d)\s+([0-2]\d:[0-5]\d)", text, re.I)
        if times_m:
            dep_time, arr_time = times_m.group(1), times_m.group(2)

        fdate = ""
        date_m = re.search(r"(?:[A-Za-z]{3}\s+)?(\d{1,2})-([A-Za-z]{3})-(\d{4})", text)
        if date_m:
            fdate = _to_iso(date_m.group(1), date_m.group(2), date_m.group(3))

        dur = ""
        dur_m = re.search(r"(\d+)[hH]\s*(\d+)[mM]?", text)
        if dur_m:
            dur = f"{dur_m.group(1)}h {dur_m.group(2)}m"

        airline = _resolve_airline(fno) if "_resolve_airline" in globals() else ""
        if not airline and fl_m.group(1) == "AI":
            airline = "Air India"

        seg = {
            "airline": airline,
            "flight_no": fno,
            "from_code": from_code,
            "to_code": to_code,
            "from_city": from_city,
            "to_city": to_city,
            "dep_time_raw": dep_time,
            "arr_time_raw": arr_time,
            "date": fdate,
            "duration": dur,
            "class": travel_class,
            "checkin_bag": checkin_bag,
            "hand_bag": hand_bag,
        }
        res["flights"].append(seg)

    # Passenger
    pax_m = re.search(
        r"PASSENGER NAME.*?\n\s*(Mr|Mrs|Ms|Mstr|Dr|Miss)\.?\s+([A-Za-z\s]+?)(?:\s+CheckIn|\s+ADULT|\s+\d+|\n)",
        text, re.I
    )
    if pax_m:
        res["passengers"].append({
            "name": re.sub(r"\s+", " ", pax_m.group(2)).strip().title(),
            "title": pax_m.group(1).title(),
            "type": "Adult"
        })

    return res


