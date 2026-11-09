"""Provider responses are synthetic; persistence and authorization use real PostgreSQL."""

import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_repositories import grant, register, task
from test_tasks import claim, gate, lease, log_body

from agent_hub.db import context
from agent_hub.delivery_evidence import DeliveryEvidence, PolicyInput, Verify, provider_observation
from agent_hub.git_gateway import branch_for
from agent_hub.models import CodeDelivery
from agent_hub.security import HubError, now

COMMIT = "a" * 40
MERGED = "b" * 40
WORKFLOW = ".github/workflows/test.yml"
OTHER_WORKFLOW = ".github/workflows/security.yml"


class Provider:
    def __init__(self, commit=COMMIT, ref="hub/test", environment="production", deployment_id=30):
        self.requests = []
        self.timestamp = (now() - timedelta(minutes=5)).isoformat()
        repo = {"full_name": "example/web"}
        self.pr = {
            "number": 10,
            "head": {"sha": commit, "ref": ref, "repo": repo},
            "base": {"ref": "main", "repo": repo},
            "state": "closed",
            "draft": False,
            "merged": True,
            "merged_at": self.timestamp,
            "merge_commit_sha": MERGED,
        }
        self.ci = {
            "id": 20,
            "workflow_id": 21,
            "head_sha": commit,
            "repository": repo,
            "head_repository": repo,
            "path": WORKFLOW,
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
            "created_at": self.timestamp,
            "updated_at": self.timestamp,
        }
        self.deployment = {
            "id": deployment_id,
            "sha": commit,
            "environment": environment,
            "created_at": self.timestamp,
        }
        self.statuses = [
            {"id": 31, "state": "success", "environment": environment, "created_at": self.timestamp}
        ]
        self.workflow_runs = None
        self.deployments = None
        self.before_response = None
        self.response_override = None

    def request(self, request):
        self.requests.append(request)
        assert request.method == "GET" and request.url.host == "api.github.com"
        assert request.url.path.startswith("/repos/example/web/")
        if self.before_response:
            callback, self.before_response = self.before_response, None
            callback()
        if self.response_override:
            return self.response_override
        path = request.url.path
        if path.endswith("/pulls/10"):
            value = self.pr
        elif path.endswith("/actions/workflows/21/runs"):
            value = {"workflow_runs": self.workflow_runs if self.workflow_runs is not None else [self.ci]}
        elif path.endswith("/actions/runs/" + str(self.ci["id"])):
            value = self.ci
        elif path.endswith("/statuses"):
            value = self.statuses
        elif path.endswith("/deployments"):
            value = self.deployments if self.deployments is not None else [self.deployment]
        elif path.endswith("/deployments/" + str(self.deployment["id"])):
            value = self.deployment
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=value)

    @property
    def transport(self):
        return httpx.MockTransport(self.request)


def policy_body(**overrides):
    return {
        "expected_version": 0,
        "required_kinds": ["ci", "deployment"],
        "workflows": [WORKFLOW],
        "environments": ["production"],
        "change_reason": "明确本仓库每次发布所需的外部交付凭据。",
        **overrides,
    }


def checkpoint(w, p=None, repo=None, commit=COMMIT):
    p = p or w.project()
    repo = repo or register(w, p)
    claimed = claim(w, p, task(w, p, repo))
    base = f"/api/projects/{p}/tasks/{claimed['task']['id']}"
    log = w.ok("POST", base + "/worklogs", who=w.root, body=log_body(claimed, git_commit=commit))
    delivered_ref = branch_for(claimed["task"]["id"], claimed["run"]["id"])
    with w.owner.begin() as db:
        db.add(
            CodeDelivery(
                project_id=uuid.UUID(p),
                repository_id=uuid.UUID(repo["id"]),
                task_id=uuid.UUID(claimed["task"]["id"]),
                run_id=uuid.UUID(claimed["run"]["id"]),
                worklog_id=uuid.UUID(log["id"]),
                author_id=uuid.UUID(w.root.user_id),
                device_id=uuid.UUID(w.root.enrollment["device_id"]),
                git_commit=commit,
                git_ref=delivered_ref,
                receipt_hash="a" * 64,
            )
        )
    submitted = w.ok(
        "POST",
        base + "/submit",
        who=w.root,
        body={**lease(claimed), "worklog_id": log["id"], "expected_version": claimed["task"]["version"]},
    )
    provider = Provider(commit, delivered_ref.removeprefix("refs/heads/"))
    w.client.app.state.delivery_transport = provider.transport
    return p, repo, submitted, log, provider


def configure(w, p, repo, **overrides):
    return w.ok(
        "PUT",
        f"/api/projects/{p}/repositories/{repo['id']}/delivery-policy",
        who=w.root,
        body=policy_body(**overrides),
    )


def observe(w, p, t, log, kind, **overrides):
    return {
        "worklog_id": log["id"],
        "expected_task_version": t["version"],
        "kind": kind,
        "provider_id": {"pull_request": 10, "merge": 10, "ci": 20, "deployment": 30, "rollback": 30}[kind],
        **overrides,
    }


def verify(w, p, t, log, kind, **overrides):
    return w.ok(
        "POST",
        f"/api/projects/{p}/tasks/{t['id']}/delivery-evidence",
        who=w.root,
        body=observe(w, p, t, log, kind, **overrides),
    )


def status(w, p, t):
    return w.ok("GET", f"/api/projects/{p}/tasks/{t['id']}/delivery-evidence", who=w.root)


def release_attempt(w, p, t):
    return w.call(
        "POST",
        f"/api/projects/{p}/tasks/{t['id']}/decisions",
        who=w.root,
        body={
            "gate": "release",
            "approved": True,
            "expected_version": t["version"],
            "worklog_id": t["submission_id"],
            "evidence": "核对当前版本的外部凭据。",
            "instance_id": str(uuid.uuid4()),
            "harness": "human",
        },
    )


def test_release_requires_every_workflow_environment_and_current_provider_state(world):
    w = world
    p, repo, t, log, provider = checkpoint(w)
    configure(w, p, repo, workflows=[WORKFLOW, OTHER_WORKFLOW], environments=["production", "staging"])
    t = gate(w, p, t, "review", override_reason="隔离环境由超管明确记录自审例外。")
    t = gate(w, p, t, "test", override_reason="隔离环境由超管明确记录自测例外。")
    assert release_attempt(w, p, t).json()["code"] == "DELIVERY_EXTERNAL_EVIDENCE_REQUIRED"
    verify(w, p, t, log, "ci")
    verify(w, p, t, log, "deployment")
    assert not status(w, p, t)["release"]["ready"]
    provider.ci["path"] = OTHER_WORKFLOW
    verify(w, p, t, log, "ci")
    provider.deployment["environment"] = provider.statuses[0]["environment"] = "staging"
    verify(w, p, t, log, "deployment")
    assert status(w, p, t)["release"]["ready"]
    provider.ci["id"] = 22
    provider.ci["conclusion"] = "failure"
    verify(w, p, t, log, "ci", provider_id=22)
    assert release_attempt(w, p, t).json()["code"] == "DELIVERY_EXTERNAL_EVIDENCE_REQUIRED"
    provider.ci["conclusion"] = "success"
    verify(w, p, t, log, "ci", provider_id=22)
    assert release_attempt(w, p, t).json()["status"] == "completed"


def test_old_success_cannot_hide_new_failed_run_or_later_environment_deployment(world):
    w = world
    p, repo, t, log, provider = checkpoint(w)
    configure(w, p, repo)
    provider.workflow_runs = [provider.ci, {**provider.ci, "id": 22, "created_at": now().isoformat()}]
    path = f"/api/projects/{p}/tasks/{t['id']}/delivery-evidence"
    assert (
        w.call("POST", path, who=w.root, body=observe(w, p, t, log, "ci")).json()["code"]
        == "DELIVERY_PROVIDER_NOT_LATEST"
    )
    provider.deployments = [
        provider.deployment,
        {**provider.deployment, "id": 32, "sha": "c" * 40, "created_at": now().isoformat()},
    ]
    assert (
        w.call("POST", path, who=w.root, body=observe(w, p, t, log, "deployment")).json()["code"]
        == "DELIVERY_PROVIDER_NOT_LATEST"
    )
    assert not status(w, p, t)["items"]


def test_wrong_commits_workflows_and_environments_do_not_create_receipts(world):
    w = world
    p, repo, t, log, provider = checkpoint(w)
    configure(w, p, repo)
    path = f"/api/projects/{p}/tasks/{t['id']}/delivery-evidence"
    provider.ci["head_sha"] = "c" * 40
    assert (
        w.call("POST", path, who=w.root, body=observe(w, p, t, log, "ci")).json()["code"]
        == "DELIVERY_PROVIDER_COMMIT_MISMATCH"
    )
    provider.ci["head_sha"], provider.ci["path"] = COMMIT, OTHER_WORKFLOW
    assert (
        w.call("POST", path, who=w.root, body=observe(w, p, t, log, "ci")).json()["code"]
        == "DELIVERY_WORKFLOW_NOT_APPROVED"
    )
    provider.deployment["environment"] = "unapproved"
    assert (
        w.call("POST", path, who=w.root, body=observe(w, p, t, log, "deployment")).json()["code"]
        == "DELIVERY_ENVIRONMENT_NOT_APPROVED"
    )
    assert not status(w, p, t)["items"]


def test_squash_merge_links_exact_delivered_head_to_ci_and_deployed_commit(world):
    w = world
    p, repo, t, log, provider = checkpoint(w)
    configure(
        w,
        p,
        repo,
        required_kinds=["merge", "ci", "deployment"],
        ci_target="merged",
        deployment_target="merged",
    )
    receipt = verify(w, p, t, log, "merge")
    provider.ci["head_sha"] = provider.deployment["sha"] = MERGED
    path = f"/api/projects/{p}/tasks/{t['id']}/delivery-evidence"
    assert (
        w.call("POST", path, who=w.root, body=observe(w, p, t, log, "ci")).json()["code"]
        == "DELIVERY_MERGE_EVIDENCE_REQUIRED"
    )
    ci = verify(w, p, t, log, "ci", merge_evidence_id=receipt["id"])
    deployment = verify(w, p, t, log, "deployment", merge_evidence_id=receipt["id"])
    assert ci["git_commit"] == COMMIT and ci["observation"]["verified_commit"] == MERGED
    assert deployment["observation"]["target_health_verified"] is False
    assert status(w, p, t)["release"]["ready"]
    alien = {**observe(w, p, t, log, "deployment"), "merge_evidence_id": str(uuid.uuid4())}
    assert w.call("POST", path, who=w.root, body=alien).json()["code"] == "DELIVERY_MERGE_EVIDENCE_REQUIRED"


def test_expired_observations_and_policy_revisions_block_release(world):
    w = world
    p, repo, t, log, _ = checkpoint(w)
    configure(w, p, repo, required_kinds=["ci"], max_age_seconds=60)
    receipt = verify(w, p, t, log, "ci")
    with w.owner.begin() as db:
        row = db.get(DeliveryEvidence, uuid.UUID(receipt["id"]))
        row.observation = {**row.observation, "observed_at": (now() - timedelta(seconds=61)).isoformat()}
    assert not status(w, p, t)["release"]["ready"]
    verify(w, p, t, log, "ci")
    assert status(w, p, t)["release"]["ready"]
    configure(w, p, repo, expected_version=1, required_kinds=["ci"])
    assert not status(w, p, t)["release"]["ready"]


def test_late_old_request_never_overrides_newer_failed_observation(world):
    w = world
    p, repo, t, log, provider = checkpoint(w)
    configure(w, p, repo, required_kinds=["ci"])
    first = verify(w, p, t, log, "ci")
    provider.ci["conclusion"] = "failure"
    verify(w, p, t, log, "ci")
    with w.owner.begin() as db:
        # A slow response may be saved later; the observation start still fences it.
        db.get(DeliveryEvidence, uuid.UUID(first["id"])).created_at = now() + timedelta(seconds=1)
    assert not status(w, p, t)["release"]["ready"]


def test_repo_scope_log_visibility_and_mutation_retries_are_rechecked(world):
    w = world
    p, repo, t, log, _ = checkpoint(w)
    alien_repo = register(w, p, "pc")
    member = w.member(p)
    w.start(member, grant(w, member, p, [(repo, ["code:read"])], caps=["project:read", "tasks:write"]))
    path = f"/api/projects/{p}/tasks/{t['id']}/delivery-evidence"
    assert w.call("POST", path, who=member, body=observe(w, p, t, log, "ci")).status_code == 404
    assert w.ok("GET", path, who=member)["items"] == []
    assert w.call("GET", path + "?repository_id=" + alien_repo["id"], who=w.root).status_code == 404
    body, key = observe(w, p, t, log, "merge"), str(uuid.uuid4())
    receipt = w.ok("POST", path, who=w.root, body=body, idem=key)
    assert w.ok("POST", path, who=w.root, body=body, idem=key) == receipt
    configure(w, p, repo, required_kinds=[])
    assert (
        w.call("POST", path, who=w.root, body=body, idem=key).json()["code"] == "DELIVERY_VERIFICATION_STALE"
    )


def test_policy_change_during_provider_io_rejects_stale_save_without_holding_locks(world):
    w = world
    p, repo, t, log, provider = checkpoint(w)
    configure(w, p, repo)
    provider.before_response = lambda: configure(w, p, repo, expected_version=1)
    response = w.call(
        "POST",
        f"/api/projects/{p}/tasks/{t['id']}/delivery-evidence",
        who=w.root,
        body=observe(w, p, t, log, "ci"),
    )
    assert response.json()["code"] == "DELIVERY_VERIFICATION_STALE"
    assert status(w, p, t)["items"] == []


def test_membership_revoked_during_provider_io_cannot_create_evidence(world):
    w = world
    p, repo, t, log, provider = checkpoint(w)
    member = w.member(p)
    w.start(
        member,
        grant(w, member, p, [(repo, ["code:read"])], caps=["project:read", "tasks:write", "logs:read"]),
    )

    def revoke():
        w.ok("POST", f"/api/projects/{p}/members/{member.user_id}/revoke", who=w.root, body={})

    provider.before_response = revoke
    response = w.call(
        "POST",
        f"/api/projects/{p}/tasks/{t['id']}/delivery-evidence",
        who=member,
        body=observe(w, p, t, log, "merge"),
    )
    assert response.status_code in {401, 403, 404, 409}
    assert status(w, p, t)["items"] == []


def test_database_rejects_receipt_mutation_and_completion_without_external_evidence(world):
    w = world
    p, repo, t, log, _ = checkpoint(w)
    configure(w, p, repo, required_kinds=["ci", "deployment"])
    receipt = verify(w, p, t, log, "ci")
    with pytest.raises(DBAPIError), w.client.app.state.sessions.begin() as db:
        context(db, w.root.user_id, w.root.enrollment["device_id"])
        db.execute(text("UPDATE delivery_evidence SET successful=false WHERE id=:id"), {"id": receipt["id"]})
    with (
        pytest.raises(DBAPIError, match="DELIVERY_EXTERNAL_EVIDENCE_REQUIRED"),
        w.client.app.state.sessions.begin() as db,
    ):
        context(db, w.root.user_id, w.root.enrollment["device_id"])
        db.execute(text("UPDATE tasks SET status='completed' WHERE id=:id"), {"id": t["id"]})


def test_confirmed_retry_returns_original_receipt_without_requiring_provider_availability(world):
    w = world
    p, _, t, log, provider = checkpoint(w)
    key, body = str(uuid.uuid4()), observe(w, p, t, log, "merge")
    path = f"/api/projects/{p}/tasks/{t['id']}/delivery-evidence"
    receipt = w.ok("POST", path, who=w.root, body=body, idem=key)
    provider.response_override = httpx.Response(503)
    count = len(provider.requests)
    assert w.ok("POST", path, who=w.root, body=body, idem=key) == receipt
    assert len(provider.requests) == count
    assert len(status(w, p, t)["items"]) == 1


def test_unpushed_declared_commit_cannot_receive_external_evidence(world):
    w = world
    p, _, t, log, _ = checkpoint(w)
    with w.owner.begin() as db:
        db.execute(text("DELETE FROM code_deliveries WHERE worklog_id=:id"), {"id": log["id"]})
    response = w.call(
        "POST",
        f"/api/projects/{p}/tasks/{t['id']}/delivery-evidence",
        who=w.root,
        body=observe(w, p, t, log, "merge"),
    )
    assert response.json()["code"] == "DELIVERY_CHECKPOINT_NOT_PUSHED"


def test_rollback_requires_both_the_replaced_and_restored_deployments(world):
    w = world
    p, repo, first_task, first_log, first_provider = checkpoint(w)
    configure(w, p, repo, required_kinds=[])
    first_provider.deployment["created_at"] = first_provider.statuses[0]["created_at"] = (
        now() - timedelta(minutes=30)
    ).isoformat()
    original = verify(w, p, first_task, first_log, "deployment")
    _, _, second_task, second_log, second_provider = checkpoint(w, p, repo, "c" * 40)
    second_provider.deployment["id"] = 40
    second_provider.deployment["created_at"] = second_provider.statuses[0]["created_at"] = (
        now() - timedelta(minutes=20)
    ).isoformat()
    replaced = verify(w, p, second_task, second_log, "deployment", provider_id=40)
    # Credential rotation must not erase historical release/rollback provenance.
    w.ok(
        "PUT",
        f"/api/projects/{p}/repositories/{repo['id']}/credential",
        who=w.root,
        body={"expected_version": 1, "token": "test-only-rotated-repository-credential"},
    )
    _, _, rollback_task, rollback_log, rollback_provider = checkpoint(w, p, repo, COMMIT)
    rollback_provider.deployment["id"] = 50
    receipt = verify(
        w,
        p,
        rollback_task,
        rollback_log,
        "rollback",
        provider_id=50,
        rollback_of_id=replaced["id"],
        rollback_to_id=original["id"],
    )
    assert receipt["successful"] and receipt["rollback_to_id"] == original["id"]
    response = w.call(
        "POST",
        f"/api/projects/{p}/tasks/{rollback_task['id']}/delivery-evidence",
        who=w.root,
        body=observe(
            w,
            p,
            rollback_task,
            rollback_log,
            "rollback",
            provider_id=50,
            rollback_of_id=original["id"],
            rollback_to_id=replaced["id"],
        ),
    )
    assert response.json()["code"] == "DELIVERY_ROLLBACK_SOURCE_MISMATCH"


@pytest.mark.parametrize("bad", [True, -1, 2**53, "20", "https://evil.example"])
def test_provider_ids_are_strict_bounded_numbers(bad):
    with pytest.raises(ValueError):
        Verify(worklog_id=uuid.uuid4(), expected_task_version=1, kind="ci", provider_id=bad)


@pytest.mark.parametrize(
    "kind,mutate",
    [
        ("ci", lambda p: p.ci.update(run_attempt=True)),
        ("ci", lambda p: p.ci.update(status="x" * 10000)),
        ("ci", lambda p: p.ci.update(updated_at="2026-01-01T00:00:00")),
        ("deployment", lambda p: p.statuses[0].update(state="invented")),
        ("deployment", lambda p: p.statuses[0].update(id=True)),
        ("merge", lambda p: p.pr.update(merged="yes")),
        ("merge", lambda p: p.pr.update(merge_commit_sha="not-a-commit")),
    ],
)
def test_malformed_provider_fields_never_become_success(kind, mutate):
    provider = Provider()
    mutate(provider)
    body = Verify(
        worklog_id=uuid.uuid4(),
        expected_task_version=1,
        kind=kind,
        provider_id={"ci": 20, "deployment": 30, "merge": 10}[kind],
    )
    with pytest.raises(HubError) as failure:
        provider_observation(
            "https://github.com/example/web",
            "main",
            COMMIT,
            "refs/heads/hub/test",
            body,
            policy_body(),
            "test-credential",
            transport=provider.transport,
        )
    assert failure.value.code == "DELIVERY_PROVIDER_INVALID"


def test_draft_pr_is_not_a_success_and_redirects_never_forward_credentials():
    provider = Provider()
    body = Verify(worklog_id=uuid.uuid4(), expected_task_version=1, kind="pull_request", provider_id=10)
    provider.pr.update(state="open", draft=True, merged=False, merged_at=None)
    observation = provider_observation(
        "https://github.com/example/web",
        "main",
        COMMIT,
        "refs/heads/hub/test",
        body,
        policy_body(),
        "test-credential",
        transport=provider.transport,
    )
    assert not observation["successful"]
    provider.response_override = httpx.Response(302, headers={"Location": "https://evil.example/steal"})
    with pytest.raises(HubError):
        provider_observation(
            "https://github.com/example/web",
            "main",
            COMMIT,
            "refs/heads/hub/test",
            body,
            policy_body(),
            "test-credential",
            transport=provider.transport,
        )
    assert all(r.url.host == "api.github.com" for r in provider.requests)


def test_policy_requires_explicit_merge_for_merged_commit_validation():
    with pytest.raises(ValueError):
        PolicyInput(**policy_body(ci_target="merged"))
    with pytest.raises(ValueError):
        PolicyInput(**policy_body(required_kinds=["deployment", "rollback"]))
