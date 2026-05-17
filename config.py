COMPANY = {
    "name": "BHARAT HORIZON TRAVELS",
    "tagline": "Your Trusted Travel Partner",
    "email": "ankitrajvm@gmail.com",
    "phone": "+91 7759069422",
    "address": "Subhash Nagar, Dehradun, UTTARAKHAND, India",
    "pincode": "248002",
}

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
    "AA": "American Airlines", "DL": "Delta Air Lines", "UA": "United Airlines",
    "AC": "Air Canada", "B6": "JetBlue Airways", "AS": "Alaska Airlines",
    "AM": "Aeromexico", "LA": "LATAM Airlines", "CM": "Copa Airlines",
    "AR": "Aerolineas Argentinas", "AV": "Avianca",
    # Africa
    "SA": "South African Airways", "ET": "Ethiopian Airlines", "KQ": "Kenya Airways",
    "MS": "EgyptAir", "AT": "Royal Air Maroc", "MK": "Air Mauritius",
}

AIRLINE_NUMERIC_CODES = {
    "6E": "890",  # IndiGo
    "AI": "098",  # Air India
    "UK": "228",  # Vistara
    "SG": "902",  # SpiceJet
    "EK": "176",  # Emirates
    "QR": "157",  # Qatar Airways
    "QP": "141",  # Akasa
    "IX": "018",  # Air India Express
    "SQ": "618",  # Singapore Airlines
    "BA": "125",  # British Airways
    "LH": "220",  # Lufthansa
    "EY": "607",  # Etihad
}

# Used to determine default baggage based on airline if user hasn't specified
AIRLINE_BAGGAGE = {
    "6E": {"hand": "7kg", "checkin": "No Baggage"}, # LCC defaults
    "SG": {"hand": "7kg", "checkin": "No Baggage"},
    "QP": {"hand": "7kg", "checkin": "No Baggage"},
    "IX": {"hand": "7kg", "checkin": "No Baggage"},
    "I5": {"hand": "7kg", "checkin": "No Baggage"},
    "AI": {"hand": "8kg", "checkin": "25kg"}, # Full service
    "UK": {"hand": "7kg", "checkin": "15kg"},
    "EK": {"hand": "7kg", "checkin": "30kg"},
    "QR": {"hand": "7kg", "checkin": "30kg"},
    "SQ": {"hand": "7kg", "checkin": "25kg"},
    "BA": {"hand": "7kg", "checkin": "23kg"},
}
