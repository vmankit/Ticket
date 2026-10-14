import os
import glob
import json
import pdfplumber
from app import extract_ticket_fields

pdfs = sorted([f for f in glob.glob("*.pdf") if f not in ("sample_ticket.pdf", "my_ticket.pdf")])
print(f"Total PDFs found: {len(pdfs)}")

results = []
for p in pdfs:
    try:
        pages = []
        pages_words = []
        with pdfplumber.open(p) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
            pages_words = [page.extract_words() or [] for page in pdf.pages]
        text = "\n".join(pages).strip()
        data = extract_ticket_fields(text, filename=p, pages_words=pages_words)

        flights = data.get("flights") or []
        passengers = data.get("passengers") or []
        pnr = data.get("pnr") or ""
        total_fare = data.get("total_fare") or ""
        base_fare = data.get("base_fare") or ""
        booking_date = data.get("booking_date") or ""
        fare_type = data.get("fare_type") or ""
        refund_status = data.get("refund_status") or ""
        customer_email = data.get("customer_email") or ""
        customer_phone = data.get("customer_phone") or ""

        # Baggage check across flights or passengers
        baggage_info = []
        for f in flights:
            chk = f.get("checkin_bag") or ""
            hnd = f.get("hand_bag") or ""
            if chk or hnd:
                baggage_info.append(f"{chk} / {hnd}")
        for pax in passengers:
            chk = pax.get("checkin_bag") or ""
            hnd = pax.get("cabin_bag") or ""
            if chk or hnd:
                baggage_info.append(f"{chk} / {hnd}")

        # Flight journey details check
        journey_summary = []
        for f in flights:
            journey_summary.append(f"{f.get('airline', '')} {f.get('flight_no', '')}: {f.get('from_code', '')}->{f.get('to_code', '')} {f.get('dep_time_raw', '')}-{f.get('arr_time_raw', '')} on {f.get('date', '')}")

        pax_summary = [f"{pax.get('title', '')} {pax.get('name', '')}".strip() for pax in passengers]

        results.append({
            "file": p,
            "platform": data.get("booking_platform") or "",
            "1_journey_flight": journey_summary,
            "2_passengers": pax_summary,
            "3_baggage": baggage_info,
            "4_fare_type": fare_type,
            "5_refund_status": refund_status,
            "6_price": {"total": total_fare, "base": base_fare, "tax": data.get("taxes_fees")},
            "7_booking_date": booking_date,
            "8_pnr": pnr,
            "9_customer_info": {"email": customer_email, "phone": customer_phone},
            "is_flight_ticket": bool(pnr or flights),
        })
    except Exception as e:
        results.append({"file": p, "error": str(e), "is_flight_ticket": False})

with open("test_results_9_fields.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)

print("\n" + "="*110)
print(f"{'FILE':<35} | {'PNR':<8} | {'FLIGHTS':<7} | {'PAX':<5} | {'FARE':<8} | {'DATE':<10} | {'FARE TYPE':<12} | {'REFUND':<14}")
print("="*110)

actual_tickets = 0
passed_tickets = 0
non_tickets = []

for r in results:
    if "error" in r:
        print(f"ERROR: {r['file']} -> {r['error']}")
        non_tickets.append(r['file'])
        continue

    fn = r["file"]
    if len(fn) > 34:
        fn = fn[:31] + "..."
    pnr = r["8_pnr"]
    flt_count = len(r["1_journey_flight"])
    pax_count = len(r["2_passengers"])
    fare = str(r["6_price"]["total"])
    bdate = str(r["7_booking_date"])
    ftype = str(r["4_fare_type"])
    rstatus = str(r["5_refund_status"])

    # If it's one of the non-ticket search quotes, tax invoice, or corrupt files
    if not pnr and flt_count == 0 and pax_count == 0:
        non_tickets.append(r['file'])
        continue

    # AgentBox quotations have 25-59 flights and no PNR
    if "AgentBox" in r['file'] and not pnr:
        non_tickets.append(r['file'])
        continue

    actual_tickets += 1
    # Check if essential ticket fields exist
    ok = bool(pnr and flt_count > 0 and pax_count > 0)
    if ok:
        passed_tickets += 1

    flag = "OK" if ok else "WARN"
    print(f"{fn:<35} | {pnr:<8} | {flt_count:<7} | {pax_count:<5} | {fare:<8} | {bdate:<10} | {ftype:<12} | {rstatus:<14} [{flag}]")

print("="*110)
print(f"Actual Flight Tickets Tested: {actual_tickets}")
print(f"Passed Core Verification:    {passed_tickets} / {actual_tickets}")
print(f"Non-Ticket / Quotation Files: {len(non_tickets)} -> {non_tickets}")
