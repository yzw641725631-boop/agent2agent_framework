"""Read-only public desktop-feed health check, without access to a signing key."""

import base64
import json
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.serialization import load_pem_public_key

from .security import now

PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAN1Zphafvya1E4RjhlnxTPfuEvhlpBWGLjNACyfKbs8Y=
-----END PUBLIC KEY-----
"""


def verify(data, *, public_key=PUBLIC_KEY, time=None):
    time = time or now()
    envelope = json.loads(data)
    if not isinstance(envelope.get("payload"), str) or len(envelope["payload"]) > 4096:
        raise ValueError("Invalid release envelope")
    if not isinstance(envelope.get("signature"), str) or len(envelope["signature"]) > 128:
        raise ValueError("Invalid release signature")
    payload = base64.urlsafe_b64decode(envelope["payload"] + "==")
    signature = base64.urlsafe_b64decode(envelope["signature"] + "==")
    load_pem_public_key(public_key).verify(signature, payload)
    release = json.loads(payload)
    issued = datetime.fromisoformat(release["issued_at"])
    expires = datetime.fromisoformat(release["expires_at"])
    if release["schema"] != 1 or release["channel"] != "stable" or release["platform"] != "win-x64":
        raise ValueError("Invalid release channel")
    sequence = release["metadata_version"]
    if type(sequence) is not int or not 1 <= sequence <= 9007199254740991:
        raise ValueError("Invalid metadata version")
    if not timedelta(0) < expires - issued <= timedelta(days=31):
        raise ValueError("Invalid metadata lifetime")
    if issued > time + timedelta(minutes=5) or expires <= time:
        raise ValueError("Release metadata expired or clock incorrect")
    return {
        "version": release["version"],
        "metadata_version": sequence,
        "expires_at": release["expires_at"],
        "remaining_hours": int((expires - time).total_seconds() / 3600),
    }


def check(origin):
    url = origin.rstrip("/") + "/desktop-updates/win-x64/stable.json"
    request = Request(url, headers={"Cache-Control": "no-cache"})
    with urlopen(request, timeout=10) as response:
        data = response.read(16385)
    if len(data) > 16384:
        raise ValueError("Release metadata too large")
    return verify(data)
