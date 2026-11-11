#!/usr/bin/env python3
"""
NAPS Pay M2M TLV Terminal Emulator
===================================
Listens on TCP port 4444 and speaks the two-phase NAPS Pay M2M protocol.

Usage:
    python3 naps_emulator.py                          # approve all, port 4444
    python3 naps_emulator.py --port 4445
    python3 naps_emulator.py --mode decline
    python3 naps_emulator.py --mode timeout           # Phase-1 hangs (tests client timeout)
    python3 naps_emulator.py --mode no_confirm        # Phase-2 hangs (tests 40-s confirmation timeout)
    python3 naps_emulator.py --mode error --code 909
    python3 naps_emulator.py --mode error --code insufficient_funds
    python3 naps_emulator.py --mode confirm_error --code already_cancelled
    python3 naps_emulator.py --mode interactive      # prompt [a]pprove or pick a decline scenario
    python3 naps_emulator.py --debug                  # print every TLV field
    python3 naps_emulator.py --profile legacy         # pre-11/2026 behaviour, for older SDK versions

Supported modes:
    approve      All payments approved (default)
    decline      Phase-1 returns RC=100 (do not honour)
    timeout      Phase-1 never responds
    no_confirm   Phase-2 never responds
    error        Phase-1 returns the code supplied with --code (name or number)
    confirm_error Phase-1 approves, Phase-2 (TM 102) refuses with --code (e.g. 302, 482)
    void_refuse  TM 103 refuses the void with --code (e.g. 302 not found, 482 already cancelled)
    void_480     TM 104 returns CR=480 (reversal accepted — a success, no receipt)
    void_995     TM 104 returns CR=995 (server processing error)
    void_no_confirm  TM 004 gets no reply
    interactive  Prompt operator to approve or choose a decline scenario per payment

Named scenarios for --code:
    approved, insufficient_funds, expired_card, wrong_pin, pin_attempts_exceeded,
    suspected_fraud, do_not_honour, card_not_active, transaction_not_allowed,
    exceeds_limits, cancelled, already_cancelled, use_chip, pin_failed,
    system_down, issuer_unavailable, server_error, record_not_found

Profiles (--profile):
    sunmi (default)  Behaves like the live Sunmi P2 (NapsPay v5.4.4) seen on 11/11/2026:
                     - TM 103 carries no receipt; TM 104 carries the only void receipt,
                       the merchant copy ("Copie Commerçant"), and the void's own STAN
                     - TM 009 is not implemented: answered TM 101 / CR 902
                     - a request ending with '!' or '?' gets no reply (the real app stalled)
                     - TM 003 without NS (004) is refused with CR 307 (spec §4.3; the
                       Ingenico Lane3600 answers the same)
                     - receipt text keeps its accents; lengths count characters
    legacy           The emulator's earlier behaviour: TM 103 merchant receipt, TM 104
                     customer receipt, TM 009 answered 109, NSA accepted as the TM 003
                     sequence, accents stripped, terminated requests answered.

Protocol notes (NapsPay v5.4.4 / Aslan.Naps SDK v1.0.8):
    Requests:  raw TLV, no terminator character
    Responses: raw TLV + end-of-message terminator, selected by --protocol:
                 v2 (default, v5.4.4+): '!'      — SDK reads until '!'
                 v1 (pre-v5.4.4):       '?'      — legacy terminals
               e.g.  python3 naps_emulator.py --protocol v1
    Confirmation (TM 002): the SDK echoes MT/DE/NCAR/DAEX from the payment
               response per spec §4.2 — the emulator verifies these against the
               approved payment and can reject a mismatch (see --strict-confirm).
    Void:      two-phase on the same connection: TM 003 → TM 103 (no receipt; legacy:
               merchant receipt), then TM 004 → TM 104 (void STAN; void receipt = merchant
               copy, legacy: customer copy). TM 103/104 replies carry NO terminator.
               TM 003 needs NS (004) → else CR=307 (legacy: NSA accepted). TM 004 needs
               NS = the TM 003 NS → else CR=307, and MT (002) → else CR=909.
"""

import argparse
import logging
import queue
import random
import socket
import sys
import threading
import time as _time
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("naps-emulator")

# ── Constants ─────────────────────────────────────────────────────────────────

PORT         = 4444
READ_BUF     = 4096
CHARSET      = "utf-8"
LINE_SEP     = "--------------------------------"
FS1          = "*"
RESPONSE_END = "!"   # NapsPay v5.4.4 terminates TCP responses with '!' (was '?' pre-v5.4.4)

# Response terminator per NAPS protocol generation. v1 (pre-v5.4.4) used '?';
# v2 (v5.4.4+, matches Aslan.Naps SDK v1.0.8) uses '!'. Requests never carry one.
PROTOCOL_TERMINATORS = {"v1": "?", "v2": "!"}

# Terminal behaviour profile, set from --profile: "sunmi" (live Sunmi P2, default) or "legacy".
PROFILE = "sunmi"

# ── ISO 8583 / NAPS response code scenarios ───────────────────────────────────

# Codes and labels track the TLV Protocol Specification v2.0 §6 response-code
# table (mirrored by LtvProtocol.ResponseCodes in Aslan.Naps SDK v1.0.8).
SCENARIOS: dict[str, tuple[str, str]] = {
    # name                  : (code, label)
    "approved"              : ("000", "Approved"),
    "do_not_honour"         : ("100", "Do not honour"),
    "expired_card"          : ("101", "Card expired"),
    "suspected_fraud"       : ("102", "Fraud suspected"),
    "pin_attempts_exceeded" : ("106", "Allowed PIN tries exceeded"),
    "wrong_pin"             : ("117", "Incorrect PIN"),
    "insufficient_funds"    : ("116", "Insufficient funds"),
    "card_not_active"       : ("118", "Card not registered"),
    "transaction_not_allowed": ("120", "Transaction not permitted at terminal"),
    "exceeds_limits"        : ("121", "Withdrawal limit exceeded"),
    "invalid_cvv"           : ("183", "Invalid CVV"),
    "cancelled"             : ("280", "Alternative amount cancelled"),
    "record_not_found"      : ("302", "Record not found on file"),
    "already_cancelled"     : ("482", "Transaction already cancelled"),
    "system_down"           : ("909", "System failure"),
    "issuer_unavailable"    : ("912", "Card issuer unavailable"),
    "server_error"          : ("995", "Server processing error"),
}

# Ordered list for the interactive decline menu (most common first)
DECLINE_MENU: list[str] = [
    "insufficient_funds",
    "wrong_pin",
    "pin_attempts_exceeded",
    "expired_card",
    "suspected_fraud",
    "do_not_honour",
    "card_not_active",
    "transaction_not_allowed",
    "exceeds_limits",
    "invalid_cvv",
    "system_down",
    "issuer_unavailable",
    "server_error",
]


# TM 104 response code per void-confirmation mode (480 = reversal accepted, a success per §4.4).
VOID_CONFIRM_CODES: dict[str, str] = {"void_480": "480", "void_995": "995"}


def resolve_code(code: str) -> str:
    """Resolve a scenario name or raw code string to a 3-digit RC."""
    if code in SCENARIOS:
        return SCENARIOS[code][0]
    return code

# ── TLV helpers ───────────────────────────────────────────────────────────────

def _ascii(value: str) -> str:
    """
    Legacy profile: replace non-ASCII chars to keep TLV byte-length == char-length.
    The Sunmi P2 sends accented text ("Commerçant", "N°") with lengths counted in characters.
    """
    if PROFILE != "legacy":
        return value
    return (value
            .replace("ç", "c").replace("Ç", "C")
            .replace("è", "e").replace("é", "e").replace("ê", "e").replace("ë", "e")
            .replace("à", "a").replace("â", "a")
            .replace("î", "i").replace("ï", "i")
            .replace("ô", "o").replace("ù", "u").replace("û", "u"))


def f(tag: str, value: str) -> str:
    """Encode one TLV field: TAG(3) + LENGTH(3) + VALUE."""
    value = _ascii(value)
    n = min(len(value), 999)
    return f"{tag}{n:03d}{value[:n]}"


def parse(raw: str) -> dict[str, str]:
    """
    Parse TLV string → {tag: value}.
    Requests arrive with no terminator; strip any stray terminator just in case.
    """
    raw = raw.rstrip("!?")
    out: dict[str, str] = {}
    i = 0
    while i + 6 <= len(raw):
        tag = raw[i:i+3]
        try:
            n = int(raw[i+3:i+6])
        except ValueError:
            break
        if i + 6 + n > len(raw):
            break
        out[tag] = raw[i+6:i+6+n]
        i += 6 + n
    return out


def _is_complete(raw: str) -> bool:
    """
    Return True when `raw` contains a fully-formed TLV message.
    Walks the TLV chain and checks that all length fields are satisfied.
    Requests carry no terminator, so we rely on the TLV structure itself.
    """
    i = 0
    while i + 6 <= len(raw):
        try:
            n = int(raw[i+3:i+6])
        except ValueError:
            return False
        i += 6 + n
    return i == len(raw) and i > 0


def dump(fields: dict[str, str]) -> str:
    NAMES = {
        "001":"TM",  "002":"MT",   "003":"NCAI","004":"NS",
        "005":"NSA", "007":"NCAR", "008":"STAN","009":"NA",
        "010":"DP",  "011":"CB",   "012":"DE",  "013":"CR",
        "014":"DA",  "015":"HE",   "016":"NPRT","017":"DAEX",
        "018":"DATR","019":"HETR", "020":"TIDE","025":"RESE",
        "040":"EM",
    }
    return "\n".join(
        f"    {tag} ({NAMES.get(tag, tag):6s}) = {val!r}"
        for tag, val in fields.items()
    )


TM_NAMES = {
    "001": "Payment",
    "002": "Confirmation",
    "003": "Cancellation",
    "004": "Cancellation confirmation",
    "008": "Duplicate receipt",
    "009": "Network test",
    "010": "Totals",
    "012": "Reset PinPAD",
    "013": "Referencing",
}

# ── Receipt builder ───────────────────────────────────────────────────────────

def _receipt_line(line_num: int, content: str, align: str, style: str) -> str:
    content = _ascii(content)
    return (
        "030002" + f"{line_num:02d}" +
        "031001" + style +
        "032001" + align +
        "033" + f"{len(content):03d}" + content
    )


def _join_lines(lines: list) -> str:
    """Join receipt lines with the '*' entry separator the SDK expects."""
    return FS1.join(lines)


def build_decline_receipt(rc: str, stan: str,
                          merchant_name: str = "TKPAY DEMO",
                          merchant_city: str = "CASABLANCA",
                          term_id: str = "00000001") -> str:
    now      = datetime.now()
    date_str = now.strftime("%d/%m/%Y %H:%M:%S")
    lines = [
        _receipt_line(0,  "TKpay",               "C", "G"),
        _receipt_line(2,  LINE_SEP,               "G", "S"),
        _receipt_line(3,  date_str,               "G", "S"),
        _receipt_line(4,  merchant_name,          "G", "S"),
        _receipt_line(6,  merchant_city,          "G", "S"),
        _receipt_line(7,  LINE_SEP,               "G", "S"),
        _receipt_line(15, f"Terminal: {term_id}", "G", "S"),
        _receipt_line(19, f"STAN: {stan}",        "G", "S"),
        _receipt_line(20, LINE_SEP,               "G", "S"),
        _receipt_line(21, "TRANSACTION REFUSEE",  "C", "G"),
        _receipt_line(22, f"Code: {rc}",          "C", "S"),
        _receipt_line(23, LINE_SEP,               "G", "S"),
    ]
    return _join_lines(lines)


def build_receipt(amount_centimes: int, stan: str, masked_card: str,
                  auth_num: str, ncai: str,
                  merchant_name: str = "TKPAY DEMO",
                  merchant_city: str = "CASABLANCA",
                  term_id: str = "00000001",
                  is_customer_copy: bool = False,
                  txn_label: str = "DEBIT") -> str:
    now         = datetime.now()
    date_str    = now.strftime("%d/%m/%Y %H:%M:%S")
    amount_mad  = f"{amount_centimes / 100:.2f}"
    copy_label  = "Copie Client" if is_customer_copy else "Copie Commerçant"

    lines = [
        _receipt_line(0,  "TKpay",                       "C", "G"),
        _receipt_line(2,  LINE_SEP,                      "G", "S"),
        _receipt_line(3,  date_str,                      "G", "S"),
        _receipt_line(4,  merchant_name,                 "G", "S"),
        _receipt_line(6,  merchant_city,                 "G", "S"),
        _receipt_line(7,  LINE_SEP,                      "G", "S"),
        _receipt_line(9,  "VISA",                        "G", "S"),
        _receipt_line(10, masked_card,                   "G", "S"),
        _receipt_line(15, f"Terminal: {term_id}",        "G", "S"),
        _receipt_line(17, f"Transaction: {stan}",        "G", "S"),
        _receipt_line(18, f"Autorisation: {auth_num}",   "G", "S"),
        _receipt_line(19, f"STAN: {stan}",               "G", "S"),
        _receipt_line(20, LINE_SEP,                      "G", "S"),
        _receipt_line(21, f"MONTANT: {amount_mad} MAD",  "G", "S"),
        _receipt_line(22, LINE_SEP,                      "G", "S"),
        _receipt_line(23, txn_label,                     "G", "S"),
        _receipt_line(24, copy_label,                    "G", "S"),
    ]
    return _join_lines(lines)


# ── Response builders ─────────────────────────────────────────────────────────

def _common_fields(req: dict, response_tm: str, rc: str) -> str:
    now  = datetime.now()
    date = now.strftime("%d%m%Y")
    time = now.strftime("%H%M%S")
    return (
        f(  "001", response_tm) +
        f(  "003", req.get("003", "0100001")) +
        f(  "004", req.get("004", "000001")) +
        f(  "013", rc) +
        f(  "002", req.get("002", "0")) +
        f(  "012", req.get("012", "504")) +
        f(  "014", date) +
        f(  "015", time)
    )


def _card_details(masked_card: str, entry_mode: str = "CC") -> str:
    return (
        f("007", masked_card) +
        f("017", "3010") +
        f("040", entry_mode)
    )


def phase1_response(req: dict, mode: str, error_code: str,
                    peer: str = "") -> str:
    if mode == "decline":
        stan = generate_stan()
        dp   = build_decline_receipt("100", stan)
        return _common_fields(req, "101", "100") + f("008", stan) + f("025", "REFUSE") + f("010", dp)

    if mode == "error":
        rc   = resolve_code(error_code)
        stan = generate_stan()
        dp   = build_decline_receipt(rc, stan)
        return _common_fields(req, "101", rc) + f("008", stan) + f("010", dp)

    if mode == "interactive":
        amount           = int(req.get("002", "0") or "0")
        approved, rc     = ask_operator(peer, amount)
        if not approved:
            stan = generate_stan()
            dp   = build_decline_receipt(rc, stan)
            return _common_fields(req, "101", rc) + f("008", stan) + f("025", "REFUSE") + f("010", dp)

    stan    = generate_stan()
    auth    = generate_approval()
    _AUTH_BY_STAN[stan] = auth           # TM 102 prints the same authorisation number
    ncai    = req.get("003", "0100001")
    amount  = int(req.get("002", "0") or "0")
    dp      = build_receipt(amount, stan, MASKED_CARD, auth, ncai, is_customer_copy=False)

    return (
        _common_fields(req, "101", "000") +
        _card_details(MASKED_CARD) +
        f("008", stan) +
        f("010", dp)
    )


def confirmation_coherence_error(req: dict, phase1_amount: int) -> str | None:
    """
    Spec §4.2: the confirmation (TM 002) echoes MT/DE/NCAR/DAEX from the approved
    payment so the terminal can reinforce coherence checks. Return a mismatch
    description if the echoed fields disagree with the approved payment, else None.
    Only MT and NCAR are checked here (DE is fixed to MAD; DAEX is informational).

    The MT check is skipped when no phase-1 amount was recorded on this connection
    (phase1_amount == 0) — there is nothing to compare against, so a standalone
    confirmation is not treated as a mismatch.
    """
    conf_mt = req.get("002")
    if conf_mt is not None and phase1_amount > 0 and int(conf_mt or "0") != phase1_amount:
        return f"MT mismatch: confirmation={conf_mt} vs payment={phase1_amount}"
    conf_ncar = req.get("007")
    if conf_ncar is not None and conf_ncar != MASKED_CARD:
        return f"NCAR mismatch: confirmation={conf_ncar} vs payment={MASKED_CARD}"
    return None


def phase2_response(req: dict, phase1_stan: str, phase1_amount: int) -> str:
    stan   = phase1_stan
    ncai   = req.get("003", "0100001")
    auth   = _AUTH_BY_STAN.pop(stan, None) or generate_approval()
    dp     = build_receipt(phase1_amount, stan, MASKED_CARD, auth, ncai, is_customer_copy=True)

    return (
        _common_fields(req, "102", "000") +
        _card_details(MASKED_CARD) +
        f("008", stan) +
        f("010", dp)
    )


def phase2_coherence_reject(req: dict, reason: str) -> str:
    """TM=102 with CR=307 (Format error) when strict-confirm coherence fails."""
    log.warning(f"  strict-confirm: {reason}")
    return _common_fields(req, "102", "307") + f("008", req.get("008", "000000"))


def phase2_refusal(req: dict, rc: str) -> str:
    """
    TM=102 with CR≠000 — the terminal refuses the confirmation (e.g. 302 sale
    not found, 482 already cancelled; the terminal auto-reverses). SDK v1.5.0
    reports this as a failed payment.
    """
    return _common_fields(req, "102", rc) + f("008", req.get("008", "000000"))


def cancellation_response(req: dict, rc: str = "000") -> str:
    """
    TM=003 → TM=103  RC=000 (void performed) — phase 1 of a two-phase void.
    v5.4.4: the SDK sends MT (002) in the cancel frame (echoed by _common_fields).
    Like NapsPay, the reply carries card details, the original transaction
    date/time (DATR/HETR) and the merchant void receipt; STAN is kept so SDKs
    that stop at TM 103 still get one. The SDK then sends TM 004.
    RC≠000 (spec §4.3: not found / other error, e.g. 302, 482) carries no card
    details or receipt.
    """
    now    = datetime.now()
    stan   = req.get("008", generate_stan())
    if PROFILE == "legacy" and not req.get("004") and req.get("005"):
        req = {**req, "004": req["005"]}   # NSA-only TM 003: echo it as NS, the value TM 004 must send
    if rc != "000":
        return _common_fields(req, "103", rc) + f("008", stan)
    resp = (
        _common_fields(req, "103", "000") +
        _card_details(MASKED_CARD) +
        f("008", stan) +
        f("018", now.strftime("%d%m%Y")) + f("019", now.strftime("%H%M%S"))
    )
    if PROFILE != "legacy":
        return resp   # live terminals send no receipt with TM 103
    amount = int(req.get("002", "0") or "0")
    dp     = build_receipt(amount, stan, MASKED_CARD, generate_approval(), req.get("003", "0100001"),
                           is_customer_copy=False, txn_label="ANNULATION")
    return resp + f("010", dp)


def cancellation_confirmation_response(req: dict, rc: str = "000") -> str:
    """
    TM=004 → TM=104 — phase 2 of a void (SDK v1.5.0+), sent on the same
    connection after TM 103. Returns the void STAN, echoes DATR/HETR (spec §4.4)
    and, only when CR=000, the customer void receipt. CR=480 also means the
    cancellation completed (spec §4.4).
    """
    stan   = generate_stan()
    amount = int(req.get("002", "0") or "0")
    resp   = _common_fields(req, "104", rc) + _card_details(MASKED_CARD) + f("008", stan)
    for tag in ("018", "019"):
        if req.get(tag):
            resp += f(tag, req[tag])
    if rc == "000":
        # Live terminals: the TM 104 receipt is the only void receipt, the merchant copy.
        dp = build_receipt(amount, stan, MASKED_CARD, generate_approval(), req.get("003", "0100001"),
                           is_customer_copy=(PROFILE == "legacy"), txn_label="ANNULATION")
        resp += f("010", dp)
    return resp


def cancellation_confirmation_reject(req: dict, rc: str) -> str:
    """TM=104 with CR≠000 and no card details or receipt (missing MT, NS mismatch)."""
    return _common_fields(req, "104", rc) + f("008", req.get("008", "000000"))


def cancellation_confirmation_error(req: dict, void_ns: str | None) -> tuple[str, str] | None:
    """
    Validate a TM 004 against the preceding TM 003 on this connection.
    Returns (rc, reason) to reject, or None when valid.
      - no MT (002)          → 909, as the live v5.4.4 terminal does
      - NS (004) missing or ≠ the TM 003 sequence (spec §3: 004.NS = 003.NS) → 307
    """
    if not req.get("002"):
        return "909", "TM 004 without MT (002)"
    ns = req.get("004")
    if void_ns is None or ns != void_ns:
        return "307", f"NS mismatch: TM 004 NS={ns} vs TM 003 sequence={void_ns}"
    return None


def network_test_response(req: dict) -> str:
    """
    TM=009 → TM=109 RC=000 (legacy profile). The Sunmi P2's NapsPay app does not implement
    TM 009: it answers TM 101 with CR 902 "Invalid transaction", echoing NCAI and NS.
    """
    if PROFILE != "legacy":
        # Field for field as the live Sunmi P2 answered: TM, NCAI, NS, CR, DA, HE.
        now = datetime.now()
        return (f("001", "101") + f("003", req.get("003", "0100001")) + f("004", req.get("004", "000001")) +
                f("013", "902") + f("014", now.strftime("%d%m%Y")) + f("015", now.strftime("%H%M%S")))
    return _common_fields(req, "109", "000")


def totals_response(req: dict) -> str:
    """TM=010 → TM=110  RC=000 with a simple totals receipt."""
    now       = datetime.now()
    date_str  = now.strftime("%d/%m/%Y %H:%M:%S")
    lines = [
        _receipt_line(0,  "TKpay",               "C", "G"),
        _receipt_line(2,  LINE_SEP,               "G", "S"),
        _receipt_line(3,  date_str,               "G", "S"),
        _receipt_line(4,  "TOTAUX DU JOUR",       "C", "G"),
        _receipt_line(5,  LINE_SEP,               "G", "S"),
        _receipt_line(6,  "VENTES:     0000000",  "G", "S"),
        _receipt_line(7,  "ANNULATIONS:       0", "G", "S"),
        _receipt_line(8,  LINE_SEP,               "G", "S"),
        _receipt_line(9,  "MONTANT:   0.00 MAD",  "G", "S"),
        _receipt_line(10, LINE_SEP,               "G", "S"),
    ]
    dp = _join_lines(lines)
    return _common_fields(req, "110", "000") + f("010", dp)


def duplicate_response(req: dict, last_stan: str | None) -> str:
    """TM=008 → TM=108  RC=000 reprints the last receipt or a dummy."""
    stan   = last_stan or generate_stan()
    amount = 0
    auth   = generate_approval()
    ncai   = req.get("003", "0100001")
    dp     = build_receipt(amount, stan, MASKED_CARD, auth, ncai, is_customer_copy=False)
    return _common_fields(req, "108", "000") + f("008", stan) + f("010", dp)


def referencing_response(req: dict) -> str:
    """
    TM=013 → TM=113  RC=000 with a parameter-load receipt.
    TIDE (020) selects the parameter set: 01=Merchant (default), 02=Recharge.
    The response echoes TIDE per spec §4.8.
    """
    now      = datetime.now()
    date_str = now.strftime("%d/%m/%Y %H:%M:%S")
    tide     = req.get("020", "01")
    is_recharge = tide == "02"
    heading  = "PARAMETRES RECHARGE" if is_recharge else "PARAMETRES MARCHAND"
    lines = [
        _receipt_line(0,  "TKpay",              "C", "G"),
        _receipt_line(2,  LINE_SEP,             "G", "S"),
        _receipt_line(3,  date_str,             "G", "S"),
        _receipt_line(4,  heading,              "C", "G"),
        _receipt_line(5,  LINE_SEP,             "G", "S"),
        _receipt_line(6,  "MID: 000000000001",  "G", "S"),
        _receipt_line(7,  "TID: 00000001",      "G", "S"),
        _receipt_line(8,  "DEVISE: MAD (504)",  "G", "S"),
        _receipt_line(9,  LINE_SEP,             "G", "S"),
        _receipt_line(10, "TKPAY DEMO",         "G", "S"),
        _receipt_line(11, "CASABLANCA",         "G", "S"),
        _receipt_line(12, LINE_SEP,             "G", "S"),
    ]
    dp = _join_lines(lines)
    # Echo TIDE (020) so the SDK sees which parameter set was loaded.
    return _common_fields(req, "113", "000") + f("020", tide) + f("010", dp)


def reset_response(req: dict) -> str:
    """TM=012 → TM=112  RC=000."""
    return _common_fields(req, "112", "000")


# ── Helpers ───────────────────────────────────────────────────────────────────

MASKED_CARD = "516794******3315"
_AUTH_BY_STAN: dict = {}   # STAN -> authorisation number of the approved TM 101

def generate_stan()     -> str: return f"{random.randint(1, 999999):06d}"
def generate_approval() -> str: return f"{random.randint(100000, 999999)}"

# ── Interactive decision prompt ───────────────────────────────────────────────

_prompt_lock  = threading.Lock()
_stdin_queue: queue.Queue[str] = queue.Queue()

def _stdin_reader() -> None:
    while True:
        try:
            line = input()
            _stdin_queue.put(line.strip().lower())
        except EOFError:
            _stdin_queue.put("a")
            break

def ask_operator(peer: str, amount_centimes: int) -> tuple[bool, str]:
    """
    Prompt the operator to approve or pick a decline scenario.
    Returns (approved: bool, response_code: str).
    """
    amount_mad = amount_centimes / 100
    with _prompt_lock:
        print(f"\n  ┌── Payment from {peer} ──────────────────────────")
        print(f"  │  Amount : {amount_mad:.2f} MAD")
        print(f"  │  Card   : {MASKED_CARD}")
        print(f"  ├─────────────────────────────────────────────────")
        print(f"  │  [a] Approve")
        for i, name in enumerate(DECLINE_MENU, 1):
            code, label = SCENARIOS[name]
            print(f"  │  [{i:2d}] Decline — {label} ({code})")
        print(f"  └─────────────────────────────────────────────────")
        print(f"  Choice → ", end="", flush=True)

        while True:
            try:
                answer = _stdin_queue.get(timeout=120)
            except queue.Empty:
                print("(timeout → auto-approve)")
                return True, "000"

            if answer in ("a", "approve", "y", "yes", ""):
                print("  → APPROVED")
                return True, "000"

            if answer.isdigit():
                idx = int(answer) - 1
                if 0 <= idx < len(DECLINE_MENU):
                    name = DECLINE_MENU[idx]
                    code, label = SCENARIOS[name]
                    print(f"  → DECLINED ({label} — {code})")
                    return False, code

            resolved = resolve_code(answer)
            if resolved != answer or (len(resolved) == 3 and resolved.isdigit()):
                label = next((v[1] for v in SCENARIOS.values() if v[0] == resolved), resolved)
                print(f"  → DECLINED ({label} — {resolved})")
                return False, resolved

            print(f"  Unknown input {answer!r} — type 'a', a number, or a code: ", end="", flush=True)


# ── Client handler ────────────────────────────────────────────────────────────

def recv_message(conn: socket.socket) -> bytes:
    """
    Read a complete TLV message from the socket.

    Requests arrive with NO terminator character (raw TLV only).
    We use TLV-structure validation to detect a complete message, with a
    200 ms inter-chunk gap as a fallback for any edge case.
    """
    data = b""
    conn.settimeout(180)

    while True:
        try:
            chunk = conn.recv(READ_BUF)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk

        # Check if the accumulated bytes form a complete TLV message
        try:
            text = data.decode(CHARSET, errors="replace")
            if _is_complete(text):
                break
        except Exception:
            pass

        # Fallback: short gap with no more data → treat as end-of-message
        conn.settimeout(0.2)
        try:
            more = conn.recv(READ_BUF)
            if more:
                data += more
            else:
                break
        except (socket.timeout, BlockingIOError):
            break
        finally:
            conn.settimeout(180)

    return data


def send(conn: socket.socket, resp: str, charset: str, terminator: str = RESPONSE_END) -> None:
    """Send a response, appending the end-of-message terminator ('!' v2, '?' v1)."""
    conn.sendall((resp + terminator).encode(charset))


def handle_client(conn: socket.socket, addr: tuple,
                  mode: str, error_code: str, strict_confirm: bool = False,
                  terminator: str = RESPONSE_END) -> None:
    peer = f"{addr[0]}:{addr[1]}"
    log.info(f"[{peer}] connected")

    current_stan:   str | None = None
    current_amount: int       = 0
    void_ns:        str | None = None

    try:
        while True:
            raw_bytes = recv_message(conn)

            if not raw_bytes:
                log.info(f"[{peer}] disconnected")
                break

            raw    = raw_bytes.decode(CHARSET, errors="replace")
            if PROFILE != "legacy" and raw.endswith(("!", "?")):
                # The live Sunmi gave no reply to a '!'-terminated request (and its app stalled).
                log.warning(f"[{peer}] request ends with {raw[-1]!r}: no reply (a real Sunmi does not answer)")
                continue
            fields = parse(raw)
            tm     = fields.get("001", "?")

            tm_name = TM_NAMES.get(tm, f"TM={tm}")
            log.info(f"[{peer}] received {tm_name} (TM={tm})")
            if log.isEnabledFor(logging.DEBUG):
                log.debug(f"[{peer}] fields:\n{dump(fields)}")

            # ── TM=001  Payment request ───────────────────────────────────────
            if tm == "001":
                if mode == "timeout":
                    log.info(f"[{peer}] MODE=timeout — hanging (client will time out)")
                    _time.sleep(300)
                    break

                resp   = phase1_response(fields, mode, error_code, peer=peer)
                rf     = parse(resp)
                rc     = rf.get("013", "?")
                stan   = rf.get("008", "?")
                current_stan   = stan
                current_amount = int(fields.get("002", "0") or "0")
                log.info(f"[{peer}] Phase-1 → RC={rc}  STAN={stan}")
                send(conn, resp, CHARSET, terminator)

            # ── TM=002  Confirmation ──────────────────────────────────────────
            elif tm == "002":
                if mode == "no_confirm":
                    log.info(f"[{peer}] MODE=no_confirm — hanging on Phase-2")
                    _time.sleep(300)
                    break

                stan_to_use = current_stan or fields.get("008", generate_stan())

                # Log the §4.2 coherence fields the SDK v1.0.8 now echoes.
                if log.isEnabledFor(logging.DEBUG):
                    log.debug(f"[{peer}] confirmation echoes: MT={fields.get('002')} "
                              f"DE={fields.get('012')} NCAR={fields.get('007')} DAEX={fields.get('017')}")

                mismatch = confirmation_coherence_error(fields, current_amount) if strict_confirm else None
                if mode == "confirm_error":
                    rc   = resolve_code(error_code)
                    resp = phase2_refusal(fields, rc)
                    log.info(f"[{peer}] Phase-2 → RC={rc} (confirmation refused)")
                    send(conn, resp, CHARSET, terminator)
                elif mismatch:
                    resp = phase2_coherence_reject(fields, mismatch)
                    log.info(f"[{peer}] Phase-2 → RC=307 (coherence reject: {mismatch})")
                    send(conn, resp, CHARSET, terminator)
                else:
                    resp = phase2_response(fields, stan_to_use, current_amount)
                    log.info(f"[{peer}] Phase-2 → RC=000  STAN={stan_to_use}")
                    send(conn, resp, CHARSET, terminator)
                    log.info(f"[{peer}] transaction complete")
                current_stan   = None
                current_amount = 0

            # ── TM=003  Cancellation/void ─────────────────────────────────────
            # TM 103/104 carry no terminator, like the live v5.4.4 terminal —
            # SDKs must fall back to inter-chunk silence framing.
            elif tm == "003":
                stan = fields.get("008", "?")
                rc   = resolve_code(error_code) if mode == "void_refuse" else "000"
                if PROFILE != "legacy" and not fields.get("004"):
                    rc = "307"   # spec §4.3: NS is mandatory; the Lane3600 answers 307 (format error)
                resp = cancellation_response(fields, rc)
                log.info(f"[{peer}] Cancellation → RC={rc}  STAN={stan}")
                send(conn, resp, CHARSET, "")
                current_stan = None
                # Sequence the TM 004 must echo in NS (legacy profile: NSA accepted for TM 003).
                tm003_ns = fields.get("004") or (fields.get("005") if PROFILE == "legacy" else None)
                void_ns = tm003_ns if rc == "000" else None

            # ── TM=004  Cancellation confirmation ────────────────────────────
            elif tm == "004":
                reject = cancellation_confirmation_error(fields, void_ns)
                void_ns = None
                if reject:
                    rc, reason = reject
                    resp = cancellation_confirmation_reject(fields, rc)
                    log.info(f"[{peer}] Cancellation confirmation → RC={rc} ({reason})")
                    send(conn, resp, CHARSET, "")
                elif mode == "void_no_confirm":
                    log.info(f"[{peer}] MODE=void_no_confirm — no reply to TM 004")
                else:
                    rc   = VOID_CONFIRM_CODES.get(mode, "000")
                    resp = cancellation_confirmation_response(fields, rc)
                    log.info(f"[{peer}] Cancellation confirmation → RC={rc}  STAN={parse(resp).get('008')}")
                    send(conn, resp, CHARSET, "")

            # ── TM=008  Duplicate receipt ─────────────────────────────────────
            elif tm == "008":
                resp = duplicate_response(fields, current_stan)
                log.info(f"[{peer}] Duplicate receipt → RC=000")
                send(conn, resp, CHARSET, terminator)

            # ── TM=009  Network test ──────────────────────────────────────────
            elif tm == "009":
                resp = network_test_response(fields)
                log.info(f"[{peer}] Network test → TM={parse(resp).get('001')} RC={parse(resp).get('013')}")
                # The live Sunmi's TM 101 / 902 reply carried no terminator.
                send(conn, resp, CHARSET, terminator if PROFILE == "legacy" else "")

            # ── TM=010  Totals ────────────────────────────────────────────────
            elif tm == "010":
                resp = totals_response(fields)
                log.info(f"[{peer}] Totals → RC=000")
                send(conn, resp, CHARSET, terminator)

            # ── TM=012  Reset PinPAD ──────────────────────────────────────────
            elif tm == "012":
                resp = reset_response(fields)
                log.info(f"[{peer}] Reset PinPAD → RC=000")
                send(conn, resp, CHARSET, terminator)

            # ── TM=013  Referencing ───────────────────────────────────────────
            elif tm == "013":
                resp = referencing_response(fields)
                log.info(f"[{peer}] Referencing → RC=000")
                send(conn, resp, CHARSET, terminator)

            else:
                log.warning(f"[{peer}] unhandled TM={tm!r}, ignoring")

    except Exception as e:
        log.error(f"[{peer}] error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        log.info(f"[{peer}] connection closed")


# ── Server ────────────────────────────────────────────────────────────────────

def run_server(host: str, port: int, mode: str, error_code: str,
               strict_confirm: bool = False, protocol: str = "v2") -> None:
    terminator = PROTOCOL_TERMINATORS[protocol]
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        srv.bind((host, port))
    except OSError as e:
        log.error(f"Cannot bind to {host}:{port} — {e}")
        sys.exit(1)

    srv.listen(5)

    w = 45
    print()
    print(f"  ┌{'─'*w}┐")
    print(f"  │{'NAPS Pay M2M TLV Terminal Emulator':^{w}}│")
    print(f"  ├{'─'*w}┤")
    print(f"  │  {'Listening on':<14}{host}:{port:<{w-16}}│")
    print(f"  │  {'Mode':<14}{mode:<{w-14}}│")
    if mode == "decline":
        print(f"  │  {'Response code':<14}{'100 (do not honour)':<{w-14}}│")
    if mode in ("error", "confirm_error", "void_refuse"):
        rc    = resolve_code(error_code)
        label = next((v[1] for v in SCENARIOS.values() if v[0] == rc), rc)
        print(f"  │  {'Response code':<14}{rc} — {label:<{w-17}}│")
    if mode == "interactive":
        print(f"  │  {'Prompt':<14}{'[a]pprove or pick decline scenario':<{w-14}}│")
    if strict_confirm:
        print(f"  │  {'Confirm':<14}{'strict §4.2 coherence (MT/NCAR)':<{w-14}}│")
    proto_label = f"{protocol} ({chr(39)}{terminator}{chr(39)}{' v5.4.4' if protocol == 'v2' else ' legacy'})"
    print(f"  │  {'Protocol':<14}{proto_label:<{w-14}}│")
    print(f"  └{'─'*w}┘")
    print()
    print("  Ctrl-C to stop\n")

    if mode == "interactive":
        threading.Thread(target=_stdin_reader, daemon=True).start()

    try:
        while True:
            conn, addr = srv.accept()
            threading.Thread(
                target=handle_client,
                args=(conn, addr, mode, error_code, strict_confirm, terminator),
                daemon=True,
            ).start()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        srv.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="NAPS Pay M2M TLV terminal emulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host",  default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    p.add_argument("--port",  default=PORT, type=int, help=f"TCP port (default: {PORT})")
    p.add_argument(
        "--mode", default="approve",
        choices=["approve", "decline", "interactive", "timeout", "no_confirm", "error", "confirm_error",
                 "void_refuse", "void_480", "void_995", "void_no_confirm"],
        help="Response mode (default: approve). interactive = prompt [a]pprove/[d]ecline per payment",
    )
    p.add_argument("--code",  default="909",
                   help="RC for --mode error / confirm_error / void_refuse — numeric (e.g. 116) or scenario name "
                        "(e.g. insufficient_funds). Default: 909")
    p.add_argument("--strict-confirm", action="store_true",
                   help="Reject the confirmation (TM 002, RC=307) when its echoed "
                        "MT/NCAR disagree with the approved payment (spec §4.2).")
    p.add_argument("--protocol", default="v2", choices=["v1", "v2"],
                   help="NAPS TLV generation: v1 terminates responses with '?' "
                        "(pre-v5.4.4), v2 with '!' (v5.4.4+, default).")
    p.add_argument("--profile", default="sunmi", choices=["sunmi", "legacy"],
                   help="Terminal behaviour: sunmi (live Sunmi P2, default) or legacy (earlier emulator).")
    p.add_argument("--debug", action="store_true", help="Print all TLV fields")
    args = p.parse_args()

    global PROFILE
    PROFILE = args.profile

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    run_server(args.host, args.port, args.mode, args.code, args.strict_confirm, args.protocol)


if __name__ == "__main__":
    main()
