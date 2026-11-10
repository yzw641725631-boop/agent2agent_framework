"""Read-only public desktop-feed health check, without access to a signing key."""

import base64
import hashlib
import json
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_public_key

from .security import now

PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAN1Zphafvya1E4RjhlnxTPfuEvhlpBWGLjNACyfKbs8Y=
-----END PUBLIC KEY-----
"""


def _key(value):
    key = load_pem_public_key(value.encode() if isinstance(value, str) else value)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Invalid update root key")
    return key


def _key_id(value):
    return hashlib.sha256(
        _key(value).public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    ).hexdigest()


def _threshold(payload, signatures, root):
    if not isinstance(signatures, list) or len(signatures) > 8:
        raise ValueError("Invalid update signatures")
    accepted = set()
    for item in signatures:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("key_id"), str)
            or not isinstance(item.get("signature"), str)
            or len(item["signature"]) > 100
        ):
            raise ValueError("Invalid update signature")
        if item["key_id"] in root["keys"]:
            try:
                _key(root["keys"][item["key_id"]]).verify(
                    base64.urlsafe_b64decode(item["signature"] + "=="), payload
                )
                accepted.add(item["key_id"])
            except (InvalidSignature, ValueError):
                pass
    if len(accepted) < root["threshold"]:
        raise ValueError("Update signature threshold not met")


def _root(envelope, public_key):
    root = {"version": 1, "threshold": 1, "keys": {_key_id(public_key): public_key}}
    chain = envelope.get("root_chain", [])
    if not isinstance(chain, list) or len(chain) > 16 or len(json.dumps(chain)) > 60000:
        raise ValueError("Invalid update root chain")
    for link in chain:
        if (
            not isinstance(link, dict)
            or not isinstance(link.get("payload"), str)
            or len(link["payload"]) > 4096
        ):
            raise ValueError("Invalid update root")
        payload = base64.urlsafe_b64decode(link["payload"] + "==")
        next_root = json.loads(payload)
        keys = next_root.get("keys")
        if (
            next_root.get("schema") != 1
            or next_root.get("type") != "agent-hub-update-root"
            or type(next_root.get("version")) is not int
            or next_root["version"] != root["version"] + 1
            or not isinstance(keys, dict)
            or not 1 <= len(keys) <= 4
            or type(next_root.get("threshold")) is not int
            or not 1 <= next_root["threshold"] <= len(keys)
        ):
            raise ValueError("Invalid update root structure")
        for key_id, value in keys.items():
            if not isinstance(value, str) or len(value) > 256 or _key_id(value) != key_id:
                raise ValueError("Invalid update root key")
        issued = datetime.fromisoformat(next_root["issued_at"])
        expires = datetime.fromisoformat(next_root["expires_at"])
        if (
            not issued.tzinfo
            or not expires.tzinfo
            or not timedelta(0) < expires - issued <= timedelta(days=366)
        ):
            raise ValueError("Invalid update root lifetime")
        _threshold(payload, link.get("signatures"), root)
        _threshold(payload, link.get("signatures"), next_root)
        root = next_root
    return root


def verify(data, *, public_key=PUBLIC_KEY, time=None):
    time = time or now()
    envelope = json.loads(data)
    if not isinstance(envelope.get("payload"), str) or len(envelope["payload"]) > 4096:
        raise ValueError("Invalid release envelope")
    payload = base64.urlsafe_b64decode(envelope["payload"] + "==")
    root = _root(envelope, public_key)
    if root["version"] == 1 and "signatures" not in envelope:
        if not isinstance(envelope.get("signature"), str) or len(envelope["signature"]) > 128:
            raise ValueError("Invalid release signature")
        signature = base64.urlsafe_b64decode(envelope["signature"] + "==")
        _key(public_key).verify(signature, payload)
    else:
        _threshold(payload, envelope.get("signatures"), root)
    if root["version"] > 1 and (
        datetime.fromisoformat(root["expires_at"]) <= time
        or datetime.fromisoformat(root["issued_at"]) > time + timedelta(minutes=5)
    ):
        raise ValueError("Update root expired or clock incorrect")
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
        "root_version": root["version"],
    }


def check(origin):
    url = origin.rstrip("/") + "/desktop-updates/win-x64/stable.json"
    request = Request(url, headers={"Cache-Control": "no-cache"})
    with urlopen(request, timeout=10) as response:
        data = response.read(65537)
    if len(data) > 65536:
        raise ValueError("Release metadata too large")
    return verify(data)
