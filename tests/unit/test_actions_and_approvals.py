"""Action arguments, the policy check, and one-time approval tokens."""

from __future__ import annotations

import time

import jwt
import pytest
from mara_core.actions import InvalidAction, action_hash, normalize_args, render_preview
from mara_core.auth.approval import ApprovalError, ApprovalSigner, ApprovalVerifier
from mara_core.config import Allowlists
from mara_orchestrator.graph.nodes.actions import check_action, typed_facts

ALLOW = Allowlists(
    github_repos=["acme-cloud/support-followups"],
    slack_channels=["#incident-review"],
    email_domains=["acme-cloud.example"],
    url_domains=["status.acme-cloud.example"],
)


def test_defaults_make_the_hash_stable() -> None:
    a = {"repo": "acme-cloud/support-followups", "title": "Follow up"}
    b = {"repo": "acme-cloud/support-followups", "title": "Follow up", "body": "", "labels": [], "assignee": None}
    assert action_hash("create_github_issue", a) == action_hash("create_github_issue", b)
    assert action_hash("create_github_issue", a) != action_hash("create_github_issue", {**a, "title": "Follow up!"})


def test_invalid_arguments_are_rejected() -> None:
    with pytest.raises(InvalidAction):
        normalize_args("create_github_issue", {"repo": "x/y", "title": "t" * 500})
    with pytest.raises(InvalidAction):
        normalize_args("delete_everything", {})


def test_preview_shows_exactly_what_will_be_sent() -> None:
    preview = render_preview("post_slack_message", {"channel": "#incident-review", "text": "INC-2041 resolved"})
    assert "#incident-review" in preview and "INC-2041 resolved" in preview


@pytest.mark.parametrize(
    "tool,args,reason",
    [
        ("create_github_issue", {"repo": "acme-cloud/secrets", "title": "x"}, "not on the allowlist"),
        ("post_slack_message", {"channel": "#general", "text": "hi"}, "not on the allowlist"),
        ("send_email", {"to": "a@acme-cloud.example", "subject": "s", "body": "b"}, "email is turned off"),
        ("post_slack_message", {"channel": "#incident-review", "text": "mail jane@corp.com"}, "personal data"),
        (
            "create_github_issue",
            {"repo": "acme-cloud/support-followups", "title": "x", "assignee": "@attacker"},
            "does not come from",
        ),
    ],
)
def test_policy_rejects(tool: str, args: dict, reason: str) -> None:
    clean, why = check_action(tool, args, allow=ALLOW, question="please file it", typed="")
    assert clean is None and reason in why


def test_policy_accepts_assignee_from_warehouse_rows_and_strips_links() -> None:
    evidence = [
        {
            "id": "ev_t2_1",
            "source": "sql",
            "data": {"columns": ["name", "owner_github"], "rows": [{"name": "Nova", "owner_github": "mchen"}]},
        }
    ]
    clean, why = check_action(
        "create_github_issue",
        {
            "repo": "acme-cloud/support-followups",
            "title": "Follow up",
            "assignee": "mchen",
            "body": "see https://status.acme-cloud.example/x and http://evil.test/payload",
        },
        allow=ALLOW,
        question="open issues for owners",
        typed=typed_facts(evidence),
    )
    assert why is None
    assert "status.acme-cloud.example" in clean["body"] and "evil.test" not in clean["body"]


def test_approval_token_round_trip(keys: dict) -> None:
    args_hash = action_hash("post_slack_message", {"channel": "#incident-review", "text": "hi"})
    token = ApprovalSigner(keys["approval_private"]).mint(
        run_id="run_1", action_id="run_1-a1", tool="post_slack_message", args_sha256=args_hash, approver="bob"
    )
    claims = ApprovalVerifier(keys["approval_public"]).verify(
        token, tool="post_slack_message", args_sha256=args_hash, action_id="run_1-a1"
    )
    assert claims.approver == "bob" and claims.jti


def test_approval_token_is_bound_to_tool_arguments_and_action(keys: dict) -> None:
    good = action_hash("post_slack_message", {"channel": "#incident-review", "text": "hi"})
    token = ApprovalSigner(keys["approval_private"]).mint(
        run_id="r", action_id="r-a1", tool="post_slack_message", args_sha256=good, approver="bob"
    )
    verifier = ApprovalVerifier(keys["approval_public"])
    changed = action_hash("post_slack_message", {"channel": "#incident-review", "text": "hi!"})
    with pytest.raises(ApprovalError):
        verifier.verify(token, tool="post_slack_message", args_sha256=changed)
    with pytest.raises(ApprovalError):
        verifier.verify(token, tool="create_github_issue", args_sha256=good)
    with pytest.raises(ApprovalError):
        verifier.verify(token, tool="post_slack_message", args_sha256=good, action_id="r-a2")


def test_expired_or_foreign_approval_tokens_fail(keys: dict, tmp_path) -> None:
    from mara_core.auth.keys import generate_ed25519

    h = action_hash("post_slack_message", {"channel": "#incident-review", "text": "hi"})
    expired = ApprovalSigner(keys["approval_private"], ttl_seconds=-60).mint(
        run_id="r", action_id="r-a1", tool="post_slack_message", args_sha256=h, approver="bob"
    )
    with pytest.raises(ApprovalError):
        ApprovalVerifier(keys["approval_public"]).verify(expired, tool="post_slack_message", args_sha256=h)
    other_priv, _ = generate_ed25519(tmp_path / "o.pem", tmp_path / "o.pub.pem")
    forged = ApprovalSigner(str(other_priv)).mint(
        run_id="r", action_id="r-a1", tool="post_slack_message", args_sha256=h, approver="mallory"
    )
    with pytest.raises(ApprovalError):
        ApprovalVerifier(keys["approval_public"]).verify(forged, tool="post_slack_message", args_sha256=h)
    assert jwt.get_unverified_header(forged)["alg"] == "EdDSA"
    assert time.time() > 0
