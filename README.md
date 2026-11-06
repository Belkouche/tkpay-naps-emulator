# NAPS Pay Terminal Emulator

A fake NAPS Pay payment terminal that runs on your machine. It speaks the same
**M2M TLV protocol over TCP** as a real terminal (NapsPay v5.4.4, TLV Protocol
Specification v2.0), so you can build and test a cash-register (ECR) integration
or an SDK without hardware, without a card and without moving money.

- One file: `naps_emulator.py`. Python 3.10+, standard library only, no install.
- Listens on TCP port **4444**, like the real terminal.
- Approves everything by default; `--mode` switches it to declines, errors,
  timeouts and void failures so you can test every branch of your code.

---

## Contents

1. [Quickstart](#1-quickstart)
2. [The TLV frame format](#2-the-tlv-frame-format)
3. [Payment flow (001 → 101, 002 → 102)](#3-payment-flow)
4. [Void flow (003 → 103, 004 → 104)](#4-void-flow)
5. [Other messages](#5-other-messages)
6. [Modes: what each one simulates](#6-modes)
7. [All flags](#7-all-flags)
8. [Protocol versions and terminators](#8-protocol-versions-and-terminators)
9. [Decline scenarios and response codes](#9-decline-scenarios-and-response-codes)
10. [Interactive mode](#10-interactive-mode)
11. [How the SDK repos use the emulator](#11-how-the-sdk-repos-use-the-emulator)
12. [Known differences from a real terminal](#12-known-differences-from-a-real-terminal)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Quickstart

Start the emulator (approves every payment):

```bash
python3 naps_emulator.py
```

In another terminal, send a payment and its confirmation with the example client
[`examples/quickstart.py`](examples/quickstart.py):

```python
def tlv(tag, value): return f"{tag}{len(value):03d}{value}"          # TAG(3) + LENGTH(3) + VALUE
...
with socket.create_connection(("127.0.0.1", port)) as s:              # one connection for both phases
    r1 = exchange(s, tlv("001", "001") + tlv("002", "15000") + tlv("003", "0100001") + tlv("004", "000001") + tlv("012", "504"))
    print(f"TM {r1['001']}  CR={r1['013']}  STAN={r1['008']}  card={r1['007']}")
    r2 = exchange(s, tlv("001", "002") + tlv("003", "0100001") + tlv("004", "000001") + tlv("008", r1["008"]))
    print(f"TM {r2['001']}  CR={r2['013']}  STAN={r2['008']}  {'approved' if r2['013'] == '000' else 'refused'}")
```

```bash
$ python3 examples/quickstart.py          # optional argument: port (default 4444)
TM 101  CR=000  STAN=437828  card=516794******3315
TM 102  CR=000  STAN=437828  approved
```

That is a complete payment: a 150.00 MAD request (`15000` centimes), the
terminal's approval with a STAN (transaction number), then the confirmation that
commits it. The rest of this guide explains each piece.

---

## 2. The TLV frame format

Every message is a flat string of ASCII fields. Each field is
**TAG (3 digits) + LENGTH (3 digits) + VALUE (LENGTH characters)**. No separators,
no header. Fields can appear in any order.

A network-test request, annotated:

```
001003009 003007 0100001 004006 000001
│  │  │   │  │   │       │  │   └─ value "000001"
│  │  │   │  │   │       │  └───── length 6
│  │  │   │  │   │       └──────── tag 004 = NS, sequence number
│  │  │   │  │   └──────────────── value "0100001" (register 01 + cashier 00001)
│  │  │   │  └──────────────────── length 7
│  │  │   └─────────────────────── tag 003 = NCAI, register/cashier id
│  │  └─────────────────────────── value "009" (network test)
│  └────────────────────────────── length 3
└───────────────────────────────── tag 001 = TM, message type
```

On the wire it is `0010030090030070100001004006000001` (spaces above are for reading only).

**Framing:**
- **Requests** (ECR → terminal) carry **no terminator**. The emulator detects the
  end of a request by walking the TLV lengths (with a 200 ms silence fallback).
- **Replies** (terminal → ECR) end with one terminator character: **`!`** (protocol
  v2, default) or **`?`** (v1). **Exception: TM 103 and TM 104 replies have no
  terminator** (see [§4](#4-void-flow)). A robust client stops at the terminator,
  or after ~1 s of silence when there is none.

**Tags you will meet:**

| Tag | Name | Meaning | Example |
|-----|------|---------|---------|
| 001 | TM   | Message type | `001` request, `101` its reply |
| 002 | MT   | Amount in centimes | `15000` = 150.00 MAD |
| 003 | NCAI | Register (2) + cashier (5) | `0100001` |
| 004 | NS   | Sequence number of this request | `000001` |
| 005 | NSA  | Cancellation sequence number (accepted on TM 003) | `000001` |
| 007 | NCAR | Card number, masked: first 6 + last 4 | `516794******3315` |
| 008 | STAN | Terminal transaction number | `437828` |
| 010 | DP   | Printable receipt (sub-TLV, see below) | |
| 012 | DE   | Currency | `504` = MAD |
| 013 | CR   | Response code | `000` = approved |
| 014 / 015 | DA / HE | Date `DDMMYYYY` / time `HHMMSS` | |
| 017 | DAEX | Card expiry `YYMM` | `3010` |
| 018 / 019 | DATR / HETR | Original transaction date / time | |
| 020 | TIDE | Referencing parameter set | `01` merchant, `02` recharge |
| 040 | EM   | Entry mode | `CC` |

**Receipts (DP, tag 010)** are a list of lines separated by `*`. Each line is itself TLV:

```
030 002 {line:02d}    line number
031 001 {S|G}         style: S normal, G bold
032 001 {C|G|D}       alignment: C centre, G left, D right
033 {len} {text}      text
```

The first line is `TKpay`, bold and centred. Receipt text is ASCII (accents are
replaced) so that TLV lengths count bytes and characters alike.

---

## 3. Payment flow

A payment always takes **two round trips on the same TCP connection**:

```
 ECR / SDK                                   Emulator
    │  TM 001  payment request               │
    │  MT=15000 NCAI NS DE ────────────────▶ │   (real terminal: customer taps card)
    │                                        │
    │ ◀──────────────── TM 101  CR=000       │   STAN, NCAR, DAEX, EM,
    │                    + merchant receipt  │   merchant receipt (DP)
    │                                        │
    │  TM 002  confirmation                  │
    │  NS = same as TM 001, STAN,            │
    │  echo MT/DE/NCAR/DAEX ───────────────▶ │
    │                                        │
    │ ◀──────────────── TM 102  CR=000       │   same STAN,
    │                    + customer receipt  │   customer receipt (DP)
```

- **TM 001 → 101.** CR=`000` means approved; anything else is a decline (see
  [§9](#9-decline-scenarios-and-response-codes)) and the flow stops there.
- **TM 002 → 102** commits the payment. Its NS must equal the TM 001 NS. A real
  terminal waits **40 seconds** for it; SDKs use a 40 s confirmation timeout.
  TM 102 can itself be refused (CR≠000, e.g. 302 sale not found, 482 already
  cancelled): treat that as a failed payment.
- SDK v1.0.8+ echoes MT, DE, NCAR and DAEX from TM 101 into TM 002 (spec §4.2).
  With `--strict-confirm` the emulator refuses TM 002 with CR=`307` when MT or NCAR
  disagree with the approved payment.

---

## 4. Void flow

A void (cancellation) is also **two round trips on one connection**:

```
 ECR / SDK                                   Emulator
    │  TM 003  cancellation request          │
    │  STAN of the sale, MT, NS (or NSA) ──▶ │
    │                                        │
    │ ◀────────────── TM 103  CR=000         │   "transaction found": NCAR, DAEX,
    │                 (NO terminator)        │   DATR/HETR, merchant receipt
    │                                        │
    │  TM 004  cancellation confirmation     │
    │  NS = TM 003 sequence, MT required ──▶ │
    │                                        │
    │ ◀────────────── TM 104  CR=000         │   reversal executed: void STAN,
    │                 (NO terminator)        │   customer receipt
```

Rules the emulator enforces, matching the spec and the live v5.4.4 terminal:

1. **No terminator on TM 103 and TM 104.** Your client must end these reads on
   silence (~1 s), not on `!`/`?`. Every other reply keeps its terminator.
2. **NS rule.** TM 004 must carry **NS (tag 004)** equal to the TM 003 sequence.
   The TM 003 sequence is its NS, or its NSA (tag 005) when NS is absent; TM 103
   echoes it back as NS. A missing or different NS, or a TM 004 with no successful
   TM 003 before it on the connection, gets **TM 104 CR=`307`**.
3. **Amount required.** TM 004 without MT (tag 002) gets **TM 104 CR=`909`**, as the
   live terminal does.
4. **Reading TM 104.** CR=`000` and CR=`480` both mean the void completed (only
   `000` carries a receipt). CR=`995`, or no TM 104 at all, means the outcome is
   unknown: check with a duplicate-receipt request (TM 008). TM 103 CR=`000` only
   means "transaction found"; nothing is reversed until TM 104.

---

## 5. Other messages

Each is one request and one reply (with terminator):

| Request | Reply | Purpose | Emulator reply |
|---------|-------|---------|----------------|
| TM 008 | TM 108 | Duplicate receipt | CR=000 + a receipt for the last STAN on the connection (amount 0) |
| TM 009 | TM 109 | Network test | CR=000 |
| TM 010 | TM 110 | Totals / settlement | CR=000 + a totals receipt (zeros) |
| TM 012 | TM 112 | Reset PIN pad | CR=000 |
| TM 013 | TM 113 | Referencing (parameter load) | CR=000, echoes TIDE (020), parameter receipt |

Any other TM is logged and **gets no reply** (see [§12](#12-known-differences-from-a-real-terminal)).

---

## 6. Modes

The emulator runs one mode per process (`--mode`). To test several, start several
emulators on different ports.

| `--mode` | What it simulates | SDK path it tests |
|----------|-------------------|-------------------|
| `approve` (default) | Every payment and void succeeds | Happy path: payment with both receipts, void completed |
| `decline` | TM 101 CR=`100` (do not honour) + decline receipt | Declined payment, no confirmation sent |
| `error` | TM 101 CR=`--code` + decline receipt | Each specific decline code (§9) |
| `interactive` | You choose approve or a decline for each payment at the console | Manual demos, ad-hoc tests (§10) |
| `timeout` | TM 001 never answered (connection held 300 s) | Payment request timeout |
| `no_confirm` | TM 101 approved, TM 002 never answered | 40 s confirmation timeout |
| `confirm_error` | TM 101 approved, TM 102 CR=`--code`, no receipt | Refused confirmation reported as failure (e.g. `--code 482`) |
| `void_refuse` | TM 103 CR=`--code` (e.g. 302 not found, 482 already cancelled) | Void refused at phase 1; no TM 004 should follow |
| `void_480` | TM 104 CR=`480`, no receipt | Void completed with the "reversal accepted" code |
| `void_995` | TM 104 CR=`995`, no receipt | Void outcome unknown → pending, not success |
| `void_no_confirm` | TM 004 never answered | Missing TM 104 → pending, not success |

Payments are approved normally in the `void_*` and `confirm_error` modes, and voids
succeed normally in the payment modes.

---

## 7. All flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--host ADDR` | `0.0.0.0` | Bind address |
| `--port N` | `4444` | TCP port |
| `--mode MODE` | `approve` | Behaviour, see [§6](#6-modes) |
| `--code CODE` | `909` | Response code for `error`, `confirm_error` and `void_refuse`: a number (`116`) or a scenario name (`insufficient_funds`) |
| `--strict-confirm` | off | Refuse TM 002 with CR=`307` when its echoed MT/NCAR differ from the approved payment |
| `--protocol v1\|v2` | `v2` | Reply terminator: `!` (v2) or `?` (v1), see [§8](#8-protocol-versions-and-terminators) |
| `--debug` | off | Log every TLV field received |

Examples:

```bash
python3 naps_emulator.py --port 4445 --debug
python3 naps_emulator.py --mode error --code insufficient_funds
python3 naps_emulator.py --mode void_refuse --code 482
python3 naps_emulator.py --mode approve --strict-confirm --protocol v1
```

---

## 8. Protocol versions and terminators

| `--protocol` | Terminator | Terminals |
|--------------|-----------|-----------|
| `v2` (default) | `!` | NapsPay v5.4.4 and later |
| `v1` | `?` | Older firmware |

The terminator is appended to every reply **except TM 103 and TM 104**, which never
have one. Requests never carry a terminator. Clients should accept either
character so they work with both generations.

---

## 9. Decline scenarios and response codes

Use a name or a number with `--code`, or pick by number in interactive mode:

| # | Name | Code | Meaning |
|---|------|------|---------|
| 1 | `insufficient_funds` | 116 | Not enough balance |
| 2 | `wrong_pin` | 117 | Incorrect PIN |
| 3 | `pin_attempts_exceeded` | 106 | Too many wrong PINs |
| 4 | `expired_card` | 101 | Card expired |
| 5 | `suspected_fraud` | 102 | Fraud suspected |
| 6 | `do_not_honour` | 100 | Generic issuer refusal |
| 7 | `card_not_active` | 118 | Card not registered |
| 8 | `transaction_not_allowed` | 120 | Not permitted at terminal |
| 9 | `exceeds_limits` | 121 | Withdrawal limit exceeded |
| 10 | `invalid_cvv` | 183 | Invalid CVV |
| 11 | `system_down` | 909 | System failure |
| 12 | `issuer_unavailable` | 912 | Issuer unavailable |
| 13 | `server_error` | 995 | Server processing error |
| | `cancelled` | 280 | Alternative amount cancelled |
| | `record_not_found` | 302 | Record not found (useful with `void_refuse`, `confirm_error`) |
| | `already_cancelled` | 482 | Transaction already cancelled |
| | `approved` | 000 | Approved |

Any other 3-digit code from the spec §6 table works too (e.g. `--code 307`).
Codes that mean success: `000`, `001`, `003`, `007` for payments; `000` and `480`
for TM 104.

---

## 10. Interactive mode

`--mode interactive` asks you what to do with each payment:

```
  ┌── Payment from 127.0.0.1:52341 ──────────────────────
  │  Amount : 150.00 MAD
  │  Card   : 516794******3315
  ├─────────────────────────────────────────────────────
  │  [a] Approve
  │  [ 1] Decline — Insufficient funds (116)
  │  [ 2] Decline — Incorrect PIN (117)
  │  ...
  └─────────────────────────────────────────────────────
  Choice →
```

Type `a` or Enter to approve, a menu number to decline with that scenario, or a raw
code (`116`) or name (`insufficient_funds`). With no answer after 120 s the payment
is approved. Concurrent payments are prompted one at a time.

---

## 11. How the SDK repos use the emulator

Every TKpay NAPS SDK has integration tests that talk to this emulator over TCP.
Because the emulator runs one mode per process, the tests read the mode from the
environment and skip the tests that need another mode. The common convention:

| Variable | Meaning |
|----------|---------|
| `NAPS_EMULATOR_PORT` | Port of the running emulator (tests skip when unset, in most repos) |
| `NAPS_EMULATOR_MODE` | The `--mode` it was started with (default `approve`) |
| `NAPS_EMULATOR_CODE` | The `--code` it was started with, for `void_refuse` / `error` |
| `NAPS_EMULATOR` | Path to `naps_emulator.py`, for suites that start it themselves |

Example with the canonical C# SDK:

```bash
python3 naps_emulator.py --port 4451 --mode void_480 &
cd ../tkpay-sdk-pos-naps-csharp
NAPS_EMULATOR_MODE=void_480 NAPS_EMULATOR_PORT=4451 dotnet test --filter Category=Integration
```

| Repo | Test file | How it runs |
|------|-----------|-------------|
| `tkpay-sdk-pos-naps-csharp` | `tests/TKPay.Naps.Tests/EmulatorVoidIntegrationTests.cs` | Start the emulator yourself, set `NAPS_EMULATOR_MODE` / `NAPS_EMULATOR_PORT`; its `void_refuse` test expects `--code 302` |
| `aslan-naps-sdk` | `tests/run-emulator-void-tests.sh` | Script starts one emulator per void mode and runs the tests |
| `tkpay-sdk-pos-naps-node` | `test/client.integration.test.ts` | Spawns the emulator from `NAPS_EMULATOR` (port `NAPS_EMULATOR_PORT`, default 4471) |
| `tkpay-sdk-pos-naps-flutter` | `test/emulator_e2e_test.dart` | Spawns the emulator from `NAPS_EMULATOR` |
| `tkpay-sdk-pos-naps-android` | `naps-sdk/src/test/.../EmulatorIntegrationTest.kt` | `NAPS_EMULATOR_PORT=… NAPS_EMULATOR_MODE=… ./gradlew :naps-sdk:testDebugUnitTest` |
| `tkpay-sdk-pos-naps-ios` | `Tests/TKPayNapsTests/EmulatorIntegrationTests.swift` | `NAPS_EMULATOR_PORT=… swift test --filter EmulatorIntegrationTests` |
| `tkpay-sdk-pos-naps-rn` | `test-integration.test.js` | `NAPS_PORT=… NAPS_EMULATOR_MODE=… npm run test:integration` |

Use a different port per test run if several suites run at once.

---

## 12. Known differences from a real terminal

The emulator follows the protocol, not a bank. Things that differ on purpose or by
simplification:

- **TM 103 carries a STAN and a merchant receipt.** Spec §4.3 lists neither; the
  emulator sends both (NapsPay firmware sends a receipt, and some SDKs read the STAN
  from TM 103). Do not rely on them.
- **Declines carry a STAN and a decline receipt.** A real terminal sends no STAN
  and no receipt with a refused TM 101.
- **No transaction store.** Any STAN can be voided and confirmed; amounts and card
  are not checked against past payments (except with `--strict-confirm`). TM 008
  reprints a receipt with amount 0.
- **Fixed card data:** `516794******3315`, expiry `3010`, entry mode `CC`. No
  authorization number (NA, 009) or cardholder name (NPRT, 016) fields.
- **TM 002 is accepted on a new connection.** A real terminal requires the same one.
- **No 40 s confirmation timer on the emulator side**; only the client enforces it.
- **Unsupported messages get no reply.** Any TM other than 001, 002, 003, 004, 008,
  009, 010, 012, 013 is logged as `unhandled` and ignored, so the client times out.
  TM 012 (reset PIN pad) is answered even though spec v2.0 does not list it.
- **TCP and ASCII TLV only.** No USB serial transport and no binary (Sunmi) TLV.

---

## 13. Troubleshooting

| Symptom | Likely cause and fix |
|---------|----------------------|
| `Cannot bind to 0.0.0.0:4444 — Address already in use` | Another emulator or a real terminal bridge is on that port. Use `--port 4460`, or find it with `lsof -iTCP:4444 -sTCP:LISTEN`. |
| Client hangs after sending TM 003 or TM 004 | Your reader waits for `!`/`?`. TM 103/104 have no terminator: end the read after ~1 s of silence. |
| TM 104 CR=`307` | TM 004 has no NS (tag 004), a different NS from TM 003, or no successful TM 003 before it on the same connection. Many SDKs used to send NSA (005) in TM 004; send NS. |
| TM 104 CR=`909` | TM 004 lacks the amount (tag 002). Echo MT from TM 103. |
| TM 102 CR=`307` | `--strict-confirm` is on and TM 002's MT or NCAR differs from the payment. |
| Payment never answered | Emulator is in `timeout` mode, or `interactive` is waiting for your choice. |
| Confirmation never answered | `no_confirm` mode. |
| Reply has odd lengths or garbled text | Send ASCII only; non-ASCII characters in a request shift the byte lengths. |
| Client reads only part of a reply | Read until the terminator (or silence), not a single `recv`. Receipts make replies ~800 bytes. |
| `SyntaxError` / `TypeError` on start | Python older than 3.10. Run with `python3 --version` ≥ 3.10. |

Run with `--debug` to see every field the emulator receives, and read its log: each
request and reply is logged with TM, CR and STAN.
