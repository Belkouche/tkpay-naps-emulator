# NAPS Pay M2M TLV Terminal Emulator

Python TCP server that emulates a NAPS Pay terminal for local development and testing.
Speaks the two-phase M2M TLV protocol on **port 4444**.

No dependencies — stdlib Python 3.10+ only.

---

## Usage

```bash
# Approve all payments (default)
python3 naps_emulator.py

# Operator prompted to approve or pick a decline scenario per payment
python3 naps_emulator.py --mode interactive

# Decline all payments (RC=005)
python3 naps_emulator.py --mode decline

# Return a specific error code — numeric or named scenario
python3 naps_emulator.py --mode error --code 909
python3 naps_emulator.py --mode error --code insufficient_funds
python3 naps_emulator.py --mode error --code expired_card

# Phase-1 never responds — tests client read timeout
python3 naps_emulator.py --mode timeout

# Phase-2 never responds — tests 40-second confirmation timeout
python3 naps_emulator.py --mode no_confirm

# Different port + verbose TLV field logging
python3 naps_emulator.py --port 4445 --debug
```

---

## Protocol

```
Your SDK / Client            Emulator (port 4444)
       │                            │
       │  Phase 1 TM=001 ──────────>│  Payment Request
       │<────────── TM=101 ─────────│  RC=000 + STAN + card + DP receipt
       │                            │
       │  Phase 2 TM=002 ──────────>│  Confirmation (same or new connection)
       │<────────── TM=102 ─────────│  RC=000 + STAN + DP customer receipt
```

The emulator keeps the TCP connection open after each response, exactly like the real terminal. Phase-2 can arrive on the same or a new connection.

---

## Response field order

Common fields per phase:

```
TM(001) → NCAI(003) → NS(004) → CR(013) → MT(002) → DE(012) → DA(014) → HE(015)
```

On approval, card details and DP receipt follow:

```
NCAR(007) → DAEX(017) → EM(040) → STAN(008) → DP(010)
```

---

## Receipt format

Each DP line is encoded as:
```
030 002 {lineNum:02d}   ← line number
031 001 {S|G}           ← format: S=normal, G=bold
032 001 {C|G|D}         ← align: C=centre, G=left, D=right
033 {len:03d} {content} ← text content
*                       ← line separator (* on all but last, ? on last)
```

The header line contains `"TKpay"` (bold, centred).

---

## Modes

| `--mode` | Phase-1 | Phase-2 | Use case |
|---|---|---|---|
| `approve` | RC=000 + full receipt | RC=000 + receipt | Normal happy path |
| `interactive` | Operator prompted per payment | RC=000 + receipt (if approved) | Manual scenario selection during dev/demo |
| `decline` | RC=005 + decline receipt | — | Generic card declined |
| `error` | RC=`--code` + decline receipt | — | Specific error scenario |
| `timeout` | Hangs forever | — | Test Phase-1 client timeout |
| `no_confirm` | RC=000 | Hangs forever | Test 40-second Phase-2 timeout |

---

## Test scenarios

Pass a scenario name to `--code` or select it by number in interactive mode:

| # | Name | Code | Description |
|---|------|------|-------------|
| 1 | `insufficient_funds` | 116 | Not enough balance |
| 2 | `wrong_pin` | 117 | Incorrect PIN entered |
| 3 | `pin_attempts_exceeded` | 106 | Too many wrong PIN attempts |
| 4 | `expired_card` | 101 | Card past expiry date |
| 5 | `suspected_fraud` | 102 | Fraud flag raised by issuer |
| 6 | `do_not_honour` | 100 | Generic issuer refusal |
| 7 | `card_not_active` | 118 | Card not yet activated |
| 8 | `transaction_not_allowed` | 120 | Transaction type blocked at terminal |
| 9 | `exceeds_limits` | 121 | Daily/weekly limit exceeded |
| 10 | `use_chip` | 265 | Contactless refused — insert card |
| 11 | `pin_failed` | 281 | PIN verification failed |
| 12 | `system_down` | 909 | Terminal/server unreachable |
| 13 | `issuer_unavailable` | 912 | Card issuer not responding |
| 14 | `server_error` | 995 | NAPS server processing error |

Examples:

```bash
python3 naps_emulator.py --mode error --code insufficient_funds
python3 naps_emulator.py --mode error --code 116   # same thing
```

---

## Interactive mode

In `--mode interactive` the emulator prompts for each Phase-1 request:

```
  ┌── Payment from 127.0.0.1:52341 ──────────────────────
  │  Amount : 150.00 MAD
  │  Card   : 516794******3315
  ├─────────────────────────────────────────────────────
  │  [a] Approve
  │  [ 1] Decline — Insufficient funds (116)
  │  [ 2] Decline — Wrong PIN (117)
  │  [ 3] Decline — PIN attempts exceeded (106)
  │  ...
  └─────────────────────────────────────────────────────
  Choice →
```

- Type `a` (or Enter) to approve
- Type a number (e.g. `1`) to decline with that scenario
- Type a raw code (e.g. `116`) or scenario name (e.g. `insufficient_funds`) directly

If no answer arrives within 120 seconds the payment is auto-approved.
Concurrent connections are serialised — one prompt at a time.

---

## Requirements

- Python 3.10+
- No external packages
