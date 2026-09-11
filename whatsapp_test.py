#!/usr/bin/env python3
"""Send one test e-ticket over WhatsApp to check the Cloud API setup.

Usage:
    python whatsapp_test.py 7759069422
    python whatsapp_test.py +971501234567 --dry-run

Reads WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_API_VERSION
from .env or the environment, generates a real ticket PDF, and sends it.
Nothing is written to the booking tracker.
"""
import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phone", help="recipient, e.g. 7759069422 or +971501234567")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the PDF and check config, but send nothing")
    args = parser.parse_args()

    os.environ.setdefault("SECRET_KEY", "whatsapp-test")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import app

    app.load_local_env()
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    version = os.getenv("WHATSAPP_API_VERSION", "v23.0").strip()

    print("Configuration")
    print(f"  access token      : {'set (%d chars)' % len(token) if token else 'MISSING'}")
    print(f"  phone number id   : {'set' if phone_id else 'MISSING'}")
    print(f"  api version       : {version}")
    if not token or not phone_id:
        print("\nSet both in .env before running. Nothing was sent.")
        return 1

    recipient = app.normalize_whatsapp_number(args.phone)
    print(f"  recipient         : {args.phone!r} -> {recipient or 'REJECTED'}")
    if not recipient:
        print("\nThat number could not be normalised. Include the country code.")
        return 1

    app.app.config["WTF_CSRF_ENABLED"] = False
    client = app.app.test_client()
    response = client.post("/generate", data={
        "booking_platform": "Direct/Walk-in", "pnr": "TEST01",
        "customer_phone": args.phone, "customer_email": "test@example.com",
        "base_fare": "3500", "taxes_fees": "850",
        "flight_count": "1", "pax_count": "1",
        "flight_0_flight_no": "6E 2341", "flight_0_from_code": "DEL",
        "flight_0_to_code": "BOM", "flight_0_date": "2026-10-01",
        "flight_0_dep_time_raw": "10:00", "flight_0_arr_time_raw": "12:10",
        "pax_0_name": "TEST PASSENGER", "pax_0_title": "Mr",
        "is_dummy": "true",
    })
    if response.status_code != 200 or response.data[:4] != b"%PDF":
        print(f"\nCould not build a test PDF (HTTP {response.status_code}).")
        print(response.data[:600].decode("utf-8", "replace"))
        return 1
    print(f"\nTest ticket built: {len(response.data):,} bytes")

    if args.dry_run:
        print("Dry run - nothing sent.")
        return 0

    print(f"Sending to {recipient} ...")
    try:
        result = app.send_pdf_to_whatsapp(
            response.data, "Ticket_TEST01.pdf", args.phone,
            "Test message from the Bharat Horizon Travels ticket generator.",
        )
    except Exception as exc:
        print(f"\nNOT SENT - {exc}")
        return 1

    api_response = result.get("api_response", {})
    message_id = ""
    if api_response.get("messages"):
        message_id = api_response["messages"][0].get("id", "")
    print(f"\nSENT to {result['recipient']}")
    if message_id:
        print(f"Message id: {message_id}")
    print("If it does not arrive, the number must have messaged your business "
          "in the last 24h, or be a registered test recipient in Meta's console.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
