# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
from dataclasses import dataclass
from genlayer import *

# ---------------------------------------------------------------- storage ---


@allow_storage
@dataclass
class Report:
    auditor: Address         # who filed the finding (and whose stake is at risk)
    title: str
    severity: str            # critical / high / medium / low
    report_url: str          # where the full write-up lives
    expected_bounty: u256    # claimed payout for this finding
    stake: u256              # anti-noise bond posted when filing
    score: u256              # 0-100 validity score from the jury
    state: u8                # 0=SUBMITTED 1=VALID 2=INVALID 3=PAID
    verdict_reason: str
    confidence: str
    stake_settled: bool


# external message interface for paying an EOA / EVM address on the chain layer
@gl.evm.contract_interface
class _Payee:
    class View:
        pass

    class Write:
        pass


# ------------------------------------------------------------------ contract --


class AuditBounty(gl.Contract):
    """A code-audit bounty pool where the triage is done on-chain.

    Projects lose audit findings to noise: a pool pays per accepted bug, so every report has to be
    triaged by a human first. AuditBounty moves that triage into the contract. The project funds a
    bounty pool; an auditor files a finding with a stake behind it; an LLM jury fetches the write-up
    and rules whether it describes a real, reproducible defect in scope. Valid findings are paid out
    of the pool and keep their stake; noise loses the stake into the pool.

    Both directions are real transfers: the pool and the stakes are custody (gl.message.value),
    payouts and refunds leave through emit_transfer, slashed stakes stay in the contract and are
    withdrawable by the project.
    """

    reports: TreeMap[u256, Report]
    next_id: u256
    bounty_pool: u256        # funded by the project, pays valid findings
    slash_pool: u256         # stakes lost by invalid findings
    admin: Address

    def __init__(self):
        self.next_id = u256(1)
        self.bounty_pool = u256(0)
        self.slash_pool = u256(0)
        self.admin = gl.message.sender_address

    # -------------------------------------------------------- internals ----

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

    def _pay(self, to: Address, amount: u256):
        """Send GEN out of this contract (external message, executed on finalization).

        The explicit ``on='finalized'`` matches the documented form for value transfers;
        without it the message may not be emitted at all.
        """
        if amount <= u256(0):
            return
        _Payee(to).emit_transfer(value=u256(amount), on='finalized')

    # ------------------------------------------------------------- views ----

    @gl.public.view
    def get_pools(self) -> dict:
        return {
            "admin": "0x" + self._addr_bytes(self.admin).hex(),
            "bounty_pool": self.bounty_pool,
            "slash_pool": self.slash_pool,
            "contract_balance": self.balance,
            "report_count": self.next_id - u256(1),
        }

    @gl.public.view
    def get_report(self, report_id: u256) -> dict:
        r = self.reports[report_id]
        return {
            "id": report_id,
            "auditor": "0x" + self._addr_bytes(r.auditor).hex(),
            "title": r.title,
            "severity": r.severity,
            "report_url": r.report_url,
            "expected_bounty": r.expected_bounty,
            "stake": r.stake,
            "score": r.score,
            "state": r.state,
            "verdict_reason": r.verdict_reason,
            "confidence": r.confidence,
            "stake_settled": r.stake_settled,
        }

    @gl.public.view
    def get_report_count(self) -> u256:
        return self.next_id - u256(1)

    @gl.public.view
    def get_contract_balance(self) -> u256:
        return self.balance

    # ------------------------------------------------------------ writes ----

    @gl.public.write.payable
    def fund_bounty_pool(self):
        """The project adds GEN to the pool that pays valid findings."""
        value = gl.message.value
        if value <= u256(0):
            raise gl.vm.UserError("Send GEN with the call")
        self.bounty_pool = self.bounty_pool + value

    @gl.public.write.payable
    def submit_report(self, title: str, severity: str, report_url: str, expected_bounty: u256) -> u256:
        """An auditor files a finding and stakes GEN behind it (payable)."""
        stake = gl.message.value
        if stake <= u256(0):
            raise gl.vm.UserError("A stake is required: send GEN with the call")
        if len(title.strip()) == 0:
            raise gl.vm.UserError("Title is required")
        sev = severity.strip().lower()
        if sev not in ("critical", "high", "medium", "low"):
            raise gl.vm.UserError("Severity must be critical, high, medium or low")
        if len(report_url.strip()) == 0 or not (
                report_url.startswith("http://") or report_url.startswith("https://")):
            raise gl.vm.UserError("A valid report URL is required")
        if expected_bounty <= u256(0):
            raise gl.vm.UserError("Expected bounty must be positive")

        r = Report(
            auditor=self._to_addr(gl.message.sender_address),
            title=title,
            severity=sev,
            report_url=report_url,
            expected_bounty=expected_bounty,
            stake=stake,
            score=u256(0),
            state=u8(0),
            verdict_reason="",
            confidence="",
            stake_settled=False,
        )
        rid = self.next_id
        self.reports[rid] = r
        self.next_id = rid + u256(1)
        return rid

    @gl.public.write
    def judge_report(self, report_id: u256):
        """LLM jury fetches the write-up and rules whether it is a real, in-scope defect."""
        r = self._rep(report_id)
        if r.state != u8(0):
            raise gl.vm.UserError("Report has already been judged")

        title_mem = str(r.title)
        severity_mem = str(r.severity)
        url_mem = str(r.report_url)
        bounty_mem = int(r.expected_bounty)
        stake_mem = int(r.stake)
        pool_mem = int(self.bounty_pool)

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
                "FINDING TITLE: " + title_mem + "\n"
                "CLAIMED SEVERITY: " + severity_mem + "\n"
                "REQUESTED BOUNTY: " + str(bounty_mem) + " wei of GEN\n"
                "POOL AVAILABLE: " + str(pool_mem) + " wei of GEN\n"
                "AUDITOR STAKE AT RISK: " + str(stake_mem) + " wei of GEN\n"
                "REPORT URL: " + url_mem + "\n\n"
                "REPORT CONTENT FETCHED RIGHT NOW:\n" + page + "\n\n"
                "Decide ONLY from the fetched content above. Accept the finding when it names a "
                "specific component, describes the mechanism of failure, and states how to reproduce "
                "it - i.e. it is actionable for a developer. Reject it when the page is unreachable, "
                "empty, generic advice, a style nit with no defect, or merely asserts a bug without "
                "a mechanism or reproduction.\n\n"
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
            mine = leader_fn()
            if not isinstance(mine, dict) or not isinstance(leaders_res.calldata, dict):
                return False
            x = mine.get("valid")
            y = leaders_res.calldata.get("valid")
            if isinstance(x, str):
                x = x.strip().lower() == "true"
            if isinstance(y, str):
                y = y.strip().lower() == "true"
            return bool(x) == bool(y)

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

        r.state = u8(1) if bool(ok) else u8(2)
        r.score = u256(score_val)
        r.verdict_reason = str(result.get("reason", ""))
        r.confidence = str(result.get("confidence", ""))

    @gl.public.write
    def settle_report(self, report_id: u256):
        """Settle a judged report exactly once: pay the bounty, or forfeit the stake."""
        r = self._rep(report_id)
        if r.state == u8(0):
            raise gl.vm.UserError("Report has not been judged yet")
        if r.state == u8(3):
            raise gl.vm.UserError("Report already paid out")
        if r.stake_settled:
            raise gl.vm.UserError("Stake already settled")

        r.stake_settled = True

        if r.state == u8(1):
            if int(r.expected_bounty) > int(self.bounty_pool):
                raise gl.vm.UserError("Bounty pool does not cover this finding")
            self.bounty_pool = self.bounty_pool - r.expected_bounty
            r.state = u8(3)                       # PAID
            self._pay(r.auditor, r.expected_bounty)
            self._pay(r.auditor, r.stake)         # honest auditor keeps the stake
        else:
            self.slash_pool = self.slash_pool + r.stake   # noise forfeits it

    @gl.public.write
    def withdraw_pool(self, amount: u256, to: Address):
        """The project withdraws slashed stakes (and unspent bounty)."""
        self._only_admin()
        if amount <= u256(0):
            raise gl.vm.UserError("Amount must be positive")
        available = self.slash_pool + self.bounty_pool
        if int(amount) > int(available):
            raise gl.vm.UserError("Not enough in the pool")

        if int(amount) <= int(self.slash_pool):
            self.slash_pool = self.slash_pool - amount
        else:
            rest = amount - self.slash_pool
            self.slash_pool = u256(0)
            if int(rest) > int(self.bounty_pool):
                raise gl.vm.UserError("Not enough in the pool")
            self.bounty_pool = self.bounty_pool - rest

        self._pay(self._to_addr(to), amount)
