# -*- coding: utf-8 -*-
"""AuditBounty direct-mode tests — audit findings triaged on-chain.

Run: pytest tests/direct/ -v

The direct VM does not model the contract's own GEN balance, so these tests assert the contract's
internal accounting (bounty pool / committed / stakes). The real movement of GEN is verified
end-to-end on Bradbury.

Amount unit: B = 0.001 GEN. Stake bounds are 0.01 GEN .. 10 GEN, so a normal report stakes 100*B.
"""

B = 1000000000000000        # 0.001 GEN
STAKE = 100 * B             # 0.1 GEN
BOUNTY = 400 * B            # 0.4 GEN
POOL = 1000 * B             # 1 GEN
DIGEST = "9f2c1a7be0d34f568192ab34cd56ef7890ab12cd34ef56789012ab34cd56ef78"


def _deploy(direct_vm, direct_deploy, admin):
    import pathlib
    import tempfile

    src = pathlib.Path(__file__).resolve().parents[2] / "contracts" / "audit_bounty.py"
    code = src.read_text(encoding="utf-8")
    tmp = pathlib.Path(tempfile.gettempdir()) / "audit_bounty_local.py"
    tmp.write_text(code, encoding="utf-8")
    direct_vm.sender = admin           # whoever deploys becomes the project admin
    return direct_deploy(str(tmp))


def _addr(v):
    from genlayer import Address
    if isinstance(v, (bytes, bytearray)):
        return Address("0x" + bytes(v).hex())
    if isinstance(v, str) and not v.startswith("0x"):
        return Address("0x" + v)
    return Address(v)


def _fund(direct_vm, c, who, amount):
    direct_vm.sender = who
    direct_vm.value = amount
    c.fund_bounty_pool()
    direct_vm.value = 0


def _report(direct_vm, c, who, title="Reentrancy in withdraw", severity="high",
            url="https://example.com/report", bounty=BOUNTY, stake=STAKE,
            project="acme-vault", revision="v1.2.3", digest=DIGEST):
    direct_vm.sender = who
    direct_vm.value = stake
    rid = c.submit_report(project, revision, title, severity, url, digest, bounty)
    direct_vm.value = 0
    return rid


def _mock_valid(direct_vm, score=90):
    direct_vm.mock_web(".*", {"status": 200, "body": "x" * 300})
    direct_vm.mock_llm(".*", {"valid": True, "score": score,
                              "reason": "names the component, the mechanism and the steps",
                              "confidence": "high"})


def _mock_invalid(direct_vm):
    direct_vm.mock_web(".*", {"status": 200, "body": "x" * 300})
    direct_vm.mock_llm(".*", {"valid": False, "score": 10,
                              "reason": "generic advice with no defect",
                              "confidence": "high"})


# ------------------------------------------------- 1. pool funding ----

def test_fund_pool_requires_value(direct_vm, direct_deploy, direct_alice):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with direct_vm.expect_revert("Send GEN with the call"):
        c.fund_bounty_pool()


def test_pool_tracks_funding(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, POOL)
    _fund(direct_vm, c, direct_bob, POOL // 2)
    p = c.get_pools()
    assert int(p["bounty_pool"]) == POOL + POOL // 2
    assert int(p["slash_pool"]) == 0
    assert int(p["committed"]) == 0


# ------------------------------- 2. REVIEW: binding to project + revision ----

def test_finding_must_name_a_project(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Regression for: bind findings to a defined project."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = STAKE
    with direct_vm.expect_revert("A project id is required"):
        c.submit_report("   ", "v1.0.0", "Reentrancy", "high",
                        "https://example.com/r", DIGEST, BOUNTY)
    direct_vm.value = 0


def test_finding_must_name_a_source_revision(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Regression for: bind findings to a source revision."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = STAKE
    with direct_vm.expect_revert("A source revision"):
        c.submit_report("acme-vault", "", "Reentrancy", "high",
                        "https://example.com/r", DIGEST, BOUNTY)
    direct_vm.value = 0


def test_report_records_scope_and_revision(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    rid = _report(direct_vm, c, direct_bob, project="acme-vault", revision="commit-abc123")
    r = c.get_report(rid)
    assert r["project_id"] == "acme-vault"
    assert r["source_revision"] == "commit-abc123"
    assert r["evidence_digest"] == DIGEST


# ------------------------- 3. REVIEW: independently verifiable evidence ----

def test_evidence_digest_is_required(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Regression for: requiring independently verifiable defect evidence."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = STAKE
    with direct_vm.expect_revert("evidence digest"):
        c.submit_report("acme-vault", "v1.0.0", "Reentrancy", "high",
                        "https://example.com/r", "short", BOUNTY)
    direct_vm.value = 0


# ------------------------------------- 4. REVIEW: duplicate payouts ----

def test_same_digest_cannot_be_filed_twice(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Regression for: preventing duplicate payouts on the same defect."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _report(direct_vm, c, direct_bob)
    direct_vm.sender = direct_bob
    direct_vm.value = STAKE
    with direct_vm.expect_revert("already on file"):
        c.submit_report("acme-vault", "v1.2.3", "Same bug again", "high",
                        "https://example.com/r2", DIGEST, BOUNTY)
    direct_vm.value = 0


def test_same_digest_allowed_after_rejection(direct_vm, direct_deploy, direct_alice, direct_bob):
    """A rejected finding frees the digest: it was not paid, so it can be argued again."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    rid = _report(direct_vm, c, direct_bob)
    _mock_invalid(direct_vm)
    c.judge_report(rid)
    assert int(c.get_report(rid)["state"]) == 2          # INVALID
    rid2 = _report(direct_vm, c, direct_bob, title="Re-filed with reproduction steps")
    assert int(rid2) == 2


def test_digest_is_scoped_to_revision(direct_vm, direct_deploy, direct_alice, direct_bob):
    """The same defect digest in a different revision is a different claim."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _report(direct_vm, c, direct_bob, revision="v1.0.0")
    rid2 = _report(direct_vm, c, direct_bob, revision="v2.0.0")
    assert int(rid2) == 2


def test_claim_lookup_exposes_the_binding(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    assert int(c.get_claim("acme-vault", "v1.2.3", DIGEST)) == 0
    rid = _report(direct_vm, c, direct_bob)
    assert int(c.get_claim("acme-vault", "v1.2.3", DIGEST)) == int(rid)


# -------------------------------- 5. REVIEW: bounded stake and bounty ----

def test_stake_below_minimum_rejected(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Regression for: enforcing bounded stake rules."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = B                 # 0.001 GEN, below the 0.01 GEN floor
    with direct_vm.expect_revert("below the minimum"):
        c.submit_report("acme-vault", "v1", "Bug", "high", "https://e.com/r", DIGEST, BOUNTY)
    direct_vm.value = 0


def test_stake_above_maximum_rejected(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = 11 * 10 ** 18     # 11 GEN, above the 10 GEN ceiling
    with direct_vm.expect_revert("above the maximum"):
        c.submit_report("acme-vault", "v1", "Bug", "high", "https://e.com/r", DIGEST, BOUNTY)
    direct_vm.value = 0


def test_bounty_above_maximum_rejected(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = STAKE
    with direct_vm.expect_revert("above the maximum"):
        c.submit_report("acme-vault", "v1", "Bug", "high", "https://e.com/r", DIGEST,
                        101 * 10 ** 18)
    direct_vm.value = 0


def test_bounty_capped_relative_to_stake(direct_vm, direct_deploy, direct_alice, direct_bob):
    """A claim cannot be inflated far beyond the stake backing it."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = STAKE             # 0.1 GEN
    with direct_vm.expect_revert("allowed multiple of the stake"):
        c.submit_report("acme-vault", "v1", "Bug", "high", "https://e.com/r", DIGEST,
                        101 * STAKE)
    direct_vm.value = 0


def test_bounty_below_minimum_rejected(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = STAKE
    with direct_vm.expect_revert("below the minimum"):
        c.submit_report("acme-vault", "v1", "Bug", "high", "https://e.com/r", DIGEST, 0)
    direct_vm.value = 0


def test_submit_requires_stake(direct_vm, direct_deploy, direct_alice):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with direct_vm.expect_revert("below the minimum"):
        c.submit_report("acme-vault", "v1", "Bug", "high", "https://e.com/r", DIGEST, BOUNTY)


def test_submit_input_validation(direct_vm, direct_deploy, direct_alice):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_alice
    direct_vm.value = STAKE
    with direct_vm.expect_revert("Title is required"):
        c.submit_report("acme-vault", "v1", "", "high", "https://e.com/r", DIGEST, BOUNTY)
    with direct_vm.expect_revert("Severity must be"):
        c.submit_report("acme-vault", "v1", "Bug", "urgent", "https://e.com/r", DIGEST, BOUNTY)
    with direct_vm.expect_revert("A valid report URL is required"):
        c.submit_report("acme-vault", "v1", "Bug", "high", "not-a-url", DIGEST, BOUNTY)
    direct_vm.value = 0


def test_report_booked_as_custody(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    rid = _report(direct_vm, c, direct_bob)
    r = c.get_report(rid)
    assert r["title"] == "Reentrancy in withdraw"
    assert r["severity"] == "high"
    assert int(r["stake"]) == STAKE
    assert int(r["expected_bounty"]) == BOUNTY
    assert int(r["state"]) == 0                # SUBMITTED
    assert int(r["filed_at"]) > 0


# ------------------------------------------------- 6. judging ----

def test_judge_requires_report(direct_vm, direct_deploy, direct_alice):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    with direct_vm.expect_revert("Report not found"):
        c.judge_report(99)


def test_judge_is_one_shot(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, POOL)
    rid = _report(direct_vm, c, direct_bob)
    _mock_valid(direct_vm)
    c.judge_report(rid)
    with direct_vm.expect_revert("already been judged"):
        c.judge_report(rid)


def test_unreachable_report_is_invalid(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, POOL)
    rid = _report(direct_vm, c, direct_bob)
    direct_vm.mock_web(".*", {"status": 500, "body": ""})
    direct_vm.mock_llm(".*", {"valid": False, "score": 0,
                              "reason": "cannot be fetched", "confidence": "high"})
    c.judge_report(rid)
    assert int(c.get_report(rid)["state"]) == 2


def test_score_is_clamped(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, POOL)
    rid = _report(direct_vm, c, direct_bob)
    direct_vm.mock_web(".*", {"status": 200, "body": "x" * 300})
    direct_vm.mock_llm(".*", {"valid": True, "score": 250,
                              "reason": "clear defect with reproduction", "confidence": "high"})
    c.judge_report(rid)
    assert int(c.get_report(rid)["score"]) == 100


# ------------- 7. REVIEW: approved payouts protected from withdrawals ----

def test_approval_is_recorded_as_committed(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Regression for: protect approved payouts from administrator withdrawals."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, POOL)
    rid = _report(direct_vm, c, direct_bob)
    _mock_valid(direct_vm)
    c.judge_report(rid)
    p = c.get_pools()
    assert int(c.get_report(rid)["state"]) == 1          # VALID
    assert int(p["committed"]) == BOUNTY
    assert int(p["withdrawable"]) == POOL - BOUNTY


def test_admin_cannot_withdraw_committed_payout(direct_vm, direct_deploy, direct_alice, direct_bob):
    """The pool has money, but it is already owed: withdrawing it must revert."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, POOL)
    rid = _report(direct_vm, c, direct_bob)
    _mock_valid(direct_vm)
    c.judge_report(rid)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("uncommitted"):
        c.withdraw_pool(POOL, _addr(direct_alice))
    # only the uncommitted remainder is reachable
    c.withdraw_pool(POOL - BOUNTY, _addr(direct_alice))
    direct_vm.value = 0


def test_withdraw_pool_admin_only(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, POOL)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the project admin"):
        c.withdraw_pool(B, _addr(direct_bob))


# ---------- 8. REVIEW: unfunded / timeout recovery with stake intact ----

def test_underfunded_approval_becomes_unfunded(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Regression for: safe recovery for underfunded reports."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, BOUNTY // 2)      # cannot cover the claim
    rid = _report(direct_vm, c, direct_bob)
    _mock_valid(direct_vm)
    c.judge_report(rid)
    assert int(c.get_report(rid)["state"]) == 4         # UNFUNDED
    assert int(c.get_pools()["committed"]) == 0         # nothing was promised


def test_unfunded_stake_can_always_be_reclaimed(direct_vm, direct_deploy, direct_alice, direct_bob):
    """The auditor is never trapped: an unfunded approval refunds the stake on demand."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, BOUNTY // 2)
    rid = _report(direct_vm, c, direct_bob)
    _mock_valid(direct_vm)
    c.judge_report(rid)
    direct_vm.sender = direct_bob
    c.reclaim_stake(rid)
    r = c.get_report(rid)
    assert int(r["state"]) == 5                          # WITHDRAWN
    assert r["stake_settled"] is True


def test_unfunded_can_be_activated_after_top_up(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, BOUNTY // 2)
    rid = _report(direct_vm, c, direct_bob)
    _mock_valid(direct_vm)
    c.judge_report(rid)
    assert int(c.get_report(rid)["state"]) == 4
    _fund(direct_vm, c, direct_alice, POOL)
    c.activate_unfunded(rid)
    assert int(c.get_report(rid)["state"]) == 1
    assert int(c.get_pools()["committed"]) == BOUNTY


def test_reclaim_is_auditor_only(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, BOUNTY // 2)
    rid = _report(direct_vm, c, direct_bob)
    _mock_valid(direct_vm)
    c.judge_report(rid)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Only the auditor"):
        c.reclaim_stake(rid)


def test_stale_report_can_be_refunded_after_timeout(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Regression for: safe timeout recovery for unresolved reports."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    rid = _report(direct_vm, c, direct_bob)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("still within the judging window"):
        c.reclaim_stake(rid)
    # 8 days later the judging window has expired
    direct_vm.warp("2026-11-30T00:00:00.000Z")
    direct_vm.sender = direct_bob
    c.reclaim_stake(rid)
    assert int(c.get_report(rid)["state"]) == 5
    assert c.get_report(rid)["stake_settled"] is True


def test_reclaim_voids_the_claim(direct_vm, direct_deploy, direct_alice, direct_bob):
    """A reclaimed digest cannot be paid later, and is free to be argued again."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, BOUNTY // 2)
    rid = _report(direct_vm, c, direct_bob)
    _mock_valid(direct_vm)
    c.judge_report(rid)
    direct_vm.sender = direct_bob
    c.reclaim_stake(rid)
    assert int(c.get_claim("acme-vault", "v1.2.3", DIGEST)) == 0
    rid2 = _report(direct_vm, c, direct_bob, title="Re-filed after reclaim")
    assert int(rid2) == 2


# ------------------------------------------------- 9. settlement ----

def test_valid_report_paid_and_stake_returned(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, POOL)
    rid = _report(direct_vm, c, direct_bob)
    _mock_valid(direct_vm)
    c.judge_report(rid)
    c.settle_report(rid)
    r = c.get_report(rid)
    assert int(r["state"]) == 3                          # PAID
    assert r["stake_settled"] is True
    p = c.get_pools()
    assert int(p["bounty_pool"]) == POOL - BOUNTY
    assert int(p["committed"]) == 0                       # commitment released on payment


def test_invalid_report_stake_slashed(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, POOL)
    rid = _report(direct_vm, c, direct_bob)
    _mock_invalid(direct_vm)
    c.judge_report(rid)
    c.settle_report(rid)
    p = c.get_pools()
    assert int(p["slash_pool"]) == STAKE
    assert int(p["bounty_pool"]) == POOL
    assert int(c.get_report(rid)["state"]) == 3


def test_settle_requires_judgement(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    rid = _report(direct_vm, c, direct_bob)
    with direct_vm.expect_revert("has not been judged"):
        c.settle_report(rid)


def test_stake_cannot_settle_twice(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, POOL)
    rid = _report(direct_vm, c, direct_bob)
    _mock_valid(direct_vm)
    c.judge_report(rid)
    c.settle_report(rid)
    with direct_vm.expect_revert("already paid out"):
        c.settle_report(rid)


def test_unfunded_cannot_be_settled_directly(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, BOUNTY // 2)
    rid = _report(direct_vm, c, direct_bob)
    _mock_valid(direct_vm)
    c.judge_report(rid)
    with direct_vm.expect_revert("unfunded"):
        c.settle_report(rid)


def test_full_audit_lifecycle(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, POOL)

    # The first registered mock wins, so bind each verdict to its own report URL marker.
    direct_vm.mock_web(".*", {"status": 200, "body": "x" * 300})
    direct_vm.mock_llm(".*good-marker.*", {"valid": True, "score": 90,
                                           "reason": "component, mechanism and reproduction present",
                                           "confidence": "high"})
    direct_vm.mock_llm(".*bad-marker.*", {"valid": False, "score": 10,
                                          "reason": "style nit with no defect",
                                          "confidence": "high"})

    good = _report(direct_vm, c, direct_bob, project="acme-vault", revision="v1.0.0",
                   url="https://example.com/good-marker", digest="a" * 64)
    c.judge_report(good)
    c.settle_report(good)

    bad = _report(direct_vm, c, direct_bob, project="acme-vault", revision="v1.0.0",
                  title="Style nit", url="https://example.com/bad-marker", digest="b" * 64)
    c.judge_report(bad)
    c.settle_report(bad)

    p = c.get_pools()
    assert int(p["bounty_pool"]) == POOL - BOUNTY
    assert int(p["slash_pool"]) == STAKE
    assert int(p["committed"]) == 0
    assert int(p["report_count"]) == 2


def test_stake_always_settles_one_way_or_the_other(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, POOL)
    rid = _report(direct_vm, c, direct_bob)
    _mock_valid(direct_vm)
    c.judge_report(rid)
    c.settle_report(rid)
    assert c.get_report(rid)["stake_settled"] is True
