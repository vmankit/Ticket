from flask import Flask, render_template, request, send_file, jsonify, render_template_string
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, black, white
from reportlab.platypus import Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode.code128 import Code128
import io
import json
import os
import re
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from functools import lru_cache
from openpyxl import Workbook, load_workbook
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

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

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
    "email": "ankitrajvm@gmail.com",
    "phone": "+91 7759069422",
    "address": "SUBHASH NAGAR, Dehradun, UTTARAKHAND, India",
    "pincode": "248002",
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


def airline_logo_url(iata_code):
    return f"https://images.kiwi.com/airlines/64/{iata_code}.png"


@lru_cache(maxsize=256)
def fetch_airline_logo_bytes(iata_code):
    if not iata_code:
        return None
    try:
        with urllib.request.urlopen(airline_logo_url(iata_code.upper()), timeout=1.5) as response:
            if getattr(response, "status", 200) != 200:
                return None
            data = response.read(120000)
            if data.startswith((b"\x89PNG", b"\xff\xd8", b"GIF")):
                return data
    except Exception:
        return None
    return None


# Cache airline payload at module level to avoid repeated dictionary rebuilding
_AIRLINE_PAYLOAD_CACHE = None

def airline_payload():
    """Returns cached airline payload dictionary."""
    global _AIRLINE_PAYLOAD_CACHE
    
    if _AIRLINE_PAYLOAD_CACHE is not None:
        return _AIRLINE_PAYLOAD_CACHE
    
    payload = {
        code: {"name": name, "iata": code, "logo": airline_logo_url(code)}
        for code, name in AIRLINES.items()
    }
    for alias, iata in AIRLINE_ALIASES.items():
        if iata in AIRLINES:
            payload[alias] = {
                "name": AIRLINES[iata],
                "iata": iata,
                "alias_of": iata,
                "logo": airline_logo_url(iata),
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
    word_match = re.search(r"\b(\d{1,2})\s*(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s*(\d{2,4})\b", value.upper())
    if word_match:
        day = int(word_match.group(1))
        month = MONTH_LOOKUP.get(word_match.group(2), 0)
        year = int(word_match.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return ""
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
    text = text or ""
    text_upper = text.upper()
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    platform = detect_platform(text_upper, filename)

    company_name = (COMPANY.get("name", "") or "").upper()
    if (company_name and company_name in text_upper and not platform) or "ANKIT TRAVELS" in text_upper:
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

    pnr = first_group([
        r"\bPNR\b\s*[:\-]?\s*([A-Z0-9]{5,8})",
        r"\bBOOKING\s*REF(?:ERENCE)?\b\s*[:\-]?\s*([A-Z0-9]{5,10})",
    ], require_digit=True)

    booking_id = first_group([
        r"\bBOOKING\s*ID\b\s*[:\-]?\s*([A-Z0-9\-]{5,20})",
        r"\bTRIP\s*ID\b\s*[:\-]?\s*([A-Z0-9\-]{5,20})",
        r"\bORDER\s*ID\b\s*[:\-]?\s*([A-Z0-9\-]{5,20})",
        r"\bTRANSACTION\s*ID\b\s*[:\-]?\s*([A-Z0-9\-]{5,20})",
    ], require_digit=True)

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
            if re.search(r"\d", upper):
                continue
            match = re.search(r"\b(MR|MRS|MS|MISS|MSTR|CHD|INF)\.?\s+([A-Z][A-Z\s']{2,40})\b", upper)
            if match:
                name = re.sub(r"\s{2,}", " ", match.group(2)).strip()
                if len(name.split()) < 2:
                    continue
                if name and all(word not in STOPWORDS_3 for word in name.split()):
                    title_name = name.title()
                    if title_name not in [p["name"] for p in passengers]:
                        passengers.append({"name": title_name})
    if not passengers and (platform or pnr or booking_id):
        for line in lines:
            upper = line.upper()
            if any(keyword in upper for keyword in ["PASSENGER", "TRAVELLER", "TRAVELER", "GUEST"]):
                continue
            if re.search(r"\b[A-Z]{2,}\b", upper) and len(upper.split()) <= 4:
                if all(word not in STOPWORDS_3 for word in upper.split()):
                    passengers.append({"name": line.title()})

    flight_numbers = []
    for match in re.finditer(r"\b([A-Z]{2,3})\s?-?\s?(\d{2,4})\b", text_upper):
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
        seg = {"from_code": from_code, "to_code": to_code}
        date_iso = find_first_date(upper)
        times = find_times(upper)
        if date_iso:
            seg["date"] = date_iso
        if times:
            seg["dep_time_raw"] = times[0]
            if len(times) > 1:
                seg["arr_time_raw"] = times[1]
        segments.append(seg)

    if not platform and not pnr and not booking_id and len(segments) > 6:
        segments = []

    # Remove exact duplicate segments
    if segments:
        seen = set()
        deduped = []
        for seg in segments:
            key = (
                seg.get("from_code", ""),
                seg.get("to_code", ""),
                seg.get("dep_time_raw", ""),
                seg.get("arr_time_raw", ""),
                seg.get("flight_no", ""),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(seg)
        segments = deduped

    if not segments and flight_numbers:
        segments = [{} for _ in flight_numbers]

    for idx, seg in enumerate(segments):
        if idx < len(flight_numbers):
            seg["flight_no"] = flight_numbers[idx]
            seg["airline"] = AIRLINES.get(extract_airline_code(flight_numbers[idx]), "")

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
    return render_template("index.html")


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
            "priority": priority
        })
    
    return jsonify(output)


@app.route("/api/airport-info", methods=["POST"])
def airport_info():
    data = request.get_json()
    code = data.get("code", "").strip().upper()
    info = AIRPORTS_DB.get(code, None)
    if info:
        return jsonify({"found": True, "city": info["city"], "airport": info["airport"], "country": info.get("country", "")})
    return jsonify({"found": False})


@app.route("/api/airlines", methods=["GET"])
def get_airlines():
    return jsonify(airline_payload())


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
        print(f"[PDF Extract Error] Failed to read PDF: {exc}")
        return jsonify({"error": f"Failed to read PDF: {exc}"}), 500

    if not text:
        print(f"[PDF Extract Warning] No readable text found in PDF: {filename}")
        return jsonify({"error": "No readable text found in PDF. (Might be a scanned image or corrupted file)"}), 422

    print(f"[PDF Extract] Processing {filename} - Text length: {len(text)} chars")
    if len(text) < 50:
        print(f"[PDF Extract Warning] Text too short ({len(text)} chars). First 500: {text}")
        return jsonify({"error": "PDF text extraction returned almost no content. This may be a scanned image. Please use a digital ticket PDF or fill details manually."}), 422
    
    extracted = extract_ticket_fields(text, filename=filename)
    
    if not extracted.get("pnr") and not extracted.get("booking_id"):
        if not extracted.get("booking_platform"):
            # No platform detected, try to find at least some booking info
            print(f"[PDF Extract Warning] Could not identify recognized platform. Text snippet: {text[:300]}")
            return jsonify({"error": "Could not recognize ticket format (MakeMyTrip, Paytm, Cleartrip, Goibibo not detected). Please fill details manually. You can also upload a ticket from supported platforms.", "detected_platform": extracted.get("booking_platform", "unknown"), "raw_text_available": True}), 422
        else:
            # Platform detected but no booking ID/PNR found
            print(f"[PDF Extract] Platform {extracted.get('booking_platform')} detected but missing booking ID/PNR")
    
    extracted["raw_excerpt"] = text[:1200]
    if extracted.get("booking_platform"):
        print(f"[PDF Extract Success] Detected platform: {extracted.get('booking_platform')}, PNR: {extracted.get('pnr')}, Booking ID: {extracted.get('booking_id')}")
    else:
        print(f"[PDF Extract] Completed but platform not recognized. Manual entry may be needed.")
    return jsonify(extracted)


@app.route("/api/test-extract", methods=["GET"])
def test_extract():
    return jsonify({"error": "PDF extraction is temporarily disabled. Please fill the form manually."}), 410


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
    fare_type = request.form.get("fare_type", "REGULAR")
    payment_method = request.form.get("payment_method", "UPI")
    card_last_4 = request.form.get("card_last_4", "")
    booking_platform = request.form.get("booking_platform", "Direct")
    is_dummy = request.form.get("is_dummy") == "true"

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
        dep_time = request.form.get(f"{prefix}dep_time", "")
        arr_time = request.form.get(f"{prefix}arr_time", "")
        duration = request.form.get(f"{prefix}duration", "")
        travel_class = request.form.get(f"{prefix}class", "Economy")
        seat = request.form.get(f"{prefix}seat", "")
        meal = request.form.get(f"{prefix}meal", "Not selected")
        checkin_bag = request.form.get(f"{prefix}checkin_bag", "")
        hand_bag = request.form.get(f"{prefix}hand_bag", "")

        try:
            dep_dt = datetime.strptime(dep_time_raw, "%H:%M")
            checkin_dt = dep_dt - timedelta(minutes=60)
            checkin_closing = checkin_dt.strftime("%H:%M")
        except:
            checkin_closing = ""

        barcode_file = request.files.get(f"{prefix}barcode")
        temp_barcode_path = None
        if barcode_file and barcode_file.filename:
            # Use secure filename with UUID to prevent path traversal attacks
            safe_filename = f"temp_barcode_{uuid.uuid4()}.png"
            temp_barcode_path = os.path.join(os.path.dirname(__file__), secure_filename(safe_filename))
            try:
                barcode_file.save(temp_barcode_path)
            except Exception as e:
                print(f"Error saving barcode: {e}")
                temp_barcode_path = None

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
                    print(f"Layover calc error: {e}")
                    continue
        except Exception as e:
            # Log error but don't fail entire PDF generation
            print(f"Layover calculation error: {e}")
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

        passengers.append({
            "name": request.form.get(f"{prefix}name", ""),
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
    try:
        base_fare = float(request.form.get("base_fare", 0))
        taxes = float(request.form.get("taxes_fees", 0))
        insurance = float(request.form.get("insurance", 0))
        meals_fee = float(request.form.get("meals_fee", 0))
        baggage_fee = float(request.form.get("baggage_fee", 0))
        seats_fee = float(request.form.get("seats_fee", 0))
        zero_cancel = float(request.form.get("zero_cancel", 0))
        discount = float(request.form.get("discount", 0))
    except:
        base_fare = taxes = insurance = meals_fee = baggage_fee = seats_fee = zero_cancel = discount = 0.0
        
    total_fare = base_fare + taxes + insurance + meals_fee + baggage_fee + seats_fee + zero_cancel - discount
    base_fare_str = f"INR {base_fare:,.2f}"
    taxes_fees_str = f"INR {taxes:,.2f}"
    total_fare_str = f"INR {total_fare:,.2f}"

    # Route summary
    if flights:
        route_summary = f"{flights[0]['from_code']} → {flights[-1]['to_code']}"
    else:
        route_summary = "N/A"

    # ══════════════════════════════════════════════════════════
    # PDF GENERATION
    # ══════════════════════════════════════════════════════════
    buffer = io.BytesIO()
    width, height = A4
    c = canvas.Canvas(buffer, pagesize=A4)

    # Modern ticket theme
    PRIMARY = HexColor("#0f2742")
    PRIMARY_DARK = HexColor("#081727")
    ACCENT = HexColor("#0e9488")
    ACCENT_SOFT = HexColor("#e7f7f5")
    LIGHT_BG = HexColor("#f5f8fb")
    WHITE = white
    DARK = HexColor("#162033")
    GRAY = HexColor("#667085")
    BORDER = HexColor("#d9e0ea")
    GREEN = HexColor("#14845f")
    GOLD = HexColor("#b7791f")
    DANGER = HexColor("#c24135")

    margin = 40
    usable_w = width - 2 * margin
    y = height - 30

    def draw_rounded_rect(x, y, w, h, r, fill_color=None, stroke_color=None):
        c.saveState()
        if fill_color:
            c.setFillColor(fill_color)
        if stroke_color:
            c.setStrokeColor(stroke_color)
            c.setLineWidth(0.5)
        p = c.beginPath()
        p.moveTo(x + r, y); p.lineTo(x + w - r, y)
        p.arcTo(x + w - r, y, x + w, y + r, -90, 90)
        p.lineTo(x + w, y + h - r)
        p.arcTo(x + w - r, y + h - r, x + w, y + h, 0, 90)
        p.lineTo(x + r, y + h)
        p.arcTo(x, y + h - r, x + r, y + h, 90, 90)
        p.lineTo(x, y + r)
        p.arcTo(x, y, x + r, y + r, 180, 90)
        p.close()
        if fill_color and stroke_color:
            c.drawPath(p, fill=1, stroke=1)
        elif fill_color:
            c.drawPath(p, fill=1, stroke=0)
        else:
            c.drawPath(p, fill=0, stroke=1)
        c.restoreState()

    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("cell", parent=styles["Normal"], fontSize=7.5, leading=10, textColor=DARK)
    header_style = ParagraphStyle("header", parent=styles["Normal"], fontSize=7, leading=9, textColor=WHITE, fontName="Helvetica-Bold")
    confirmed_style = ParagraphStyle("confirmed", parent=styles["Normal"], fontSize=7.5, leading=10, textColor=GREEN, fontName="Helvetica-Bold")

    def draw_monogram(x, y, size, label="AT"):
        draw_rounded_rect(x, y, size, size, 8, fill_color=ACCENT)
        c.setFillColor(WHITE)
        c.setFont("Helvetica-Bold", 13)
        c.drawCentredString(x + size / 2, y + size / 2 - 4, label)

    def draw_pill(x, y, text, fill_color, text_color=WHITE, pad_x=9):
        c.setFont("Helvetica-Bold", 7)
        tw = c.stringWidth(text, "Helvetica-Bold", 7)
        w = tw + pad_x * 2
        draw_rounded_rect(x, y, w, 18, 9, fill_color=fill_color, stroke_color=fill_color)
        c.setFillColor(text_color)
        c.drawCentredString(x + w / 2, y + 6, text)
        return w

    def draw_label_value(x, y, label, value, max_chars=28, value_color=DARK):
        c.setFillColor(GRAY)
        c.setFont("Helvetica", 6.8)
        c.drawString(x, y, label.upper())
        c.setFillColor(value_color)
        c.setFont("Helvetica-Bold", 9)
        value = str(value or "")
        c.drawString(x, y - 13, value[:max_chars])

    def draw_section_heading(title, y_pos):
        c.setFillColor(PRIMARY)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(margin + 2, y_pos, title)
        c.setStrokeColor(BORDER)
        c.setLineWidth(0.6)
        c.line(margin, y_pos - 6, margin + usable_w, y_pos - 6)

    def airline_logo_flowable(code, size=23):
        logo_bytes = fetch_airline_logo_bytes(code)
        if not logo_bytes:
            return None
        try:
            return Image(io.BytesIO(logo_bytes), width=size, height=size)
        except Exception:
            return None

    def draw_qr_box(x, y, data, caption):
        qr_img = Image(generate_qr(data), width=46, height=46)
        draw_rounded_rect(x, y, 58, 68, 8, fill_color=WHITE, stroke_color=BORDER)
        qr_img.drawOn(c, x + 6, y + 18)
        c.setFillColor(GRAY)
        c.setFont("Helvetica-Bold", 5.7)
        c.drawCentredString(x + 29, y + 7, caption[:18])

    # ══════════════════════════════════════════════════════════
    # HEADER
    # ══════════════════════════════════════════════════════════
    header_h = 92
    y -= header_h
    draw_rounded_rect(margin, y, usable_w, header_h, 10, fill_color=PRIMARY_DARK)
    c.setFillColor(ACCENT)
    c.rect(margin, y, usable_w, 4, stroke=0, fill=1)

    draw_monogram(margin + 18, y + 30, 42)

    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 19)
    c.drawString(margin + 72, y + 58, COMPANY["name"])
    c.setFillColor(HexColor("#b8c7d8"))
    c.setFont("Helvetica", 8)
    c.drawString(margin + 72, y + 34, f"{agency_email}  /  {agency_phone}")
    c.drawString(margin + 72, y + 20, f"{COMPANY['address']} - {COMPANY['pincode']}")

    # E-TICKET badge
    bw, bh = 122, 48
    bx = margin + usable_w - bw - 18
    by = y + 25
    draw_rounded_rect(bx, by, bw, bh, 9, fill_color=WHITE)
    c.setFillColor(PRIMARY)
    c.setFont("Helvetica-Bold", 15)
    c.drawCentredString(bx + bw / 2, by + 27, "E-TICKET")
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 7)
    c.drawCentredString(bx + bw / 2, by + 12, "STATUS: CONFIRMED")

    y -= 14

    # ══════════════════════════════════════════════════════════
    # ROUTE VISUAL
    # ══════════════════════════════════════════════════════════
    route_h = 82
    y -= route_h
    draw_rounded_rect(margin, y, usable_w, route_h, 10, fill_color=WHITE, stroke_color=BORDER)
    c.setFillColor(LIGHT_BG)
    c.rect(margin + 1, y + 1, usable_w - 2, 22, stroke=0, fill=1)

    from_code = flights[0]["from_code"] if flights else "FROM"
    to_code = flights[-1]["to_code"] if flights else "TO"
    from_city = flights[0]["from_city"] or from_code if flights else ""
    to_city = flights[-1]["to_city"] or to_code if flights else ""
    travel_date_text = flights[0]["date"] if flights else ""

    c.setFillColor(PRIMARY)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(margin + 16, y + route_h - 17, "TRIP ROUTE")
    draw_pill(margin + usable_w - 162, y + route_h - 20, f"{len(flights)} SEGMENT{'S' if len(flights) != 1 else ''}", ACCENT)

    route_mid_y = y + 39
    c.setStrokeColor(BORDER)
    c.setLineWidth(2)
    c.line(margin + 111, route_mid_y, margin + 300, route_mid_y)
    c.setFillColor(ACCENT)
    c.circle(margin + 111, route_mid_y, 5, stroke=0, fill=1)
    c.circle(margin + 300, route_mid_y, 5, stroke=0, fill=1)
    c.setStrokeColor(ACCENT)
    c.setLineWidth(1.4)
    c.line(margin + 123, route_mid_y, margin + 288, route_mid_y)
    c.setFillColor(PRIMARY)
    c.setFont("Helvetica-Bold", 23)
    c.drawString(margin + 16, y + 33, from_code)
    c.drawString(margin + 318, y + 33, to_code)
    c.setFillColor(GRAY)
    c.setFont("Helvetica", 7.2)
    c.drawString(margin + 16, y + 22, from_city[:24])
    c.drawString(margin + 318, y + 22, to_city[:24])
    c.setFillColor(PRIMARY)
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(margin + 205, y + 48, "NON-TRANSFERABLE")
    c.setFillColor(GRAY)
    c.setFont("Helvetica", 7)
    c.drawCentredString(margin + 205, y + 27, travel_date_text)

    draw_qr_box(
        margin + usable_w - 78,
        y + 7,
        f"PNR:{pnr}|BOOKING:{booking_id}|ROUTE:{route_summary}|PAX:{len(passengers)}",
        "PNR QR",
    )

    y -= 14

    # ══════════════════════════════════════════════════════════
    # BOOKING SUMMARY
    # ══════════════════════════════════════════════════════════
    section_h = 86
    y -= section_h
    draw_rounded_rect(margin, y, usable_w, section_h, 10, fill_color=LIGHT_BG, stroke_color=BORDER)

    c.setFillColor(PRIMARY)
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(margin + 15, y + section_h - 18, "BOOKING SUMMARY")
    draw_pill(margin + usable_w - 100, y + section_h - 23, "CONFIRMED", GREEN)

    col_w = usable_w / 3
    labels = [
        ("Booking ID", booking_id),
        ("Booking Date", booking_date_display),
        ("PNR / Booking Ref", pnr),
        ("Route", route_summary),
        ("Fare Type", fare_type),
        ("Refund Status", refund_status),
    ]
    for idx, (label, value) in enumerate(labels):
        row = idx // 3
        col = idx % 3
        cx = margin + 15 + col * col_w
        cy = y + section_h - 41 - (row * 28)
        value_color = DANGER if label == "Refund Status" and "Non-Refundable" in value else DARK
        draw_label_value(cx, cy, label, value, max_chars=24, value_color=value_color)

    y -= 16

    # ══════════════════════════════════════════════════════════
    # FLIGHT DETAILS (supports multiple flights / layover)
    # ══════════════════════════════════════════════════════════
    y -= 4
    draw_section_heading(f"FLIGHT DETAILS  ({len(flights)} Segment{'s' if len(flights) > 1 else ''})", y)
    y -= 18

    flight_headers = ["Flight No.", "Airline", "Departure", "Arrival", "Date", "Duration", "Class", "Seat / Meal / Baggage"]
    flight_col_w = [usable_w * 0.10, usable_w * 0.18, usable_w * 0.16, usable_w * 0.15, usable_w * 0.12, usable_w * 0.08, usable_w * 0.10, usable_w * 0.11]

    table_data = [[Paragraph(h, header_style) for h in flight_headers]]

    for fi, fl in enumerate(flights):
        checkin_txt = f"<br/><font size='5' color='#b7791f'>Check-in closes: {fl.get('checkin_closing', '')}</font>" if fl.get('checkin_closing') else ""
        logo = airline_logo_flowable(fl.get("airline_code"), size=22)
        airline_text = Paragraph(
            f"<b>{fl['airline'] or 'Airline'}</b><br/><font size='6' color='#667085'>{fl.get('airline_code', '')}</font>",
            cell_style,
        )
        if logo:
            airline_cell = Table(
                [[logo, airline_text]],
                colWidths=[25, flight_col_w[1] - 25],
                style=TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]),
            )
        else:
            airline_cell = airline_text
        row = [
            Paragraph(f"<b>{fl['flight_no']}</b>", cell_style),
            airline_cell,
            Paragraph(f"<b>{fl['from_full']}</b><br/><font color='#667085'>{fl['dep_time']}</font>{checkin_txt}", cell_style),
            Paragraph(f"<b>{fl['to_full']}</b><br/><font color='#667085'>{fl['arr_time']}</font>", cell_style),
            Paragraph(fl["date"], cell_style),
            Paragraph(fl["duration"], cell_style),
            Paragraph(fl["class"], cell_style),
            Paragraph(
                f"<b>Seat:</b> {fl.get('seat') or 'N/A'}<br/><b>Meal:</b> {fl.get('meal') or 'Not selected'}<br/><b>Bag:</b> {fl.get('checkin_bag') or 'Airline Default'} / {fl.get('hand_bag') or 'Airline Default'}",
                cell_style,
            ),
        ]
        table_data.append(row)

        # Add layover row between flights
        if fi < len(flights) - 1:
            next_fl = flights[fi + 1]
            layover_dur = next_fl.get("layover", "")
            if layover_dur:
                layover_text = f"{layover_dur} layover in {fl['to_city'] or fl['to_code']}"
            else:
                layover_text = f"Layover in {fl['to_city'] or fl['to_code']}"
            layover_para = Paragraph(f'<font color="#b7791f"><b>{layover_text}</b></font>', cell_style)
            layover_row = [layover_para] + [Paragraph("", cell_style)] * 6
            table_data.append(layover_row)

    t = Table(table_data, colWidths=flight_col_w)

    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("BACKGROUND", (0, 1), (-1, -1), WHITE),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, HexColor("#fbfcfe")]),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]

    # Color layover rows
    row_idx = 1
    for fi in range(len(flights)):
        row_idx += 1
        if fi < len(flights) - 1:
            # Layover row
            style_cmds.append(("BACKGROUND", (0, row_idx), (-1, row_idx), HexColor("#fff8ed")))
            style_cmds.append(("SPAN", (0, row_idx), (-1, row_idx)))
            row_idx += 1

    t.setStyle(TableStyle(style_cmds))
    tw, th = t.wrap(usable_w, 400)
    y -= th
    t.drawOn(c, margin, y)

    y -= 16

    # Show segment PNRs if provided (for multi-PNR connecting flights)
    segment_pnrs = [(i+1, fl.get("segment_pnr")) for i, fl in enumerate(flights) if fl.get("segment_pnr")]
    if segment_pnrs:
        pnr_text = "Segment PNRs: " + " | ".join([f"Flight {seg}: {pnr}" for seg, pnr in segment_pnrs])
        segment_pnr_para = Paragraph(f'<font size="7" color="#0f2742"><b>{pnr_text}</b></font>', cell_style)
        segment_pnr_para.wrapOn(c, usable_w, 20)
        segment_pnr_para.drawOn(c, margin, y)
        y -= 14

    # ══════════════════════════════════════════════════════════
    # PASSENGER DETAILS
    # ══════════════════════════════════════════════════════════
    draw_section_heading("PASSENGER DETAILS", y)
    
    if customer_phone or customer_email:
        c.setFillColor(GRAY); c.setFont("Helvetica", 8)
        c.drawRightString(margin + usable_w - 5, y + 1, f"{customer_phone}  /  {customer_email}")
        
    y -= 18

    pax_headers = ["No", "Passenger Name", "Sector", "Ticket Number", "Seat", "Meal", "Check-in\nBaggage", "Hand\nBaggage"]
    pax_col_w = [usable_w * 0.05, usable_w * 0.22, usable_w * 0.12, usable_w * 0.15, usable_w * 0.10, usable_w * 0.12, usable_w * 0.14, usable_w * 0.10]

    pax_table_data = [[Paragraph(h.replace("\n", "<br/>"), header_style) for h in pax_headers]]

    for pi, pax in enumerate(passengers):
        # Build Name + Passport details
        name_html = pax["name"]
        if pax["passport"] or pax["dob"] or pax["doe"]:
            name_html += "<br/><font color='#616161' size='6'>"
            if pax["passport"]: name_html += f"Passport: {pax['passport']}<br/>"
            if pax["dob"]: name_html += f"DOB: {pax['dob']}<br/>"
            if pax["doe"]: name_html += f"DOE: {pax['doe']}<br/>"
            name_html += "</font>"
            
        # Build Sector + Barcode array
        sector_elements = []
        for fl in flights:
            sector_str = f"{fl['from_code']}-{fl['to_code']}"
            sector_elements.append(Paragraph(sector_str, cell_style))
            
            if fl.get("barcode_path") and os.path.exists(fl["barcode_path"]):
                try:
                    img = Image(fl["barcode_path"], width=50, height=20)
                    sector_elements.append(img)
                except:
                    pass
            sector_elements.append(Spacer(1, 6))

        # Build seat/meal/baggage display per segment when available
        seats_html = ""
        meals_html = ""
        checkin_html = ""
        hand_html = ""
        if pax.get("seats_per_segment"):
            seats_html = "<br/>".join([f"Flight {idx+1}: {s or 'N/A'}" for idx, s in enumerate(pax.get("seats_per_segment"))])
        else:
            seats_html = pax.get("seat", "") or "N/A"

        if pax.get("meals_per_segment"):
            meals_html = "<br/>".join([f"Flight {idx+1}: {m or 'Not selected'}" for idx, m in enumerate(pax.get("meals_per_segment"))])
        else:
            meals_html = pax.get("meal", "Not selected")

        if pax.get("checkin_per_segment"):
            checkin_html = "<br/>".join([f"Flight {idx+1}: {v or 'Airline Default'}" for idx, v in enumerate(pax.get("checkin_per_segment"))])
        else:
            checkin_html = pax.get("checkin_bag", "Airline Default")

        if pax.get("hand_per_segment"):
            hand_html = "<br/>".join([f"Flight {idx+1}: {v or 'Airline Default'}" for idx, v in enumerate(pax.get("hand_per_segment"))])
        else:
            hand_html = pax.get("hand_bag", "Airline Default")

        row = [
            Paragraph(str(pi + 1), cell_style),
            Paragraph(name_html, cell_style),
            sector_elements,
            Paragraph(pax["ticket_no"], cell_style),
            Paragraph(seats_html, cell_style),
            Paragraph(meals_html, cell_style),
            Paragraph(checkin_html, cell_style),
            Paragraph(hand_html, cell_style),
        ]
        pax_table_data.append(row)

    pt = Table(pax_table_data, colWidths=pax_col_w)
    pt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("BACKGROUND", (0, 1), (-1, -1), WHITE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, HexColor("#fbfcfe")]),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    pw, ph = pt.wrap(usable_w, 300)
    y -= ph
    pt.drawOn(c, margin, y)

    y -= 16

    # ══════════════════════════════════════════════════════════
    # FARE DETAILS
    # ══════════════════════════════════════════════════════════
    draw_section_heading("FARE DETAILS", y)
    y -= 18

    fare_items = [
        ("Base Fare", base_fare),
        ("Airline Taxes & Fees", taxes),
        ("Insurance", insurance),
        ("Meals", meals_fee),
        ("Baggage", baggage_fee),
        ("Seats", seats_fee),
        ("Zero Cancel", zero_cancel),
        ("Discount", -discount)
    ]
    active_fare_items = [(lbl, val) for lbl, val in fare_items if val != 0 or lbl in ["Base Fare", "Airline Taxes & Fees"]]
    
    fare_h = 45 + (len(active_fare_items) * 14)
    if y - fare_h < 50:
        c.showPage(); y = height - 50
    y -= fare_h
    draw_rounded_rect(margin, y, usable_w, fare_h, 10, fill_color=WHITE, stroke_color=BORDER)

    # Left side — breakdown
    lx = margin + 15
    current_y = y + fare_h - 18
    for label, val in active_fare_items:
        c.setFillColor(GRAY); c.setFont("Helvetica", 8)
        if label == "Discount":
            c.setFillColor(HexColor("#e53935"))
        c.drawString(lx, current_y, label)
        c.setFillColor(DARK); c.setFont("Helvetica-Bold", 10)
        if label == "Discount":
            c.setFillColor(HexColor("#e53935"))
        c.drawString(lx + 120, current_y, f"INR {val:,.2f}")
        current_y -= 14

    # Divider
    c.setStrokeColor(BORDER); c.setLineWidth(0.5)
    c.line(lx, current_y + 6, lx + 200, current_y + 6)
    
    c.setFillColor(PRIMARY); c.setFont("Helvetica-Bold", 9)
    c.drawString(lx, current_y - 8, "Total Amount")
    c.setFillColor(ACCENT); c.setFont("Helvetica-Bold", 12)
    c.drawString(lx + 120, current_y - 8, total_fare_str)

    # Right side — Total highlight box
    fare_box_w = 160
    fare_box_h = 50
    fare_box_x = margin + usable_w - fare_box_w - 10
    fare_box_y = y + (fare_h - fare_box_h) / 2
    draw_rounded_rect(fare_box_x, fare_box_y, fare_box_w, fare_box_h, 9, fill_color=PRIMARY)

    c.setFillColor(HexColor("#b8c7d8")); c.setFont("Helvetica", 7)
    c.drawCentredString(fare_box_x + fare_box_w / 2, fare_box_y + 34, "TOTAL AMOUNT")
    c.setFillColor(WHITE); c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(fare_box_x + fare_box_w / 2, fare_box_y + 14, total_fare_str)

    # Payment details
    y -= 18
    pm_text = f"Paid via {payment_method}"
    if payment_method in ["Credit Card", "Debit Card"] and card_last_4:
        pm_text += f" ending in {card_last_4}"
    c.setFont("Helvetica-Bold", 7)
    pill_w = c.stringWidth("PAID", "Helvetica-Bold", 7) + 18
    pill_x = margin + usable_w - pill_w
    draw_pill(pill_x, y + 2, "PAID", GREEN)
    c.setFillColor(GRAY); c.setFont("Helvetica", 8)
    c.drawRightString(pill_x - 8, y + 8, pm_text)

    y -= 16

    y -= 20
    if y < 350:
        c.showPage()
        y = height - 50

    # ══════════════════════════════════════════════════════════
    # TERMS & CONDITIONS
    # ══════════════════════════════════════════════════════════
    draw_section_heading("TRAVEL CHECKLIST", y)
    y -= 14
    checklist_h = 48
    y -= checklist_h
    draw_rounded_rect(margin, y, usable_w, checklist_h, 10, fill_color=LIGHT_BG, stroke_color=BORDER)
    c.setFillColor(PRIMARY)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(margin + 14, y + 29, "Before airport arrival")
    c.setFillColor(DARK)
    c.setFont("Helvetica", 8)
    c.drawString(margin + 14, y + 16, "1) Complete web check-in before arriving at the airport.")
    c.drawString(margin + 275, y + 16, "2) Report at least 3 hours before flight departure.")
    y -= 16

    tc_data = [
        [Paragraph("Important Terms & Conditions", ParagraphStyle("tcheader", parent=styles["Normal"], fontSize=9, textColor=WHITE, fontName="Helvetica-Bold")), ""],
        [Paragraph("All Flight timings are shown in local timezones", cell_style), Paragraph("Change in the Name or Title of the Passenger is not allowed", cell_style)],
        [Paragraph("Carry Photo ID / Passport for Check-in. Carry a print-out or present this email for check-in. For Infant, it is mandatory to carry the Birth Certificate", cell_style), Paragraph("Customer to report to the Airport atleast 2 hours in Domestic and 3 hours in International Flights, before departure time", cell_style)],
        [Paragraph("Use Airline PNR while talking to Airlines<br/>Use Booking ID for all communication with Bharat Horizon Travels", cell_style), Paragraph("Check the Baggage Allowance – Cabin and Check-in – No Free Baggage Allowance for Infants. Meals, Seats, Special Requests are not guaranteed", cell_style)],
        [Paragraph("For cancellation/date change, Airlines Fees & Bharat Horizon Travels Service Fees will apply. Incase of no-show, tickets are non-refundable", cell_style), Paragraph("For International Trips, Ensure your passport is valid for more than 6 months. Please check Transit & Destination Visa Requirement", cell_style)],
        [Paragraph("Any Refund Claims arising due to cancellation / delay of flight by the Airline shall be subject to Bharat Horizon Travels receiving the refund from the Airline. In the Event Airline does not refund the amount to the Bharat Horizon Travels, Bharat Horizon Travels shall not be held liable for the same", cell_style), Paragraph("In case a booking confirmation e-mail and sms gets delayed or fails because of technical reasons or as a result of incorrect e-mail ID / phone number provided by the user etc, a ticket will be considered 'booked' as long as the ticket shows up on the confirmation page or in the User Login section of Bharat Horizon Travels", cell_style)],
        [Paragraph("Post booking, you should check/update your contact details on the airlines website to make sure you get the latest update directly from airlines. In SOTO fares, post booking functions are not supported. Please contact airlines directly", cell_style), Paragraph("Convenience, Trip Care, Zero Cancel & Cash Back Sign-Up Fees are not refundable under any circumstances, once a confirmed ticket is booked/generated. If you are booking a special discounted return fare then both flights have to be cancelled together", cell_style)],
        [Paragraph("Cancellation of Flight Ticket upto 24 hours will be dealt by Bharat Horizon Travels. Less than 24 hours, you should cancel it directly with the airlines and inform us for the refund processing", cell_style), Paragraph("GST Credit (if applicable) will be provided directly by the airlines to the traveller", cell_style)],
    ]
    
    tc_table = Table(tc_data, colWidths=[usable_w/2, usable_w/2])
    tc_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("SPAN", (0, 0), (-1, 0)),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("BACKGROUND", (0, 1), (-1, -1), WHITE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, HexColor("#fbfcfe")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    tw, th = tc_table.wrap(usable_w, 800)
    if y - th < 120:
        c.showPage()
        y = height - 50
    y -= th
    tc_table.drawOn(c, margin, y)

    y -= 25
    c.setFillColor(DARK); c.setFont("Helvetica", 8)
    c.drawCentredString(width/2, y, "Bharat Horizon Travels is not liable for any Discrepancy / Deficiency in service by the Airline or Service Providers. Any discrepancy regarding")
    y -= 11
    c.drawCentredString(width/2, y, "this ticket, please inform us within 3 hrs of Issuance. After that we are not liable for any changes.")
    y -= 13
    c.setFillColor(ACCENT)
    c.drawCentredString(width/2, y, "DGCA Passenger Charter – Check here")
    
    y -= 20
    c.setStrokeColor(HexColor("#e0e0e0")); c.setLineWidth(0.5)
    c.line(margin, y, margin + usable_w, y)
    y -= 15
    c.setFillColor(DARK); c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(width/2, y, "Bharat Horizon Travels")
    y -= 14
    c.setFont("Helvetica", 10)
    c.drawCentredString(width/2, y, "Dehradun")
    y -= 14
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(width/2, y, "Have a Nice Trip")
    y -= 10
    c.line(margin, y, margin + usable_w, y)

    # Decorative side strips
    c.setFillColor(PRIMARY)
    c.rect(0, 0, 8, height, fill=1, stroke=0)
    c.setFillColor(ACCENT)
    c.rect(width - 8, 0, 8, height, fill=1, stroke=0)

    c.save()
    buffer.seek(0)
    
    # ── Save Tracking to Excel ──────────────────────────────────
    lead_pax = passengers[0]["name"] if passengers else "Unknown"
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
        payment_method,
        fare_type,
        refund_status,
        "Confirmed",
        ""
    ]
    if not is_dummy:
        try:
            save_to_excel(excel_data)
        except Exception as e:
            print(f"Error saving to Excel: {e}")

    # Clean up temp barcode images
    for fl in flights:
        p = fl.get("barcode_path")
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except:
                pass

    output_path = os.path.join(os.path.dirname(__file__), "ticket_output.pdf")
    pdf_bytes = buffer.getvalue()
    with open(output_path, "wb") as f:
        f.write(pdf_bytes)
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
        details{margin-top:20px;padding-top:15px;border-top:1px solid #eee;font-size:11px;color:#999}
        pre{background:#f8f9fa;padding:8px;overflow:auto;max-height:200px}
    </style>
</head>
<body>
    <div class="box">
        <div class="ok">WHATSAPP SENT</div>
        <h1>Ticket sent to {{ phone }}</h1>
        <p>The generated PDF ticket was uploaded and sent as a WhatsApp document.</p>
        <a href="/">Create another ticket</a>
        {% if message_id %}<small>Message ID: {{ message_id }}</small>{% endif %}
        
        <details>
            <summary>API Response Details (Debug)</summary>
            <pre>{{ full_response | tojson(indent=2) }}</pre>
        </details>
    </div>
</body>
</html>
            """, phone=customer_phone, message_id=message_id, full_response=api_resp)
        except Exception as e:
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
        a{display:inline-block;margin-top:14px;background:#0f2742;color:#fff;text-decoration:none;padding:12px 18px;border-radius:10px;font-weight:800}
    </style>
</head>
<body>
    <div class="box">
        <div class="bad">WHATSAPP NOT SENT</div>
        <h1>Ticket generated, but WhatsApp failed</h1>
        <p>The PDF is saved as <b>ticket_output.pdf</b>. Check the setup/error below.</p>
        <pre>{{ error }}</pre>
        <a href="/">Back to generator</a>
    </div>
</body>
</html>
            """, error=str(e)), 500

    return send_file(buffer, as_attachment=True, download_name=dl_name, mimetype="application/pdf")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
