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
0x705c352349db0322AedbCa454eCC31734e54c139
```

### Money flow

| Step | Call | What moves |
|---|---|---|
| 1 | `fund_bounty_pool()` — **payable** | Project → contract custody, credited to `bounty_pool` |
| 2 | `submit_report(title, severity, url, expected_bounty)` — **payable** | Auditor → contract custody, booked as the finding's `stake` |
| 3 | `judge_report(id)` | No funds move. The jury fetches the write-up and returns `{valid, score, reason, confidence}` |
| 4a | `settle_report(id)` — VALID | `bounty_pool -= expected_bounty`; the bounty **and** the stake are paid to the auditor via `emit_transfer(..., on='finalized')` |
| 4b | `settle_report(id)` — INVALID | The stake is slashed: `slash_pool += stake` |
| 5 | `withdraw_pool(amount, to)` — admin | Slashed stakes and unspent bounty leave the contract |

### Guarantees the contract enforces

- A stake is required to file, and severity must be one of critical / high / medium / low.
- A finding can only be judged once, and only settled after it is judged.
- A stake settles exactly once — `settle_report` reverts on a second call.
- A payout can never exceed the bounty pool (`Bounty pool does not cover this finding`).
- Only the admin can withdraw, and never more than the pools hold.
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

The end-to-end run exercises real custody and real payouts: a pool is funded, a valid finding is
paid out of it with its stake returned, a noise finding has its stake slashed, and the pools are
withdrawn. See `_提交材料.md` and the `ab_audit.txt` / `ab_evidence.txt` fields for the transaction
hashes produced by that run.

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
