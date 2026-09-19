"""Where bookings are recorded, and where booking IDs come from.

The tracker and the booking-ID counter are the only state this app keeps. On a
host with an ephemeral filesystem - a free Render instance is rebuilt on every
deploy and every wake from idle - anything written beside the code goes with
it, which loses recorded bookings and re-seeds the counter so IDs already given
to customers are handed out a second time.

Set DATABASE_URL and both live in Postgres instead, outliving the container.
Leave it unset and this falls through to the spreadsheet, so local use and an
instance with a mounted disk behave exactly as before.
"""

import logging
import os
import threading
from datetime import datetime

import excel_tracker

log = logging.getLogger("ticket")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Seconds to wait for Postgres before giving up and using the spreadsheet.
CONNECT_TIMEOUT = int(os.environ.get("DB_CONNECT_TIMEOUT", "5"))

# Mirrors the spreadsheet's columns so a row reads the same in either store.
COLUMNS = (
    "generated_on", "booking_platform", "pnr", "booking_id", "lead_passenger",
    "total_pax", "customer_phone", "route", "travel_date", "departure_time",
    "flight_nos", "total_amount", "fare_type", "refund_status",
    "flight_status", "notes",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id               BIGSERIAL PRIMARY KEY,
    generated_on     TIMESTAMPTZ NOT NULL DEFAULT now(),
    booking_platform TEXT,
    pnr              TEXT,
    booking_id       TEXT,
    lead_passenger   TEXT,
    total_pax        INTEGER,
    customer_phone   TEXT,
    route            TEXT,
    travel_date      TEXT,
    departure_time   TEXT,
    flight_nos       TEXT,
    total_amount     TEXT,
    fare_type        TEXT,
    refund_status    TEXT,
    flight_status    TEXT,
    notes            TEXT
);

-- One row per prefix and day. The sequence is bumped inside a transaction that
-- holds a row lock, so two workers issuing IDs at once cannot read the same
-- value and hand out the same booking ID twice.
CREATE TABLE IF NOT EXISTS booking_counter (
    prefix   TEXT NOT NULL,
    day      DATE NOT NULL,
    sequence INTEGER NOT NULL,
    PRIMARY KEY (prefix, day)
);
"""

_pool = None
_pool_lock = threading.Lock()


def _connect():
    """Return a pooled connection, or None when Postgres is not configured.

    A failure here is never fatal: the caller falls back to the spreadsheet so
    a database that is unreachable degrades the tracker rather than stopping
    tickets from being issued.
    """
    global _pool
    if not DATABASE_URL:
        return None
    with _pool_lock:
        if _pool is None:
            try:
                from psycopg_pool import ConnectionPool

                # A free Postgres plan caps connections tightly, so keep the
                # pool small; gunicorn runs two workers, each with its own.
                # The timeouts are short on purpose: a database that is down
                # should drop us onto the spreadsheet in seconds rather than
                # leaving someone waiting on a ticket while the pool retries.
                _pool = ConnectionPool(
                    DATABASE_URL, min_size=1, max_size=3, timeout=CONNECT_TIMEOUT,
                    kwargs={"autocommit": True, "connect_timeout": CONNECT_TIMEOUT},
                    open=True,
                )
                with _pool.connection() as conn:
                    conn.execute(_SCHEMA)
                log.info("Booking store: Postgres ready.")
            except Exception as exc:
                log.error("Booking store: Postgres unavailable (%s) - "
                          "falling back to the spreadsheet.", exc)
                _pool = False
    return None if _pool is False else _pool


def save_booking(data):
    """Record one booking. Returns True when it was stored."""
    pool = _connect()
    if pool is None:
        return excel_tracker.save_to_excel(data)

    # The spreadsheet's first column is a running serial the database supplies
    # itself, and its first data cell is the timestamp this replaces.
    values = list(data[1:])
    values += [""] * (len(COLUMNS) - 1 - len(values))
    placeholders = ", ".join(["%s"] * (len(COLUMNS) - 1))
    try:
        with pool.connection() as conn:
            conn.execute(
                f"INSERT INTO bookings ({', '.join(COLUMNS[1:])}) "
                f"VALUES ({placeholders})",
                values[:len(COLUMNS) - 1],
            )
        return True
    except Exception as exc:
        log.error("Booking store: insert failed (%s) - writing to the "
                  "spreadsheet instead.", exc)
        return excel_tracker.save_to_excel(data)


def get_next_booking_id(platform_code="AT"):
    """Issue the next booking ID for today, as PREFIX-YYYYMMDD-NNNN."""
    pool = _connect()
    if pool is None:
        return excel_tracker.get_next_booking_id(platform_code)

    prefix = (platform_code or "AT").upper()
    today = datetime.now().date()
    try:
        with pool.connection() as conn:
            # ON CONFLICT ... DO UPDATE makes the read and the bump one
            # statement, so concurrent workers serialise on the row rather
            # than racing between a SELECT and an UPDATE.
            row = conn.execute(
                """
                INSERT INTO booking_counter (prefix, day, sequence)
                VALUES (%s, %s, 1)
                ON CONFLICT (prefix, day)
                DO UPDATE SET sequence = booking_counter.sequence + 1
                RETURNING sequence
                """,
                (prefix, today),
            ).fetchone()
        return f"{prefix}-{today.strftime('%Y%m%d')}-{row[0]:04d}"
    except Exception as exc:
        log.error("Booking store: could not issue an ID (%s) - falling back "
                  "to the spreadsheet counter.", exc)
        return excel_tracker.get_next_booking_id(platform_code)


def fetch_bookings(limit=None):
    """Every booking, newest first. Empty when Postgres is not configured."""
    pool = _connect()
    if pool is None:
        return []
    query = ("SELECT id, " + ", ".join(COLUMNS) +
             " FROM bookings ORDER BY id DESC")
    params = ()
    if limit:
        query += " LIMIT %s"
        params = (limit,)
    try:
        with pool.connection() as conn:
            return conn.execute(query, params).fetchall()
    except Exception as exc:
        log.error("Booking store: read failed (%s).", exc)
        return []


def backend():
    """Which store is in use, for /api/health."""
    return "postgres" if _connect() is not None else "spreadsheet"
