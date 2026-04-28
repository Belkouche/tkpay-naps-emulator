# NAPS Pay M2M TLV Terminal Emulator

Python TCP server that emulates a NAPS Pay terminal for local development and testing.
Speaks the exact two-phase M2M TLV protocol on **port 4444**, 





No dependencies — stdlib Python 3.10+ only.

---

## Usage

```bash
# Approve all payments (default)
python3 naps_emulator.py

# Decline all payments (RC=005)
python3 naps_emulator.py --mode decline

# Return a specific response code
python3 naps_emulator.py --mode error --code 909

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
| `approve` | RC=000 + full receipt | RC=000 + receipt | Normal flow |
| `decline` | RC=005 + REFUSE | — | Card declined |
| `error` | RC=`--code` | — | Specific error codes (909, 302, 482…) |
| `timeout` | Hangs forever | — | Test Phase-1 client timeout |
| `no_confirm` | RC=000 | Hangs forever | Test 40-second Phase-2 timeout |

---

## Requirements

- Python 3.10+
- No external packages
