#!/usr/bin/env python3
"""Send one payment (TM 001) and its confirmation (TM 002) to the emulator on localhost:4444."""
import socket, sys

def tlv(tag, value): return f"{tag}{len(value):03d}{value}"          # TAG(3) + LENGTH(3) + VALUE
def fields(reply): return {reply[i:i+3]: reply[i+6:i+6+int(reply[i+3:i+6])] for i in iter_tags(reply)}
def iter_tags(r, i=0):
    while i + 6 <= len(r): yield i; i += 6 + int(r[i+3:i+6])
def exchange(sock, frame):
    sock.sendall(frame.encode()); reply = b""
    while not reply.endswith((b"!", b"?")): reply += sock.recv(4096)   # reply ends with '!' (v2) or '?' (v1)
    return fields(reply[:-1].decode())

port = int(sys.argv[1]) if len(sys.argv) > 1 else 4444
with socket.create_connection(("127.0.0.1", port)) as s:              # one connection for both phases
    r1 = exchange(s, tlv("001", "001") + tlv("002", "15000") + tlv("003", "0100001") + tlv("004", "000001") + tlv("012", "504"))
    print(f"TM {r1['001']}  CR={r1['013']}  STAN={r1['008']}  card={r1['007']}")
    r2 = exchange(s, tlv("001", "002") + tlv("003", "0100001") + tlv("004", "000001") + tlv("008", r1["008"]))
    print(f"TM {r2['001']}  CR={r2['013']}  STAN={r2['008']}  {'approved' if r2['013'] == '000' else 'refused'}")
