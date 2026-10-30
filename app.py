from flask import Flask, g, render_template, request, send_file, jsonify, render_template_string
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, black, white
from reportlab.platypus import Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode.code128 import Code128
import ast
import base64
import io
import json
import logging
import os
import re
import tempfile
import uuid
from html import escape
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import lru_cache
from openpyxl import Workbook, load_workbook
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

# Try to import CSRF protection, but make it optional
try:
    from flask_wtf.csrf import CSRFProtect
    CSRF_AVAILABLE = True
except ImportError:
    CSRF_AVAILABLE = False
    print("[WARN] flask_wtf not installed. CSRF protection disabled. Run: pip install flask-wtf")

# ─── Import comprehensive airport DB from CSV extraction ────────────
from airports_data import AIRPORTS_DB

# airports_data.py is generated from airport-codes.csv, whose "municipality" is
# the town an airport administratively sits in - not the name airlines print on
# a ticket. IXB came out as "Siliguri" when every carrier calls it Bagdogra.
# Corrections live here rather than in the generated file so regenerating it
# does not silently undo them.
# Printed at the foot of every ticket. Portal-specific lines from the source
# e-tickets are deliberately absent: "log on to the MyTrip section" names an
# agency portal that is not this one, and the partial-cancellation rule applies
# to a fare product this agency does not sell.
ADDITIONAL_INFORMATION = [
    "Use your airline PNR for all communication with the airline about this booking.",
    "Carry a printed copy of this e-ticket, or show it on your phone, at check-in and airport entry.",
    "Check-in opens 3 hours before departure and closes 60 minutes prior. Report at least 2 hours early.",
    "Web check-in is mandatory on the airline's website and closes 60 minutes before departure.",
    "Carry a valid government-issued photo ID. For infants, a date-of-birth certificate is mandatory.",
    "For international flights, a passport and the relevant visa and travel documents are mandatory.",
    "Reconfirm your departure time at least 24 hours before travel.",
    "Reconfirm the airport terminal before travel; it can change after booking.",
    "All times shown are local (24-hr format) at the relevant airport.",
    "Check your baggage allowance and the airline's requirements before heading to the airport.",
    "Power banks and portable chargers are allowed only in hand baggage, never in checked-in baggage.",
    "Checked-in baggage containing a power bank will not be loaded, for security reasons.",
    "On flights from Srinagar and Jammu, hand baggage is not permitted for security reasons.",
    "No meal is served on flights under 2 hours.",
    "Cancellation and reissue are subject to airline policies, which can change at any time.",
    "We are not responsible for airline delays, cancellations or missed connecting flights.",
    "Show your barcode at the airport entry gate; it covers every flight on this ticket.",
    "The PNR and ticket number are your proof of purchase. Keep this document until after travel.",
]

AIRPORT_CITY_OVERRIDES = {
    "IXB": "Bagdogra",      # municipality Siliguri
    "GOI": "Goa",           # municipality Vasco da Gama
    "GOX": "Goa",           # municipality Mopa
    "DMM": "Dammam",        # municipality Ad Dammam
    "MCT": "Muscat",        # municipality Muscat/Seeb
    "IXD": "Prayagraj",     # Allahabad renamed
    "IXG": "Belagavi",      # Belgaum renamed
    "SLV": "Shimla",        # municipality Jubbarhatti
    "KJB": "Kurnool",       # municipality Orvakal
    "RJA": "Rajahmundry",   # municipality Madhurapudi
    "SAG": "Shirdi",        # municipality Kakadi
    "SDW": "Sindhudurg",    # municipality Chipi
    "TCR": "Tuticorin",     # municipality Vagaikulam
    "DXN": "Noida",         # municipality Gautam Buddha Nagar
}

for _code, _entry in AIRPORTS_DB.items():
    _city = AIRPORT_CITY_OVERRIDES.get(_code)
    if _city:
        _entry["city"] = _city
    elif "(" in (_entry.get("city") or ""):
        # "Dehradun (Jauligrant)" - the bracketed locality is noise on a ticket.
        _entry["city"] = _entry["city"].split("(")[0].strip()
from booking_store import backend as booking_backend, get_next_booking_id, save_booking
from utils import generate_qr
from ticket_pdf import (
    ITINERARY_BASE_MM,
    PAX_LABEL_BASE_MM,
    TicketCanvas,
    draw_additional_info,
    draw_fares,
    draw_flight,
    draw_footer,
    draw_header,
    draw_layover,
    draw_passengers,
)
from ticket_parsing import (
    STANDARD_AIRLINES,
    STANDARD_AIRLINE_COLORS,
    looks_like_own_ticket,
    normalize_text,
    OCR_PAGE_TEXT_FLOOR,
    ocr_available,
    ocr_pdf_pages,
    parse_agency_ticket,
    ocr_passenger_names,
    parse_airindia_express_itinerary,
    parse_akasa_ticket,
    parse_boarding_pass,
    parse_cleartrip_ticket,
    parse_columnar_ticket,
    parse_makemytrip_ticket,
    parse_own_ticket,
    parse_paytm_ticket,
    parse_stacked_itinerary,
    normalize_baggage_label,
    parse_regional_ticket,
    parse_indigo_itinerary,
    parse_maha_or_b2b_portal,
)
from validation import (
    parse_money,
    validate_booking,
    validate_flights,
    validate_passengers,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("ticket")

MAX_UPLOAD_BYTES = 15 * 1024 * 1024

app = Flask(__name__)

DEV_SECRET_KEY = "dev-secret-key-change-me"
_secret_key = os.environ.get("SECRET_KEY", "").strip()
IS_PRODUCTION = os.environ.get("FLASK_ENV", "").lower() == "production" or bool(
    os.environ.get("RENDER") or os.environ.get("DYNO")
)
if not _secret_key:
    if IS_PRODUCTION:
        # Session cookies and CSRF tokens signed with a public constant are
        # forgeable by anyone who has read this repository.
        raise RuntimeError(
            "SECRET_KEY environment variable must be set in production. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    _secret_key = DEV_SECRET_KEY
    log.warning("SECRET_KEY not set - using the insecure development key.")
app.config["SECRET_KEY"] = _secret_key
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Initialize CSRF protection if available
if CSRF_AVAILABLE:
    csrf = CSRFProtect(app)
    app.config['WTF_CSRF_TIME_LIMIT'] = None  # No time limit on CSRF tokens
    print("[OK] CSRF protection enabled")
else:
    # Provide dummy csrf_token function for templates when CSRF not available
    @app.context_processor
    def inject_csrf_token():
        return dict(csrf_token=lambda: "")
    print("[WARN] CSRF protection disabled (flask_wtf not installed)")


def load_local_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_local_env()

# ─── Registered Agencies ────────────────────────────────────────────
AGENCIES = {
    "bharat_horizon": {
        "id": "bharat_horizon",
        "name": "BHARAT HORIZON TRAVELS",
        "tagline": "Your Trusted Travel Partner",
        "email": "ankit.gupta200392@gmail.com",
        "phone": "+91 7759069422",
        "address": "NEW ASHOK NAGAR, Delhi, India",
        "pincode": "110096",
        "initials": "BH",
    },
    "js_julfaqar": {
        "id": "js_julfaqar",
        "name": "JS JULFAQAR TRAVELS",
        "tagline": "Your Trusted Travel Partner",
        "email": "",
        "phone": "+91 9601074574",
        "address": "Plot No 309, Resma Nagar, Bhindi Bazar, Unn, Surat, Gujarat - 394210, India",
        "pincode": "394210",
        "initials": "JS",
    },
}
DEFAULT_AGENCY_ID = "js_julfaqar"
COMPANY = AGENCIES[DEFAULT_AGENCY_ID]

# ─── Airline Database (comprehensive) ───────────────────────────────
# Single source of truth lives in ticket_parsing.STANDARD_AIRLINES, so the
# parsers and the rest of the app always agree on which carriers exist.
AIRLINES = dict(STANDARD_AIRLINES)
AIRLINE_COLORS = dict(STANDARD_AIRLINE_COLORS)

# ICAO / common 3-letter aliases mapped to their IATA code.
# This lets entries like "AIX 123" resolve the same way as "IX 123".
AIRLINE_ALIASES = {
    # India
    "IGO": "6E", "AIC": "AI", "VTI": "UK", "SEJ": "SG", "GOW": "G8",
    "AKJ": "QP", "IAD": "I5", "AIX": "IX", "AXB": "IX", "SDG": "S5", "LLR": "9I",
    # Middle East
    "UAE": "EK", "ETD": "EY", "QTR": "QR", "FDB": "FZ", "OMA": "WY",
    "GFA": "GF", "SVA": "SV", "KAC": "KU", "RJA": "RJ", "MEA": "ME",
    "ABY": "G9", "FAD": "F3", "JZR": "J9", "OMS": "OV", "IYE": "IY",
    "ELY": "LY", "IRM": "W5", "IRA": "IR", "NAS": "XY",
    # Asia-Pacific
    "SIA": "SQ", "THA": "TG", "MAS": "MH", "GIA": "GA", "CPA": "CX",
    "PAL": "PR", "HVN": "VN", "KAL": "KE", "AAR": "OZ", "ANA": "NH",
    "JAL": "JL", "CAL": "CI", "EVA": "BR", "CSN": "CZ", "CES": "MU",
    "CCA": "CA", "CHH": "HU", "JSA": "3K", "TGW": "TR", "AXM": "AK",
    "AIQ": "FD", "AWQ": "QZ", "ALK": "UL", "RNA": "RA", "BBC": "BG",
    "PIA": "PK",
    "VJC": "VJ", "BAV": "QH", "BKP": "PG", "TLM": "SL", "NOK": "DD",
    "TVJ": "VZ", "LNI": "JT", "BTK": "ID", "MXD": "OD", "FFM": "FY",
    "RBA": "BI", "CEB": "5J", "APJ": "MM", "JJA": "7C", "JNA": "LJ",
    "TWB": "TW", "ABL": "BX", "CQH": "9C", "CXA": "MF", "CSZ": "ZH",
    "CSC": "3U", "CRK": "HX", "HKE": "UO", "JJP": "GK", "LAO": "QV",
    # South Asia
    "UBG": "BS", "DRK": "KB", "BHA": "U4", "NYT": "YT", "HIM": "H9",
    "DQA": "Q2", "ABQ": "PA",
    # Europe
    "BAW": "BA", "DLH": "LH", "AFR": "AF", "KLM": "KL", "SWR": "LX",
    "AUA": "OS", "ITY": "AZ", "IBE": "IB", "SAS": "SK", "FIN": "AY",
    "THY": "TK", "LOT": "LO", "TAP": "TP", "EIN": "EI", "BEL": "SN",
    "ROT": "RO", "ASL": "JU", "CTN": "OU", "RYR": "FR", "EZY": "U2",
    "WZZ": "W6", "VLG": "VY", "NAX": "DY",
    "VIR": "VS", "PGT": "PC", "SXS": "XQ", "AEE": "A3", "EWG": "EW",
    "TRA": "HV", "BTI": "BT", "ICE": "FI", "CSA": "OK", "LGL": "LG",
    "CFG": "DE", "EXS": "LS", "WIF": "WF", "AFL": "SU", "SBI": "S7",
    "SVR": "U6", "UTA": "UT", "KZR": "KC", "UZB": "HY", "AHY": "J2",
    "AUI": "PS",
    # Americas
    "AAL": "AA", "UAL": "UA", "DAL": "DL", "SWA": "WN", "JBU": "B6",
    "ASA": "AS", "NKS": "NK", "FFT": "F9", "HAL": "HA", "ACA": "AC",
    "AMX": "AM", "LAN": "LA", "AVA": "AV", "CMP": "CM", "GLO": "G3",
    "WJA": "WS", "POE": "PD", "TSC": "TS", "SCX": "SY", "VOI": "Y4",
    "VIV": "VB", "ARG": "AR", "AZU": "AD",
    # Oceania & Africa
    "QFA": "QF", "ANZ": "NZ", "VOZ": "VA", "JST": "JQ", "ETH": "ET",
    "SAA": "SA", "MSR": "MS", "KQA": "KQ", "RAM": "AT", "RWD": "WB",
    "FJI": "FJ", "ANG": "PX", "TAR": "TU", "DAH": "AH", "MAU": "MK",
    "SEY": "HM", "ATC": "TC", "DTA": "DT",
}


# Cache airline payload at module level to avoid repeated dictionary rebuilding
_AIRLINE_PAYLOAD_CACHE = None

def airline_payload():
    """Returns cached airline payload dictionary."""
    global _AIRLINE_PAYLOAD_CACHE
    
    if _AIRLINE_PAYLOAD_CACHE is not None:
        return _AIRLINE_PAYLOAD_CACHE
    
    payload = {
        code: {
            "name": name,
            "iata": code,
            "color": AIRLINE_COLORS.get(code, "#0F2742"),
        }
        for code, name in AIRLINES.items()
    }
    for alias, iata in AIRLINE_ALIASES.items():
        if iata in AIRLINES:
            payload[alias] = {
                "name": AIRLINES[iata],
                "iata": iata,
                "alias_of": iata,
                "color": AIRLINE_COLORS.get(iata, "#0F2742"),
            }
    
    _AIRLINE_PAYLOAD_CACHE = payload
    return payload


def extract_airline_code(flight_no):
    compact = re.sub(r"[^A-Z0-9]", "", (flight_no or "").upper())
    payload = airline_payload()
    for size in (3, 2):
        code = compact[:size]
        if code in payload:
            return payload[code]["iata"]
    return compact[:2]


MONTH_LOOKUP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

STOPWORDS_3 = {
    "THE", "FOR", "AND", "ARE", "NOT", "YOU", "YOUR", "THIS", "THAT", "WAS", "WITH",
    "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC", "MON", "TUE", "WED",
    "THU", "FRI", "SAT", "SUN", "NON", "ANY", "OUT", "DUE", "AIR", "PNR", "GST",
    "BAG", "PAY", "REF", "TAX", "FEE", "NET", "SRV", "MRN", "HRS", "APP",
    "OLD", "NEW", "WAY", "MMT", "FTI", "HAS", "WON", "END",
}


def normalize_time_token(token):
    if not token:
        return ""
    token = token.replace(".", ":").strip()
    parts = token.split(":")
    if len(parts) != 2:
        return ""
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return ""
    if hour > 23 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def parse_date_str(value):
    value = (value or "").strip()
    if not value:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        try:
            datetime.strptime(value, "%Y-%m-%d")
            return value
        except ValueError:
            return ""
    iso_match = re.search(r"\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})\b", value)
    if iso_match:
        try:
            return datetime(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    slash_match = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", value)
    if slash_match:
        day = int(slash_match.group(1))
        month = int(slash_match.group(2))
        year = int(slash_match.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    upper = value.upper()
    month_names = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
    # Separators vary across issuers: "11 Jul 2026", "11 Jul, 2026",
    # "11 July 2026", "11-Jul-2026" and the month-first "Jul 11, 2026".
    # (?!\d) stops a 4-digit year being split into a day and a 2-digit year,
    # which turned "32 Jan 2026" into 2026-01-20 instead of rejecting it.
    patterns = (
        # The year may be abbreviated with an apostrophe, as in "04 MAY '26".
        rf"\b(\d{{1,2}})(?:ST|ND|RD|TH)?(?!\d)[\s\-.]*({month_names})[A-Z]*[\s\-.,']*(\d{{2,4}})\b",
        rf"\b({month_names})[A-Z]*[\s\-.]+(\d{{1,2}})(?:ST|ND|RD|TH)?(?!\d)[\s\-.,']+(\d{{2,4}})\b",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, upper)
        if not match:
            continue
        if index == 0:
            day, month_abbr, year = match.group(1), match.group(2), match.group(3)
        else:
            month_abbr, day, year = match.group(1), match.group(2), match.group(3)
        month = MONTH_LOOKUP.get(month_abbr, 0)
        year = int(year)
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, int(day)).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def parse_datetime_local(value):
    """Read an <input type="datetime-local"> value, or None if unusable."""
    value = (value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


@lru_cache(maxsize=None)
def _iata_timezones():
    """IATA code -> IANA timezone. Empty when the dataset is unavailable."""
    try:
        import airportsdata

        return {code: entry["tz"] for code, entry in airportsdata.load("IATA").items()
                if entry.get("tz")}
    except Exception:
        log.warning("airportsdata unavailable - flight durations will assume one timezone.")
        return {}


def airport_timezone(code):
    return _iata_timezones().get((code or "").strip().upper())


def compute_duration(dep_raw, arr_raw, from_code="", to_code="", travel_date=""):
    """Flight time between two airports, as "2h 10m".

    Departure and arrival are quoted in each airport's own local time, so
    subtracting the clock times is only right when both share a timezone.
    Across timezones it is wrong by the offset between them — Mumbai to
    London read 4h 45m for a 9h 15m flight.
    """
    try:
        dep = datetime.strptime((dep_raw or "").strip(), "%H:%M")
        arr = datetime.strptime((arr_raw or "").strip(), "%H:%M")
    except ValueError:
        return ""

    from_tz, to_tz = airport_timezone(from_code), airport_timezone(to_code)
    if from_tz and to_tz and from_tz != to_tz:
        try:
            day = datetime.strptime((travel_date or "").strip(), "%Y-%m-%d").date()
        except ValueError:
            day = datetime.now().date()
        try:
            departure = datetime.combine(day, dep.time(), tzinfo=ZoneInfo(from_tz))
            arrival = datetime.combine(day, arr.time(), tzinfo=ZoneInfo(to_tz))
            while arrival <= departure:
                arrival += timedelta(days=1)
            minutes = int((arrival - departure).total_seconds() // 60)
            return f"{minutes // 60}h {minutes % 60}m"
        except Exception as exc:
            log.warning("Timezone-aware duration failed (%s); using clock difference.", exc)

    minutes = int((arr - dep).total_seconds() // 60) % (24 * 60)
    return f"{minutes // 60}h {minutes % 60}m"



def find_first_date(value):
    date = parse_date_str(value)
    return date


def find_times(value):
    times = []
    for match in re.finditer(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", value or ""):
        times.append(normalize_time_token(f"{match.group(1)}:{match.group(2)}"))
    return [t for t in times if t]


def is_valid_airport_code(code):
    return code in AIRPORTS_DB and code not in STOPWORDS_3


def is_potential_airport_code(code):
    return bool(re.fullmatch(r"[A-Z]{3}", code or "")) and code not in STOPWORDS_3


def detect_platform(text_upper, filename=""):
    haystack = f"{text_upper} {filename.upper()}"
    mapping = {
        "JS JULFAQAR": "JS JULFAQAR TRAVELS",
        "BHARAT HORIZON": "BHARAT HORIZON TRAVELS",
        "ANKIT TRAVELS": "ANKIT TRAVELS",
        "MAKEMYTRIP": "MakeMyTrip",
        "CLEARTRIP": "Cleartrip",
        "GOIBIBO": "Goibibo",
        "PAYTM": "Paytm",
        "GOINDIGO": "IndiGo",
        "INDIGO": "IndiGo",
        "AIR INDIA EXPRESS": "Air India Express",
        "AKASA": "Akasa Air",
        "SPICEJET": "SpiceJet",
        "FLIPKART": "Flipkart",
    }
    for key, name in mapping.items():
        if key in haystack:
            return name
    return ""


# Tesseract's page-segmentation modes disagree about what a ticket is. The
# "uniform block" mode this reads with first keeps table columns together,
# which is what the itinerary needs, but it will merge away a name sitting on
# its own in a card - and the default mode, which finds that name, scrambles
# the tables. Neither is right everywhere, so a reading that came up short is
# given a second opinion and only the gaps are filled in.
SECOND_OPINION_PSM = (3,)

def _second_opinion(data, extracted, filename):
    """Fill fields a scan did not yield by reading it again, segmented differently.

    This costs another OCR pass, so it only runs when something important is
    actually missing: a clean scan answers on the first reading and never pays
    for this.
    """
    missing = [key for key in ("pnr", "booking_id") if not extracted.get(key)]
    if not extracted.get("passengers"):
        missing.append("passengers")
    if not extracted.get("flights"):
        missing.append("flights")
    if not missing:
        return extracted

    log.info("First reading of %s left %s empty - trying another segmentation.",
             filename, ", ".join(missing))
    try:
        pages = ocr_pdf_pages(
            data, is_airport=is_valid_airport_code,
            is_airline=lambda code: code in airline_payload(),
            psm_order=SECOND_OPINION_PSM)
    except Exception:
        log.exception("Second reading failed; keeping the first.")
        return extracted
    if not pages:
        return extracted

    second = extract_ticket_fields(
        "\n".join(page["text"] for page in pages), filename=filename,
        pages_words=[page["words"] for page in pages])
    filled = []
    for key in missing:
        if second.get(key) and not extracted.get(key):
            extracted[key] = second[key]
            filled.append(key)
    if filled:
        log.info("Second reading of %s recovered %s.", filename, ", ".join(filled))
    return extracted


def extract_ticket_with_gemini_vision(pdf_bytes, filename=""):
    """Parse scanned / photographed ticket PDF pages via Gemini Vision."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        import pypdfium2 as pdfium
        import urllib.request
        import base64
        doc = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
        page_imgs = []
        for i in range(min(2, len(doc))):
            buf = io.BytesIO()
            doc[i].render(scale=1.5).to_pil().save(buf, format="JPEG", quality=80)
            page_imgs.append(base64.b64encode(buf.getvalue()).decode("ascii"))

        prompt = (
            "Extract all flight ticket details from this image as JSON with exact keys: "
            "pnr, booking_id, booking_platform, booking_date (YYYY-MM-DD), customer_email, customer_phone, "
            "base_fare, taxes_fees, total_fare, discount, refund_status, fare_type, "
            "flights: list of {flight_no, airline, from_code (3-letter IATA), to_code (3-letter IATA), "
            "date (YYYY-MM-DD), dep_time_raw (HH:MM), arr_time_raw (HH:MM), class, from_terminal, to_terminal, "
            "checkin_bag, hand_bag}, "
            "passengers: list of {title, name, type, seats, passport, dob, doe}. "
            "Return ONLY valid JSON."
        )
        parts = [{"text": prompt}]
        for b64 in page_imgs:
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})

        models = ["gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-flash-lite"]
        for m in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": parts}],
                "generationConfig": {"response_mime_type": "application/json"}
            }
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    cand_text = resp_data["candidates"][0]["content"]["parts"][0]["text"]
                    parsed = json.loads(cand_text)
                    if parsed and (parsed.get("pnr") or parsed.get("flights")):
                        log.info("Gemini vision successfully parsed %s with model %s", filename, m)
                        if parsed.get("discount"):
                            parsed["discount"] = str(parsed["discount"]).replace("-", "").strip()
                        return parsed
            except Exception as exc:
                log.warning("Gemini vision attempt with %s failed: %s", m, exc)
                continue
    except Exception as exc:
        log.warning("Gemini vision extraction failed for %s: %s", filename, exc)
    return None


def extract_ticket_fields(text, filename="", pages_words=None):
    """Read a ticket, repairing scanned passenger names from the word layout."""
    result = _extract_ticket_fields(text, filename=filename, pages_words=pages_words)

    # Only OCR supplies a per-word confidence, so this is how a recognised page
    # is told from a digital one. A name cell is the part of a scanned row that
    # the text patterns lose first, and the word positions still hold it.
    from_ocr = any(word.get("conf") is not None
                   for page in (pages_words or []) for word in page[:1])
    if result and from_ocr:
        try:
            geometric = ocr_passenger_names(pages_words)
        except Exception:
            log.exception("Geometric passenger read failed; keeping the text result.")
            geometric = []
        if geometric:
            existing = result.get("passengers") or []
            for index, person in enumerate(geometric):
                if index < len(existing):
                    # Keep whatever else the row yielded (seat, meal, baggage).
                    existing[index]["name"] = person["name"]
                else:
                    existing.append(person)
            result["passengers"] = existing
    return result


def _extract_ticket_fields(text, filename="", pages_words=None):
    # Typographic dashes and currency signs are common in issuer PDFs and stop
    # every ASCII pattern below from matching.
    text = normalize_text(text or "")
    text_upper = text.upper()
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    platform = detect_platform(text_upper, filename)

    # MakeMyTrip and Goibibo tickets
    if "MAKEMYTRIP" in text_upper or "MAKEMY TRIP" in text_upper or "GOIBIBO" in text_upper or "BARCODE(S) FOR YOUR JOURNEY" in text_upper or ("BOOKING DETAILS" in text_upper and "TRAVELLER SEAT MEAL" in text_upper):
        mmt = parse_makemytrip_ticket(text)
        if mmt and (mmt.get("pnr") or mmt.get("flights")):
            mmt["booking_platform"] = platform or mmt.get("booking_platform") or "MakeMyTrip"
            return mmt

    # Paytm flight e-tickets
    if "PAYTM" in text_upper or "ONE97" in text_upper or "ONWARD FLIGHT PNR" in text_upper:
        py = parse_paytm_ticket(text)
        if py and (py.get("pnr") or py.get("flights")):
            py["booking_platform"] = platform or "Paytm"
            return py

    # Cleartrip (standard Trip ID and B2B white-label agent tickets)
    if "CLEARTRIP" in text_upper or "REFERENCE ID CT" in text_upper or re.search(r"\bCT\d{7,}\b", text_upper) or "TRIP ID" in text_upper:
        ct = parse_cleartrip_ticket(text)
        if ct and (ct.get("pnr") or ct.get("flights")):
            ct["booking_platform"] = platform or "Cleartrip"
            return ct

    # Akasa Air direct tickets
    if "AKASA" in text_upper or "QP " in text_upper or "BOOKING REFERENCE/PNR" in text_upper:
        ak = parse_akasa_ticket(text)
        if ak and (ak.get("pnr") or ak.get("flights")):
            ak["booking_platform"] = platform or "Akasa Air"
            return ak

    # Air India Express official itineraries
    if "OPERATED BY AIR INDIA EXPRESS" in text_upper or ("ITINERARY GENERATED ON" in text_upper and "AIR INDIA EXPRESS" in text_upper):
        aix = parse_airindia_express_itinerary(text)
        if aix and (aix.get("pnr") or aix.get("flights")):
            aix["booking_platform"] = platform or "Air India Express"
            return aix

    # Regional / Heliport tickets (e.g. Heritage Aviation)
    if "GAULAPAR" in text_upper or "NAINI SAINI" in text_upper or "HERITAGE AVIATION" in text_upper:
        reg = parse_regional_ticket(text)
        if reg and (reg.get("pnr") or reg.get("flights")):
            reg["booking_platform"] = platform or "Direct/Walk-in"
            return reg

    # IndiGo official multi-leg itineraries
    if "GOINDIGO" in text_upper or "6E ADD-ONS" in text_upper or ("PNR/BOOKING REFERENCE" in text_upper and "6E " in text_upper):
        ig = parse_indigo_itinerary(text)
        if ig and (ig.get("pnr") or ig.get("flights")):
            ig["booking_platform"] = platform or "IndiGo"
            return ig

    # Boarding passes
    if (("BOARDING PASS" in text_upper or "BOARDINGPASS" in text_upper) or ("BOARDING TIME" in text_upper and "GATE" in text_upper)) and "ITINERARY" not in text_upper and any(k in text_upper for k in ("GATE", "SEQUENCE", "ZONE", "SEAT")):
        bp = parse_boarding_pass(text)
        if bp and (bp.get("pnr") or bp.get("flights")):
            bp["booking_platform"] = platform or bp.get("booking_platform") or "Direct"
            return bp

    # B2B portal / MAHA tickets (Booking Reference F-O..., TRAVELERS DETAILS, AIRLINE PNR)
    if "TRAVELERS DETAILS" in text_upper or ("BOOKING REFERENCE" in text_upper and "AIRLINE PNR" in text_upper) or "MAHA" in text_upper:
        maha = parse_maha_or_b2b_portal(text)
        if maha and (maha.get("pnr") or maha.get("flights")):
            maha["booking_platform"] = platform or "Direct/Walk-in"
            return maha

    # Some agency tickets lay the itinerary out as a real table. Flattened to
    # text those columns interleave, so they are read from word positions
    # instead - but only when that table is actually present.
    if pages_words and not looks_like_own_ticket(text_upper):
        try:
            columnar = parse_columnar_ticket(pages_words)
        except Exception:
            log.exception("Columnar parse failed; falling back to the text parsers.")
            columnar = {}
        if columnar.get("flights") and (columnar.get("pnr") or columnar.get("booking_id")):
            columnar["booking_platform"] = platform or "Direct/Walk-in"
            columnar.setdefault("customer_email", "")
            columnar.setdefault("customer_phone", "")
            columnar.setdefault("base_fare", "")
            columnar.setdefault("taxes_fees", "")
            return columnar

    # Tickets this app generated have a known layout, so read them directly
    # rather than putting them through the heuristics meant for OTA formats.
    if looks_like_own_ticket(text_upper):
        own = parse_own_ticket(text)
        if own and (own.get("pnr") or own.get("flights")):
            own["booking_platform"] = own.get("booking_platform") or platform or "Direct/Walk-in"
            return own

    # Agency-issued tickets carry no OTA branding but follow a recognisable
    # flight-table shape, so try that before the OTA heuristics give up.
    if not platform:
        agency = parse_agency_ticket(text)
        if agency and agency.get("flights") and (agency.get("pnr") or agency.get("booking_id")):
            agency["booking_platform"] = agency.get("booking_platform") or "Direct/Walk-in"
            return agency

    if not platform and not any(token in text_upper for token in ["BOOKING", "TICKET", "PNR", "TRAVELLER", "TRAVELER", "BOARDING"]):
        return {
            "booking_platform": "",
            "pnr": "",
            "booking_id": "",
            "booking_date": "",
            "customer_email": "",
            "customer_phone": "",
            "base_fare": "",
            "taxes_fees": "",
            "total_fare": "",
            "discount": "",
            "flights": [],
            "passengers": [],
        }

    def first_group(patterns, source=text_upper, require_digit=False):
        for pat in patterns:
            match = re.search(pat, source, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if require_digit and not re.search(r"\d", value):
                    continue
                if len(value) < 5:
                    continue
                return value
        return ""

    # Airline PNRs are frequently all letters (YDKKHA), so a digit cannot be
    # required; guard against matching the neighbouring words instead.
    PNR_NOISE = {
        "ETICKET", "TICKET", "NUMBER", "STATUS", "AIRLINE", "FLIGHT", "BOOKING",
        "DETAILS", "SECTOR", "SEATNO", "REFERENCE", "CONFIRM", "CONFIRMED",
        # Passenger-type words sit in the same row as the PNR on many tickets.
        "ADULT", "CHILD", "INFANT", "ADULTS", "SENIOR", "ECONOMY", "BUSINESS",
        "BOOKED", "PASSENGER", "BARCODE", "WHERE", "WHICH", "TRAVEL", "SINGLE",
        "ONLINE", "AIRPORT", "PERSON", "SPECIAL", "OPTION",
    }

    def looks_like_pnr(value):
        value = (value or "").strip().upper()
        return bool(re.fullmatch(r"[A-Z0-9]{5,8}", value)) and value not in PNR_NOISE

    pnr = ""
    # [ \t] rather than \s: allowing newlines matched "PNR" at the end of one
    # line against an unrelated word on the next.
    for pattern in (
        r"\bPNR\b[ \t]*(?:NO\.?|NUMBER)?[ \t]*[:\-]?[ \t]*([A-Z0-9]{5,8})\b",
        r"\bBOOKING[ \t]*REF(?:ERENCE)?\b[ \t]*[:\-]?[ \t]*([A-Z0-9]{5,10})\b",
    ):
        match = re.search(pattern, text_upper)
        if match and looks_like_pnr(match.group(1)):
            pnr = match.group(1).strip()
            break

    # Column layouts print the value on the row beneath its heading. Require a
    # real heading: "PNR" alone is also an airport code (Pointe Noire), so it
    # appears in reference tables that are not tickets at all.
    if not pnr:
        heading_words = ("PASSENGER", "NAME", "TICKET", "SEAT", "STATUS", "AIRLINE")
        for idx, line in enumerate(lines):
            upper_line = line.upper()
            if not re.search(r"\bPNR\b", upper_line):
                continue
            if sum(1 for word in heading_words if word in upper_line) < 2:
                continue
            for following in lines[idx + 1:idx + 3]:
                # Drop a row index and any "Mr Firstname Lastname" so the
                # traveller's surname is not mistaken for the reference.
                stripped = re.sub(r"^\s*\d{1,2}[.)]\s*", "", following.upper())
                stripped = re.sub(r"\b(MR|MRS|MS|MISS|MSTR)\.?(\s+[A-Z][A-Z']*)+", " ", stripped)
                tokens = [t for t in re.findall(r"\b[A-Z0-9]{5,8}\b", stripped)
                          if looks_like_pnr(t)]
                if tokens:
                    pnr = tokens[-1]
                    break
            if pnr:
                break
    if pnr and pnr.upper() in ("WEBSITE", "AIRLINE", "TICKET", "ONLINE", "FLIGHT", "PORTAL"):
        pnr = ""

    booking_id = first_group([
        r"\bBOOKING\s*ID\b\s*[:\-]?\s*([A-Z0-9\-]{5,20})",
        r"\bTRIP\s*ID\b\s*[:\-]?\s*([A-Z0-9\-]{5,20})",
        r"\bORDER\s*ID\b\s*[:\-]?\s*([A-Z0-9\-]{5,20})",
        r"\bTRANSACTION\s*ID\b\s*[:\-]?\s*([A-Z0-9\-]{5,20})",
    ], require_digit=True)

    # Same column-heading layout as the PNR above.
    if not booking_id:
        for idx, line in enumerate(lines):
            if not re.search(r"\bBOOKING\s*(ID|REF|NO)\b", line, re.I):
                continue
            if re.search(r"\bBOOKING\s*(ID|REF|NO)\b[^A-Z0-9]*[A-Z0-9]{5,}", line, re.I):
                continue
            for following in lines[idx + 1:idx + 4]:
                token = re.fullmatch(r"([A-Z0-9][A-Z0-9\-]{4,19})", following.strip().upper())
                if token:
                    booking_id = token.group(1)
                    break
            if booking_id:
                break

    booking_date = ""
    booking_date_match = re.search(r"(BOOKING DATE|BOOKED ON|DATE OF BOOKING|ISSUE DATE)\s*[:\-]?\s*([0-9A-Za-z /-]{6,})", text_upper)
    if booking_date_match:
        booking_date = parse_date_str(booking_date_match.group(2))

    if platform == "Paytm":
        for idx, line in enumerate(lines):
            upper = line.upper()
            if "FLIGHT PNR" in upper and idx + 1 < len(lines):
                next_line = lines[idx + 1].upper()
                tokens = re.findall(r"[A-Z0-9]{5,8}", next_line)
                if tokens:
                    pnr = tokens[-1]
            if "BOOKED ON" in upper and not booking_date:
                booking_date = parse_date_str(line)

    if platform == "MakeMyTrip" and not booking_date:
        match = re.search(r"BOOKED ON\s+(\d{1,2}\s+[A-Z]{3}\s+\d{4})", text_upper)
        if match:
            booking_date = parse_date_str(match.group(1))

    # The first address on a ticket is nearly always the issuer's support desk,
    # so a mailbox that reads like one is skipped rather than handed back as
    # the traveller's.
    emails = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text_upper, flags=re.IGNORECASE)
    customer_email = ""
    for candidate in emails:
        local = candidate.split("@", 1)[0].lower()
        if any(token in local for token in SUPPORT_MAILBOXES):
            continue
        customer_email = candidate
        break

    # Any run of ten-plus digits used to qualify, so booking references
    # (260427555096) and issuer helplines (0124-4628747) were handed back as
    # the customer's number. Accept only a shape a mobile actually takes.
    phone = ""
    reference_digits = {re.sub(r"\D", "", value) for value in (booking_id, pnr) if value}
    reference_digits.discard("")
    for match in re.finditer(r"\+?\d[\d\s\-]{8,}\d", text):
        candidate = match.group(0)
        if ":" in candidate:
            continue
        # An issuer's helpline has the same shape as a mobile, so the words
        # introducing it are what tell them apart.
        lead = text[max(0, match.start() - 60):match.start()].upper()
        if any(word in lead for word in HELPLINE_LEAD_INS):
            continue
        digits = re.sub(r"\D", "", candidate)
        if any(digits == ref or (ref and ref in digits) for ref in reference_digits):
            continue
        plus = candidate.strip().startswith("+")
        mobile = (
            re.fullmatch(r"[6-9]\d{9}", digits)
            or re.fullmatch(r"0[6-9]\d{9}", digits)
            or re.fullmatch(r"91[6-9]\d{9}", digits)
        )
        if mobile or (plus and 10 <= len(digits) <= 15):
            phone = candidate.strip()
            break

    total_fare = ""
    total_match = re.search(r"\b(TOTAL(?: AMOUNT)?|GRAND TOTAL|AMOUNT PAID|TOTAL FARE)\b[^0-9]{0,15}([0-9,]+(?:\.\d{1,2})?)", text_upper)
    if total_match:
        total_fare = total_match.group(2).replace(",", "")

    base_fare = ""
    base_match = re.search(r"\b(BASE FARE|FARE)\b[^0-9]{0,12}([0-9,]+(?:\.\d{1,2})?)", text_upper)
    if base_match:
        base_fare = base_match.group(2).replace(",", "")

    taxes_fees = ""
    tax_match = re.search(r"\b(TAX|TAXES|FEES|TAXES AND FEES)\b[^0-9]{0,12}([0-9,]+(?:\.\d{1,2})?)", text_upper)
    if tax_match:
        taxes_fees = tax_match.group(2).replace(",", "")

    # A discount prints as a negative because it is subtracted from the line
    # above it. The form field is unsigned and /generate subtracts it again, so
    # the sign is dropped to keep it from being added back to the total.
    discount = ""
    discount_match = re.search(r"\bDISCOUNT\b[^0-9-]{0,12}(-?[0-9,]+(?:\.\d{1,2})?)", text_upper)
    if discount_match:
        discount = discount_match.group(1).replace(",", "").lstrip("-")

    passengers = []
    if platform or pnr or booking_id or ("TRAVELLER" in text_upper or "PASSENGER" in text_upper):
        excl_keywords = [
            "IMPORTANT", "PLEASE", "CARRY", "SECURITY", "CHECK-IN", "BOOKING", "ITINERARY",
            "E-TICKET", "TRIP", "ROUTE", "SEGMENT", "FARE", "PAYMENT", "AMOUNT",
            "BHARAT", "HORIZON", "TRAVELS", "ANKIT",
        ]
        for line in lines:
            upper = line.upper()
            if any(re.search(r"\b" + kw + r"\b", upper) for kw in excl_keywords):
                continue
            # Rows are often numbered ("1. Ms Alka Agarwal, YDKKHA"), so drop a
            # leading index before rejecting the line for containing digits.
            candidate = re.sub(r"^\s*\d{1,2}[.)]\s*", "", upper)
            match = re.search(
                r"\b(MR|MRS|MS|MISS|MSTR|CHD|INF)\.?\s+([A-Z][A-Z\s']{2,40}?)"
                r"(?=\s*[,|]|\s{2,}|\s+\d|\s*\(|\s*-|$)", candidate)
            if match:
                title = match.group(1).capitalize()
                raw_name = match.group(2)
                # Strip trailing type markers (Adult, Male, Female, etc.)
                raw_name = re.sub(r"\b(ADULT|CHILD|INFANT|MALE|FEMALE)\b.*$", "", raw_name, flags=re.I)
                name = re.sub(r"\s{2,}", " ", raw_name).strip()
                if len(name.split()) < 2:
                    continue
                if name and all(word not in STOPWORDS_3 for word in name.split()):
                    title_name = name.title()
                    if title_name not in [p["name"] for p in passengers]:
                        passengers.append({"name": title_name, "title": title})

    # Booking references are often UUIDs, and a chunk like "...-fd86-..." looks
    # exactly like a flight number, so drop them before scanning.
    flight_scan_text = re.sub(
        r"\b[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\b",
        " ", text_upper)

    flight_numbers = []
    for match in re.finditer(r"\b([A-Z]{2,3})\s?-?\s?(\d{2,4})\b", flight_scan_text):
        code = match.group(1)
        number = match.group(2)
        iata = AIRLINE_ALIASES.get(code, code)
        if iata in AIRLINES:
            flight_no = f"{iata} {number}"
            if flight_no not in flight_numbers:
                flight_numbers.append(flight_no)

    segments = []
    for idx, line in enumerate(lines):
        upper = line.upper()
        combined = upper
        if idx + 1 < len(lines):
            combined = f"{upper} {lines[idx + 1].upper()}"
        flight_line = re.search(r"\b([A-Z]{2,3})\s?-?\s?(\d{3,4})\b", upper)
        route_time = re.search(
            r"\b([A-Z]{3})\s+([0-2]\d[:.][0-5]\d)\b.*?\b([0-2]\d[:.][0-5]\d)\b\s+([A-Z]{3})\b",
            upper,
        )
        if not route_time:
            route_time = re.search(
                r"\b([A-Z]{3})\s+([0-2]\d[:.][0-5]\d)\b.*?\b([0-2]\d[:.][0-5]\d)\b\s+([A-Z]{3})\b",
                combined,
            )
        if not flight_line:
            flight_line = re.search(r"\b([A-Z]{2,3})\s?-?\s?(\d{3,4})\b", combined)
        if flight_line and route_time:
            if flight_line.group(2).startswith("0"):
                continue
            from_code = route_time.group(1)
            to_code = route_time.group(4)
            if is_potential_airport_code(from_code) and is_potential_airport_code(to_code):
                seg = {
                    "from_code": from_code,
                    "to_code": to_code,
                    "dep_time_raw": normalize_time_token(route_time.group(2)),
                    "arr_time_raw": normalize_time_token(route_time.group(3)),
                    "flight_no": f"{flight_line.group(1)} {flight_line.group(2)}",
                    "airline": AIRLINES.get(extract_airline_code(f"{flight_line.group(1)} {flight_line.group(2)}"), ""),
                }
                # The travel date usually sits on the row under the times.
                window = "\n".join(lines[idx:idx + 3])
                seg_date = find_first_date(window)
                if seg_date:
                    seg["date"] = seg_date
                segments.append(seg)
                continue
        if not any(k in upper for k in ["FROM", "TO", "DEPART", "ARRIV", "FLIGHT", "SECTOR", "ROUTE", "PNR"]):
            if not re.search(r"\b[A-Z]{2,3}\s?\d{2,4}\b", upper):
                continue
        route_match = re.search(r"\b([A-Z]{3})\s*[-–]\s*([A-Z]{3})\b", upper)
        from_code = to_code = ""
        if route_match:
            from_code, to_code = route_match.group(1), route_match.group(2)
        else:
            from_to = re.search(r"\bFROM\b[^A-Z]{0,10}([A-Z]{3})\b.*\bTO\b[^A-Z]{0,10}([A-Z]{3})\b", upper)
            if from_to:
                from_code, to_code = from_to.group(1), from_to.group(2)
        if not (is_valid_airport_code(from_code) and is_valid_airport_code(to_code)):
            codes = [c for c in re.findall(r"\b[A-Z]{3}\b", upper) if is_valid_airport_code(c)]
            if len(codes) >= 2:
                from_code, to_code = codes[0], codes[1]
        if not (is_valid_airport_code(from_code) and is_valid_airport_code(to_code)):
            continue
        if from_code == to_code:
            # A single code repeated in prose is not a leg.
            continue
        seg = {"from_code": from_code, "to_code": to_code}
        # The date is often printed on the row below the route, so widen the
        # search past the current line when it is not on it.
        date_iso = find_first_date(upper) or find_first_date("\n".join(lines[idx:idx + 3]))
        times = find_times(upper)
        if date_iso:
            seg["date"] = date_iso
        if times:
            seg["dep_time_raw"] = times[0]
            if len(times) > 1:
                seg["arr_time_raw"] = times[1]
        segments.append(seg)

    # A page of airport codes yields plenty of "routes" that carry no time,
    # date or flight number. Real legs have at least one of those.
    if not platform:
        substantive = [
            seg for seg in segments
            if seg.get("dep_time_raw") or seg.get("date") or seg.get("flight_no")
        ]
        if not substantive:
            segments = []
        elif len(segments) > 6:
            segments = substantive

    # Collapse segments describing the same leg. The flight number is
    # deliberately not part of the key: the same leg is often matched twice,
    # once with a flight number and once without, and keying on it kept both.
    if segments:
        merged = {}
        order = []
        for seg in segments:
            key = (
                seg.get("from_code", ""),
                seg.get("to_code", ""),
                seg.get("dep_time_raw", ""),
                seg.get("arr_time_raw", ""),
            )
            if key not in merged:
                merged[key] = dict(seg)
                order.append(key)
            else:
                # Keep whichever copy carries more detail.
                for field, value in seg.items():
                    if value and not merged[key].get(field):
                        merged[key][field] = value
        segments = [merged[k] for k in order]

    # Compact tickets stack the route, times and date on bare lines with no
    # labels, so nothing above matches them.
    if not any(seg.get("from_code") for seg in segments):
        stacked = parse_stacked_itinerary(
            text, is_valid_airport_code, lambda code: code in airline_payload())
        if stacked:
            segments = stacked
            flight_numbers = []

    if not segments and flight_numbers:
        segments = [{} for _ in flight_numbers]

    # Only attach loose flight numbers when the counts line up. Otherwise the
    # extras are stray text that merely looks like a flight code (an address
    # or a time), and pairing them with a leg invents a flight that is not on
    # the ticket.
    if len(flight_numbers) == len(segments):
        for seg, flight_no in zip(segments, flight_numbers):
            if not seg.get("flight_no"):
                seg["flight_no"] = flight_no
                seg["airline"] = AIRLINES.get(extract_airline_code(flight_no), "")

    fallback_cbag = ""
    fallback_hbag = ""
    c_m = re.search(r"Check[- ]?in(?:\s*Baggage)?\s*[:\s]?\s*(\d{1,2}\s*kgs?(?:\s*\([^\)]+\))?|\d{1,2}\s*KG|Not Allowed|No Baggage|Nil|None)", text, re.I)
    if c_m:
        fallback_cbag = normalize_baggage_label(c_m.group(1), is_checkin=True)
    h_m = re.search(r"(?:Cabin|Hand)(?:\s*Baggage)?\s*[:\s]?\s*(\d{1,2}\s*kgs?(?:\s*\([^\)]+\))?|\d{1,2}\s*KG|Not Allowed|No Baggage|Nil|None)", text, re.I)
    if h_m:
        fallback_hbag = normalize_baggage_label(h_m.group(1), is_checkin=False)

    for seg in segments:
        if not seg.get("checkin_bag") and fallback_cbag:
            seg["checkin_bag"] = fallback_cbag
        if not seg.get("hand_bag") and fallback_hbag:
            seg["hand_bag"] = fallback_hbag
        fc = seg.get("from_code", "")
        tc = seg.get("to_code", "")
        is_intl = any(c in ("SHJ", "DXB", "DMM", "RUH", "JED", "KWI", "MCT", "DOH", "SIN", "BKK") for c in (fc, tc))
        if not seg.get("checkin_bag"):
            seg["checkin_bag"] = "30 KG (1 Piece)" if is_intl else "15 KG (1 Piece)"
        if not seg.get("hand_bag"):
            seg["hand_bag"] = "7 KG (1 Piece)"
        if not seg.get("date") and booking_date:
            seg["date"] = booking_date

    return {
        "booking_platform": platform,
        "pnr": pnr,
        "booking_id": booking_id,
        "booking_date": booking_date,
        "customer_email": customer_email,
        "customer_phone": phone,
        "base_fare": base_fare,
        "taxes_fees": taxes_fees,
        "total_fare": total_fare,
        "discount": discount,
        "flights": segments,
        "passengers": passengers,
    }



@app.route("/")
def index():
    # The page used to hardcode the trading name and a "BT" monogram while the
    # PDF derived both from COMPANY, so renaming the agency changed the ticket
    # but not the site it was issued from.
    initials = COMPANY.get("initials") or "".join(w[0] for w in re.findall(r"[A-Za-z]+", COMPANY["name"]))[:2].upper()
    return render_template(
        "index.html",
        fare_types=FARE_TYPES,
        company=COMPANY,
        agencies=AGENCIES,
        current_agency_id=DEFAULT_AGENCY_ID,
        initials=initials or "BH",
    )


@app.route("/api/search-airports", methods=["GET"])
def search_airports():
    """Optimized airport search with single-pass algorithm."""
    query = request.args.get("q", "").strip().upper()
    if len(query) < 1:
        return jsonify([])

    results = []
    max_results = 15

    # Single pass through database with priority scoring
    for code, info in AIRPORTS_DB.items():
        priority = None
        
        if code == query:
            priority = 0  # Exact match - highest
        elif code.startswith(query):
            priority = 1  # Code starts with query
        elif info["city"].upper().startswith(query):
            priority = 2  # City starts with query
        elif query in info["airport"].upper():
            priority = 3  # Query found in airport name
        
        if priority is not None:
            results.append((priority, code, info))
        
        # Early exit if we have enough high-priority results
        if len(results) >= max_results * 2 and priority and priority > 1:
            break
    
    # Sort by priority and convert to JSON format
    results.sort(key=lambda x: x[0])
    
    output = []
    for priority, code, info in results[:max_results]:
        output.append({
            "code": code,
            "city": info["city"],
            "airport": info["airport"],
            "country": info.get("country", ""),
            # Lets the page tell whether a route crosses timezones, and so
            # whether its clock-difference estimate would be misleading.
            "tz": airport_timezone(code) or "",
            "priority": priority
        })
    
    return jsonify(output)


@app.route("/api/airport-info", methods=["POST"])
def airport_info():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip().upper()
    if not code:
        return jsonify({"found": False, "error": "A 'code' field is required."}), 400
    info = AIRPORTS_DB.get(code)
    if info:
        return jsonify({"found": True, "city": info["city"], "airport": info["airport"], "country": info.get("country", "")})
    return jsonify({"found": False})


@app.route("/api/airlines", methods=["GET"])
def get_airlines():
    return jsonify(airline_payload())


@app.route("/api/health", methods=["GET"])
def health():
    """Surface optional-feature availability, so a missing tesseract binary or
    an unreachable database can be spotted without issuing a ticket to find
    out. "store": "spreadsheet" on a host without a disk means recorded
    bookings will not survive the next deploy."""
    return jsonify({
        "status": "ok",
        "airports": len(AIRPORTS_DB),
        "airlines": len(airline_payload()),
        "ocr": ocr_available(),
        "store": booking_backend(),
    })


@app.route("/api/next_booking_id", methods=["GET"])
def next_booking_id():
    platform = request.args.get("platform", "AT").strip().upper()
    platform_code = "".join(ch for ch in platform if ch.isalnum())[:6] or "AT"
    return jsonify({"booking_id": get_next_booking_id(platform_code)})


def _parse_single_pdf_bytes(data, filename):
    try:
        import pdfplumber
    except ImportError:
        return {"error": "pdfplumber is not installed. Run: pip install -r requirements.txt"}, 500

    try:
        pages_words = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
            pages_words = [page.extract_words() or [] for page in pdf.pages]
        text = "\n".join(pages).strip()
    except Exception as exc:
        log.error("Failed to read PDF: %s", exc)
        return {"error": f"Failed to read PDF {filename}: {exc}"}, 500

    used_ocr = False
    ocr_conf = None
    blank_pages = {i for i, page in enumerate(pages) if len(page) < OCR_PAGE_TEXT_FLOOR}
    if not pages or blank_pages:
        log.info("%d of %d pages in %s carry no readable text - trying OCR.",
                 len(blank_pages), len(pages) or 1, filename)
        ocr_pages = ocr_pdf_pages(
            data, is_airport=is_valid_airport_code,
            is_airline=lambda code: code in airline_payload(),
            only_pages=blank_pages or None)
        if ocr_pages:
            merged, merged_words = list(pages), list(pages_words)
            for page in ocr_pages:
                i = page["index"]
                if i < len(merged) and len(merged[i]) >= max(len(page["text"]), OCR_PAGE_TEXT_FLOOR):
                    continue
                while len(merged) <= i:
                    merged.append("")
                    merged_words.append([])
                merged[i], merged_words[i] = page["text"], page["words"]
                used_ocr = True
            if used_ocr:
                text = "\n".join(merged).strip()
                pages, pages_words = merged, merged_words
                ocr_conf = round(sum(p["conf"] for p in ocr_pages) / len(ocr_pages), 1)

    if len(text) < 50:
        vision_res = extract_ticket_with_gemini_vision(data, filename=filename)
        if vision_res:
            if not vision_res.get("booking_platform"):
                vision_res["booking_platform"] = "Flipkart" if "FLIPKART" in filename.upper() else "Direct/Walk-in"
            for f in vision_res.get("flights", []):
                if f.get("date"):
                    f["date"] = parse_date_str(f["date"])
                if f.get("flight_no"):
                    f["flight_no"] = re.sub(r"[-_]+", " ", f["flight_no"]).strip()
                if not f.get("airline") and f.get("flight_no"):
                    f["airline"] = AIRLINES.get(extract_airline_code(f["flight_no"]), "")
                if not f.get("checkin_bag"):
                    f["checkin_bag"] = "15 KG (1 Piece)"
                if not f.get("hand_bag"):
                    f["hand_bag"] = "7 KG (1 Piece)"
            return vision_res, 200

        if not ocr_available():
            return {
                "error": f"Scanned ticket {filename} and OCR is unavailable on server.",
            }, 422
        return {
            "error": f"Scanned ticket {filename} could not be read.",
        }, 422

    extracted = extract_ticket_fields(text, filename=filename, pages_words=pages_words)
    if used_ocr:
        extracted = _second_opinion(data, extracted, filename)
        extracted["used_ocr"] = True
        extracted["ocr_confidence"] = ocr_conf

    extracted["raw_excerpt"] = text[:1200]

    # Barcode
    bc_path, bc_data = extract_barcode_from_pdf_bytes(data)
    if not bc_path and extracted.get("pnr"):
        bc_path, bc_data = generate_code128_barcode(extracted.get("pnr"))
    if bc_path:
        extracted["barcode_token"] = bc_path
        extracted["barcode_data"] = bc_data

    return extracted, 200


@app.route("/api/parse-ticket", methods=["POST"])
def parse_ticket():
    files = request.files.getlist("ticket_pdf")
    if not files or all(not f or not f.filename for f in files):
        return jsonify({"error": "No file selected."}), 400

    valid_uploads = [f for f in files if f and f.filename and f.filename.lower().endswith(".pdf")]
    if not valid_uploads:
        return jsonify({"error": "Only PDF files are supported."}), 400

    results = []
    for upload in valid_uploads:
        pdf_header = upload.read(4)
        upload.seek(0)
        if pdf_header != b"%PDF":
            return jsonify({"error": f"Invalid PDF file {upload.filename}. Does not start with %PDF."}), 400
        
        data = upload.read()
        extracted, status_code = _parse_single_pdf_bytes(data, upload.filename)
        if status_code != 200:
            if len(valid_uploads) == 1:
                return jsonify(extracted), status_code
            log.warning("Skipping failed parse for %s: %s", upload.filename, extracted)
            continue
        results.append(extracted)

    if not results:
        return jsonify({"error": "Could not extract booking details from the uploaded PDF(s)."}), 422

    if len(results) == 1:
        return jsonify(results[0])

    # ── Merge multiple parsed tickets into a single multi-segment ticket ──
    merged = dict(results[0])
    all_flights = []
    all_pnrs = []
    total_base = 0.0
    total_taxes = 0.0
    total_fare = 0.0
    has_fare = False

    for res in results:
        pnr_val = res.get("pnr", "").strip()
        if pnr_val and pnr_val not in all_pnrs:
            all_pnrs.append(pnr_val)

        res_flights = res.get("flights") or []
        for fl in res_flights:
            fl_copy = dict(fl)
            if pnr_val and not fl_copy.get("segment_pnr"):
                fl_copy["segment_pnr"] = pnr_val
            # Each source ticket's barcode was captured at a different
            # resolution (one scanned out of a PDF, another generated fresh),
            # so stacking them as-is leaves one looking bigger than the
            # other. Regenerating every segment's barcode from its own PNR
            # keeps all of them the same size on a merged itinerary.
            gen_path, gen_data = generate_code128_barcode(fl_copy.get("segment_pnr") or pnr_val or merged.get("pnr", ""))
            if gen_path:
                fl_copy["barcode_token"] = gen_path
                fl_copy["barcode_data"] = gen_data
            all_flights.append(fl_copy)

        try:
            b = float(res.get("base_fare") or 0)
            t = float(res.get("taxes_fees") or 0)
            tot = float(res.get("total_fare") or 0)
            if tot > 0 or b > 0:
                has_fare = True
                total_base += b
                total_taxes += t
                total_fare += tot
        except (ValueError, TypeError):
            pass

    # Tickets are merged in whichever order they were uploaded, which is not
    # necessarily the order they are flown. Sorting the segments by departure
    # and rebuilding the PNR list from that same order keeps the codes in the
    # header lined up with the per-segment rows in the passenger table.
    all_flights.sort(key=lambda f: (f.get("date") or "", f.get("dep_time_raw") or ""))
    ordered_pnrs = []
    for fl in all_flights:
        code = (fl.get("segment_pnr") or "").strip()
        if code and code not in ordered_pnrs:
            ordered_pnrs.append(code)
    if ordered_pnrs:
        all_pnrs = ordered_pnrs

    merged["flights"] = all_flights
    if all_pnrs:
        merged["pnr"] = " / ".join(all_pnrs)
    if has_fare:
        merged["base_fare"] = str(round(total_base, 2))
        merged["taxes_fees"] = str(round(total_taxes, 2))
        merged["total_fare"] = str(round(total_fare, 2))

    log.info("Merged %d tickets: %d flights, PNRs: %s", len(results), len(all_flights), merged.get("pnr"))
    return jsonify(merged)


@app.route("/api/ai-prompt", methods=["GET"])
def ai_prompt():
    """Return the AI extraction prompt so the front end can display it."""
    prompt = (
        "Extract ALL flight ticket information from this image/PDF and return ONLY a valid JSON object (no markdown, no explanation, no extra text). "
        "Use this EXACT structure:\n\n"
        "{\n"
        '  "pnr": "AIRLINE PNR CODE",\n'
        '  "booking_id": "BOOKING/ORDER ID if visible",\n'
        '  "booking_platform": "MakeMyTrip / Cleartrip / Goibibo / Paytm / Direct etc.",\n'
        '  "booking_date": "YYYY-MM-DD",\n'
        '  "customer_email": "email if visible",\n'
        '  "customer_phone": "phone if visible",\n'
        '  "base_fare": "number only e.g. 3500",\n'
        '  "taxes_fees": "number only e.g. 850",\n'
        '  "total_fare": "number only e.g. 4350",\n'
        '  "discount": "number only, 0 if none",\n'
        '  "refund_status": "Refundable / Non-Refundable / Partially Refundable",\n'
        '  "fare_type": "REGULAR / SAVER / FLEXI / NDC FARE / SME FARE / CORPORATE FARE / STUDENT FARE / SPECIAL FARE",\n'
        '  "flights": [\n'
        "    {\n"
        '      "flight_no": "6E 2341 (IATA code space number)",\n'
        '      "airline": "IndiGo",\n'
        '      "from_code": "DEL (3-letter IATA)",\n'
        '      "to_code": "BOM (3-letter IATA)",\n'
        '      "date": "YYYY-MM-DD",\n'
        '      "dep_time_raw": "HH:MM (24hr format)",\n'
        '      "arr_time_raw": "HH:MM (24hr format)",\n'
        '      "class": "Economy / Premium Economy / Business / First Class",\n'
        '      "from_terminal": "T1 if visible",\n'
        '      "to_terminal": "T2 if visible",\n'
        '      "checkin_bag": "15 KG (1 Piece) / 20 KG (1 Piece) / 30 KG (1 Piece) / No Baggage etc.",\n'
        '      "hand_bag": "7 KG (1 Piece) / 10 KG (1 Piece) / No Baggage"\n'
        "    }\n"
        "  ],\n"
        '  "passengers": [\n'
        "    {\n"
        '      "title": "Mr / Mrs / Ms / Mstr / Dr",\n'
        '      "name": "FULL NAME IN CAPS",\n'
        '      "type": "Adult / Child / Infant",\n'
        '      "seats": ["12A"],\n'
        '      "passport": "passport number if visible",\n'
        '      "dob": "DD Mon YYYY if visible",\n'
        '      "doe": "DD Mon YYYY (passport expiry) if visible"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "RULES:\n"
        "- Return ONLY the JSON. No markdown code fences, no commentary.\n"
        "- If a field is not visible, use empty string \"\".\n"
        "- For multiple flights (connecting), add multiple objects in the flights array.\n"
        "- For multiple passengers, add multiple objects in the passengers array.\n"
        "- Flight number format: IATA code + space + number (e.g. \"6E 2341\", \"AI 865\", \"SG 178\").\n"
        "- Times must be 24-hour HH:MM format.\n"
        "- Dates must be YYYY-MM-DD format.\n"
        "- Fare amounts must be plain numbers without currency symbols or commas.\n"
        "- Baggage format: \"XX KG (N Piece)\" or \"No Baggage\"."
    )
    return jsonify({"prompt": prompt})


def _loads_llm_json(json_str):
    """Parse JSON an LLM produced, repairing what they habitually get wrong.

    Returns (data, error). Chat models emit trailing commas and Python-style
    single quotes, and copying out of a chat window turns straight quotes into
    curly ones - all of which json.loads rejects outright, which is what the
    paste box kept erroring on.
    """
    attempts = [json_str]

    # Invalid backslash escapes (\@, \., \_ ...).
    attempts.append(re.sub(r'\\([^"\\/bfnrtu])', r'\1', json_str))

    # Curly quotes from a chat window, plus trailing commas before } or ].
    straight = (json_str
                .replace("“", '"').replace("”", '"')
                .replace("‘", "'").replace("’", "'"))
    attempts.append(re.sub(r",(\s*[}\]])", r"\1", straight))

    last_error = None
    for candidate in attempts:
        try:
            return json.loads(candidate), None
        except json.JSONDecodeError as exc:
            last_error = exc

    # Single-quoted keys, or Python's True/False/None. literal_eval understands
    # those natively and, unlike swapping quotes by regex, cannot execute code.
    try:
        value = ast.literal_eval(straight)
        if isinstance(value, dict):
            return value, None
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        pass

    return None, last_error


@app.route("/api/parse-ai-data", methods=["POST"])
def parse_ai_data():
    """Accept AI-extracted JSON and normalize it for the form auto-fill.

    The front end sends the raw text the user pasted from ChatGPT / Gemini.
    This endpoint strips markdown fences, parses the JSON, and validates
    that the essential fields are present before returning them in the same
    shape that applyExtractedData() on the client expects.
    """
    payload = request.get_json(silent=True) or {}
    raw_text = (payload.get("ai_text") or "").strip()
    if not raw_text:
        return jsonify({"error": "No AI data provided. Paste the JSON output from ChatGPT/Gemini."}), 400

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw_text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    cleaned = cleaned.strip()

    # Try to find JSON object in the text
    json_match = re.search(r"\{[\s\S]*\}", cleaned)
    if not json_match:
        return jsonify({
            "error": "Could not find valid JSON in the pasted text. "
                     "Make sure you copy the complete JSON output from the AI."
        }), 400

    json_str = json_match.group(0)
    data, parse_error = _loads_llm_json(json_str)
    if data is None:
        return jsonify({
            "error": f"Invalid JSON: {parse_error}. Make sure the AI output is complete and properly formatted."
        }), 400

    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object, not an array or primitive."}), 400

    # Normalize and validate flights
    flights = data.get("flights") or []
    if not isinstance(flights, list):
        flights = []
    normalized_flights = []
    for f in flights:
        if not isinstance(f, dict):
            continue
        seg = {
            "flight_no": str(f.get("flight_no") or "").strip(),
            "airline": str(f.get("airline") or "").strip(),
            "from_code": str(f.get("from_code") or "").strip().upper()[:3],
            "to_code": str(f.get("to_code") or "").strip().upper()[:3],
            "date": str(f.get("date") or "").strip(),
            "dep_time_raw": str(f.get("dep_time_raw") or f.get("dep_time") or "").strip(),
            "arr_time_raw": str(f.get("arr_time_raw") or f.get("arr_time") or "").strip(),
            "class": str(f.get("class") or f.get("travel_class") or "Economy").strip(),
            "from_terminal": str(f.get("from_terminal") or f.get("dep_terminal") or "").strip(),
            "to_terminal": str(f.get("to_terminal") or f.get("arr_terminal") or "").strip(),
            "checkin_bag": str(f.get("checkin_bag") or f.get("check_in_baggage") or "").strip(),
            "hand_bag": str(f.get("hand_bag") or f.get("cabin_baggage") or f.get("hand_baggage") or "").strip(),
            "segment_pnr": str(f.get("segment_pnr") or "").strip(),
        }
        # Normalize baggage labels
        if seg["checkin_bag"]:
            seg["checkin_bag"] = normalize_baggage_label(seg["checkin_bag"], is_checkin=True)
        if seg["hand_bag"]:
            seg["hand_bag"] = normalize_baggage_label(seg["hand_bag"], is_checkin=False)
        # Normalize time format
        for key in ("dep_time_raw", "arr_time_raw"):
            seg[key] = normalize_time_token(seg[key])
        # Normalize date
        if seg["date"]:
            seg["date"] = parse_date_str(seg["date"]) or seg["date"]
        else:
            seg["date"] = parse_date_str(str(data.get("booking_date") or "")) or datetime.now().strftime("%Y-%m-%d")
        # Auto-detect airline from flight number
        if seg["flight_no"] and not seg["airline"]:
            seg["airline"] = AIRLINES.get(extract_airline_code(seg["flight_no"]), "")
        normalized_flights.append(seg)

    # Normalize passengers
    passengers = data.get("passengers") or []
    if not isinstance(passengers, list):
        passengers = []
    normalized_pax = []
    for p in passengers:
        if not isinstance(p, dict):
            continue
        raw_name = str(p.get("name") or "").strip().upper()
        pax_title = str(p.get("title") or "Mr").strip()
        if "/" in raw_name:
            parts = [part.strip() for part in raw_name.split("/") if part.strip()]
            if len(parts) == 2:
                first_part = parts[1]
                for t in ("MR", "MRS", "MS", "MSTR", "DR"):
                    if first_part.endswith(" " + t):
                        first_part = first_part[:-len(t)-1].strip()
                        if not p.get("title"):
                            pax_title = t.title()
                raw_name = f"{first_part} {parts[0]}".strip()
            else:
                raw_name = raw_name.replace("/", " ").strip()

        pax = {
            "title": pax_title,
            "name": raw_name,
            "type": str(p.get("type") or p.get("pax_type") or "Adult").strip(),
            "passport": str(p.get("passport") or "").strip(),
            "dob": str(p.get("dob") or "").strip(),
            "doe": str(p.get("doe") or "").strip(),
        }
        # Handle seats - AI may return string or array
        seats = p.get("seats") or p.get("seat")
        if isinstance(seats, list):
            pax["seats"] = [str(s).strip() for s in seats]
        elif seats:
            pax["seats"] = [str(seats).strip()]
        else:
            pax["seats"] = []

        normalized_pax.append(pax)

    # Build normalized result
    def safe_str(key, fallback=""):
        val = data.get(key)
        return str(val).strip() if val is not None and str(val).strip() else fallback

    def safe_fare(key):
        val = data.get(key)
        if val is None or str(val).strip() == "":
            return ""
        cleaned = str(val).replace(",", "").replace("₹", "").replace("INR", "").replace("-", "").strip()
        try:
            return str(abs(float(cleaned)))
        except ValueError:
            return ""

    result = {
        "pnr": re.sub(r"[^A-Z0-9]", "", safe_str("pnr").upper()),
        "booking_id": safe_str("booking_id"),
        "booking_platform": safe_str("booking_platform"),
        "booking_date": parse_date_str(safe_str("booking_date")) or safe_str("booking_date"),
        "customer_email": safe_str("customer_email"),
        "customer_phone": safe_str("customer_phone"),
        "base_fare": safe_fare("base_fare"),
        "taxes_fees": safe_fare("taxes_fees"),
        "total_fare": safe_fare("total_fare"),
        "discount": safe_fare("discount"),
        "refund_status": safe_str("refund_status"),
        "fare_type": safe_str("fare_type"),
        "flights": normalized_flights,
        "passengers": normalized_pax,
    }

    # Handle masked phone numbers (e.g. +91-87******48) or phone numbers with < 7 digits
    phone_raw = result["customer_phone"]
    digits = re.sub(r"\D", "", phone_raw)
    if not phone_raw or "*" in phone_raw or len(digits) < 7:
        result["customer_phone"] = COMPANY.get("phone", "+91 9876543210")

    # Handle masked emails (e.g. a********m@g***l.com) or invalid emails
    email_raw = result["customer_email"]
    if not email_raw or "*" in email_raw or "@" not in email_raw or "." not in email_raw:
        if result["passengers"] and result["passengers"][0].get("name"):
            clean_name = re.sub(r"[^a-zA-Z]+", ".", result["passengers"][0]["name"].lower()).strip(".")
            result["customer_email"] = f"{clean_name}@gmail.com" if clean_name else COMPANY.get("email", "support@traveleticket.com")
        else:
            result["customer_email"] = COMPANY.get("email", "support@traveleticket.com")

    # If total_fare is present but base_fare is missing, map total_fare into base_fare so the amount is never lost
    has_total = result["total_fare"] and result["total_fare"] not in ("0", "0.0")
    has_base = result["base_fare"] and result["base_fare"] not in ("0", "0.0")
    if has_total and not has_base:
        result["base_fare"] = result["total_fare"]
        result["taxes_fees"] = "0"
    elif not result["base_fare"]:
        result["base_fare"] = "0"

    if not result["taxes_fees"]:
        result["taxes_fees"] = "0"

    if not result["total_fare"] or result["total_fare"] in ("0", "0.0"):
        try:
            bf = float(result["base_fare"] or 0)
            tf = float(result["taxes_fees"] or 0)
            result["total_fare"] = str(round(bf + tf, 2))
        except Exception:
            result["total_fare"] = result["base_fare"] or "0"

    # Auto-generate barcode from PNR
    if result["pnr"]:
        bc_path, bc_data = generate_code128_barcode(result["pnr"])
        if bc_path:
            result["barcode_token"] = bc_path
            result["barcode_data"] = bc_data

    log.info("AI data parsed: pnr=%s, %d flight(s), %d passenger(s)",
             result["pnr"], len(result["flights"]), len(result["passengers"]))

    return jsonify(result)


if CSRF_AVAILABLE:
    csrf.exempt(parse_ai_data)


@app.route("/api/test-extract", methods=["GET"])
def test_extract():
    return jsonify({"status": "ready", "message": "PDF extraction active."})


@app.route("/api/load-preset", methods=["GET"])
def load_preset():
    filename = secure_filename(request.args.get("file", "new12.pdf"))
    allowed_presets = {"new12.pdf", "new13.pdf", "new14.pdf", "new15.pdf"}
    if filename not in allowed_presets:
        return jsonify({"error": "Invalid preset file"}), 400

    file_path = os.path.join(os.path.dirname(__file__), filename)
    if not os.path.exists(file_path):
        return jsonify({"error": f"File {filename} not found"}), 404

    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
            pages_words = [page.extract_words() or [] for page in pdf.pages]
        text = "\n".join(pages).strip()
        extracted = extract_ticket_fields(text, filename=filename, pages_words=pages_words)
        return jsonify(extracted)
    except Exception as exc:
        log.exception("Error extracting preset %s: %s", filename, exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/download-sample/<filename>", methods=["GET"])
def download_sample(filename):
    safe_name = secure_filename(filename)
    allowed_downloads = {
        "generated_new12.pdf", "generated_new13.pdf", "generated_new14.pdf", "generated_new15.pdf",
        "new12.pdf", "new13.pdf", "new14.pdf", "new15.pdf"
    }
    if safe_name in allowed_downloads:
        filepath = os.path.join(os.path.dirname(__file__), safe_name)
        if os.path.exists(filepath):
            return send_file(filepath, as_attachment=True, download_name=safe_name, mimetype="application/pdf")
    return jsonify({"error": "File not found"}), 404


MAX_BARCODE_BYTES = 4 * 1024 * 1024

# Mailboxes that belong to whoever issued the ticket, not to the traveller.
SUPPORT_MAILBOXES = (
    "support", "care", "noreply", "no-reply", "donotreply", "do-not-reply",
    "info", "contact", "help", "service", "booking", "feedback", "admin",
    "sales", "enquiry", "inquiry", "customer",
)

# Wording that introduces the issuer's own number rather than the customer's.
HELPLINE_LEAD_INS = (
    "SUPPORT", "HELPLINE", "HELP LINE", "TOLL FREE", "TOLL-FREE", "CARE",
    "CALL US", "CONTACT US", "REACH US", "CUSTOMER SERVICE", "ASSISTANCE",
    "QUERIES", "24X7", "24/7", "HELPDESK",
)

TICKET_STATUSES = ("Confirmed", "On Hold", "Waitlisted", "Cancelled", "Refunded")
# Printed on the ticket and recorded in the tracker, so the list is fixed here
# rather than trusting whatever the form posts back.
FARE_TYPES = (
    "REGULAR", "PUBLISHED", "SERIES FARE", "LITE", "SAVER", "FLEXI",
    "NDC FARE", "SME FARE", "CORPORATE FARE", "STUDENT FARE",
    "SENIOR CITIZEN FARE", "ARMED FORCES FARE", "SPECIAL FARE",
    "RETAIL FARE", "TOUR FARE", "OTHERS",
)
PASSENGER_TITLES = ("Mr", "Mrs", "Ms", "Mstr", "Dr")
PASSENGER_TYPES = ("Adult", "Child", "Infant")


def format_money(amount):
    return f"INR {amount:,.2f}"


def extract_barcode_from_pdf_bytes(pdf_bytes):
    """Auto-fetches embedded airport/airline barcodes from uploaded PDF."""
    try:
        import pypdfium2 as pdfium
        from PIL import ImageStat
        doc = pdfium.PdfDocument(pdf_bytes)
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            for obj in page.get_objects():
                if obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE:
                    bounds = obj.get_bounds() # left, bottom, right, top
                    w = bounds[2] - bounds[0]
                    h = bounds[3] - bounds[1]
                    aspect = w / h if h > 0 else 0
                    if 1.3 <= aspect <= 8.0 and w >= 50 and h >= 12:
                        bm = obj.get_bitmap()
                        pil_img = bm.to_pil()
                        pw, ph = pil_img.size
                        if pw < 80 or ph < 20:
                            continue
                        rgb = pil_img.convert("RGB")
                        stat = ImageStat.Stat(rgb)
                        r_mean, g_mean, b_mean = stat.mean[:3]
                        # Barcodes are B&W / grayscale
                        if abs(r_mean - g_mean) > 18 or abs(g_mean - b_mean) > 18:
                            continue
                        if stat.stddev[0] < 25:
                            continue
                        if bounds[1] > 740 and page_idx == 0:
                            continue
                        
                        path = os.path.join(tempfile.gettempdir(), secure_filename(f"temp_barcode_{uuid.uuid4()}.png"))
                        pil_img.save(path, format="PNG")
                        buf = io.BytesIO()
                        pil_img.save(buf, format="PNG")
                        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                        return path, f"data:image/png;base64,{b64}"
    except Exception as exc:
        log.warning("Could not auto-extract barcode from PDF: %s", exc)
    return None, None


def build_barcode_payload(*, pnr, flight, passengers, booking_id):
    """The text a gate scanner reads off the ticket.

    Code 128 holds one short field, so it could only ever carry the PNR. PDF417
    is a 2D symbology with room for the whole segment, so the scan identifies
    the flight rather than just the booking.
    """
    lead = passengers[0] if passengers else {}
    name = " ".join(part for part in (lead.get("title"), lead.get("name")) if part)
    fields = (
        ("PNR", pnr),
        ("BKG", booking_id),
        ("PAX", name.upper()),
        ("FLT", (flight.get("flight_no") or "").upper()),
        ("FROM", flight.get("from_code")),
        ("TO", flight.get("to_code")),
        ("DATE", flight.get("date_raw") or flight.get("date")),
        ("DEP", flight.get("dep_time_raw")),
        ("ARR", flight.get("arr_time_raw")),
        ("CLS", flight.get("class")),
        ("SEAT", flight.get("seat")),
    )
    return "|".join(f"{key}:{str(value).strip()}"
                    for key, value in fields if str(value or "").strip())


def generate_pdf417_barcode(payload, scale=3, ratio=3, padding=4):
    """Render `payload` as a PDF417 symbol, or return (None, None).

    Falls back rather than raising: a ticket without a scannable symbol is
    still a valid ticket, but one that fails to render is not a ticket at all.
    """
    text = (payload or "").strip()
    if not text:
        return None, None
    try:
        from pdf417gen import encode, render_image
        codes = encode(text, columns=6, security_level=4)
        img = render_image(codes, scale=scale, ratio=ratio, padding=padding)
        buffer = io.BytesIO()
        img.convert("RGB").save(buffer, format="PNG")
        raw = buffer.getvalue()
        path = os.path.join(tempfile.gettempdir(),
                            secure_filename(f"temp_pdf417_{uuid.uuid4()}.png"))
        with open(path, "wb") as handle:
            handle.write(raw)
        if not hasattr(g, "temp_files"):
            g.temp_files = []
        g.temp_files.append(path)
        return path, "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception as exc:
        log.warning("PDF417 barcode could not be generated: %s", exc)
        return None, None


def generate_code128_barcode(data, height=55, bar_width=2):
    """Pure-Python standard Code128 barcode generator."""
    try:
        from PIL import Image as PILImage, ImageDraw
        PATS = [
            '212222', '222122', '222221', '121223', '121322', '131222', '122213', '122312',
            '132212', '221213', '221312', '231212', '112232', '122132', '122231', '113222',
            '123122', '123221', '223211', '221132', '221231', '213212', '223112', '312131',
            '311222', '321122', '321221', '312212', '322112', '322211', '212123', '212321',
            '232121', '111323', '131123', '131321', '112313', '132113', '132311', '211313',
            '231113', '231311', '112133', '112331', '132131', '113123', '113321', '133121',
            '313121', '211331', '231131', '213113', '213311', '213131', '311123', '311321',
            '331121', '312113', '312311', '332111', '314111', '221411', '431111', '111224',
            '111422', '121124', '121421', '141122', '141221', '112214', '112412', '122114',
            '122411', '142112', '142211', '241211', '221114', '413111', '241112', '134111',
            '111242', '121142', '121241', '114212', '124112', '124211', '411212', '421112',
            '421211', '212141', '214121', '412121', '111143', '111341', '131141', '114113',
            '114311', '411113', '411311', '113141', '114131', '311141', '411131',
        ]
        pattern = '211214' # Start B
        checksum = 104
        val_str = str(data).strip().upper() or "FLIGHT"
        for i, char in enumerate(val_str):
            val = ord(char) - 32
            if 0 <= val < len(PATS):
                checksum += val * (i + 1)
                pattern += PATS[val]
        chk_val = checksum % 103
        if 0 <= chk_val < len(PATS):
            pattern += PATS[chk_val]
        pattern += '2331112'

        total_modules = sum(int(c) for c in pattern)
        margin = 12
        img_w = total_modules * bar_width + 2 * margin
        img = PILImage.new('RGB', (img_w, height), 'white')
        draw = ImageDraw.Draw(img)
        x = margin
        is_bar = True
        for c in pattern:
            w = int(c) * bar_width
            if is_bar:
                draw.rectangle([x, 0, x + w - 1, height], fill='black')
            x += w
            is_bar = not is_bar
        
        path = os.path.join(tempfile.gettempdir(), secure_filename(f"temp_barcode_{uuid.uuid4()}.png"))
        img.save(path, format="PNG")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        return path, f"data:image/png;base64,{b64}"
    except Exception as exc:
        log.warning("Could not generate fallback Code128 barcode: %s", exc)
        return None, None


def save_base64_barcode(data_url):
    """Decode a base64 data URL and save as a temp PNG file."""
    try:
        header, encoded = data_url.split(",", 1)
        data = base64.b64decode(encoded)
        from PIL import Image as PILImage
        with PILImage.open(io.BytesIO(data)) as img:
            normalised = img.convert("RGB")
        path = os.path.join(tempfile.gettempdir(), secure_filename(f"temp_barcode_{uuid.uuid4()}.png"))
        normalised.save(path, format="PNG")
        if not hasattr(g, "temp_files"):
            g.temp_files = []
        g.temp_files.append(path)
        return path
    except Exception as exc:
        log.warning("Could not decode base64 barcode: %s", exc)
        return None


def save_barcode_upload(upload):
    """Persist an uploaded barcode image to a temp file, or return None.

    The upload is fully decoded and re-encoded as PNG rather than trusting the
    filename, content type or magic bytes. A header-only check is not enough:
    a truncated image passes it and then crashes ReportLab during PDF layout,
    because Image flowables decode lazily, long after the upload is handled.
    """
    if not upload or not upload.filename:
        return None

    data = upload.read(MAX_BARCODE_BYTES + 1)
    if len(data) > MAX_BARCODE_BYTES:
        log.warning("Rejected barcode upload %r: larger than 4 MB.", upload.filename)
        return None

    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(data)) as probe:
            probe.verify()  # checksum/structure check; consumes the object
        with PILImage.open(io.BytesIO(data)) as image:
            normalised = image.convert("RGB")
    except Exception as exc:
        log.warning("Rejected barcode upload %r: not a readable image (%s).", upload.filename, exc)
        return None

    path = os.path.join(
        tempfile.gettempdir(), secure_filename(f"temp_barcode_{uuid.uuid4()}.png")
    )
    try:
        normalised.save(path, format="PNG")
    except (OSError, ValueError) as exc:
        log.warning("Could not save barcode upload: %s", exc)
        return None

    # Registered so the file is removed even if PDF generation raises.
    if not hasattr(g, "temp_files"):
        g.temp_files = []
    g.temp_files.append(path)
    return path


def cleanup_temp_files(flights):
    """Remove temp barcode uploads. Safe to call more than once."""
    for flight in flights or []:
        path = flight.get("barcode_path")
        if not path:
            continue
        _remove_temp_file(path)
        flight["barcode_path"] = None


def _remove_temp_file(path):
    try:
        os.remove(path)
    except OSError:
        pass
    if hasattr(g, "temp_files") and path in g.temp_files:
        g.temp_files.remove(path)


@app.teardown_request
def _cleanup_leftover_temp_files(_exception):
    """Safety net: drop any barcode temp file the request did not clean up."""
    for path in list(getattr(g, "temp_files", [])):
        try:
            os.remove(path)
        except OSError:
            pass


VALIDATION_ERROR_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Check your ticket details</title>
    <style>
        *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
        body{font-family:Inter,-apple-system,Segoe UI,Arial,sans-serif;background:#eef2f6;
             color:#162033;min-height:100vh;display:flex;align-items:center;
             justify-content:center;padding:24px;line-height:1.6}
        .box{background:#fff;border:1px solid #d9e0ea;border-radius:14px;
             box-shadow:0 24px 70px rgba(15,39,66,.12);padding:32px;max-width:620px;width:100%}
        .tag{display:inline-block;background:#fdecea;color:#c24135;padding:6px 12px;
             border-radius:999px;font-weight:800;font-size:.7rem;letter-spacing:1px;margin-bottom:14px}
        h1{color:#0f2742;font-size:1.4rem;margin-bottom:6px}
        p.lead{color:#667085;font-size:.9rem;margin-bottom:18px}
        ul{list-style:none;display:flex;flex-direction:column;gap:8px;margin-bottom:24px}
        li{background:#f6f8fb;border-left:3px solid #c24135;border-radius:6px;
           padding:10px 14px;font-size:.86rem}
        a{display:inline-block;background:#0e9488;color:#fff;text-decoration:none;
          padding:12px 22px;border-radius:10px;font-weight:800;font-size:.88rem}
        a:hover{background:#0b7f75}
    </style>
</head>
<body>
    <div class="box">
        <div class="tag">TICKET NOT GENERATED</div>
        <h1>Please fix {{ count }} item{{ '' if count == 1 else 's' }}</h1>
        <p class="lead">Nothing was saved. Go back, correct the details below and generate again.</p>
        <ul>{% for error in errors %}<li>{{ error }}</li>{% endfor %}</ul>
        <a href="javascript:history.back()">Back to the form</a>
    </div>
</body>
</html>"""


def render_validation_errors(errors):
    """Return validation failures as JSON for API clients, HTML for the form."""
    if request.accept_mimetypes.best == "application/json" or request.is_json:
        return jsonify({"error": "Validation failed", "errors": errors}), 400
    return render_template_string(VALIDATION_ERROR_PAGE, errors=errors, count=len(errors)), 400


@app.route("/generate", methods=["POST"])
def generate_ticket():
    # ── Gather form data ─────────────────────────────────────
    booking_id = request.form.get("booking_id", "")
    booking_date = request.form.get("booking_date", "")
    pnr = request.form.get("pnr", "")
    
    # ── Agency Selection & Contact ─────────────────
    agency_id = request.form.get("agency_id", DEFAULT_AGENCY_ID).strip()
    selected_agency = dict(AGENCIES.get(agency_id, COMPANY))
    agency_phone = request.form.get("agency_phone", "").strip() or selected_agency.get("phone", "")
    agency_email = request.form.get("agency_email", "").strip() or selected_agency.get("email", "")
    if agency_phone:
        selected_agency["phone"] = agency_phone
    if agency_email:
        selected_agency["email"] = agency_email
    
    # ── Custom Contact ─────────────────────────────
    customer_phone = request.form.get("customer_phone", "").strip()
    customer_email = request.form.get("customer_email", "").strip()
    
    refund_status = request.form.get("refund_status", "Refundable")
    fare_type = request.form.get("fare_type", "REGULAR").strip().upper()
    if fare_type not in FARE_TYPES:
        fare_type = "REGULAR"
    booking_platform = request.form.get("booking_platform", "Direct")
    is_dummy = request.form.get("is_dummy") == "true"
    # Kept out of the printed ticket, not out of the tracker.
    hide_fare = request.form.get("hide_fare") == "true"
    hide_agency = request.form.get("hide_agency") == "true"

    ticket_status = request.form.get("ticket_status", "Confirmed").strip() or "Confirmed"
    if ticket_status not in TICKET_STATUSES:
        ticket_status = "Confirmed"
    trip_type = request.form.get("trip_type", "One Way").strip() or "One Way"

    # GST / company billing (used on Indian B2B invoices)
    gst_company = request.form.get("gst_company", "").strip()
    gstin = request.form.get("gstin", "").strip().upper()

    # How the traveller paid, printed under the amount. UPI is what this
    # agency takes nearly every time, so it is the default rather than a
    # field that has to be set on each ticket.
    payment_method = request.form.get("payment_method", "").strip() or "UPI"
    if payment_method.upper() == "NONE":
        payment_method = ""
    # Only ever the last four digits reach the ticket. A full card number is
    # cut down to four here rather than trusted from the form, so pasting one
    # in cannot put it on a document that gets emailed around.
    # Only a card is named by its digits; a UPI or cash payment has nothing
    # the traveller would recognise, so the field is ignored for those.
    if payment_method.lower() in ("card", "credit card", "debit card"):
        pay_ref = re.sub(r"\D", "", request.form.get("payment_ref", ""))[-4:]
        if pay_ref:
            payment_method = f"{payment_method} ending {pay_ref}"

    # The issue stamp defaults to now, but a ticket is sometimes written up
    # after the fact and has to carry the time it was actually issued.
    issued_at = parse_datetime_local(request.form.get("issued_at")) or datetime.now()

    # Free text printed under the amount. Replaces the automatic payment
    # line, so whatever goes there is chosen rather than assumed.
    remarks = " ".join(request.form.get("remarks", "").split())[:200]

    # Format booking date
    try:
        bd_obj = datetime.strptime(booking_date, "%Y-%m-%d")
        booking_date_display = bd_obj.strftime("%a, %d %b %Y")
    except Exception:
        booking_date_display = booking_date or datetime.now().strftime("%a, %d %b %Y")

    # ── Parse multiple flights ───────────────────────────────
    flights = []
    flight_indices = sorted(
        {
            int(match.group(1))
            for key in request.form.keys()
            if (match := re.match(r"flight_(\d+)_flight_no$", key))
        }
    )
    if not flight_indices:
        flight_indices = list(range(int(request.form.get("flight_count", "1") or 1)))
    for i in flight_indices:
        prefix = f"flight_{i}_"
        flight_no = request.form.get(f"{prefix}flight_no", "")
        manual_layover = request.form.get(f"{prefix}layover", "").strip()
        airline_name = request.form.get(f"{prefix}airline", "")
        airline_code = extract_airline_code(flight_no)
        detected_airline = airline_payload().get(airline_code)
        if not airline_name and detected_airline:
            airline_name = detected_airline["name"]
        from_code = request.form.get(f"{prefix}from_code", "").upper()
        from_city = request.form.get(f"{prefix}from_city", "")
        to_code = request.form.get(f"{prefix}to_code", "").upper()
        to_city = request.form.get(f"{prefix}to_city", "")
        travel_date = request.form.get(f"{prefix}date", "")
        dep_time_raw = request.form.get(f"{prefix}dep_time_raw", "")
        arr_time_raw = request.form.get(f"{prefix}arr_time_raw", "")
        # Always computed here rather than taking the submitted value: the
        # browser only subtracts clock times and has no timezone data, so it
        # would override the correct figure on international routes.
        duration = compute_duration(dep_time_raw, arr_time_raw,
                                    from_code, to_code, travel_date)
        travel_class = request.form.get(f"{prefix}class", "Economy")
        seat = request.form.get(f"{prefix}seat", "")
        meal = request.form.get(f"{prefix}meal", "Not selected")
        checkin_bag = request.form.get(f"{prefix}checkin_bag", "")
        hand_bag = request.form.get(f"{prefix}hand_bag", "")
        # Shown as a pill under each side's date, as on a printed boarding pass.
        from_terminal = " ".join(request.form.get(f"{prefix}from_terminal", "").split())[:12]
        to_terminal = " ".join(request.form.get(f"{prefix}to_terminal", "").split())[:12]

        try:
            dep_dt = datetime.strptime(dep_time_raw, "%H:%M")
            checkin_dt = dep_dt - timedelta(minutes=60)
            checkin_closing = checkin_dt.strftime("%H:%M")
        except ValueError:
            checkin_closing = ""

        barcode_file = request.files.get(f"{prefix}barcode")
        temp_barcode_path = save_barcode_upload(barcode_file)
        if not temp_barcode_path:
            bc_token = request.form.get(f"{prefix}barcode_token", "").strip() or request.form.get("auto_barcode_token", "").strip()
            bc_data = request.form.get(f"{prefix}barcode_data", "").strip() or request.form.get("auto_barcode_data", "").strip()
            if bc_token and os.path.exists(bc_token):
                temp_barcode_path = bc_token
            elif bc_data and bc_data.startswith("data:image"):
                temp_barcode_path = save_base64_barcode(bc_data)

        segment_pnr = request.form.get(f"{prefix}segment_pnr", "").strip()

        # Auto-fill city and airport name from DB
        if not from_city and from_code in AIRPORTS_DB:
            from_city = AIRPORTS_DB[from_code]["city"]
        if not to_city and to_code in AIRPORTS_DB:
            to_city = AIRPORTS_DB[to_code]["city"]
        from_airport = AIRPORTS_DB.get(from_code, {}).get("airport", "")
        to_airport = AIRPORTS_DB.get(to_code, {}).get("airport", "")

        # Format date
        try:
            td_obj = datetime.strptime(travel_date, "%Y-%m-%d")
            date_display = td_obj.strftime("%a, %d %b %Y")
        except Exception:
            date_display = travel_date

        from_full = f"{from_city} ({from_code})" if from_city else from_code
        to_full = f"{to_city} ({to_code})" if to_city else to_code

        # Get segment PNR if provided (for multi-PNR connecting flights)
        segment_pnr = request.form.get(f"{prefix}segment_pnr", "").strip()

        flights.append({
            "flight_no": flight_no,
            "airline": airline_name,
            "airline_code": airline_code,
            "from_code": from_code, "from_city": from_city, "from_full": from_full,
            "from_airport": from_airport,
            "to_code": to_code, "to_city": to_city, "to_full": to_full,
            "to_airport": to_airport,
            "date": date_display, "date_raw": travel_date,
            "dep_time_raw": dep_time_raw, "arr_time_raw": arr_time_raw,
            "duration": duration, "class": travel_class,
            "seat": seat,
            "meal": meal,
            "checkin_bag": checkin_bag,
            "hand_bag": hand_bag,
            "from_terminal": from_terminal,
            "to_terminal": to_terminal,
            "barcode_path": temp_barcode_path,
            "checkin_closing": checkin_closing,
            "manual_layover": manual_layover,
            "segment_pnr": segment_pnr
        })
        
    for fi in range(len(flights)-1):
        fl1 = flights[fi]
        fl2 = flights[fi+1]
        try:
            if fl2.get("manual_layover"):
                fl2["layover"] = fl2["manual_layover"]
            else:
                # Validate required fields exist and are non-empty
                if not all([fl1.get('date_raw'), fl1.get('dep_time_raw'), fl1.get('arr_time_raw'),
                           fl2.get('date_raw'), fl2.get('dep_time_raw')]):
                    continue
                
                fmt = "%Y-%m-%d %H:%M"
                try:
                    dep1 = datetime.strptime(f"{fl1['date_raw']} {fl1['dep_time_raw']}", fmt)
                    arr1 = datetime.strptime(f"{fl1['date_raw']} {fl1['arr_time_raw']}", fmt)
                    dep2 = datetime.strptime(f"{fl2['date_raw']} {fl2['dep_time_raw']}", fmt)
                    
                    # Handle overnight arrival (arrival next day)
                    if arr1 < dep1:
                        arr1 += timedelta(days=1)
                    
                    diff = dep2 - arr1
                    if diff.total_seconds() > 0:
                        hours = int(diff.total_seconds() // 3600)
                        mins = int((diff.total_seconds() % 3600) // 60)
                        layover_type = "Long layover" if hours >= 3 else "Short layover"
                        fl2["layover"] = f"{hours}h {mins}m ({layover_type})"
                except ValueError as e:
                    # Invalid date/time format - skip layover calculation
                    log.warning("Layover calculation error: %s", e)
                    continue
        except Exception as e:
            # Log error but don't fail entire PDF generation
            log.warning("Layover calculation error: %s", e)
            continue

    # The header prints the PNRs as one string while the passenger table
    # prints one per segment, so the two have to agree on order. Re-sort the
    # submitted string into segment order, but only when it holds exactly the
    # same codes - a single master PNR alongside per-leg codes is left alone.
    submitted_pnrs = [part.strip().upper() for part in pnr.split("/") if part.strip()]
    if len(submitted_pnrs) > 1:
        segment_order = []
        for flight in flights:
            code = (flight.get("segment_pnr") or "").strip().upper()
            if code and code not in segment_order:
                segment_order.append(code)
        if sorted(segment_order) == sorted(submitted_pnrs):
            pnr = " / ".join(segment_order)

    # ── Parse multiple passengers ────────────────────────────
    passengers = []
    pax_indices = sorted(
        {
            int(match.group(1))
            for key in request.form.keys()
            if (match := re.match(r"pax_(\d+)_name$", key))
        }
    )
    if not pax_indices:
        pax_indices = list(range(int(request.form.get("pax_count", "1") or 1)))
    for i in pax_indices:
        prefix = f"pax_{i}_"
        # Build per-segment passenger allocations: seats, meals, baggage
        seats_per_segment = []
        meals_per_segment = []
        checkin_per_segment = []
        hand_per_segment = []
        for fi in range(len(flights)):
            # Per-passenger per-segment inputs take precedence when provided
            seat_val = request.form.get(f"pax_{i}_seat_{fi}")
            if not seat_val:
                # Fall back to flight-level seat (flight-level is source-of-truth)
                seat_val = request.form.get(f"flight_{fi}_seat", "")
            meals_val = request.form.get(f"pax_{i}_meal_{fi}") or request.form.get(f"flight_{fi}_meal", "Not selected")
            ck_val = request.form.get(f"pax_{i}_checkin_bag_{fi}") or request.form.get(f"flight_{fi}_checkin_bag", "")
            hd_val = request.form.get(f"pax_{i}_hand_bag_{fi}") or request.form.get(f"flight_{fi}_hand_bag", "")
            seats_per_segment.append(seat_val or "")
            meals_per_segment.append(meals_val or "Not selected")
            checkin_per_segment.append(ck_val or "Airline Default")
            hand_per_segment.append(hd_val or "Airline Default")

        # Legacy single-field fallbacks
        single_seat = request.form.get(f"{prefix}seat", "")
        single_meal = request.form.get(f"{prefix}meal", "")
        single_ck = request.form.get(f"{prefix}checkin_bag", "")
        single_hand = request.form.get(f"{prefix}hand_bag", "")

        pax_title = request.form.get(f"{prefix}title", "").strip()
        pax_type = request.form.get(f"{prefix}type", "Adult").strip() or "Adult"

        pax_raw_name = request.form.get(f"{prefix}name", "").strip()
        if "/" in pax_raw_name:
            parts = [p.strip() for p in pax_raw_name.split("/") if p.strip()]
            if len(parts) == 2:
                pax_raw_name = f"{parts[1]} {parts[0]}"
            else:
                pax_raw_name = pax_raw_name.replace("/", " ")

        passengers.append({
            "name": pax_raw_name,
            "title": pax_title if pax_title in PASSENGER_TITLES else "",
            "pax_type": pax_type if pax_type in PASSENGER_TYPES else "Adult",
            "pnr": pnr,
            "seat": single_seat,
            "meal": single_meal,
            "checkin_bag": single_ck,
            "hand_bag": single_hand,
            "seats_per_segment": seats_per_segment,
            "meals_per_segment": meals_per_segment,
            "checkin_per_segment": checkin_per_segment,
            "hand_per_segment": hand_per_segment,
        })

    # Scannable symbol per segment. Generated here rather than in the flight
    # loop above because the payload names the traveller, who is only known
    # once the passenger rows have been read.
    for flight in flights:
        if flight.get("barcode_path"):
            continue
        payload = build_barcode_payload(
            pnr=flight.get("segment_pnr") or pnr,
            flight=flight, passengers=passengers, booking_id=booking_id)
        bc_path, _ = generate_pdf417_barcode(payload)
        if bc_path:
            flight["barcode_path"] = bc_path

    # ── Fare ─────────────────────────────────────────────────
    # The ticket prints one figure, the amount paid, so that is the one figure
    # the agent enters: the net price they want the traveller to see. A split
    # into base fare and taxes was only ever summed back up before printing.
    fare_errors = []
    net_price = parse_money(request.form.get("net_price"), "Net Price", fare_errors, required=True)
    # The agent's margin. It is added into the one figure the ticket prints;
    # the traveller never sees it broken out.
    markup = parse_money(request.form.get("markup"), "Markup", fare_errors)
    total_fare = net_price + markup
    if not fare_errors and total_fare < 0:
        fare_errors.append("Net Price plus Markup cannot be negative.")

    errors = (
        validate_booking(request.form)
        + validate_flights(flights, trip_type=trip_type)
        + validate_passengers(passengers)
        + fare_errors
    )
    if errors:
        cleanup_temp_files(flights)
        return render_validation_errors(errors)
    total_fare_str = format_money(total_fare)

    # Route summary
    if flights:
        route_summary = f"{flights[0]['from_code']} → {flights[-1]['to_code']}"
    else:
        route_summary = "N/A"

    # ══════════════════════════════════════════════════════════
    # PDF GENERATION
    # ══════════════════════════════════════════════════════════
    buffer = io.BytesIO()
    t = TicketCanvas(buffer)

    draw_header(t, company=selected_agency, pnr=pnr, booking_id=booking_id, status=ticket_status,
                show_agency=not hide_agency)

    t.section("Itinerary", at=ITINERARY_BASE_MM)
    for index, flight in enumerate(flights):
        if index and flight.get("layover"):
            draw_layover(t, f"{flight['layover']} in {flight.get('from_city') or flight.get('from_code')}")
        draw_flight(t, flight, index, len(flights))

    t.section("Travellers", at=PAX_LABEL_BASE_MM)
    draw_passengers(t, passengers, flights)

    draw_fares(t, total_fare_str, remarks=remarks,
               gst_company=gst_company, gstin=gstin,
               payment_method=payment_method, show_amount=not hide_fare)

    draw_additional_info(t, ADDITIONAL_INFORMATION)

    contact_parts = []
    if agency_email:
        contact_parts.append(agency_email)
    contact_line = "  ·  ".join(contact_parts)

    draw_footer(
        t,
        company=selected_agency,
        show_agency=not hide_agency,
        issued=f"Issued {issued_at.strftime('%d %b %Y, %H:%M')} IST  ·  {fare_type}",
        contact_line=contact_line,
        refund_status=refund_status,
        # Everything the traveller has to act on is in Additional Information
        # above; repeating it here only made the foot of the ticket read twice.
        terms="",
    )

    t.save()
    buffer.seek(0)
    
    # ── Save Tracking to Excel ──────────────────────────────────
    lead_pax = passengers[0]["name"] if passengers else "Unknown"
    notes = "  |  ".join(part for part in (
        remarks,
        f"GST: {gst_company} ({gstin})" if gstin else "",
    ) if part)
    flight_nos = ", ".join(f["flight_no"] for f in flights)
    travel_date_val = flights[0]["date"] if flights else ""
    dep_time_val = flights[0]["dep_time_raw"] if flights else ""
    
    excel_data = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        booking_platform,
        pnr,
        booking_id,
        lead_pax,
        len(passengers),
        customer_phone,
        route_summary,
        travel_date_val,
        dep_time_val,
        flight_nos,
        total_fare,
        fare_type,
        refund_status,
        ticket_status,
        notes,
    ]
    if not is_dummy:
        try:
            save_booking(excel_data)
        except Exception as e:
            log.error("Error recording the booking: %s", e)

    cleanup_temp_files(flights)

    pdf_bytes = buffer.getvalue()
    # Writing a fixed ticket_output.pdf on every request races between
    # concurrent generations and fails on read-only hosts, so it is opt-in.
    if os.getenv("SAVE_LAST_PDF", "").lower() in ("1", "true", "yes"):
        try:
            with open(os.path.join(os.path.dirname(__file__), "ticket_output.pdf"), "wb") as f:
                f.write(pdf_bytes)
        except OSError as exc:
            log.warning("Could not write ticket_output.pdf: %s", exc)
    buffer.seek(0)

    dl_name = f"Ticket_{pnr or booking_id or 'output'}.pdf"
    return send_file(buffer, as_attachment=True, download_name=dl_name, mimetype="application/pdf")


ERROR_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }}</title>
    <style>
        *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
        body{font-family:Inter,-apple-system,Segoe UI,Arial,sans-serif;background:#eef2f6;
             color:#162033;min-height:100vh;display:flex;align-items:center;
             justify-content:center;padding:24px;line-height:1.6}
        .box{background:#fff;border:1px solid #d9e0ea;border-radius:14px;
             box-shadow:0 24px 70px rgba(15,39,66,.12);padding:32px;max-width:520px;
             width:100%;text-align:center}
        .code{font-size:3rem;font-weight:800;color:#0e9488;line-height:1}
        h1{color:#0f2742;font-size:1.3rem;margin:10px 0 6px}
        p{color:#667085;font-size:.9rem;margin-bottom:22px}
        a{display:inline-block;background:#0e9488;color:#fff;text-decoration:none;
          padding:12px 24px;border-radius:10px;font-weight:800;font-size:.88rem}
        a:hover{background:#0b7f75}
    </style>
</head>
<body>
    <div class="box">
        <div class="code">{{ status }}</div>
        <h1>{{ title }}</h1>
        <p>{{ message }}</p>
        <a href="/">Back to the generator</a>
    </div>
</body>
</html>"""


def _error_response(status, title, message):
    if request.path.startswith("/api/") or request.is_json:
        # Name the path. A host that rewrites requests can hand the app a path
        # the visitor never asked for, and a 404 that does not say which path
        # was not found gives no way to tell that apart from a real one.
        return jsonify({"error": title, "message": message,
                        "path": request.path}), status
    return render_template_string(ERROR_PAGE, status=status, title=title, message=message), status


@app.errorhandler(404)
def handle_not_found(_error):
    return _error_response(404, "Page not found", "That page does not exist.")


@app.errorhandler(413)
def handle_too_large(_error):
    return _error_response(
        413,
        "Upload too large",
        f"Files must be under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
    )


@app.errorhandler(500)
@app.errorhandler(Exception)
def handle_unexpected(error):
    # Let Flask's own HTTP errors (404, 413, ...) keep their status codes.
    if isinstance(error, HTTPException):
        return _error_response(error.code, error.name, error.description)
    log.exception("Unhandled error on %s %s", request.method, request.path)
    return _error_response(
        500,
        "Something went wrong",
        "The ticket could not be generated. Please try again, and check the "
        "server logs if this keeps happening.",
    )


if __name__ == "__main__":
    import sys
    port = int(os.environ.get("PORT", 5000))
    # Disable the reloader and debug mode for hosted environments that
    # restrict signal.signal usage (e.g., Streamlit Cloud).
    try:
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except Exception as e:
        print("Failed to start Flask dev server:", e)
        sys.exit(1)
