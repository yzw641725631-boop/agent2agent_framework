"""Published readers, immutable revisions, database guards and independent review."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_guides import CONTENT, approve, generate
from test_repositories import grant, register, task
from test_tasks import claim, gate, submit

from agent_hub.db import context, session_factory
from agent_hub.guide_lifecycle import Amendment, Publication

REASON = "根据已发布功能和反馈核对修订，保留原始说明及审核依据。"
UPDATED = {**CONTENT, "title": "新版登录操作说明", "changes": "新增说明目录的修订发布记录。"}


def published(w):
    p = w.project()
    repo = register(w, p)
    run = claim(w, p, task(w, p, repo, documentation={"kind": "user", "module": "登录说明"}))
    submitted, _ = submit(w, p, run)
    job = w.ok("GET", f"/api/projects/{p}/guides", who=w.root)["items"][0]
    approve(w, p, generate(w, p, job), "project")
    submitted = gate(w, p, submitted, "review", override_reason=REASON)
    submitted = gate(w, p, submitted, "test", override_reason=REASON)
    gate(w, p, submitted, "release")
    reader = w.member(p, "guide-reader")
    w.start(reader, grant(w, reader, p, [], caps=["project:read", "guides:read"]))
    return p, repo, job, reader, f"/api/projects/{p}/guides/{job['id']}"


def amend(w, base, **values):
    return w.ok(
        "POST",
        base + "/amendments",
        who=w.root,
        body={"base_version": 0, "content": UPDATED, "reason": REASON, **values},
    )


def reviews(w, base, amendment):
    for gate_name in ("product", "test"):
        w.ok(
            "POST",
            f"{base}/amendments/{amendment['id']}/decisions",
            who=w.root,
            body={"gate": gate_name, "approved": True, "reason": REASON, "override_reason": REASON},
        )


def publish(w, base, **values):
    return w.ok(
        "POST",
        base + "/publications",
        who=w.root,
        body={"expected_version": 0, "action": "published", "reason": REASON, **values},
    )


def test_unapproved_revision_never_changes_readers_or_search_and_old_content_is_retained(world):
    w = world
    p, _, _, reader, base = published(w)
    amendment = amend(w, base)
    assert w.ok("GET", base, who=reader)["content"] == CONTENT
    assert w.ok("GET", base + "/amendments", who=reader)["items"] == []
    assert w.call("GET", base + "/amendments/" + amendment["id"], who=reader).status_code == 404
    assert not w.ok("GET", f"/api/projects/{p}/guides?query=新版登录", who=reader)["items"]
    denied = w.call(
        "POST",
        base + "/publications",
        who=w.root,
        body={
            "expected_version": 0,
            "action": "published",
            "amendment_id": amendment["id"],
            "reason": REASON,
        },
    )
    assert denied.json()["code"] == "GUIDES_NOT_APPROVED"
    reviews(w, base, amendment)
    result = publish(w, base, amendment_id=amendment["id"])
    assert result["catalog_version"] == 1 and result["content"] == UPDATED
    assert w.ok("GET", base, who=reader)["content"] == UPDATED
    assert (
        w.ok("GET", f"/api/projects/{p}/guides?query=新版登录", who=reader)["items"][0]["title"]
        == UPDATED["title"]
    )
    assert w.ok("GET", base + "/publications/0", who=reader)["content"] == CONTENT
    assert w.ok("GET", base + "/publications/1", who=reader)["content"] == UPDATED
    visible = w.ok("GET", base + "/amendments", who=reader)["items"][0]
    assert not set(visible) & {"reason", "author_id", "reviews"}
    with w.owner() as db:
        assert db.scalar(select(Amendment)).content == UPDATED
        assert db.scalar(select(Publication)).version == 1


def test_stale_revision_review_is_blocked_after_other_revision_is_published(world):
    w = world
    _, _, _, _, base = published(w)
    first, second = amend(w, base), amend(w, base, content={**UPDATED, "title": "并行修订不可悄悄覆盖"})
    self_review = w.call(
        "POST",
        f"{base}/amendments/{first['id']}/decisions",
        who=w.root,
        body={"gate": "product", "approved": True, "reason": REASON},
    )
    assert self_review.json()["code"] == "GUIDE_INDEPENDENT_REVIEW_REQUIRED"
    reviews(w, base, first)
    publish(w, base, amendment_id=first["id"])
    stale = w.call(
        "POST",
        f"{base}/amendments/{second['id']}/decisions",
        who=w.root,
        body={"gate": "test", "approved": True, "reason": REASON, "override_reason": REASON},
    )
    assert stale.json()["code"] == "GUIDE_PUBLICATION_VERSION_CONFLICT"
    assert (
        w.call(
            "POST",
            base + "/amendments",
            who=w.root,
            body={"base_version": 0, "content": UPDATED, "reason": REASON},
        ).status_code
        == 409
    )


def test_archive_restore_keeps_content_but_hides_default_directory(world):
    w = world
    p, _, _, reader, base = published(w)
    archived = publish(w, base, action="archived")
    assert archived["archived"] and archived["catalog_version"] == 1
    assert not w.ok("GET", f"/api/projects/{p}/guides", who=reader)["items"]
    assert w.ok("GET", f"/api/projects/{p}/guides?include_archived=true", who=reader)["items"][0]["archived"]
    assert (
        w.call(
            "POST",
            base + "/amendments",
            who=w.root,
            body={"base_version": 1, "content": UPDATED, "reason": REASON},
        ).json()["code"]
        == "GUIDE_ARCHIVED"
    )
    restored = publish(w, base, expected_version=1, action="restored")
    assert not restored["archived"] and restored["content"] == CONTENT and restored["catalog_version"] == 2
    assert [r["action"] for r in w.ok("GET", base + "/publications", who=reader)["items"]] == [
        "restored",
        "archived",
    ]


def test_reader_feedback_is_private_and_can_be_resolved_without_draft_or_source_access(world):
    w = world
    p, _, _, reader, base = published(w)
    feedback = w.ok(
        "POST",
        base + "/feedback",
        who=reader,
        body={
            "publication_version": 0,
            "kind": "outdated",
            "body": "页面上的登录入口已更名，请核对说明中的表述。",
        },
    )
    other = w.member(p, "other-reader")
    w.start(other, grant(w, other, p, [], caps=["project:read", "guides:read"]))
    assert w.ok("GET", base + "/feedback", who=other)["items"] == []
    denied = w.call(
        "POST",
        f"{base}/feedback/{feedback['id']}/resolutions",
        who=reader,
        body={"state": "resolved", "reason": REASON},
    )
    assert denied.status_code in (403, 404)
    assert w.ok("GET", base + "/feedback", who=reader)["items"][0]["state"] == "open"
    w.ok(
        "POST",
        f"{base}/feedback/{feedback['id']}/resolutions",
        who=w.root,
        body={"state": "resolved", "reason": "说明已按页面入口名称修订，发布后请重新查看。"},
    )
    assert w.ok("GET", base + "/feedback", who=reader)["items"][0]["state"] == "resolved"


def test_database_blocks_forged_publication_and_rewriting_immutable_amendments(world, database):
    w = world
    _, _, job, _, base = published(w)
    amendment = amend(w, base)
    runtime = session_factory(database[0])
    try:
        for statement, parameters in [
            ("UPDATE guide_amendments SET content='{}'::jsonb WHERE id=:id", {"id": amendment["id"]}),
            (
                "INSERT INTO guide_publications"
                "(job_id,version,author_id,device_id,action,amendment_id,reason) "
                "VALUES(:job,1,:user,:device,'published',:amendment,:reason)",
                {
                    "job": job["id"],
                    "user": w.root.user_id,
                    "device": w.root.enrollment["device_id"],
                    "amendment": amendment["id"],
                    "reason": REASON,
                },
            ),
        ]:
            with runtime() as db:
                context(db, w.root.user_id, w.root.enrollment["device_id"])
                with pytest.raises(DBAPIError):
                    db.execute(text(statement), parameters)
                    db.commit()
    finally:
        runtime.kw["bind"].dispose()


def test_source_read_only_cannot_amend_and_independent_reviewers_can_approve(world):
    w = world
    p, repo, _, _, base = published(w)
    reader = w.administrator(p, "source-reader")
    caps = ["project:read", "tasks:write", "tasks:publish", "tasks:test"]
    w.start(reader, grant(w, reader, p, [(repo, ["code:read"])], caps=caps))
    assert w.call(
        "POST",
        base + "/amendments",
        who=reader,
        body={"base_version": 0, "content": UPDATED, "reason": REASON},
    ).status_code in (403, 404)
    amendment = amend(w, base)
    for gate_name in ("product", "test"):
        result = w.ok(
            "POST",
            f"{base}/amendments/{amendment['id']}/decisions",
            who=reader,
            body={"gate": gate_name, "approved": True, "reason": REASON},
        )
    assert len(result["reviews"]) == 2 and all(r["author_id"] == reader.user_id for r in result["reviews"])
    assert publish(w, base, amendment_id=amendment["id"])["content"] == UPDATED
