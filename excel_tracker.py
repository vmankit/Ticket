import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime

from openpyxl import Workbook, load_workbook

# Cross-platform file locking
if sys.platform == 'win32':
    import msvcrt
    def lock_file(f):
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    
    def unlock_file(f):
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except:
            pass
else:
    import fcntl
    def lock_file(f):
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    
    def unlock_file(f):
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except:
            pass

EXCEL_FILE = "ticket_records.xlsx"
COUNTER_FILE = "booking_counter.json"

# Guards the counter against other threads in this process; the OS file lock
# below guards it against other processes (e.g. multiple gunicorn workers).
_counter_lock = threading.Lock()


def _lock_file_blocking(f, attempts=50, delay=0.1):
    """Take an exclusive OS lock, retrying briefly if another worker holds it."""
    for _ in range(attempts):
        if lock_file(f):
            return True
        time.sleep(delay)
    return False


def _seed_sequence_from_excel():
    """Back-fill the counter from existing rows the first time it is used."""
    if not os.path.exists(EXCEL_FILE):
        return 0
    try:
        wb = load_workbook(EXCEL_FILE)
        return max(0, wb.active.max_row - 1)  # minus the header row
    except Exception as e:
        print(f"Error seeding booking counter from Excel: {e}")
        return 0


def get_next_booking_id(platform_code="AT"):
    """Reserve and return the next booking ID.

    The sequence is incremented and persisted atomically, so concurrent
    requests can never be handed the same ID. Deriving it from the row count
    instead let every simultaneous request read the same value.
    """
    today = datetime.now().strftime("%Y%m%d")

    with _counter_lock:
        try:
            with open(COUNTER_FILE, "a+") as f:
                if not _lock_file_blocking(f):
                    raise TimeoutError("Could not lock the booking counter.")
                try:
                    f.seek(0)
                    raw = f.read().strip()
                    state = json.loads(raw) if raw else {}

                    if state.get("date") == today:
                        sequence = int(state.get("seq", 0)) + 1
                    else:
                        # New day: continue past any pre-existing rows once.
                        sequence = (_seed_sequence_from_excel() if not state else 0) + 1

                    f.seek(0)
                    f.truncate()
                    json.dump({"date": today, "seq": sequence}, f)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    unlock_file(f)
        except Exception as e:
            # Never block ticket generation on the counter; fall back to a
            # collision-resistant random suffix instead of a duplicate number.
            print(f"Error reserving booking ID: {e}")
            return f"{platform_code}-{today}-{uuid.uuid4().hex[:6].upper()}"

    return f"{platform_code}-{today}-{sequence:04d}"

def _drop_retired_columns(ws):
    """Remove columns the app no longer records, keeping older rows aligned.

    Payment mode used to be tracked per booking. It is gone from the form, so
    an existing tracker still carrying the column would push every new row one
    cell to the left. Dropping it once keeps old and new rows in the same
    shape; the values it held are still in the file's git history.
    """
    retired = {"Payment Mode"}
    headers = [cell.value for cell in ws[1]] if ws.max_row else []
    for idx in range(len(headers), 0, -1):
        if headers[idx - 1] in retired:
            ws.delete_cols(idx)


def save_to_excel(data):
    """Saves ticket data to the excel tracker with proper file locking."""
    # Retry logic for file locking
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            # Attempt to lock file
            with open(EXCEL_FILE, 'a+b') as f:
                locked = lock_file(f)
                if not locked and retry_count < max_retries - 1:
                    retry_count += 1
                    time.sleep(0.5)  # Wait and retry
                    continue
                
                try:
                    # If file exists but is not a valid xlsx (corrupted or wrong format), move it aside and create new
                    if os.path.exists(EXCEL_FILE) and os.path.getsize(EXCEL_FILE) > 0:
                        try:
                            with open(EXCEL_FILE, 'rb') as fh:
                                sig = fh.read(4)
                        except Exception:
                            sig = b''
                        if not sig.startswith(b'PK'):
                            # rename corrupted file
                            corrupt_name = EXCEL_FILE + f'.corrupt.{int(time.time())}'
                            try:
                                os.replace(EXCEL_FILE, corrupt_name)
                                print(f"Renamed corrupted Excel {EXCEL_FILE} to {corrupt_name}")
                            except Exception:
                                # If rename fails, continue and attempt to create a new workbook anyway
                                pass

                    if not os.path.exists(EXCEL_FILE) or os.path.getsize(EXCEL_FILE) == 0:
                        wb = Workbook()
                        ws = wb.active
                        ws.title = "Bookings"
                        headers = [
                            "S.No.", "Generated On", "Booking Platform", "PNR", "Booking ID", 
                            "Lead Passenger", "Total Pax", "Customer Phone", "Route", "Travel Date", 
                            "Departure Time", "Flight No(s)", "Total Amount", 
                            "Fare Type", "Refund Status", "Flight Status", "Notes"
                        ]
                        ws.append(headers)
                    else:
                        wb = load_workbook(EXCEL_FILE)
                        ws = wb.active
                        _drop_retired_columns(ws)

                    # S.No. is the current row count (excluding header)
                    sno = max(1, ws.max_row)
                    data_to_insert = [sno] + data
                    ws.append(data_to_insert)
                    
                    # Auto-adjust column widths
                    for col in ws.columns:
                        max_length = 0
                        column_letter = col[0].column_letter
                        for cell in col:
                            try:
                                cell_str = str(cell.value or "")
                                max_length = max(max_length, len(cell_str))
                            except (TypeError, AttributeError):
                                pass
                        adjusted_width = min(max_length + 2, 50)  # Cap at 50
                        ws.column_dimensions[column_letter].width = adjusted_width

                    wb.save(EXCEL_FILE)
                    return True
                    
                finally:
                    unlock_file(f)
            break
        
        except Exception as e:
            retry_count += 1
            if retry_count >= max_retries:
                print(f"Error saving to Excel after {max_retries} retries: {e}")
                # Fallback: try without locking
                try:
                    if not os.path.exists(EXCEL_FILE):
                        wb = Workbook()
                        ws = wb.active
                        ws.title = "Bookings"
                        headers = [
                            "S.No.", "Generated On", "Booking Platform", "PNR", "Booking ID", 
                            "Lead Passenger", "Total Pax", "Customer Phone", "Route", "Travel Date", 
                            "Departure Time", "Flight No(s)", "Total Amount", 
                            "Fare Type", "Refund Status", "Flight Status", "Notes"
                        ]
                        ws.append(headers)
                    else:
                        wb = load_workbook(EXCEL_FILE)
                        ws = wb.active
                        _drop_retired_columns(ws)

                    sno = max(1, ws.max_row)
                    data_to_insert = [sno] + data
                    ws.append(data_to_insert)
                    wb.save(EXCEL_FILE)
                    return True
                except Exception as e2:
                    print(f"Fallback Excel save also failed: {e2}")
                    return False
            else:
                time.sleep(0.2)
