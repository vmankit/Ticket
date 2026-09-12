"""Server-side validation for ticket generation.

The browser form carries `required` attributes, but those are trivially
bypassed (curl, a disabled-JS client, a stale tab). Everything that reaches
/generate is validated here before a PDF is produced or a row is written to
the tracker.
"""

import re
from datetime import datetime, timedelta

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")
PNR_RE = re.compile(r"^[A-Z0-9]{4,10}$")
AIRPORT_CODE_RE = re.compile(r"^[A-Z]{3}$")
FLIGHT_NO_RE = re.compile(r"^[A-Z0-9]{2,3}\s?\d{1,4}[A-Z]?$")

MAX_FLIGHTS = 8
MAX_PASSENGERS = 9
MAX_FARE = 10_000_000


class ValidationError(Exception):
    """Raised when submitted ticket data cannot produce a valid ticket."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(f"{len(errors)} validation error(s)")


def parse_money(raw, field_label, errors, required=False, allow_negative=False):
    """Parse a money field, tolerating '3,500', '₹3500', 'INR 3 500.50'.

    Each field is parsed independently so one malformed entry cannot silently
    zero out an entire fare breakdown.
    """
    text = str(raw or "").strip()
    if not text:
        if required:
            errors.append(f"{field_label} is required.")
        return 0.0

    cleaned = re.sub(r"[,\s₹]|INR|RS\.?", "", text, flags=re.IGNORECASE)
    try:
        value = float(cleaned)
    except ValueError:
        errors.append(f"{field_label} must be a number (got \"{text}\").")
        return 0.0

    if value != value or value in (float("inf"), float("-inf")):
        errors.append(f"{field_label} must be a real number.")
        return 0.0
    if not allow_negative and value < 0:
        errors.append(f"{field_label} cannot be negative.")
        return 0.0
    if abs(value) > MAX_FARE:
        errors.append(f"{field_label} is unrealistically large.")
        return 0.0
    return round(value, 2)


def _parse_date(value):
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _parse_time(value):
    try:
        return datetime.strptime(str(value).strip(), "%H:%M")
    except (ValueError, TypeError):
        return None


def validate_booking(form):
    """Validate the booking-level fields. Returns a list of error strings."""
    errors = []

    pnr = (form.get("pnr") or "").strip().upper()
    if not pnr:
        errors.append("PNR / Booking Reference is required.")
    elif not PNR_RE.match(pnr):
        errors.append("PNR must be 4-10 letters or digits (no spaces or symbols).")

    if not (form.get("booking_platform") or "").strip():
        errors.append("Booking Platform is required.")

    email = (form.get("customer_email") or "").strip()
    if not email:
        errors.append("Customer Email is required.")
    elif not EMAIL_RE.match(email):
        errors.append(f"Customer Email \"{email}\" is not a valid email address.")

    phone = (form.get("customer_phone") or "").strip()
    digits = re.sub(r"\D", "", phone)
    if not phone:
        errors.append("Customer Phone is required.")
    elif not 7 <= len(digits) <= 15:
        errors.append(f"Customer Phone \"{phone}\" does not look like a valid number.")

    booking_date = (form.get("booking_date") or "").strip()
    if booking_date and _parse_date(booking_date) is None:
        errors.append("Booking Date must be a valid date.")

    return errors


def validate_flights(flights):
    """Validate parsed flight segments. `flights` is the list built in /generate."""
    errors = []

    if not flights:
        errors.append("At least one flight segment is required.")
        return errors
    if len(flights) > MAX_FLIGHTS:
        errors.append(f"At most {MAX_FLIGHTS} flight segments are supported.")
        return errors

    for idx, flight in enumerate(flights, start=1):
        label = f"Flight {idx}"

        flight_no = (flight.get("flight_no") or "").strip().upper()
        if not flight_no:
            errors.append(f"{label}: Flight Number is required.")
        elif not FLIGHT_NO_RE.match(flight_no):
            errors.append(
                f"{label}: Flight Number \"{flight_no}\" is not valid "
                "(expected something like \"6E 2341\")."
            )

        from_code = (flight.get("from_code") or "").strip().upper()
        to_code = (flight.get("to_code") or "").strip().upper()
        for code, side in ((from_code, "From"), (to_code, "To")):
            if not code:
                errors.append(f"{label}: {side} airport code is required.")
            elif not AIRPORT_CODE_RE.match(code):
                errors.append(
                    f"{label}: {side} airport code \"{code}\" must be 3 letters (e.g. DEL)."
                )
        if from_code and from_code == to_code:
            errors.append(f"{label}: departure and arrival airports cannot both be {from_code}.")

        if _parse_date(flight.get("date_raw")) is None:
            errors.append(f"{label}: Travel Date is required and must be a valid date.")
        if _parse_time(flight.get("dep_time_raw")) is None:
            errors.append(f"{label}: Departure time is required (HH:MM).")
        if _parse_time(flight.get("arr_time_raw")) is None:
            errors.append(f"{label}: Arrival time is required (HH:MM).")

    # Connecting segments must run forward in time and actually connect.
    for idx in range(len(flights) - 1):
        current, following = flights[idx], flights[idx + 1]
        label = f"Flight {idx + 2}"

        if current.get("to_code") and following.get("from_code"):
            if current["to_code"] != following["from_code"]:
                errors.append(
                    f"{label}: departs from {following['from_code']} but "
                    f"Flight {idx + 1} arrives at {current['to_code']}."
                )

        current_date = _parse_date(current.get("date_raw"))
        current_arr = _parse_time(current.get("arr_time_raw"))
        current_dep = _parse_time(current.get("dep_time_raw"))
        following_date = _parse_date(following.get("date_raw"))
        following_dep = _parse_time(following.get("dep_time_raw"))
        if not all([current_date, current_arr, current_dep, following_date, following_dep]):
            continue

        arrival = current_date.replace(hour=current_arr.hour, minute=current_arr.minute)
        departure = current_date.replace(hour=current_dep.hour, minute=current_dep.minute)
        if arrival < departure:  # flight lands the next day
            arrival += timedelta(days=1)
        next_departure = following_date.replace(
            hour=following_dep.hour, minute=following_dep.minute
        )
        if next_departure < arrival:
            errors.append(
                f"{label}: departs before Flight {idx + 1} arrives. "
                "Check the travel dates and times."
            )

    return errors


def validate_passengers(passengers):
    errors = []

    if not passengers:
        errors.append("At least one passenger is required.")
        return errors
    if len(passengers) > MAX_PASSENGERS:
        errors.append(f"At most {MAX_PASSENGERS} passengers are supported per ticket.")
        return errors

    for idx, passenger in enumerate(passengers, start=1):
        name = (passenger.get("name") or "").strip()
        if not name:
            errors.append(f"Passenger {idx}: name is required.")
        elif len(name) < 2:
            errors.append(f"Passenger {idx}: name \"{name}\" is too short.")
        elif not re.match(r"^[A-Za-z][A-Za-z .'\-]*$", name):
            errors.append(
                f"Passenger {idx}: name \"{name}\" may only contain "
                "letters, spaces, apostrophes, hyphens and periods."
            )

    return errors
