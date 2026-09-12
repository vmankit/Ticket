from flask import Flask, g, render_template, request, send_file, jsonify, render_template_string
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, black, white
from reportlab.platypus import Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode.code128 import Code128
import base64
import io
import json
import logging
import os
import re
import tempfile
import uuid
from html import escape
import urllib.error
import urllib.request
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
    print("⚠️ Warning: flask_wtf not installed. CSRF protection disabled. Run: pip install flask-wtf")

# ─── Import comprehensive airport DB from CSV extraction ────────────
from airports_data import AIRPORTS_DB
from excel_tracker import get_next_booking_id, save_to_excel
from utils import generate_qr, generate_ticket_number
from ticket_pdf import (
    TicketCanvas,
    draw_fares,
    draw_flight,
    draw_footer,
    draw_header,
    draw_layover,
    draw_passengers,
)
from ticket_parsing import (
    looks_like_own_ticket,
    normalize_text,
    ocr_available,
    ocr_pdf_bytes,
    parse_agency_ticket,
    parse_own_ticket,
    parse_stacked_itinerary,
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
    print("✓ CSRF protection enabled")
else:
    # Provide dummy csrf_token function for templates when CSRF not available
    @app.context_processor
    def inject_csrf_token():
        return dict(csrf_token=lambda: "")
    print("⚠️ CSRF protection disabled (flask_wtf not installed)")


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

# ─── Company Details ────────────────────────────────────────────────
COMPANY = {
    "name": "BHARAT HORIZON TRAVELS",
    "tagline": "Your Trusted Travel Partner",
    "email": "ankit.gupta200392@gmail.com",
    "phone": "+91 7759069422",
    "address": "NEW ASHOK NAGAR, Delhi, India",
    "pincode": "110096",
}

# ─── Airline Database (comprehensive) ───────────────────────────────
AIRLINES = {
    # Indian Airlines
    "6E": "IndiGo", "AI": "Air India", "UK": "Vistara", "SG": "SpiceJet",
    "G8": "Go First", "QP": "Akasa Air", "I5": "AirAsia India",
    "IX": "Air India Express", "S5": "Star Air", "2T": "TruJet",
    "8Y": "Pan Pacific Airlines", "E5": "Air Arabia India",
    # Middle East
    "EK": "Emirates", "EY": "Etihad Airways", "QR": "Qatar Airways",
    "FZ": "flydubai", "WY": "Oman Air", "GF": "Gulf Air", "SV": "Saudia",
    "XY": "flynas", "G9": "Air Arabia", "KU": "Kuwait Airways",
    "RJ": "Royal Jordanian", "ME": "Middle East Airlines",
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
    "UL": "SriLankan Airlines", "RA": "Nepal Airlines",
    "BG": "Biman Bangladesh Airlines", "PK": "Pakistan International Airlines",
    "WS": "WestJet",
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
    # Americas
    "AA": "American Airlines", "UA": "United Airlines",
    "DL": "Delta Air Lines", "WN": "Southwest Airlines",
    "B6": "JetBlue Airways", "AS": "Alaska Airlines",
    "NK": "Spirit Airlines", "F9": "Frontier Airlines",
    "HA": "Hawaiian Airlines", "AC": "Air Canada",
    "AM": "Aeromexico", "LA": "LATAM Airlines", "AV": "Avianca",
    "CM": "Copa Airlines", "G3": "Gol Linhas Aéreas",
    # Oceania & Africa
    "QF": "Qantas", "NZ": "Air New Zealand", "VA": "Virgin Australia",
    "JQ": "Jetstar", "ET": "Ethiopian Airlines",
    "SA": "South African Airways", "MS": "EgyptAir",
    "KQ": "Kenya Airways", "AT": "Royal Air Maroc", "WB": "RwandAir",
}

# ICAO / common 3-letter aliases mapped to their IATA code.
# This lets entries like "AIX 123" resolve the same way as "IX 123".
AIRLINE_ALIASES = {
    # India
    "IGO": "6E", "AIC": "AI", "VTI": "UK", "SEJ": "SG", "GOW": "G8",
    "AKJ": "QP", "IAD": "I5", "AIX": "IX", "AXB": "IX", "SDG": "S5",
    # Middle East
    "UAE": "EK", "ETD": "EY", "QTR": "QR", "FDB": "FZ", "OMA": "WY",
    "GFA": "GF", "SVA": "SV", "KAC": "KU", "RJA": "RJ", "MEA": "ME",
    "ABY": "G9",
    # Asia-Pacific
    "SIA": "SQ", "THA": "TG", "MAS": "MH", "GIA": "GA", "CPA": "CX",
    "PAL": "PR", "HVN": "VN", "KAL": "KE", "AAR": "OZ", "ANA": "NH",
    "JAL": "JL", "CAL": "CI", "EVA": "BR", "CSN": "CZ", "CES": "MU",
    "CCA": "CA", "CHH": "HU", "JSA": "3K", "TGW": "TR", "AXM": "AK",
    "AIQ": "FD", "AWQ": "QZ", "ALK": "UL", "RNA": "RA", "BBC": "BG",
    "PIA": "PK", "WJA": "WS",
    # Europe
    "BAW": "BA", "DLH": "LH", "AFR": "AF", "KLM": "KL", "SWR": "LX",
    "AUA": "OS", "ITY": "AZ", "IBE": "IB", "SAS": "SK", "FIN": "AY",
    "THY": "TK", "LOT": "LO", "TAP": "TP", "EIN": "EI", "BEL": "SN",
    "ROT": "RO", "ASL": "JU", "CTN": "OU", "RYR": "FR", "EZY": "U2",
    "WZZ": "W6", "VLG": "VY", "NAX": "DY",
    # Americas
    "AAL": "AA", "UAL": "UA", "DAL": "DL", "SWA": "WN", "JBU": "B6",
    "ASA": "AS", "NKS": "NK", "FFT": "F9", "HAL": "HA", "ACA": "AC",
    "AMX": "AM", "LAN": "LA", "AVA": "AV", "CMP": "CM", "GLO": "G3",
    # Oceania & Africa
    "QFA": "QF", "ANZ": "NZ", "VOZ": "VA", "JST": "JQ", "ETH": "ET",
    "SAA": "SA", "MSR": "MS", "KQA": "KQ", "RAM": "AT", "RWD": "WB",
}


# Cache airline payload at module level to avoid repeated dictionary rebuilding
_AIRLINE_PAYLOAD_CACHE = None

def airline_payload():
    """Returns cached airline payload dictionary."""
    global _AIRLINE_PAYLOAD_CACHE
    
    if _AIRLINE_PAYLOAD_CACHE is not None:
        return _AIRLINE_PAYLOAD_CACHE
    
    payload = {
        code: {"name": name, "iata": code}
        for code, name in AIRLINES.items()
    }
    for alias, iata in AIRLINE_ALIASES.items():
        if iata in AIRLINES:
            payload[alias] = {
                "name": AIRLINES[iata],
                "iata": iata,
                "alias_of": iata,
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
        rf"\b(\d{{1,2}})(?!\d)[\s\-.]*({month_names})[A-Z]*[\s\-.,']*(\d{{2,4}})\b",
        rf"\b({month_names})[A-Z]*[\s\-.]+(\d{{1,2}})(?!\d)[\s\-.,']+(\d{{2,4}})\b",
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


def to_12_hour(raw):
    """"14:05" -> "2:05 PM". Returns "" for anything unparseable."""
    try:
        return datetime.strptime((raw or "").strip(), "%H:%M").strftime("%-I:%M %p")
    except ValueError:
        return ""


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
        "MAKEMYTRIP": "MakeMyTrip",
        "CLEARTRIP": "Cleartrip",
        "GOIBIBO": "Goibibo",
        "PAYTM": "Paytm",
    }
    for key, name in mapping.items():
        if key in haystack:
            return name
    return ""


def extract_ticket_fields(text, filename=""):
    # Typographic dashes and currency signs are common in issuer PDFs and stop
    # every ASCII pattern below from matching.
    text = normalize_text(text or "")
    text_upper = text.upper()
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    platform = detect_platform(text_upper, filename)

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

    emails = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text_upper, flags=re.IGNORECASE)
    customer_email = emails[0] if emails else ""

    phone = ""
    phone_match = re.findall(r"\b\+?\d[\d\s\-]{8,}\d\b", text)
    for candidate in phone_match:
        digits = re.sub(r"\D", "", candidate)
        if 10 <= len(digits) <= 13:
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

    passengers = []
    if platform or pnr or booking_id or ("TRAVELLER" in text_upper or "PASSENGER" in text_upper):
        for line in lines:
            upper = line.upper()
            if any(keyword in upper for keyword in [
                "IMPORTANT", "PLEASE", "CARRY", "SECURITY", "CHECK-IN", "BOOKING", "ITINERARY",
                "E-TICKET", "TRIP", "ROUTE", "SEGMENT", "FARE", "PAYMENT", "AMOUNT",
                "BHARAT", "HORIZON", "TRAVELS", "ANKIT",
            ]):
                continue
            # Rows are often numbered ("1. Ms Alka Agarwal, YDKKHA"), so drop a
            # leading index before rejecting the line for containing digits.
            candidate = re.sub(r"^\s*\d{1,2}[.)]\s*", "", upper)
            match = re.search(
                r"\b(MR|MRS|MS|MISS|MSTR|CHD|INF)\.?\s+([A-Z][A-Z\s']{2,40}?)"
                r"(?=\s*[,|]|\s{2,}|\s+\d|$)", candidate)
            if match:
                name = re.sub(r"\s{2,}", " ", match.group(2)).strip()
                if len(name.split()) < 2:
                    continue
                if name and all(word not in STOPWORDS_3 for word in name.split()):
                    title_name = name.title()
                    if title_name not in [p["name"] for p in passengers]:
                        passengers.append({"name": title_name})

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
        "flights": segments,
        "passengers": passengers,
    }


def normalize_whatsapp_number(phone):
    """Normalize phone number to WhatsApp format (with country code).
    
    Handles various formats:
    - Indian: 9876543210, 09876543210 → 919876543210
    - With +91: +919876543210 → 919876543210
    - Invalid formats are rejected
    """
    if not phone:
        return ""
    
    # Remove all non-digit characters
    digits = re.sub(r"\D", "", str(phone))
    
    # Reject if obviously invalid
    if len(digits) < 10 or len(digits) > 15:
        return ""
    
    # Handle India numbers (10 or 11 digits)
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]  # Remove leading 0
    
    if len(digits) == 10:
        # Assume India if exactly 10 digits
        digits = "91" + digits
    elif len(digits) == 12 and digits.startswith("91"):
        # Already has India country code
        pass
    else:
        # For other countries, accept if 11-13 digits with country code
        if len(digits) < 11 or len(digits) > 15:
            return ""
    
    return digits


def encode_multipart_form(fields, files):
    boundary = "----AnkitTravelsBoundary" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    chunks = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(),
            b"\r\n",
        ])
    for name, file_info in files.items():
        filename, content_type, data = file_info
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            data,
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return boundary, b"".join(chunks)


def whatsapp_graph_request(path, payload=None, method="POST", content_type="application/json"):
    """Make WhatsApp Cloud API request with retry logic."""
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    api_version = os.getenv("WHATSAPP_API_VERSION", "v23.0").strip()
    if not token or not phone_number_id:
        raise RuntimeError("WhatsApp Cloud API is not configured. Set WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID.")

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/{path.lstrip('/')}"
    data = None
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "AnkitTravelsTicketGenerator/1.0"}
    if payload is not None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = content_type

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            error_data = json.loads(body)
            error_msg = error_data.get("error", {})
            if isinstance(error_msg, dict):
                error_text = error_msg.get("message", str(error_msg))
            else:
                error_text = str(error_msg)
            raise RuntimeError(f"WhatsApp API error {exc.code}: {error_text}")
        except (json.JSONDecodeError, TypeError):
            raise RuntimeError(f"WhatsApp API error {exc.code}: {body[:200]}")
    except urllib.error.URLError as exc:
        # DNS failure, refused connection or timeout — the most common real
        # failures. Surface them like the HTTP errors instead of raw urllib.
        raise RuntimeError(
            f"Could not reach the WhatsApp API ({exc.reason}). "
            "Check the server's network connection and try again."
        )


def send_pdf_to_whatsapp(pdf_bytes, filename, to_phone, caption):
    recipient = normalize_whatsapp_number(to_phone)
    if not recipient or len(recipient) < 11:
        raise RuntimeError("Customer WhatsApp number is invalid. Use country code, e.g. +91XXXXXXXXXX.")

    boundary, body = encode_multipart_form(
        fields={"messaging_product": "whatsapp", "type": "application/pdf"},
        files={"file": (filename, "application/pdf", pdf_bytes)},
    )
    media_response = whatsapp_graph_request("media", payload=body, content_type=f"multipart/form-data; boundary={boundary}")
    media_id = media_response.get("id")
    if not media_id:
        raise RuntimeError(f"WhatsApp media upload did not return a media id: {media_response}")

    message_payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "document",
        "document": {
            "id": media_id,
            "filename": filename,
            "caption": caption[:1024],
        },
    }
    return {
        "recipient": recipient,
        "api_response": whatsapp_graph_request("messages", payload=message_payload),
    }


print(f"[Bharat Horizon Travels] Loaded {len(AIRPORTS_DB)} airports from CSV database")
print(f"[Bharat Horizon Travels] Loaded {len(airline_payload())} airline code mappings")


# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html", fare_types=FARE_TYPES)


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
    """Surface optional-feature availability, so a missing tesseract binary on
    the server can be spotted without uploading a scan to find out."""
    return jsonify({
        "status": "ok",
        "airports": len(AIRPORTS_DB),
        "airlines": len(airline_payload()),
        "ocr": ocr_available(),
        "whatsapp": bool(
            os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
            and os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
        ),
    })


@app.route("/api/whatsapp-config", methods=["GET"])
def whatsapp_config():
    load_local_env()
    return jsonify({
        "access_token_configured": bool(os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()),
        "phone_number_id_configured": bool(os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()),
        "api_version": os.getenv("WHATSAPP_API_VERSION", "v23.0").strip(),
    })


@app.route("/api/next_booking_id", methods=["GET"])
def next_booking_id():
    platform = request.args.get("platform", "AT").strip().upper()
    platform_code = "".join(ch for ch in platform if ch.isalnum())[:6] or "AT"
    return jsonify({"booking_id": get_next_booking_id(platform_code)})


@app.route("/api/parse-ticket", methods=["POST"])
def parse_ticket():
    if "ticket_pdf" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    upload = request.files["ticket_pdf"]
    if not upload or not upload.filename:
        return jsonify({"error": "No file selected."}), 400

    filename = upload.filename
    if not filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported."}), 400

    if request.content_length and request.content_length > 15 * 1024 * 1024:
        return jsonify({"error": "File too large. Max 15MB."}), 400

    # Validate PDF magic bytes (security check)
    pdf_header = upload.read(4)
    upload.seek(0)
    if pdf_header != b"%PDF":
        return jsonify({"error": "Invalid PDF file. File does not start with PDF header."}), 400

    try:
        import pdfplumber
    except ImportError:
        return jsonify({"error": "pdfplumber is not installed. Run: pip install -r requirements.txt"}), 500

    try:
        data = upload.read()
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        text = "\n".join(pages).strip()
    except Exception as exc:
        log.error("Failed to read PDF: %s", exc)
        return jsonify({"error": f"Failed to read PDF: {exc}"}), 500

    # Too little embedded text means the pages are images, so read them with OCR.
    used_ocr = False
    if len(text) < 50:
        log.info("Only %d chars of text in %s - trying OCR.", len(text), filename)
        ocr_text = ocr_pdf_bytes(data)
        if len(ocr_text) > len(text):
            text, used_ocr = ocr_text, True
            log.info("OCR recovered %d chars from %s", len(text), filename)

    if len(text) < 50:
        if not ocr_available():
            log.warning("Scanned PDF and OCR is unavailable: %s", filename)
            return jsonify({
                "error": "This looks like a scanned or photographed ticket, and text "
                         "recognition is not installed on the server. Please fill the "
                         "form manually, or upload the original digital PDF.",
            }), 422
        log.warning("OCR could not read %s", filename)
        return jsonify({
            "error": "This looks like a scanned ticket, but the text could not be read. "
                     "Try a clearer scan or the original digital PDF, or fill the form manually.",
        }), 422

    log.info("Processing %s - %d chars%s", filename, len(text), " via OCR" if used_ocr else "")

    extracted = extract_ticket_fields(text, filename=filename)
    
    if not extracted.get("pnr") and not extracted.get("booking_id") and not extracted.get("flights"):
        if not extracted.get("booking_platform"):
            # Distinguish "this isn't a ticket" from "it is, but unreadable" —
            # they need different things from the user.
            ticket_markers = ("PNR", "FLIGHT", "PASSENGER", "BOARDING", "AIRLINE", "DEPART")
            hits = sum(1 for marker in ticket_markers if marker in text.upper())
            if hits < 2:
                log.warning("Upload does not look like a ticket: %s", filename)
                return jsonify({
                    "error": "This file does not look like a flight ticket. "
                             "Upload the e-ticket PDF you received from the airline or booking site.",
                }), 422
            log.warning("Ticket recognised but no booking details could be read.")
            return jsonify({
                "error": "This looks like a ticket, but the booking details could not be read "
                         "automatically. Please fill the form manually.",
                "raw_text_available": True,
            }), 422
        else:
            # Platform detected but no booking ID/PNR found
            log.info("Platform %s detected but booking ID/PNR missing", extracted.get("booking_platform"))
    
    extracted["raw_excerpt"] = text[:1200]
    if extracted.get("booking_platform"):
        log.info(
            "Extracted platform=%s pnr=%s booking_id=%s",
            extracted.get("booking_platform"), extracted.get("pnr"), extracted.get("booking_id"),
        )
    else:
        log.info("PDF parsed but platform not recognised; manual entry may be needed.")
    return jsonify(extracted)


@app.route("/api/test-extract", methods=["GET"])
def test_extract():
    return jsonify({"error": "PDF extraction is temporarily disabled. Please fill the form manually."}), 410


MAX_BARCODE_BYTES = 4 * 1024 * 1024

TICKET_STATUSES = ("Confirmed", "On Hold", "Waitlisted", "Cancelled", "Refunded")
# Printed on the ticket and recorded in the tracker, so the list is fixed here
# rather than trusting whatever the form posts back.
FARE_TYPES = (
    "REGULAR", "SAVER", "FLEXI", "NDC FARE", "SME FARE", "CORPORATE FARE",
    "STUDENT FARE", "SENIOR CITIZEN FARE", "ARMED FORCES FARE",
    "SPECIAL FARE", "RETAIL FARE", "TOUR FARE",
)
PASSENGER_TITLES = ("Mr", "Mrs", "Ms", "Mstr", "Dr")
PASSENGER_TYPES = ("Adult", "Child", "Infant")


def format_money(amount):
    return f"INR {amount:,.2f}"


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
    
    # ── Agency Contact ─────────────────────────────
    agency_phone = request.form.get("agency_phone", "").strip() or COMPANY["phone"]
    agency_email = request.form.get("agency_email", "").strip() or COMPANY["email"]
    
    # ── Custom Contact ─────────────────────────────
    customer_phone = request.form.get("customer_phone", "").strip()
    customer_email = request.form.get("customer_email", "").strip()
    
    refund_status = request.form.get("refund_status", "Refundable")
    fare_type = request.form.get("fare_type", "REGULAR").strip().upper()
    if fare_type not in FARE_TYPES:
        fare_type = "REGULAR"
    booking_platform = request.form.get("booking_platform", "Direct")
    is_dummy = request.form.get("is_dummy") == "true"

    ticket_status = request.form.get("ticket_status", "Confirmed").strip() or "Confirmed"
    if ticket_status not in TICKET_STATUSES:
        ticket_status = "Confirmed"
    trip_type = request.form.get("trip_type", "One Way").strip() or "One Way"

    # GST / company billing (used on Indian B2B invoices)
    gst_company = request.form.get("gst_company", "").strip()
    gstin = request.form.get("gstin", "").strip().upper()

    # The issue stamp defaults to now, but a ticket is sometimes written up
    # after the fact and has to carry the time it was actually issued.
    issued_at = parse_datetime_local(request.form.get("issued_at")) or datetime.now()

    # Free text printed under the amount. Replaces the automatic "Paid via X"
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
        # The 12-hour fields are filled by JavaScript on submit. Derive them
        # from the raw 24-hour values when they are missing, otherwise a
        # client without JS produces a ticket with no flight times on it.
        dep_time = request.form.get(f"{prefix}dep_time", "") or to_12_hour(dep_time_raw)
        arr_time = request.form.get(f"{prefix}arr_time", "") or to_12_hour(arr_time_raw)
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

        try:
            dep_dt = datetime.strptime(dep_time_raw, "%H:%M")
            checkin_dt = dep_dt - timedelta(minutes=60)
            checkin_closing = checkin_dt.strftime("%H:%M")
        except ValueError:
            checkin_closing = ""

        barcode_file = request.files.get(f"{prefix}barcode")
        temp_barcode_path = save_barcode_upload(barcode_file)

        # Auto-fill city from DB
        if not from_city and from_code in AIRPORTS_DB:
            from_city = AIRPORTS_DB[from_code]["city"]
        if not to_city and to_code in AIRPORTS_DB:
            to_city = AIRPORTS_DB[to_code]["city"]

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
            "to_code": to_code, "to_city": to_city, "to_full": to_full,
            "date": date_display, "date_raw": travel_date,
            "dep_time": dep_time, "arr_time": arr_time,
            "dep_time_raw": dep_time_raw, "arr_time_raw": arr_time_raw,
            "duration": duration, "class": travel_class,
            "seat": seat,
            "meal": meal,
            "checkin_bag": checkin_bag,
            "hand_bag": hand_bag,
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
    first_airline_code = ""
    if flights:
        first_airline_code = extract_airline_code(flights[0].get("flight_no", ""))
    for i in pax_indices:
        prefix = f"pax_{i}_"
        ticket_no = request.form.get(f"{prefix}ticket_no", "").strip()
        if not ticket_no:
            ticket_no = generate_ticket_number(first_airline_code)
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

        passengers.append({
            "name": request.form.get(f"{prefix}name", ""),
            "title": pax_title if pax_title in PASSENGER_TITLES else "",
            "pax_type": pax_type if pax_type in PASSENGER_TYPES else "Adult",
            "passport": request.form.get(f"{prefix}passport", ""),
            "dob": request.form.get(f"{prefix}dob", ""),
            "doe": request.form.get(f"{prefix}doe", ""),
            "ticket_no": ticket_no,
            "seat": single_seat,
            "meal": single_meal,
            "checkin_bag": single_ck,
            "hand_bag": single_hand,
            "seats_per_segment": seats_per_segment,
            "meals_per_segment": meals_per_segment,
            "checkin_per_segment": checkin_per_segment,
            "hand_per_segment": hand_per_segment,
        })

    # ── Fare details ─────────────────────────────────────────
    # Each field is parsed independently: a single malformed entry must not
    # silently zero the whole breakdown and issue an INR 0.00 ticket.
    fare_errors = []
    base_fare = parse_money(request.form.get("base_fare"), "Base Fare", fare_errors, required=True)
    taxes = parse_money(request.form.get("taxes_fees"), "Airline Taxes & Fees", fare_errors, required=True)
    insurance = parse_money(request.form.get("insurance"), "Insurance", fare_errors)
    meals_fee = parse_money(request.form.get("meals_fee"), "Meals", fare_errors)
    baggage_fee = parse_money(request.form.get("baggage_fee"), "Baggage", fare_errors)
    seats_fee = parse_money(request.form.get("seats_fee"), "Seats", fare_errors)
    zero_cancel = parse_money(request.form.get("zero_cancel"), "Zero Cancel", fare_errors)
    discount = parse_money(request.form.get("discount"), "Discount", fare_errors)

    total_fare = base_fare + taxes + insurance + meals_fee + baggage_fee + seats_fee + zero_cancel - discount
    if not fare_errors and total_fare < 0:
        fare_errors.append("Discount cannot exceed the total of all other fare components.")

    errors = (
        validate_booking(request.form)
        + validate_flights(flights)
        + validate_passengers(passengers)
        + fare_errors
    )
    if errors:
        cleanup_temp_files(flights)
        return render_validation_errors(errors)
    base_fare_str = format_money(base_fare)
    taxes_fees_str = format_money(taxes)
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

    draw_header(t, company=COMPANY, pnr=pnr, booking_id=booking_id, status=ticket_status)

    t.section("Itinerary")
    for index, flight in enumerate(flights):
        if index and flight.get("layover"):
            draw_layover(t, f"{flight['layover']} in {flight.get('from_city') or flight.get('from_code')}")
        draw_flight(t, flight, index, len(flights))

    t.section("Passengers")
    draw_passengers(t, passengers, flights)

    draw_fares(t, total_fare_str, remarks=remarks,
               gst_company=gst_company, gstin=gstin)

    draw_footer(
        t,
        company=COMPANY,
        issued=f"Issued {issued_at.strftime('%d %b %Y, %H:%M')} IST"
               f"  ·  {fare_type}  ·  {refund_status}",
        contact_line=f"{agency_email}  ·  {agency_phone}",
        terms="Show your barcode at the airport entry gate — it covers every flight on this "
              "ticket. Carry a valid government-issued photo ID. Check your baggage allowance "
              "and airline-specific requirements before heading to the airport. The PNR and "
              "ticket number above are your proof of purchase — keep this document handy until "
              "after travel.",
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
    dep_time_val = flights[0]["dep_time"] if flights else ""
    
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
            save_to_excel(excel_data)
        except Exception as e:
            log.error("Error saving to Excel: %s", e)

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
    if request.form.get("delivery_action") == "whatsapp":
        caption = (
            f"Dear {lead_pax}, your e-ticket is attached.\n"
            f"PNR: {pnr or '-'}\n"
            f"Route: {route_summary}\n"
            f"Travel Date: {travel_date_val or '-'}\n\n"
            f"Regards,\n{COMPANY['name']}"
        )
        try:
            result = send_pdf_to_whatsapp(pdf_bytes, dl_name, customer_phone, caption)
            api_resp = result.get("api_response", {})
            message_id = ""
            if api_resp.get("messages"):
                message_id = api_resp["messages"][0].get("id", "")
            return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WhatsApp Sent</title>
    <style>
        body{font-family:Inter,Arial,sans-serif;background:#eef2f6;color:#162033;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center}
        .box{background:#fff;border:1px solid #d9e0ea;border-radius:12px;box-shadow:0 24px 70px rgba(15,39,66,.12);padding:30px;max-width:520px}
        h1{margin:0 0 8px;color:#0f2742;font-size:24px}
        p{color:#667085;line-height:1.55}
        .ok{display:inline-block;background:#e7f7f5;color:#0e9488;padding:6px 10px;border-radius:999px;font-weight:800;font-size:12px;margin-bottom:14px}
        a{display:inline-block;margin-top:14px;background:#0e9488;color:#fff;text-decoration:none;padding:12px 18px;border-radius:10px;font-weight:800}
        small{display:block;margin-top:16px;color:#667085;word-break:break-all}
    </style>
</head>
<body>
    <div class="box">
        <div class="ok">WHATSAPP SENT</div>
        <h1>Ticket sent to {{ phone }}</h1>
        <p>The generated PDF ticket was uploaded and sent as a WhatsApp document.</p>
        <a href="/">Create another ticket</a>
        {% if message_id %}<small>Message ID: {{ message_id }}</small>{% endif %}
    </div>
</body>
</html>
            """, phone=customer_phone, message_id=message_id)
        except Exception as e:
            log.exception("WhatsApp delivery failed")
            return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WhatsApp Not Sent</title>
    <style>
        body{font-family:Inter,Arial,sans-serif;background:#eef2f6;color:#162033;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center}
        .box{background:#fff;border:1px solid #d9e0ea;border-radius:12px;box-shadow:0 24px 70px rgba(15,39,66,.12);padding:30px;max-width:620px}
        h1{margin:0 0 8px;color:#0f2742;font-size:24px}
        p{color:#667085;line-height:1.55}
        .bad{display:inline-block;background:#fff1f0;color:#c24135;padding:6px 10px;border-radius:999px;font-weight:800;font-size:12px;margin-bottom:14px}
        pre{white-space:pre-wrap;background:#f6f8fb;border:1px solid #d9e0ea;border-radius:8px;padding:12px;color:#162033;font-size:12px}
        a{display:inline-block;margin-top:14px;margin-right:10px;background:#0f2742;color:#fff;text-decoration:none;padding:12px 18px;border-radius:10px;font-weight:800}
        a.primary{background:#0e9488}
    </style>
</head>
<body>
    <div class="box">
        <div class="bad">WHATSAPP NOT SENT</div>
        <h1>Ticket generated, but WhatsApp failed</h1>
        <p>The ticket itself is fine — download it below and send it manually,
           or fix the problem and generate again.</p>
        <pre>{{ error }}</pre>
        <a class="primary" href="data:application/pdf;base64,{{ pdf_b64 }}" download="{{ filename }}">Download the ticket</a>
        <a href="/">Back to generator</a>
    </div>
</body>
</html>
            """,
                error=str(e),
                pdf_b64=base64.b64encode(pdf_bytes).decode("ascii"),
                filename=dl_name,
            ), 500

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
        return jsonify({"error": title, "message": message}), status
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
