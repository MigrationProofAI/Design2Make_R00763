# test_sap.py — with relaxed TLS to match the SAP appliance
import os
import ssl
import urllib3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from dotenv import load_dotenv

print(">>> running NEW version with TLS adapter")
load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class SAPHttpAdapter(HTTPAdapter):
    """Allow the older TLS ciphers the SAP appliance offers."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context(ciphers="DEFAULT:@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE          # appliance cert is self-signed
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


host   = os.getenv("SAP_HOST")
port   = os.getenv("SAP_HTTPS_PORT", "44301")
client = os.getenv("SAP_CLIENT", "100")
user   = os.getenv("SAP_USER")
pw     = os.getenv("SAP_PASS")

url = f"https://{host}:{port}/sap/opu/odata/sap/API_PRODUCT_SRV/A_Product"
print("URL :", url)

session = requests.Session()
session.mount("https://", SAPHttpAdapter())     # <-- the fix

try:
    r = session.get(
        url,
        params={"sap-client": client, "$top": 1, "$format": "json"},
        auth=(user, pw),
        headers={"Accept": "application/json"},
        timeout=30,
        verify=False,          # <-- add this line
    )
    print("Status:", r.status_code)
    print(r.text[:500])
except Exception as e:
    print("ERROR TYPE:", type(e).__name__)
    print("ERROR    :", repr(e))