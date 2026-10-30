# Sample tickets

Real supplier e-tickets, one per format the parser has had to handle:
MakeMyTrip, Cleartrip, Goibibo, EaseMyTrip, Flipkart, Akasa, airline-issued
PDFs and a boarding pass.

They are the parse regression set. After changing anything in
`ticket_parsing.py` or the extraction path in `app.py`, run every one of them
through the parser and compare the output with what it produced before the
change - a fix for one format has repeatedly broken another.

```bash
python test_all_pdfs.py
```

Two of them (`E-Ticket_MAIN01.pdf`, `Flipkart Flight E-Ticket.pdf`) yield no
flights and always have; they are kept so that stays visible rather than being
rediscovered.

Ignored by git (`*.pdf`), so this folder is local to each checkout and the
tickets - which carry real passenger names - are never pushed.
