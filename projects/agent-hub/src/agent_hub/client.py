"""Internal REST test client; this is not an OAuth or MCP implementation."""

import hashlib
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .security import b64


class DeviceClient:
    def __init__(self, base_url: str, key_path: Path, passphrase: str):
        origin = urlsplit(base_url)
        if origin.path.rstrip("/") or origin.query or origin.fragment or origin.username:
            raise ValueError("Use a server origin without a path or credentials")
        if origin.scheme != "https" and not (
            origin.scheme == "http" and origin.hostname in {"localhost", "127.0.0.1"}
        ):
            raise ValueError("HTTPS is required for remote access")
        self.base_url = base_url.rstrip("/")
        self.key = serialization.load_pem_private_key(key_path.read_bytes(), password=passphrase.encode())
        if not isinstance(self.key, ec.EllipticCurvePrivateKey) or not isinstance(
            self.key.curve, ec.SECP256R1
        ):
            raise ValueError("Device key must use P-256")
        self.jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key()))
        self.http = httpx.Client(base_url=self.base_url, timeout=20, follow_redirects=False)

    def close(self):
        self.http.close()

    def request(self, method: str, path: str, *, token=None, body=None, idempotency_key=None):
        if not path.startswith("/api/") or "#" in path or ".." in path or "\\" in path:
            raise ValueError("Use an Agent Hub API path")
        claims = {
            "htm": method.upper(),
            "htu": self.base_url + path.split("?", 1)[0],
            "iat": int(time.time()),
            "jti": str(uuid.uuid4()),
        }
        if token:
            claims["ath"] = b64(hashlib.sha256(token.encode()).digest())
        headers = {
            "DPoP": jwt.encode(
                claims, self.key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": self.jwk}
            )
        }
        if token:
            headers["Authorization"] = f"DPoP {token}"
        if method.upper() not in {"GET", "HEAD"}:
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        # On a network timeout the caller retains its idempotency key and checks/retries explicitly.
        response = self.http.request(method, path, headers=headers, json=body)
        response.raise_for_status()
        return response.json()
