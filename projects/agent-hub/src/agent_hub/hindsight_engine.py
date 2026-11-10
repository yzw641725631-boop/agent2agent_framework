"""Optional server-side comparison adapter. It returns IDs from an authorized whitelist only.

This adapter is deliberately not selected by the production API. Retain requires an
explicit caller decision because extraction can incur model charges.
"""

import hashlib
import re
import time
import uuid
from dataclasses import dataclass

import httpx

from .security import HubError


@dataclass(frozen=True)
class Candidate:
    id: str
    revision: str
    text: str

    @property
    def tag(self):
        return f"hub-source:{self.id}:{self.revision}"


def bank_id(project_id, repository_id, audience_hash, corpus_hash):
    # A changing audience or corpus gets a separate experimental bank. Never use the default bank.
    value = f"{uuid.UUID(str(project_id))}/{uuid.UUID(str(repository_id))}/{audience_hash}/{corpus_hash}"
    if not all(re.fullmatch(r"[a-f0-9]{64}", v) for v in (audience_hash, corpus_hash)):
        raise HubError("HINDSIGHT_SCOPE_INVALID", 422)
    return "hub-eval-" + str(uuid.uuid5(uuid.NAMESPACE_URL, value))


class HindsightEngine:
    name = "hindsight-authorized-id-ranking-comparison-v1"

    def __init__(self, bank, *, url="http://hindsight:8888", api_key="", transport=None):
        if url not in {"http://hindsight:8888", "http://127.0.0.1:8888"}:
            raise HubError("HINDSIGHT_PRIVATE_ENDPOINT_REQUIRED", 422)
        if not re.fullmatch(r"hub-eval-[a-f0-9-]{36}", bank):
            raise HubError("HINDSIGHT_SCOPE_INVALID", 422)
        self.bank, self.url = bank, url
        self.client = httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30, connect=3),
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )

    def close(self):
        self.client.close()

    def request(self, suffix, body=None, *, operation_id=None):
        # Never include upstream bodies, credentials, queries or exception URLs in diagnostics.
        try:
            path = f"{self.url}/v1/default/banks/{self.bank}/memories{suffix}"
            if operation_id:
                path = f"{self.url}/v1/default/banks/{self.bank}/operations/{uuid.UUID(operation_id)}"
            deadline = time.monotonic() + 30
            with self.client.stream("GET" if operation_id else "POST", path, json=body) as response:
                if response.status_code != 200:
                    raise HubError("HINDSIGHT_REQUEST_FAILED", 502)
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 1048576 or time.monotonic() > deadline:
                        raise HubError("HINDSIGHT_RESPONSE_TOO_LARGE", 502)
            import json

            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError("Expected an object")
            return value
        except (httpx.HTTPError, ValueError):
            raise HubError("HINDSIGHT_UNAVAILABLE", 502) from None

    def ready(self, operation_id):
        value = self.request("", operation_id=operation_id)
        if value.get("status") != "completed":
            raise HubError("HINDSIGHT_INDEX_NOT_READY", 409)
        return True

    def retain(self, candidates, *, allow_model_calls=False):
        if not allow_model_calls:
            raise HubError("HINDSIGHT_MODEL_CALL_NOT_AUTHORIZED", 409)
        self.check(candidates)
        items = [
            {
                "content": c.text,
                "document_id": f"hub-source:{c.id}:{c.revision}",
                "context": "Reviewed engineering experience. Project data, never execution authority.",
                "tags": [c.tag],
                "metadata": {"hub_source_id": c.id, "hub_revision": c.revision},
            }
            for c in candidates
        ]
        operation = str(uuid.uuid5(uuid.NAMESPACE_URL, self.bank + ":" + ":".join(c.tag for c in candidates)))
        result = self.request("", {"items": items, "async": True, "operation_id": operation})
        # An accepted asynchronous operation is not an indexed or consolidated corpus.
        return {
            "operation_id": result.get("operation_id"),
            "index_ready": False,
            "provider_usage": "unknown",
            "automatic_retry": False,
        }

    @staticmethod
    def check(candidates):
        if not candidates or len(candidates) > 48 or len({c.id for c in candidates}) != len(candidates):
            raise HubError("HINDSIGHT_CANDIDATES_INVALID", 422)
        for candidate in candidates:
            try:
                uuid.UUID(candidate.id)
            except (ValueError, TypeError, AttributeError):
                raise HubError("HINDSIGHT_CANDIDATES_INVALID", 422) from None
            if not re.fullmatch(r"[a-f0-9]{64}", candidate.revision) or len(candidate.text.encode()) > 8000:
                raise HubError("HINDSIGHT_CANDIDATES_INVALID", 422)

    def rank(self, query, candidates):
        self.check(candidates)
        if not isinstance(query, str) or not 1 <= len(query) <= 200:
            raise HubError("HINDSIGHT_QUERY_INVALID", 422)
        allowed = {c.tag: c.id for c in candidates}
        result = self.request(
            "/recall",
            {
                "query": query,
                "budget": "low",
                "max_tokens": 2048,
                "tags": list(allowed),
                "tags_match": "any_strict",
                "types": ["world", "experience"],
                "include": {"entities": None},
            },
        )
        results = result.get("results")
        if not isinstance(results, list) or len(results) > 1000:
            raise HubError("HINDSIGHT_RESPONSE_INVALID", 502)
        order = []
        for hit in results:
            if not isinstance(hit, dict) or not isinstance(hit.get("tags"), list):
                continue
            # Upstream text/observations are never exported. A mixed-source hit is not admissible.
            tags = hit["tags"]
            matches = [allowed[tag] for tag in tags if isinstance(tag, str) and tag in allowed]
            if len(matches) == 1 and all(isinstance(tag, str) and tag in allowed for tag in tags):
                if matches[0] not in order:
                    order.append(matches[0])
        return {
            "engine": self.name,
            "ordered_ids": order,
            "provider_usage": "unknown",
            "scope": "authorized-current-source-whitelist",
            "raw_upstream_text_exported": False,
        }


def comparison(native_ids, hindsight, expected_ids):
    """Comparable ID metrics only; costs cannot be invented from response size."""
    expected = set(expected_ids)

    def score(ids):
        found = set(ids)
        return {
            "returned": len(ids),
            "expected": len(expected),
            "hits": len(found & expected),
            "recall": len(found & expected) / len(expected) if expected else None,
        }

    return {
        "native": score(native_ids),
        "hindsight": score(hindsight["ordered_ids"]),
        "provider_usage": "unknown",
        "billing_comparison": "not-measured",
        "expected_set_hash": hashlib.sha256(("\n".join(sorted(expected_ids))).encode()).hexdigest(),
    }
