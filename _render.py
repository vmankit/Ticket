import os
os.system("pip install PyMuPDF -q")
import fitz
doc = fitz.open('ticket_output.pdf')
page = doc.load_page(0)
pix = page.get_pixmap(dpi=150)
pix.save('ticket_preview.png')
print("Saved ticket_preview.png")
