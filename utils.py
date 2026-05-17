import random
import io
import qrcode
from datetime import datetime, timedelta
from config import AIRLINE_NUMERIC_CODES

def generate_ticket_number(airline_code: str) -> str:
    """Generates a realistic 13-digit ticket number (e.g. 890-4521789630)"""
    numeric = AIRLINE_NUMERIC_CODES.get(airline_code.upper(), "000")
    serial = "".join([str(random.randint(0, 9)) for _ in range(10)])
    return f"{numeric}-{serial}"

def get_checkin_closing(dep_time_raw: str, route_type="domestic"):
    """Returns check-in closing time (45 mins prior for domestic, 60 for international)."""
    try:
        dep_dt = datetime.strptime(dep_time_raw, "%H:%M")
        minutes = 45 if route_type == "domestic" else 60
        checkin_dt = dep_dt - timedelta(minutes=minutes)
        return checkin_dt.strftime("%H:%M")
    except:
        return ""

def generate_qr(data: str, size=60):
    """Generates a QR code and returns it as an in-memory BytesIO object.
    
    Truncates data if exceeds QR v40 capacity (2953 bytes).
    """
    # QR v40 max capacity is ~2953 bytes
    max_data_length = 2953
    
    if len(data) > max_data_length:
        print(f"Warning: QR data truncated from {len(data)} to {max_data_length} bytes")
        data = data[:max_data_length]
    
    try:
        qr = qrcode.make(data)
        buf = io.BytesIO()
        qr.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"Error generating QR code: {e}")
        # Return a placeholder
        return None

def check_page_break(c, y, needed_height, margin_bottom=60, height=841.89):
    """Checks if remaining space is sufficient. If not, creates new page."""
    if y - needed_height < margin_bottom:
        c.showPage()
        return height - 50 # Reset y to top margin
    return y
