#!/usr/bin/env python3
"""
NAPS Pay M2M TLV Terminal Emulator
===================================
Listens on TCP port 4444 and speaks the two-phase NAPS Pay M2M protocol.

Usage:
    python3 naps_emulator.py                     # approve all, port 4444
    python3 naps_emulator.py --port 4445
    python3 naps_emulator.py --mode decline
    python3 naps_emulator.py --mode timeout      # Phase-1 hangs (tests client timeout)
    python3 naps_emulator.py --mode no_confirm   # Phase-2 hangs (tests 40-s confirmation timeout)
    python3 naps_emulator.py --mode error --code 909
    python3 naps_emulator.py --debug             # print every TLV field

Supported modes:
    approve      All payments approved (default)
    decline      Phase-1 returns RC=005 (declined)
    timeout      Phase-1 never responds
    no_confirm   Phase-2 never responds
    error        Phase-1 returns the code supplied with --code
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
READ_BUF     = 1024
CHARSET      = "utf-8"
LINE_SEP     = "--------------------------------"
FS1          = "*"
FS2          = "?"

# ── TLV helpers ──────────────────────────────────────────────────────────────

def f(tag: str, value: str) -> str:
    """Encode one TLV field: TAG(3) + LENGTH(3) + VALUE (length capped at 999)."""
    n = min(len(value), 999)
    return f"{tag}{n:03d}{value[:n]}"


def parse(raw: str) -> dict[str, str]:
    """Parse TLV string → {tag: value}."""
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


def dump(fields: dict[str, str]) -> str:
    NAMES = {
        "001":"TM",  "002":"MT",   "003":"NCAI","004":"NS",
        "005":"NSA", "007":"NCAR", "008":"STAN","009":"NA",
        "010":"DP",  "012":"DE",   "013":"CR",  "014":"DA",
        "015":"HE",  "016":"NPRT", "017":"DAEX","018":"DATR",
        "019":"HETR","025":"RESE", "040":"EM",
    }
    return "\n".join(
        f"    {tag} ({NAMES.get(tag, tag):6s}) = {val!r}"
        for tag, val in fields.items()
    )


TM_NAMES = {
    "001": "Payment",
    "002": "Confirmation",
    "003": "Cancellation",
    "008": "Duplicate receipt",
    "009": "Network test",
    "010": "Totals",
    "012": "Reset PinPAD",
    "013": "Referencing",
}

# ── Receipt builder ───────────────────────────────────────────────────────────

def _receipt_line(line_num: int, content: str, align: str, style: str,
                  last: bool = False) -> str:
    sub = (
        "030002" + f"{line_num:02d}" +
        "031001" + style +
        "032001" + align +
        "033" + f"{len(content):03d}" + content +
        (FS2 if last else FS1)
    )
    return sub


def build_receipt(amount_centimes: int, stan: str, masked_card: str,
                  auth_num: str, ncai: str,
                  merchant_name: str = "TKPAY DEMO",
                  merchant_city: str = "CASABLANCA",
                  term_id: str = "00000001",
                  is_customer_copy: bool = False) -> str:
    now         = datetime.now()
    date_str    = now.strftime("%d/%m/%Y %H:%M:%S")
    amount_mad  = f"{amount_centimes / 100:.2f}"
    label       = "DEBIT"
    copy_label  = "Copie Client" if is_customer_copy else "Copie Commerçant"
    footer1     = "devenezcommerçantNAPS" if is_customer_copy else "Conservez-moi, je peux être utile!"
    footer2     = "APPELEZLE0522917474"  if is_customer_copy else "www.naps.ma"

    lines = [
        _receipt_line(0,  "TKpay",                      "C", "G"),
        _receipt_line(2,  LINE_SEP,                     "G", "S"),
        _receipt_line(3,  date_str,                     "G", "S"),
        _receipt_line(4,  merchant_name,                "G", "S"),
        _receipt_line(6,  merchant_city,                "G", "S"),
        _receipt_line(7,  LINE_SEP,                     "G", "S"),
        _receipt_line(9,  "VISA",                       "G", "S"),
        _receipt_line(10, masked_card,                  "G", "S"),
        _receipt_line(15, f"Terminal: {term_id}",       "G", "S"),
        _receipt_line(17, f"Transaction: {stan}",       "G", "S"),
        _receipt_line(18, f"Autorisation: {auth_num}",  "G", "S"),
        _receipt_line(19, f"STAN: {stan}",              "G", "S"),
        _receipt_line(20, LINE_SEP,                     "G", "S"),
        _receipt_line(21, f"MONTANT: {amount_mad} MAD", "G", "S"),
        _receipt_line(22, LINE_SEP,                     "G", "S"),
        _receipt_line(23, label,                        "G", "S"),
        _receipt_line(24, copy_label,                   "G", "S"),
        _receipt_line(25, LINE_SEP,                     "G", "S"),
        _receipt_line(26, footer1,                      "G", "S"),
        _receipt_line(27, footer2,                      "G", "S", last=True),
    ]
    return "".join(lines)


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
        base = _common_fields(req, "101", "005")
        stan = generate_stan()
        return base + f("008", stan) + f("025", "REFUSE")

    if mode == "error":
        base = _common_fields(req, "101", error_code)
        return base + f("008", generate_stan())

    if mode == "interactive":
        amount = int(req.get("002", "0") or "0")
        approved = ask_operator(peer, amount)
        if not approved:
            base = _common_fields(req, "101", "005")
            stan = generate_stan()
            return base + f("008", stan) + f("025", "REFUSE")

    stan    = generate_stan()
    auth    = generate_approval()
    ncai    = req.get("003", "0100001")
    amount  = int(req.get("002", "0") or "0")
    dp      = build_receipt(amount, stan, MASKED_CARD, auth, ncai, is_customer_copy=False)

    return (
        _common_fields(req, "101", "000") +
        _card_details(MASKED_CARD) +
        f("008", stan) +
        f("010", dp)
    )


def phase2_response(req: dict, phase1_stan: str) -> str:
    stan   = phase1_stan
    ncai   = req.get("003", "0100001")
    amount = int(req.get("002", "0") or "0")
    auth   = generate_approval()
    dp     = build_receipt(amount, stan, MASKED_CARD, auth, ncai, is_customer_copy=True)

    return (
        _common_fields(req, "102", "000") +
        _card_details(MASKED_CARD) +
        f("008", stan) +
        f("010", dp)
    )


def cancellation_response(req: dict) -> str:
    """TM=003 → TM=103  RC=000 (void accepted)."""
    stan = req.get("008", generate_stan())
    return _common_fields(req, "103", "000") + f("008", stan)


def network_test_response(req: dict) -> str:
    """TM=009 → TM=109  RC=000."""
    return _common_fields(req, "109", "000")


def totals_response(req: dict) -> str:
    """TM=010 → TM=110  RC=000 with a simple totals receipt."""
    now       = datetime.now()
    date_str  = now.strftime("%d/%m/%Y %H:%M:%S")
    lines = [
        _receipt_line(0,  "TKpay",              "C", "G"),
        _receipt_line(2,  LINE_SEP,              "G", "S"),
        _receipt_line(3,  date_str,              "G", "S"),
        _receipt_line(4,  "TOTAUX DU JOUR",      "C", "G"),
        _receipt_line(5,  LINE_SEP,              "G", "S"),
        _receipt_line(6,  "VENTES:     0000000", "G", "S"),
        _receipt_line(7,  "ANNULATIONS:       0","G", "S"),
        _receipt_line(8,  LINE_SEP,              "G", "S"),
        _receipt_line(9,  "MONTANT:   0.00 MAD", "G", "S"),
        _receipt_line(10, LINE_SEP,              "G", "S", last=True),
    ]
    dp = "".join(lines)
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
    """TM=013 → TM=113  RC=000 with basic merchant config receipt."""
    now      = datetime.now()
    date_str = now.strftime("%d/%m/%Y %H:%M:%S")
    lines = [
        _receipt_line(0,  "TKpay",              "C", "G"),
        _receipt_line(2,  LINE_SEP,              "G", "S"),
        _receipt_line(3,  date_str,              "G", "S"),
        _receipt_line(4,  "PARAMETRES",          "C", "G"),
        _receipt_line(5,  LINE_SEP,              "G", "S"),
        _receipt_line(6,  "MID: 000000000001",   "G", "S"),
        _receipt_line(7,  "TID: 00000001",       "G", "S"),
        _receipt_line(8,  "DEVISE: MAD (504)",   "G", "S"),
        _receipt_line(9,  LINE_SEP,              "G", "S"),
        _receipt_line(10, "TKPAY DEMO",          "G", "S"),
        _receipt_line(11, "CASABLANCA",           "G", "S"),
        _receipt_line(12, LINE_SEP,              "G", "S", last=True),
    ]
    dp = "".join(lines)
    return _common_fields(req, "113", "000") + f("010", dp)


def reset_response(req: dict) -> str:
    """TM=012 → TM=112  RC=000."""
    return _common_fields(req, "112", "000")


# ── Helpers ───────────────────────────────────────────────────────────────────

MASKED_CARD = "516794******3315"

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

def ask_operator(peer: str, amount_centimes: int) -> bool:
    amount_mad = amount_centimes / 100
    with _prompt_lock:
        print(f"\n  ┌── Payment from {peer} ──")
        print(f"  │  Amount : {amount_mad:.2f} MAD")
        print(f"  │  Card   : {MASKED_CARD}")
        print(f"  └── [a] Approve   [d] Decline  → ", end="", flush=True)

        while True:
            try:
                answer = _stdin_queue.get(timeout=120)
            except queue.Empty:
                print("(timeout → auto-approve)")
                return True
            if answer in ("a", "approve", "y", "yes", ""):
                print("APPROVED")
                return True
            if answer in ("d", "decline", "n", "no"):
                print("DECLINED")
                return False
            print(f"  Unknown input {answer!r} — type 'a' or 'd': ", end="", flush=True)


# ── Client handler ────────────────────────────────────────────────────────────

def recv_message(conn: socket.socket) -> bytes:
    """
    Read a complete TLV message from the socket.
    Uses a 200 ms inter-chunk gap to detect end-of-message (the terminal
    keeps the connection open between phases).
    """
    data = b""
    while True:
        try:
            chunk = conn.recv(READ_BUF)
            if not chunk:
                break
            data += chunk
            if len(chunk) < READ_BUF:
                conn.settimeout(0.2)
                try:
                    more = conn.recv(READ_BUF)
                    if more:
                        data += more
                except (socket.timeout, BlockingIOError):
                    pass
                finally:
                    conn.settimeout(None)
                break
        except socket.timeout:
            break
    return data


def send(conn: socket.socket, resp: str, charset: str) -> None:
    """Send a response, appending '?' as end-of-message terminator."""
    conn.sendall((resp + FS2).encode(charset))


def handle_client(conn: socket.socket, addr: tuple,
                  mode: str, error_code: str) -> None:
    peer = f"{addr[0]}:{addr[1]}"
    log.info(f"[{peer}] connected")

    conn.settimeout(180)

    current_stan: str | None = None

    try:
        while True:
            try:
                raw_bytes = recv_message(conn)
            except socket.timeout:
                log.info(f"[{peer}] idle timeout")
                break

            if not raw_bytes:
                log.info(f"[{peer}] disconnected")
                break

            raw    = raw_bytes.decode(CHARSET, errors="replace")
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
                current_stan = stan
                log.info(f"[{peer}] Phase-1 → RC={rc}  STAN={stan}")
                send(conn, resp, CHARSET)

            # ── TM=002  Confirmation ──────────────────────────────────────────
            elif tm == "002":
                if mode == "no_confirm":
                    log.info(f"[{peer}] MODE=no_confirm — hanging on Phase-2")
                    _time.sleep(300)
                    break

                stan_to_use = current_stan or fields.get("008", generate_stan())
                resp = phase2_response(fields, stan_to_use)
                log.info(f"[{peer}] Phase-2 → RC=000  STAN={stan_to_use}")
                send(conn, resp, CHARSET)
                log.info(f"[{peer}] transaction complete")
                current_stan = None

            # ── TM=003  Cancellation/void ─────────────────────────────────────
            elif tm == "003":
                stan = fields.get("008", "?")
                resp = cancellation_response(fields)
                log.info(f"[{peer}] Cancellation → RC=000  STAN={stan}")
                send(conn, resp, CHARSET)
                current_stan = None

            # ── TM=008  Duplicate receipt ─────────────────────────────────────
            elif tm == "008":
                resp = duplicate_response(fields, current_stan)
                log.info(f"[{peer}] Duplicate receipt → RC=000")
                send(conn, resp, CHARSET)

            # ── TM=009  Network test ──────────────────────────────────────────
            elif tm == "009":
                resp = network_test_response(fields)
                log.info(f"[{peer}] Network test → RC=000")
                send(conn, resp, CHARSET)

            # ── TM=010  Totals ────────────────────────────────────────────────
            elif tm == "010":
                resp = totals_response(fields)
                log.info(f"[{peer}] Totals → RC=000")
                send(conn, resp, CHARSET)

            # ── TM=012  Reset PinPAD ──────────────────────────────────────────
            elif tm == "012":
                resp = reset_response(fields)
                log.info(f"[{peer}] Reset PinPAD → RC=000")
                send(conn, resp, CHARSET)

            # ── TM=013  Referencing ───────────────────────────────────────────
            elif tm == "013":
                resp = referencing_response(fields)
                log.info(f"[{peer}] Referencing → RC=000")
                send(conn, resp, CHARSET)

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

def run_server(host: str, port: int, mode: str, error_code: str) -> None:
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
    if mode in ("decline", "error"):
        rc = error_code if mode == "error" else "005"
        print(f"  │  {'Response code':<14}{rc:<{w-14}}│")
    if mode == "interactive":
        print(f"  │  {'Prompt':<14}{'[a]pprove / [d]ecline per payment':<{w-14}}│")
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
                args=(conn, addr, mode, error_code),
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
        choices=["approve", "decline", "interactive", "timeout", "no_confirm", "error"],
        help="Response mode (default: approve). interactive = prompt [a]pprove/[d]ecline per payment",
    )
    p.add_argument("--code",  default="909", help="RC for --mode error (default: 909)")
    p.add_argument("--debug", action="store_true", help="Print all TLV fields")
    args = p.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    run_server(args.host, args.port, args.mode, args.code)


if __name__ == "__main__":
    main()
