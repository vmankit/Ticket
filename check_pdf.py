import os, time
p='ticket_output.pdf'
if os.path.exists(p):
    print('FOUND', os.path.getsize(p), 'bytes', time.ctime(os.path.getmtime(p)))
else:
    print('MISSING')
