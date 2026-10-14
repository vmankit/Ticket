"""
Generates a sample flight e-ticket PDF using the app's generation pipeline.
Usage:
    python generate_sample.py [output_filename.pdf]
"""

import sys
from datetime import datetime
from app import app

def generate_sample_pdf(output_path="sample_ticket.pdf"):
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()

    today_str = datetime.now().strftime("%Y-%m-%d")

    data = {
        "booking_id": "MMT2605071001",
        "booking_date": today_str,
        "pnr": "MAIN01",
        "booking_platform": "MakeMyTrip",
        "customer_email": "traveler@example.com",
        "customer_phone": "+91 9876543210",
        "base_fare": "6000",
        "taxes_fees": "1200",
        "flight_count": "2",
        # Leg 1: New Delhi (DEL) to Mumbai (BOM)
        "flight_0_flight_no": "6E 1234",
        "flight_0_airline": "IndiGo",
        "flight_0_from_code": "DEL",
        "flight_0_from_city": "New Delhi",
        "flight_0_to_code": "BOM",
        "flight_0_to_city": "Mumbai",
        "flight_0_date": today_str,
        "flight_0_dep_time_raw": "10:00",
        "flight_0_arr_time_raw": "12:00",
        "flight_0_dep_time": "10:00 AM",
        "flight_0_arr_time": "12:00 PM",
        "flight_0_seat": "12A",
        "flight_0_meal": "Veg Meal",
        "flight_0_checkin_bag": "15 KG (1 Piece)",
        "flight_0_hand_bag": "7 KG (1 Piece)",
        "flight_0_segment_pnr": "PNR001",
        # Leg 2: Mumbai (BOM) to Bangalore (BLR)
        "flight_1_flight_no": "6E 5678",
        "flight_1_airline": "IndiGo",
        "flight_1_from_code": "BOM",
        "flight_1_from_city": "Mumbai",
        "flight_1_to_code": "BLR",
        "flight_1_to_city": "Bangalore",
        "flight_1_date": today_str,
        "flight_1_dep_time_raw": "14:00",
        "flight_1_arr_time_raw": "16:00",
        "flight_1_dep_time": "02:00 PM",
        "flight_1_arr_time": "04:00 PM",
        "flight_1_seat": "14C",
        "flight_1_meal": "Non-Veg Meal",
        "flight_1_checkin_bag": "20 KG (1 Piece)",
        "flight_1_hand_bag": "10 KG (1 Piece)",
        "flight_1_segment_pnr": "PNR002",
        # Passenger details
        "pax_count": "1",
        "pax_0_name": "JOHN DOE",
        "pax_0_title": "Mr",
        "pax_0_type": "Adult",
        "pax_0_dob": "1990-01-15",
        "pax_0_passport": "K1234567",
        "pax_0_seat_0": "12A",
        "pax_0_meal_0": "Veg Meal",
        "pax_0_seat_1": "14C",
        "pax_0_meal_1": "Non-Veg Meal",
        # Ticket configuration
        "ticket_status": "Confirmed",
        "refund_status": "Refundable",
        "fare_type": "REGULAR",
        "trip_type": "One Way",
        "is_dummy": "false",
    }

    response = client.post("/generate", data=data)
    if response.status_code == 200:
        with open(output_path, "wb") as f:
            f.write(response.data)
        print(f"[SUCCESS] Sample e-ticket PDF generated: {output_path} ({len(response.data):,} bytes)")
        return output_path
    else:
        print(f"[ERROR] Failed to generate PDF. HTTP {response.status_code}")
        print(response.data[:400])
        return None

if __name__ == "__main__":
    out_file = sys.argv[1] if len(sys.argv) > 1 else "sample_ticket.pdf"
    generate_sample_pdf(out_file)
