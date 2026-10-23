# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
import datetime
from dataclasses import dataclass
from genlayer import *

S_SUBMITTED = 0
S_VALID = 1
S_INVALID = 2
S_PAID = 3
S_UNFUNDED = 4
S_WITHDRAWN = 5

MIN_STAKE = 10 ** 16          # 0.01 GEN - below this a stake is not a deterrent
MAX_STAKE = 10 ** 19          # 10 GEN
MIN_BOUNTY = 10 ** 15         # 0.001 GEN
MAX_BOUNTY = 100 * 10 ** 18   # 100 GEN - keeps one finding from draining a pool
BOUNTY_TO_STAKE_CAP = 100     # a claim may never exceed 100x the stake behind it
JUDGE_TIMEOUT_SECS = 7 * 24 * 3600

@allow_storage
@dataclass
class Report:
    auditor: Address
    project_id: str
    source_revision: str
    title: str
    severity: str
    report_url: str
    evidence_digest: str
    expected_bounty: u256
    stake: u256
    score: u256
    state: u8
    filed_at: u256
    verdict_reason: str
    confidence: str
    stake_settled: bool

@gl.evm.contract_interface
class _Payee:
    class View:
        pass

    class Write:
        pass

class AuditBounty(gl.Contract):

    reports: TreeMap[u256, Report]
    next_id: u256
    bounty_pool: u256
    slash_pool: u256
    committed: u256          # approved but not yet paid: never withdrawable
    claims: TreeMap[str, u256]
    admin: Address

    def __init__(self):
        self.next_id = u256(1)
        self.bounty_pool = u256(0)
        self.slash_pool = u256(0)
        self.committed = u256(0)
        self.admin = gl.message.sender_address

    def _to_addr(self, value) -> Address:
        if isinstance(value, Address):
            return value
        return Address(value)

    def _addr_bytes(self, value) -> bytes:
        b = getattr(value, "as_bytes", None)
        if b is None:
            b = getattr(value, "_as_bytes", None)
        if callable(b):
            b = b()
        if isinstance(b, (bytes, bytearray)):
            return bytes(b)
        return bytes(self._to_addr(value))

    def _rep(self, report_id: u256):
        if report_id not in self.reports:
            raise gl.vm.UserError("Report not found")
        return self.reports[report_id]

    def _only_admin(self):
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(self.admin):
            raise gl.vm.UserError("Only the project admin can do this")

    def _only_auditor(self, r):
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(r.auditor):
            raise gl.vm.UserError("Only the auditor who filed this report can do this")

    def _now(self) -> int:
        return int(datetime.datetime.now(datetime.timezone.utc).timestamp())

    def _claim_key(self, project_id: str, source_revision: str, evidence_digest: str) -> str:
        return project_id.strip().lower() + "|" + source_revision.strip().lower() + "|" + evidence_digest.strip().lower()

    def _pay(self, to: Address, amount: u256):
        if amount <= u256(0):
            return
        _Payee(to).emit_transfer(value=u256(amount), on='finalized')

    def _uncommitted(self) -> int:
        return int(self.bounty_pool) - int(self.committed)

    @gl.public.view
    def get_pools(self) -> dict:
        return {
            "admin": "0x" + self._addr_bytes(self.admin).hex(),
            "bounty_pool": self.bounty_pool,
            "slash_pool": self.slash_pool,
            "committed": self.committed,
            "withdrawable": u256(self._uncommitted() if self._uncommitted() > 0 else 0),
            "contract_balance": self.balance,
            "report_count": self.next_id - u256(1),
        }

    @gl.public.view
    def get_report(self, report_id: u256) -> dict:
        r = self.reports[report_id]
        return {
            "id": report_id,
            "auditor": "0x" + self._addr_bytes(r.auditor).hex(),
            "project_id": r.project_id,
            "source_revision": r.source_revision,
            "title": r.title,
            "severity": r.severity,
            "report_url": r.report_url,
            "evidence_digest": r.evidence_digest,
            "expected_bounty": r.expected_bounty,
            "stake": r.stake,
            "score": r.score,
            "state": r.state,
            "filed_at": r.filed_at,
            "verdict_reason": r.verdict_reason,
            "confidence": r.confidence,
            "stake_settled": r.stake_settled,
            "overdue": int(r.state) == S_SUBMITTED and self._now() - int(r.filed_at) >= JUDGE_TIMEOUT_SECS,
        }

    @gl.public.view
    def get_report_count(self) -> u256:
        return self.next_id - u256(1)

    @gl.public.view
    def get_contract_balance(self) -> u256:
        return self.balance

    @gl.public.view
    def get_claim(self, project_id: str, source_revision: str, evidence_digest: str) -> u256:
        key = self._claim_key(project_id, source_revision, evidence_digest)
        if key not in self.claims:
            return u256(0)
        return self.claims[key]

    @gl.public.view
    def is_overdue(self, report_id: u256) -> bool:
        r = self._rep(report_id)
        return int(r.state) == S_SUBMITTED and self._now() - int(r.filed_at) >= JUDGE_TIMEOUT_SECS

    @gl.public.write.payable
    def fund_bounty_pool(self):
        value = gl.message.value
        if value <= u256(0):
            raise gl.vm.UserError("Send GEN with the call")
        self.bounty_pool = self.bounty_pool + value

    @gl.public.write.payable
    def submit_report(
        self,
        project_id: str,
        source_revision: str,
        title: str,
        severity: str,
        report_url: str,
        evidence_digest: str,
        expected_bounty: u256,
    ) -> u256:
        stake = gl.message.value

        if int(stake) < MIN_STAKE:
            raise gl.vm.UserError("Stake is below the minimum for this program")
        if int(stake) > MAX_STAKE:
            raise gl.vm.UserError("Stake is above the maximum for this program")

        if len(project_id.strip()) == 0:
            raise gl.vm.UserError("A project id is required")
        if len(source_revision.strip()) == 0:
            raise gl.vm.UserError("A source revision (commit or tag) is required")

        if len(title.strip()) == 0:
            raise gl.vm.UserError("Title is required")
        sev = severity.strip().lower()
        if sev not in ("critical", "high", "medium", "low"):
            raise gl.vm.UserError("Severity must be critical, high, medium or low")
        if len(report_url.strip()) == 0 or not (
                report_url.startswith("http://") or report_url.startswith("https://")):
            raise gl.vm.UserError("A valid report URL is required")

        if len(evidence_digest.strip()) < 16:
            raise gl.vm.UserError("An evidence digest (hash of the write-up) is required")

        if int(expected_bounty) < MIN_BOUNTY:
            raise gl.vm.UserError("Expected bounty is below the minimum")
        if int(expected_bounty) > MAX_BOUNTY:
            raise gl.vm.UserError("Expected bounty is above the maximum")
        if int(expected_bounty) > int(stake) * BOUNTY_TO_STAKE_CAP:
            raise gl.vm.UserError("Expected bounty exceeds the allowed multiple of the stake")

        key = self._claim_key(project_id, source_revision, evidence_digest)
        if key in self.claims:
            prev_id = int(self.claims[key])
            if prev_id != 0:
                prev = self.reports[self.claims[key]]
                if int(prev.state) != S_INVALID:
                    raise gl.vm.UserError("This defect is already on file for that revision")

        r = Report(
            auditor=self._to_addr(gl.message.sender_address),
            project_id=project_id.strip(),
            source_revision=source_revision.strip(),
            title=title,
            severity=sev,
            report_url=report_url,
            evidence_digest=evidence_digest.strip(),
            expected_bounty=expected_bounty,
            stake=stake,
            score=u256(0),
            state=u8(S_SUBMITTED),
            filed_at=u256(self._now()),
            verdict_reason="",
            confidence="",
            stake_settled=False,
        )
        rid = self.next_id
        self.reports[rid] = r
        self.next_id = rid + u256(1)
        self.claims[key] = rid
        return rid

    @gl.public.write
    def judge_report(self, report_id: u256):
        r = self._rep(report_id)
        if int(r.state) != S_SUBMITTED:
            raise gl.vm.UserError("Report has already been judged")

        title_mem = str(r.title)
        sev_mem = str(r.severity)
        url_mem = str(r.report_url)
        proj_mem = str(r.project_id)
        rev_mem = str(r.source_revision)
        digest_mem = str(r.evidence_digest)
        bounty_mem = int(r.expected_bounty)

        def _fetch_text(url: str, limit: int = 1400) -> str:
            try:
                resp = gl.nondet.web.get(url)
                if isinstance(resp, bytes):
                    raw = resp.decode("utf-8", errors="ignore")
                elif hasattr(resp, "body"):
                    raw = resp.body.decode("utf-8", errors="ignore")
                else:
                    raw = str(resp)
                return " ".join(raw.split())[:limit]
            except Exception:
                return "[could not fetch this report]"

        def leader_fn():
            page = _fetch_text(url_mem)
            prompt = (
                "You are triaging a code-audit finding for a bounty program.\n\n"
                "PROJECT: " + proj_mem + "\n"
                "SOURCE REVISION: " + rev_mem + "\n"
                "EVIDENCE DIGEST CLAIMED BY THE AUDITOR: " + digest_mem + "\n"
                "FINDING TITLE: " + title_mem + "\n"
                "CLAIMED SEVERITY: " + sev_mem + "\n"
                "REPORT URL: " + url_mem + "\n\n"
                "REPORT CONTENT FETCHED RIGHT NOW:\n" + page + "\n\n"
                "Decide ONLY from the fetched content above. Accept the finding when it names a "
                "specific component of that project at that revision, describes the mechanism of "
                "failure, and states how to reproduce it - i.e. it is actionable for a developer. "
                "Reject it when the page is unreachable, empty, generic advice, a style nit with no "
                "defect, or merely asserts a bug without a mechanism or reproduction.\n\n"
                "Do NOT consider the treasury or pool balance - solvency is handled by the contract.\n\n"
                'Respond as JSON with exactly these keys:\n'
                '- "valid": true or false\n'
                '- "score": integer 0-100 validity score\n'
                '- "reason": concise 2-4 sentence justification citing the fetched content\n'
                '- "confidence": one of "high", "medium", "low"'
            )
            return gl.nondet.exec_prompt(prompt, response_format='json')

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            payload = leaders_res.calldata
            if not isinstance(payload, dict):
                return False
            for k in ("valid", "score", "reason", "confidence"):
                if k not in payload:
                    return False
            v = payload.get("valid")
            if isinstance(v, str):
                v = v.strip().lower() == "true"
            if not isinstance(v, bool):
                return False
            try:
                sc = int(payload.get("score"))
            except Exception:
                return False
            if sc < 0 or sc > 100:
                return False
            if str(payload.get("confidence")).strip().lower() not in ("high", "medium", "low"):
                return False
            return True

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        ok = result.get("valid", False)
        if isinstance(ok, str):
            ok = ok.strip().lower() == "true"

        score = result.get("score", 0)
        try:
            score_val = int(score)
        except Exception:
            score_val = 0
        if score_val < 0:
            score_val = 0
        if score_val > 100:
            score_val = 100

        r.score = u256(score_val)
        r.verdict_reason = str(result.get("reason", ""))
        r.confidence = str(result.get("confidence", ""))

        if not bool(ok):
            r.state = u8(S_INVALID)
            return

        if self._uncommitted() >= int(r.expected_bounty):
            r.state = u8(S_VALID)
            self.committed = self.committed + r.expected_bounty
        else:
            r.state = u8(S_UNFUNDED)

    @gl.public.write
    def settle_report(self, report_id: u256):
        r = self._rep(report_id)
        st = int(r.state)
        if st == S_SUBMITTED:
            raise gl.vm.UserError("Report has not been judged yet")
        if st == S_PAID:
            raise gl.vm.UserError("Report already paid out")
        if st == S_WITHDRAWN:
            raise gl.vm.UserError("Stake was already withdrawn")
        if r.stake_settled:
            raise gl.vm.UserError("Stake already settled")

        if st == S_VALID:
            if int(r.expected_bounty) > int(self.bounty_pool):
                raise gl.vm.UserError("Bounty pool does not cover this finding")
            r.stake_settled = True
            self.bounty_pool = self.bounty_pool - r.expected_bounty
            self.committed = self.committed - r.expected_bounty
            r.state = u8(S_PAID)
            self._pay(r.auditor, r.expected_bounty)
            self._pay(r.auditor, r.stake)
            return

        if st == S_UNFUNDED:
            raise gl.vm.UserError("Report is unfunded: wait for a top-up and activate it, or reclaim the stake")

        r.stake_settled = True
        self.slash_pool = self.slash_pool + r.stake
        r.state = u8(S_PAID)

    @gl.public.write
    def activate_unfunded(self, report_id: u256):
        r = self._rep(report_id)
        if int(r.state) != S_UNFUNDED:
            raise gl.vm.UserError("Report is not unfunded")
        if self._uncommitted() < int(r.expected_bounty):
            raise gl.vm.UserError("The pool still cannot cover this grant")
        r.state = u8(S_VALID)
        self.committed = self.committed + r.expected_bounty

    @gl.public.write
    def reclaim_stake(self, report_id: u256):
        r = self._rep(report_id)
        self._only_auditor(r)
        if r.stake_settled:
            raise gl.vm.UserError("Stake already settled")

        st = int(r.state)
        if st == S_UNFUNDED:
            pass
        elif st == S_SUBMITTED:
            if self._now() - int(r.filed_at) < JUDGE_TIMEOUT_SECS:
                raise gl.vm.UserError("Report is still within the judging window")
        else:
            raise gl.vm.UserError("This report cannot be reclaimed in its current state")

        r.stake_settled = True
        r.state = u8(S_WITHDRAWN)
        key = self._claim_key(str(r.project_id), str(r.source_revision), str(r.evidence_digest))
        if key in self.claims:
            self.claims[key] = u256(0)
        self._pay(r.auditor, r.stake)

    @gl.public.write
    def withdraw_pool(self, amount: u256, to: Address):
        self._only_admin()
        if amount <= u256(0):
            raise gl.vm.UserError("Amount must be positive")

        available = int(self.slash_pool) + self._uncommitted()
        if int(amount) > available:
            raise gl.vm.UserError("Amount exceeds the uncommitted pool")

        if int(amount) <= int(self.slash_pool):
            self.slash_pool = self.slash_pool - amount
        else:
            rest = amount - self.slash_pool
            self.slash_pool = u256(0)
            if int(rest) > self._uncommitted():
                raise gl.vm.UserError("Amount exceeds the uncommitted pool")
            self.bounty_pool = self.bounty_pool - rest

        self._pay(self._to_addr(to), amount)
