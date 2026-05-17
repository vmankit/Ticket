import requests, json, os
url='http://127.0.0.1:5000/api/parse-ticket'
fp='Ticket_VEL63X.pdf'
if not os.path.exists(fp):
    print('MISSING',fp)
    raise SystemExit(1)
with open(fp,'rb') as f:
    files={'ticket_pdf':(os.path.basename(fp),f,'application/pdf')}
    try:
        r=requests.post(url,files=files,timeout=60)
        print('STATUS',r.status_code)
        try:
            print(json.dumps(r.json(),indent=2))
        except Exception:
            print('RESP TEXT', r.text[:800])
    except Exception as e:
        print('ERROR',e)
