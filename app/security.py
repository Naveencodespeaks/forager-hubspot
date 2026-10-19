import base64
import hashlib
import hmac
import re
import time
from urllib.parse import unquote

def verify(body: bytes, headers, method, uri, secret, version='v1', now=None):
    if version == 'v3':
        timestamp = headers.get('x-hubspot-request-timestamp', '')
        try:
            if abs((time.time() if now is None else now)*1000-int(timestamp)) > 300000:
                return False
        except ValueError:
            return False
        # Decode only the characters specified by HubSpot; leave %26 etc. intact.
        uri = re.sub(r'%(?:3A|2F|3F|40|21|24|27|28|29|2A|2C|3B)',
                     lambda m: unquote(m.group()), uri, flags=re.I)
        raw = method.encode()+uri.encode()+body+timestamp.encode()
        expected = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(expected, headers.get('x-hubspot-signature-v3', ''))
    if headers.get('x-hubspot-signature-version') != 'v1':
        return False
    expected = hashlib.sha256(secret.encode()+body).hexdigest()
    return hmac.compare_digest(expected, headers.get('x-hubspot-signature',''))
