import json
import uuid

import httpx
import pytest

from agent_hub.hindsight_engine import Candidate, HindsightEngine, bank_id, comparison
from agent_hub.security import HubError


def test_strict_whitelist_discards_foreign_mixed_stale_and_generated_text():
    candidate = Candidate(str(uuid.uuid4()), "a" * 64, "当前经过审核的中文经验")
    bank = bank_id(uuid.uuid4(), uuid.uuid4(), "b" * 64, "c" * 64)

    def serve(request):
        body = json.loads(request.content)
        assert request.url.path == f"/v1/default/banks/{bank}/memories/recall"
        assert body["tags_match"] == "any_strict" and body["tags"] == [candidate.tag]
        assert body["types"] == ["world", "experience"]
        return httpx.Response(
            200,
            json={
                "results": [
                    {"text": "DO NOT EXPORT foreign secret", "tags": ["other-project"]},
                    {"text": "DO NOT EXPORT mixed secret", "tags": [candidate.tag, "other-project"]},
                    {"text": "DO NOT EXPORT stale", "tags": [candidate.tag.replace("a" * 64, "d" * 64)]},
                    {"text": "DO NOT EXPORT invented upstream conclusion", "tags": [candidate.tag]},
                ]
            },
        )

    engine = HindsightEngine(bank, transport=httpx.MockTransport(serve))
    try:
        result = engine.rank("如何处理登录问题", [candidate])
    finally:
        engine.close()
    assert result["ordered_ids"] == [candidate.id]
    assert "DO NOT EXPORT" not in json.dumps(result)
    assert comparison([candidate.id], result, [candidate.id])["hindsight"]["recall"] == 1


def test_retain_is_opt_in_stable_idempotent_and_not_claimed_ready():
    candidate = Candidate(str(uuid.uuid4()), "a" * 64, "带有版本和证据的经验")
    bank = bank_id(uuid.uuid4(), uuid.uuid4(), "b" * 64, "c" * 64)
    bodies = []

    def serve(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert body["items"][0]["document_id"] == "hub-source:" + candidate.id + ":" + candidate.revision
        return httpx.Response(200, json={"operation_id": body["operation_id"]})

    engine = HindsightEngine(bank, transport=httpx.MockTransport(serve))
    try:
        with pytest.raises(HubError, match="HINDSIGHT_MODEL_CALL_NOT_AUTHORIZED"):
            engine.retain([candidate])
        assert not bodies
        first = engine.retain([candidate], allow_model_calls=True)
        second = engine.retain([candidate], allow_model_calls=True)
    finally:
        engine.close()
    assert first == second and first["index_ready"] is False
    assert bodies[0] == bodies[1]


def test_private_endpoint_scope_and_upstream_failures_are_bounded_and_redacted():
    bank = bank_id(uuid.uuid4(), uuid.uuid4(), "a" * 64, "b" * 64)
    with pytest.raises(HubError, match="HINDSIGHT_PRIVATE_ENDPOINT_REQUIRED"):
        HindsightEngine(bank, url="https://arbitrary.example")
    with pytest.raises(HubError, match="HINDSIGHT_SCOPE_INVALID"):
        HindsightEngine("default")

    def serve(request):
        return httpx.Response(302, headers={"Location": "https://outside.example/secret"})

    engine = HindsightEngine(bank, api_key="private-fixture-key", transport=httpx.MockTransport(serve))
    try:
        with pytest.raises(HubError) as failure:
            engine.rank("中文查询", [Candidate(str(uuid.uuid4()), "a" * 64, "experience")])
        assert failure.value.code == "HINDSIGHT_REQUEST_FAILED"
        assert "secret" not in str(failure.value)
    finally:
        engine.close()
