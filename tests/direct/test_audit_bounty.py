# -*- coding: utf-8 -*-
"""AuditBounty direct-mode tests — audit findings triaged on-chain.

Run: pytest tests/direct/ -v

Note: the direct VM does not model the contract's own GEN balance, so these tests assert the
contract's internal accounting (bounty pool / stakes). The real movement of GEN is verified
end-to-end on Bradbury.
"""

B = 1000000000000000        # 0.001 GEN


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
    return Address(v)


def _fund(direct_vm, c, who, amount):
    direct_vm.sender = who
    direct_vm.value = amount
    c.fund_bounty_pool()
    direct_vm.value = 0


def _report(direct_vm, c, who, title="Reentrancy in withdraw", severity="high",
            url="https://example.com/report", bounty=400, stake=100):
    direct_vm.sender = who
    direct_vm.value = stake
    rid = c.submit_report(title, severity, url, bounty)
    direct_vm.value = 0
    return rid


# ------------------------------------------------- 1. pool funding ----

def test_fund_pool_requires_value(direct_vm, direct_deploy, direct_alice):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with direct_vm.expect_revert("Send GEN with the call"):
        c.fund_bounty_pool()


def test_pool_tracks_funding(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, 1000)
    _fund(direct_vm, c, direct_bob, 500)
    p = c.get_pools()
    assert int(p["bounty_pool"]) == 1500
    assert int(p["slash_pool"]) == 0


# ------------------------------------------------- 2. filing ----

def test_submit_requires_stake(direct_vm, direct_deploy, direct_alice):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with direct_vm.expect_revert("A stake is required"):
        c.submit_report("Reentrancy", "high", "https://example.com/r", 400)


def test_submit_input_validation(direct_vm, direct_deploy, direct_alice):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_alice
    direct_vm.value = 100
    with direct_vm.expect_revert("Title is required"):
        c.submit_report("", "high", "https://example.com/r", 400)
    with direct_vm.expect_revert("Severity must be"):
        c.submit_report("Bug", "urgent", "https://example.com/r", 400)
    with direct_vm.expect_revert("A valid report URL is required"):
        c.submit_report("Bug", "high", "not-a-url", 400)
    with direct_vm.expect_revert("Expected bounty must be positive"):
        c.submit_report("Bug", "high", "https://example.com/r", 0)
    direct_vm.value = 0


def test_report_booked_as_custody(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    rid = _report(direct_vm, c, direct_bob, stake=100, bounty=400)
    r = c.get_report(rid)
    assert r["title"] == "Reentrancy in withdraw"
    assert r["severity"] == "high"
    assert int(r["stake"]) == 100
    assert int(r["expected_bounty"]) == 400
    assert int(r["state"]) == 0                # SUBMITTED
    assert r["stake_settled"] is False
    assert int(c.get_report_count()) == 1


# ------------------------------------------------- 3. triage ----

def test_judge_requires_report(direct_vm, direct_deploy, direct_alice):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Report not found"):
        c.judge_report(9)


def test_judge_is_one_shot(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    rid = _report(direct_vm, c, direct_bob)
    direct_vm.mock_web(r".*example\.com.*",
                       {"status": 200, "body": "<html>reentrancy in withdraw: state updated after external call, repro steps included</html>"})
    direct_vm.mock_llm(r".*", {"valid": True, "score": 88,
                               "reason": "Specific component, mechanism and repro steps.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.judge_report(rid)
    with direct_vm.expect_revert("Report has already been judged"):
        c.judge_report(rid)


def test_unreachable_report_is_invalid(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    rid = _report(direct_vm, c, direct_bob)
    direct_vm.mock_web(r".*", {"status": 404, "body": ""})
    direct_vm.mock_llm(r".*", {"valid": False, "score": 5,
                               "reason": "The report could not be fetched.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.judge_report(rid)
    r = c.get_report(rid)
    assert int(r["state"]) == 2                # INVALID
    assert int(r["score"]) == 5


def test_score_is_clamped(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    rid = _report(direct_vm, c, direct_bob)
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>detailed defect</html>"})
    direct_vm.mock_llm(r".*", {"valid": True, "score": 9999,
                               "reason": "Solid.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.judge_report(rid)
    assert int(c.get_report(rid)["score"]) == 100


# ------------------------------------------------- 4. settlement ----

def test_valid_report_paid_and_stake_returned(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, 1000)
    rid = _report(direct_vm, c, direct_bob, stake=100, bounty=400)
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>actionable defect with repro</html>"})
    direct_vm.mock_llm(r".*", {"valid": True, "score": 90,
                               "reason": "Actionable.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.judge_report(rid)

    # only the admin settles
    direct_vm.sender = direct_bob
    c.settle_report(rid)                        # settle itself is permissionless
    r = c.get_report(rid)
    assert int(r["state"]) == 3                # PAID
    assert r["stake_settled"] is True
    # 1000 pool - 400 bounty = 600; nothing slashed
    p = c.get_pools()
    assert int(p["bounty_pool"]) == 600
    assert int(p["slash_pool"]) == 0


def test_invalid_report_stake_slashed(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, 1000)
    rid = _report(direct_vm, c, direct_bob, stake=100, bounty=400)
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>just use better variable names</html>"})
    direct_vm.mock_llm(r".*", {"valid": False, "score": 10,
                               "reason": "Style opinion, no defect.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.judge_report(rid)
    c.settle_report(rid)

    p = c.get_pools()
    assert int(p["slash_pool"]) == 100         # stake forfeited
    assert int(p["bounty_pool"]) == 1000       # untouched
    assert c.get_report(rid)["stake_settled"] is True


def test_settle_requires_judgement(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    rid = _report(direct_vm, c, direct_bob)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Report has not been judged yet"):
        c.settle_report(rid)


def test_stake_cannot_settle_twice(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, 1000)
    rid = _report(direct_vm, c, direct_bob, stake=100, bounty=400)
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>noise</html>"})
    direct_vm.mock_llm(r".*", {"valid": False, "score": 5,
                               "reason": "Noise.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.judge_report(rid)
    c.settle_report(rid)
    with direct_vm.expect_revert("Stake already settled"):
        c.settle_report(rid)


def test_cannot_pay_beyond_pool(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, 100)     # pool only 100
    rid = _report(direct_vm, c, direct_bob, stake=100, bounty=400)
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>real defect</html>"})
    direct_vm.mock_llm(r".*", {"valid": True, "score": 80,
                               "reason": "Real.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.judge_report(rid)
    with direct_vm.expect_revert("Bounty pool does not cover this finding"):
        c.settle_report(rid)


# ------------------------------------------------- 5. withdrawal ----

def test_withdraw_pool_admin_only_and_capped(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, 1000)
    rid = _report(direct_vm, c, direct_bob, stake=100, bounty=400)
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>noise</html>"})
    direct_vm.mock_llm(r".*", {"valid": False, "score": 5,
                               "reason": "Noise.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.judge_report(rid)
    c.settle_report(rid)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the project admin can do this"):
        c.withdraw_pool(50, _addr(direct_bob))

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Amount must be positive"):
        c.withdraw_pool(0, _addr(direct_alice))
    with direct_vm.expect_revert("Not enough in the pool"):
        c.withdraw_pool(5000, _addr(direct_alice))

    c.withdraw_pool(100, _addr(direct_alice))   # exactly the slashed amount
    p = c.get_pools()
    assert int(p["slash_pool"]) == 0
    assert int(p["bounty_pool"]) == 1000


# ------------------------------------------------- 6. lifecycle ----

def test_full_audit_lifecycle(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Fund -> file -> triage -> pay -> withdraw. Pools end at zero."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, 1000)
    rid = _report(direct_vm, c, direct_bob, stake=100, bounty=300)
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>reproducible defect</html>"})
    direct_vm.mock_llm(r".*", {"valid": True, "score": 86,
                               "reason": "Clear repro.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.judge_report(rid)
    c.settle_report(rid)

    p = c.get_pools()
    assert int(p["bounty_pool"]) == 700        # 1000 - 300
    c.withdraw_pool(700, _addr(direct_alice))
    assert int(c.get_pools()["bounty_pool"]) == 0


def test_stake_always_settles_one_way_or_the_other(direct_vm, direct_deploy, direct_alice, direct_bob):
    """No finding can leave its stake stranded: it returns if valid, is slashed if not."""
    c = _deploy(direct_vm, direct_deploy, direct_alice)
    _fund(direct_vm, c, direct_alice, 1000)

    good = _report(direct_vm, c, direct_alice, title="Real bug", stake=100, bounty=200,
                   url="https://example.com/real")
    direct_vm.mock_web(r".*example\.com/real.*", {"status": 200, "body": "<html>actionable defect</html>"})
    direct_vm.mock_web(r".*example\.com/noise.*", {"status": 200, "body": "<html>just a style nit</html>"})
    direct_vm.mock_llm(r".*actionable defect.*", {"valid": True, "score": 82,
                                                  "reason": "Actionable.", "confidence": "high"})
    direct_vm.mock_llm(r".*style nit.*", {"valid": False, "score": 12,
                                          "reason": "No defect.", "confidence": "high"})
    direct_vm.sender = direct_alice
    c.judge_report(good)
    c.settle_report(good)
    assert c.get_report(good)["stake_settled"] is True
    assert int(c.get_pools()["slash_pool"]) == 0

    bad = _report(direct_vm, c, direct_bob, title="Style nit", stake=100, bounty=200,
                  url="https://example.com/noise")
    direct_vm.sender = direct_bob
    c.judge_report(bad)
    assert int(c.get_report(bad)["state"]) == 2
    c.settle_report(bad)
    assert c.get_report(bad)["stake_settled"] is True
    assert int(c.get_pools()["slash_pool"]) == 100
