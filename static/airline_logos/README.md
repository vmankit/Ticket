# Airline Logo Assets

This directory contains transparent PNG logo marks for airlines identified by their 2-letter IATA carrier code (`<IATA>.png`).

## Source
- Fetched from the public travel-distribution logo CDNs `images.kiwi.com/airlines`
  and `pics.avs.io`, at 128-200px with RGBA alpha transparency.
- They are drawn into the carrier badge on each itinerary card at print resolution.

## Why some carriers have no logo here
The badge is a 5.8mm circle. A wide wordmark fitted to that width renders only
1-2mm tall, which is illegible in print, so carriers whose logo would render
below ~2.2mm are deliberately left out and fall back to the colour badge
instead. Square and roundel marks are kept. Before adding a logo, check how
tall it renders at 5.8mm wide rather than just dropping the file in.

## Usage Rights Notice
All airline trademarks, service marks, and logos remain the intellectual property of their respective carriers. The travel agency issuing tickets through this system must verify and confirm that it is authorized (via IATA accreditation, GDS ticketing agreements, or direct airline agency partnerships) to display these brand marks on itinerary documents issued to passengers.

## Fallback
If an airline logo is missing, zero-byte or corrupt, the PDF renderer falls back
to a brand-coloured circle badge carrying the IATA code, with the code drawn in
white or dark text whichever gives the better contrast against that colour. A
missing or broken logo never fails ticket generation.
