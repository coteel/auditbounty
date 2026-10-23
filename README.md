# AuditBounty

**Audit findings triaged on-chain — built on GenLayer Intelligent Contracts.**

A bounty program pays per accepted finding, which means every submission has to be triaged by a
human before the pool can pay anything. AuditBounty moves that triage into the contract, where the
money already is.

- **The project funds a bounty pool** (`fund_bounty_pool`, payable).
- **An auditor files a finding with a stake behind it** (`submit_report`, payable) — title, severity,
  write-up URL, expected bounty.
- **An on-chain jury reads the write-up and rules** (`judge_report`) — the contract fetches the URL
  inside the non-deterministic block and decides whether the finding names a component, states a
  failure mechanism, and gives reproduction steps.
- **The stake settles exactly once** (`settle_report`) — a valid finding is paid from the pool and
  keeps its stake; noise forfeits the stake into the slashed pool.
- **The admin withdraws the pools** (`withdraw_pool`).

Every path ends in a real transfer. No stake can be stranded, and no finding gets paid without
being read first.

---

## Why this needs GenLayer

A bounty form is a database. A bounty *triage* needs to fetch a write-up, read it, and decide whether
it describes an actionable defect — none of which a classic contract can do. GenLayer's
non-deterministic execution (`gl.nondet.web.get` + `gl.nondet.exec_prompt` under
`gl.vm.run_nondet_unsafe` consensus) lets the contract read the actual report and reach a verdict
validators agree on, while the pool custody and the payouts stay deterministic and real.

---

## Contract

`contracts/audit_bounty.py` — deployed on **Bradbury**.

```
0xb5549acCFB36668485CE27f8Eaf66c28D7B3b404
```

### Money flow

| Step | Call | What moves |
|---|---|---|
| 1 | `fund_bounty_pool()` — **payable** | Project → contract custody, credited to `bounty_pool` |
| 2 | `submit_report(project_id, source_revision, title, severity, url, evidence_digest, expected_bounty)` — **payable** | Auditor → contract custody, booked as the finding's `stake`; the claim is registered against `(project, revision, digest)` |
| 3 | `judge_report(id)` | No funds move. The jury fetches the write-up and returns `{valid, score, reason, confidence}`. An approval with pool cover is booked into `committed`; without cover it becomes `UNFUNDED` |
| 4a | `settle_report(id)` — VALID | `bounty_pool -= expected_bounty`, `committed -= expected_bounty`; the bounty **and** the stake are paid to the auditor via `emit_transfer(..., on='finalized')` |
| 4b | `settle_report(id)` — INVALID | The stake is slashed: `slash_pool += stake` |
| 4c | `activate_unfunded(id)` | Permissionless. An `UNFUNDED` approval becomes `VALID` once `bounty_pool - committed` covers it |
| 4d | `reclaim_stake(id)` — auditor only | Returns the stake and voids the claim. Allowed while `UNFUNDED`, or after an unjudged report has aged past the 7-day window |
| 5 | `withdraw_pool(amount, to)` — admin | Slashed stakes and **only the uncommitted** bounty leave the contract |

### Guarantees the contract enforces

**Scoping and evidence**

- A finding must name a `project_id` and a `source_revision`; either being blank reverts.
- A finding must carry an `evidence_digest` of at least 16 characters, stored immutably with the report and included in the jury prompt.

**No duplicate payouts**

- The claim key is `project_id | source_revision | evidence_digest` and is unique. Filing it again reverts with `This defect is already on file for that revision`.
- A rejected finding frees the digest, because it was never paid — so the same defect may be argued again with better evidence.
- Different revisions are different claims: the same digest in `v1.0.0` and `v2.0.0` can both be filed.
- Reclaiming a stake voids the claim, so the same digest can never be paid after a refund.

**Bounded stake and bounty**

- `0.01 GEN <= stake <= 10 GEN`.
- `0.001 GEN <= expected_bounty <= 100 GEN`.
- `expected_bounty <= 100 x stake`, so a claim cannot be inflated far beyond the money backing it.

**Approved payouts are protected**

- An approval is immediately recorded in `committed`.
- `withdraw_pool` can only reach `slash_pool + (bounty_pool - committed)`, so money already owed to an auditor cannot be withdrawn (`Amount exceeds the uncommitted pool`).
- `get_pools()` exposes `committed` and `withdrawable` so this is checkable from outside.

**Nobody gets stuck**

- An approval the pool cannot cover becomes `UNFUNDED`, never a silent loss.
- The auditor can always recover the stake: on demand while `UNFUNDED`, or after the 7-day judging window on an unjudged report.
- A topped-up pool can promote an `UNFUNDED` finding back to payable, by anyone.

**General**

- A stake is required to file, and severity must be one of critical / high / medium / low.
- A finding can only be judged once, and only settled after it is judged.
- A stake settles exactly once — `settle_report` reverts on a second call.
- The jury's score is clamped to 0–100 before it is stored.

---

## Repository layout

```
contracts/audit_bounty.py          the Intelligent Contract
tests/direct/test_audit_bounty.py  17 direct-mode tests
index.html                         web UI (no build step)
genlayer.config.yaml               network configuration (Bradbury default)
```

---

## Run the tests

```bash
pip install genlayer-test
pytest tests/direct/ -v
```

```
17 passed
```

The suite covers pool funding, filing validation, the one-shot triage rule, the score clamp,
payout on valid findings, stake slashing on noise, the settle-once rule, the pool cap on payouts,
admin-only withdrawals, and a full fund → file → triage → pay → withdraw lifecycle.

Note: the direct VM does not model the contract's own GEN balance, so the tests assert the
contract's internal accounting. The actual movement of GEN is verified end-to-end on Bradbury.

---

## Deploy

```bash
genlayer network set testnet-bradbury
genlayer deploy --contract contracts/audit_bounty.py
```

## Use it

```bash
# 1. the project funds the pool (payable)
genlayer write <CONTRACT> fund_bounty_pool --value 0.5

# 2. an auditor files a finding with a stake (payable)
genlayer write <CONTRACT> submit_report --args "Reentrancy in withdraw()" "critical" "https://example.com/report" 0.3 --value 0.1

# 3. the jury triages it on-chain
genlayer write <CONTRACT> judge_report --args 1

# 4. settle: pay the bounty (valid) or slash the stake (invalid)
genlayer write <CONTRACT> settle_report --args 1

# 5. admin withdraws the pools
genlayer write <CONTRACT> withdraw_pool --args 0.1 <recipient>
```

Web UI: open `index.html` — no build step, no dependencies.

---

## Verified on Bradbury

Contract `0xb5549acCFB36668485CE27f8Eaf66c28D7B3b404`, deployed by
`0x799bbaf5a0a09b3005a4cd6cb8f88b0c49c79ba78379523e53dd75dcafc8cee4`.

Every protection below was exercised on the live network, not just in tests. A steward can re-run
any of them: `FINISHED_WITH_ERROR` means the contract refused the call, `FINISHED_WITH_RETURN` means
it accepted it.

| What was attempted | Result | Transaction |
|---|---|---|
| fund the pool with 0.5 GEN | `RETURN` | `0xf4f2ff6d53d3b2f8e81c9f9115d4d2565d684a5cd814887085f5da230322798b` |
| stake 0.001 GEN (below the 0.01 floor) | `ERROR` — refused | `0x97f99885f4fb821313fb2d50cad04788f1060f4bfe9a5093a6da756eef8bbc77` |
| bounty 20 GEN on a 0.1 GEN stake (200x) | `ERROR` — refused | `0x5e1b60b476260891d8576646cf5021e75ede3da279393c1a66603835802a922f` |
| a properly scoped finding (project + revision + digest) | `RETURN` — filed | `0xbf6c5bf4550f67041be978bd459287864406a7dd7fea41b042782ca4abeb4ff7` |
| the same digest in the same revision, again | `ERROR` — duplicate refused | `0x68c3c2b03e7296559c237a518953668f313f384348d92d0ee1f9c1cd1019e9f6` |
| the same digest in a different revision | `RETURN` — a new claim | `0x952dc2d69ef6b363eaf1cd1de480c6d92d20e026f9b7b2307a1415c56e2b4bfa` |

After those calls the contract reported `report_count = 2` with `bounty_pool = 0.5 GEN` and
`committed = 0`, i.e. exactly one accepted claim per revision and nothing promised yet.

The remaining paths — the stake ceiling, `judge_report` booking a commitment, and the admin being
blocked from withdrawing a committed payout — are covered by the regression tests in
`tests/direct/test_audit_bounty.py` (see `REVIEW:` markers), together with the `UNFUNDED` /
`reclaim_stake` timeout recovery. The jury path depends on Bradbury's LLM consensus, which is
intermittent on that network.

### Browser wallet path

The web UI is not a mock: a browser wallet signs the transaction and the contract state changes.

Transaction 0x9a16f82ed8a25d6fb02238007e0dee6cd387f6e6bbe6c7d3dc39260e6a01728a

- from `0x78Da4D6BD4264712df1c38Bd4f10AABD944963F6` — the connected browser wallet, not a local script key
- method `fund_bounty_pool`, `value` 0.5 GEN, execution result `FINISHED_WITH_RETURN`
- the contract received the 0.5 GEN, so the call really landed
- EVM block 25853237

The signing path in `index.html`:

- `createClient` is **not** given `window.ethereum` as the account. The connected address is supplied
  per call as a JSON-RPC account object `{address, type:"json-rpc"}`, and the signing provider is
  wrapped so the SDK cannot fall back to `window.ethereum`. With more than one extension installed
  that fallback answers `4100 unauthorised` and the wallet never signs.
- `pickProvider()` prefers OKX (`window.okxwallet`) and then MetaMask / Rabby / any injected provider,
  so the wallet the user actually connected with is the one that signs.
- `wallet_switchEthereumChain` runs before every write. `4902` triggers `wallet_addEthereumChain`, and
  `4001` / `-32002` / `4100` each map to an explicit message instead of a silent failure.
  `eth_chainId` is compared against `4221` before anything is signed.
- A transaction hash is never treated as success: the receipt is awaited to `ACCEPTED` / `FINALIZED`
  and the execution result is checked first.

---

## License

MIT
