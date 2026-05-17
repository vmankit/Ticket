import io
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, black, white
from reportlab.platypus import Table, TableStyle, Paragraph, Spacer, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
from config import COMPANY, AIRLINES
from utils import generate_qr, check_page_break

# Colors & Styles
PRIMARY = HexColor("#1a237e")
SECONDARY = HexColor("#e65100")
DARK = HexColor("#212121")
GRAY = HexColor("#757575")
LIGHT_BG = HexColor("#f5f7fa")
BORDER = HexColor("#e0e0e0")
WHITE = white
GREEN = HexColor("#2e7d32")

styles = getSampleStyleSheet()
header_style = ParagraphStyle('Header', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8, textColor=WHITE, spaceBefore=4, spaceAfter=4)
cell_style = ParagraphStyle('Cell', parent=styles['Normal'], fontName='Helvetica', fontSize=8, textColor=DARK, leading=10, spaceBefore=4, spaceAfter=4)

def draw_rounded_rect(c, x, y, width, height, radius, fill_color=None, stroke_color=BORDER):
    c.saveState()
    if fill_color:
        c.setFillColor(fill_color)
    else:
        c.setFillColor(WHITE)
    c.setStrokeColor(stroke_color)
    c.setLineWidth(0.5)
    c.roundRect(x, y, width, height, radius, stroke=1, fill=1)
    c.restoreState()

def generate_pdf(flights, passengers, form_data):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin = 30
    usable_w = width - 2 * margin

    y = height - margin

    # ── Header ──
    c.setFillColor(PRIMARY)
    c.rect(0, height - 70, width, 70, stroke=0, fill=1)
    c.setFillColor(SECONDARY)
    c.rect(0, height - 72, width, 2, stroke=0, fill=1)

    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 24)
    c.drawString(margin, height - 45, "E-TICKET")
    c.setFont("Helvetica", 10)
    c.drawString(margin, height - 60, "Please carry a valid photo ID and this e-ticket to the airport.")

    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(width - margin, height - 35, COMPANY["name"])
    c.setFont("Helvetica", 8)
    c.drawRightString(width - margin, height - 48, COMPANY["email"])
    c.drawRightString(width - margin, height - 60, COMPANY["phone"])

    y -= 85

    # ── Booking Summary ──
    section_h = 80
    y = check_page_break(c, y, section_h)
    y -= section_h
    draw_rounded_rect(c, margin, y, usable_w, section_h, 6, fill_color=LIGHT_BG)

    c.setFillColor(PRIMARY)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin + 15, y + section_h - 18, "BOOKING SUMMARY")

    c.setStrokeColor(BORDER); c.setLineWidth(0.5)
    c.line(margin + 15, y + section_h - 23, margin + usable_w - 15, y + section_h - 23)

    col_w = usable_w / 4
    labels = [
        ("Booking ID", form_data.get("booking_id", "")),
        ("Booking Date", form_data.get("booking_date_display", "")),
        ("PNR / Booking Ref", form_data.get("pnr", "")),
        ("Route", form_data.get("route_summary", "")),
        ("Fare Type", form_data.get("fare_type", "")),
        ("Refund Status", form_data.get("refund_status", "")),
    ]
    for idx, (label, value) in enumerate(labels):
        row = idx // 4
        col = idx % 4
        cx = margin + 15 + col * col_w
        cy = y + section_h - 40 - (row * 30)
        c.setFillColor(GRAY); c.setFont("Helvetica", 7)
        c.drawString(cx, cy, label)
        
        if label == "Refund Status" and "Non-Refundable" in value:
            c.setFillColor(HexColor("#e53935"))
        else:
            c.setFillColor(DARK)
            
        c.setFont("Helvetica-Bold", 10)
        c.drawString(cx, cy - 14, value[:22])
        
    if flights:
        c.setFillColor(GRAY); c.setFont("Helvetica", 7)
        c.drawString(margin + 15, y + 6, f"Travel Date: {flights[0]['date']}")

    y -= 15

    # ── Flight Details ──
    c.setFillColor(PRIMARY); c.setFont("Helvetica-Bold", 10)
    c.drawString(margin + 5, y, f"FLIGHT DETAILS  ({len(flights)} Segment{'s' if len(flights) > 1 else ''})")
    y -= 8

    flight_headers = ["Airline & Flight", "Departure", "Arrival", "Timing & Duration", "Class"]
    flight_col_w = [usable_w * 0.18, usable_w * 0.26, usable_w * 0.26, usable_w * 0.20, usable_w * 0.10]

    table_data = [[Paragraph(h, header_style) for h in flight_headers]]

    for fi, fl in enumerate(flights):
        checkin_txt = f"<br/><font size='6' color='#e65100'>Check-in counter closes {fl.get('checkin_closing', '')}</font>" if fl.get('checkin_closing') else ""
        timing_str = f"<b><font size='9'>{fl['dep_time']}</font></b> &rarr; <b><font size='9'>{fl['arr_time']}</font></b><br/>{fl['duration']} | Non-stop"
        
        row = [
            Paragraph(f"{fl['airline']}<br/>{fl['flight_no']}", cell_style),
            Paragraph(f"{fl['from_full']}<br/><font color='#616161'>{fl['date']}</font>{checkin_txt}", cell_style),
            Paragraph(f"{fl['to_full']}<br/><font color='#616161'>{fl['date']}</font>", cell_style),
            Paragraph(timing_str, cell_style),
            Paragraph(fl["class"], cell_style),
        ]
        table_data.append(row)

    pt = Table(table_data, colWidths=flight_col_w)
    pt.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), PRIMARY),
        ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, HexColor("#fafafa")]),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
    ]))
    
    tw, th = pt.wrap(usable_w, height)
    y = check_page_break(c, y, th + 20)
    y -= th
    pt.drawOn(c, margin, y)

    # Layovers
    for fi in range(len(flights)-1):
        fl1 = flights[fi]
        fl2 = flights[fi+1]
        layover_text = fl2.get("layover", "")
        if layover_text:
            y -= 15
            c.setFillColor(HexColor("#f57c00"))
            c.setFont("Helvetica-Bold", 7)
            y = check_page_break(c, y, 15)
            c.drawCentredString(width/2, y, f"------------------------- {layover_text} layover in {fl1['to_city']} -------------------------")
    
    y -= 20

    # ── Passenger Details ──
    c.setFillColor(PRIMARY); c.setFont("Helvetica-Bold", 10)
    c.drawString(margin + 5, y, "PASSENGER DETAILS")
    
    if form_data.get("customer_phone") or form_data.get("customer_email"):
        c.setFillColor(GRAY); c.setFont("Helvetica", 8)
        c.drawRightString(margin + usable_w - 5, y, f"Customer Contact: {form_data.get('customer_phone')}  |  Email: {form_data.get('customer_email')}")
        
    y -= 8

    pax_headers = ["No", "Passenger Name", "Sector", "Ticket Number", "Seat", "Meal", "Baggage Details"]
    pax_col_w = [usable_w * 0.05, usable_w * 0.25, usable_w * 0.12, usable_w * 0.15, usable_w * 0.13, usable_w * 0.12, usable_w * 0.18]

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
            
        # Build Sector + QR Code array
        sector_elements = []
        for fl in flights:
            sector_str = f"{fl['from_code']}-{fl['to_code']}"
            sector_elements.append(Paragraph(sector_str, cell_style))
            
            # Use QR code if requested!
            qr_data = f"PNR:{form_data.get('pnr')}|FLT:{fl['flight_no']}|PAX:{pax['name']}|RTE:{sector_str}"
            qr_buf = generate_qr(qr_data)
            qr_img = RLImage(qr_buf, width=25, height=25)
            sector_elements.append(qr_img)
            
            sector_elements.append(Spacer(1, 4))

        bag_html = f"Check-in: {pax['checkin_bag']}<br/>Cabin: {pax['hand_bag']}"
        row = [
            Paragraph(str(pi + 1), cell_style),
            Paragraph(name_html, cell_style),
            sector_elements,
            Paragraph(pax["ticket_no"], cell_style),
            Paragraph(pax["seat"], cell_style),
            Paragraph(pax["meal"], cell_style),
            Paragraph(bag_html, cell_style),
        ]
        pax_table_data.append(row)

    pt2 = Table(pax_table_data, colWidths=pax_col_w)
    pt2.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), PRIMARY),
        ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [WHITE, HexColor("#fafafa")]),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
    ]))
    
    tw2, th2 = pt2.wrap(usable_w, height)
    y = check_page_break(c, y, th2 + 20)
    y -= th2
    pt2.drawOn(c, margin, y)

    y -= 25

    # ── Fare Details ──
    active_fare_items = form_data.get("active_fare_items", [])
    
    fare_h = 45 + (len(active_fare_items) * 14)
    y = check_page_break(c, y, fare_h + 20)
    y -= fare_h
    draw_rounded_rect(c, margin, y, usable_w, fare_h, 6, fill_color=LIGHT_BG)

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

    c.setStrokeColor(BORDER); c.setLineWidth(0.5)
    c.line(lx, current_y + 6, lx + 200, current_y + 6)
    
    c.setFillColor(PRIMARY); c.setFont("Helvetica-Bold", 9)
    c.drawString(lx, current_y - 8, "Total Amount")
    c.setFillColor(PRIMARY); c.setFont("Helvetica-Bold", 12)
    c.drawString(lx + 120, current_y - 8, form_data.get("total_fare_str", ""))

    fare_box_w = 160
    fare_box_h = 50
    fare_box_x = margin + usable_w - fare_box_w - 10
    fare_box_y = y + (fare_h - fare_box_h) / 2
    draw_rounded_rect(c, fare_box_x, fare_box_y, fare_box_w, fare_box_h, 5, fill_color=PRIMARY)

    c.setFillColor(HexColor("#bbdefb")); c.setFont("Helvetica", 7)
    c.drawCentredString(fare_box_x + fare_box_w / 2, fare_box_y + 34, "TOTAL AMOUNT")
    c.setFillColor(WHITE); c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(fare_box_x + fare_box_w / 2, fare_box_y + 14, form_data.get("total_fare_str", ""))

    # Payment details
    y -= 25
    c.setFillColor(GREEN); c.setFont("Helvetica-Bold", 8)
    c.drawRightString(margin + usable_w, y + 14, "Payment Status: Confirmed")
    c.setFillColor(GRAY); c.setFont("Helvetica", 8)
    pm_mode = f"Mode: {form_data.get('payment_method')}"
    if form_data.get('payment_method') in ["Credit Card", "Debit Card"] and form_data.get('card_last_4'):
        pm_mode += f" (**{form_data.get('card_last_4')})"
    c.drawRightString(margin + usable_w, y + 2, pm_mode)

    y -= 25

    # ── T&C ──
    tc_h = 240
    y = check_page_break(c, y, tc_h, margin_bottom=10)
    y -= tc_h
    
    c.setFillColor(PRIMARY); c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, y + tc_h - 10, "MANDATORY CHECK-LIST FOR PASSENGERS")
    
    c.setFillColor(DARK); c.setFont("Helvetica", 9)
    c.drawString(margin, y + tc_h - 26, "1) Complete your web check-in before arriving at the airport")
    c.drawString(margin, y + tc_h - 40, "2) Report to the airport: 2 hours prior for Domestic, 3 hours prior for International flights")
    
    tc_data = [
        [Paragraph("<b>Terms & Conditions</b>", header_style), Paragraph("<b>Cancellation & Rescheduling</b>", header_style)],
        [
            Paragraph("""
            • Valid ID proof is mandatory for travel.<br/>
            • Baggage allowances are subject to airline policies. Excess baggage will be charged at the airport.<br/>
            • Passengers must comply with all COVID-19 or health regulations applicable at the time of travel.<br/>
            • The airline reserves the right to deny boarding for late reporting.
            """, cell_style),
            Paragraph("""
            • Cancellation requests must be submitted at least 24 hours prior to departure.<br/>
            • Refunds are processed as per airline policies and fare rules.<br/>
            • Service fees and convenience charges are non-refundable.<br/>
            • Rescheduling is subject to seat availability and applicable fare differences.
            """, cell_style)
        ]
    ]
    tc_table = Table(tc_data, colWidths=[usable_w/2, usable_w/2])
    tc_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), SECONDARY),
        ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    tc_table.wrapOn(c, usable_w, height)
    tc_table.drawOn(c, margin, y + 40)

    # Footer
    c.setFillColor(GRAY); c.setFont("Helvetica", 8)
    c.drawCentredString(width/2, margin - 10, f"{COMPANY['name']}  |  {COMPANY['address']}  |  !!! Have a Nice Trip !!!")

    c.save()
    buffer.seek(0)
    return buffer
