import base64
import json
from datetime import timedelta

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent_hub.release_monitor import verify
from agent_hub.security import now


def test_release_monitor_accepts_a_signed_feed_and_reports_expiry():
    key = Ed25519PrivateKey.generate()
    time = now()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    release = {
        "schema": 1,
        "channel": "stable",
        "platform": "win-x64",
        "version": "1.0.0",
        "metadata_version": 4,
        "issued_at": time.isoformat(),
        "expires_at": (time + timedelta(days=30)).isoformat(),
    }
    payload = json.dumps(release).encode()
    data = json.dumps(
        {
            "payload": base64.urlsafe_b64encode(payload).decode(),
            "signature": base64.urlsafe_b64encode(key.sign(payload)).decode(),
        }
    )
    assert verify(data, public_key=public, time=time)["remaining_hours"] == 720
    with pytest.raises(ValueError, match="expired"):
        verify(data, public_key=public, time=time + timedelta(days=30))
    with pytest.raises(InvalidSignature):
        verify(
            data,
            public_key=Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo),
            time=time,
        )
