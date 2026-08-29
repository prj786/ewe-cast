#!/usr/bin/env python3
# wfd_sink_sim.py — a Samsung TV impersonator, for testing without a TV.
#
# Speaks the sink side of the WFD RTSP exchange against a source listening
# on localhost:7236 (run the daemon with EWE_CAST_TEST_SINK=127.0.0.1 and
# start the "Loopback WFD sink"), then opens the RTP port it negotiated and
# verifies actual media arrives: RTP header sane, payload type 33, payload
# a run of 188-byte MPEG-TS packets each starting with the 0x47 sync byte.
#
# Exit 0 + a JSON verdict on stdout when the whole dance worked. The caps it
# advertises are lifted from a real UE40 series log: CEA mask allowing
# 720p30/1080p30, AAC audio, RTP on 19000.
import json
import re
import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
RTP_PORT = 19000
verdict = {"m_seen": [], "rtp_packets": 0, "ts_ok": 0, "pt": None, "resolution": None}


def recv_msg(sock, buf):
    while True:
        if b"\r\n\r\n" in buf:
            head, rest = buf.split(b"\r\n\r\n", 1)
            headers = {}
            lines = head.decode().split("\r\n")
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            clen = int(headers.get("content-length", "0") or "0")
            if len(rest) >= clen:
                return lines[0], headers, rest[:clen].decode(), rest[clen:]
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("source hung up")
        buf += chunk


def send(sock, text):
    sock.sendall(text.encode())


def respond(sock, headers, extra=None, body=""):
    h = {"CSeq": headers.get("cseq", "0")}
    h.update(extra or {})
    if body:
        h["Content-Type"] = "text/parameters"
        h["Content-Length"] = str(len(body))
    send(sock, "RTSP/1.0 200 OK\r\n"
         + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n" + body)


sock = socket.create_connection((HOST, 7236), timeout=10)
buf = b""
cseq = 100
sent_setup = False
session = None
deadline = time.monotonic() + 30

while time.monotonic() < deadline:
    first, headers, body, buf = recv_msg(sock, buf)

    if first.startswith("RTSP/1.0"):
        # a response to one of OUR requests (M6 SETUP / M7 PLAY answers)
        if sent_setup and session is None:
            m = re.search(r"Session:\s*(\w+)", f"Session: {headers.get('session','')}")
            session = (headers.get("session", "").split(";")[0] or "12345678")
            verdict["m_seen"].append("M6-answered")
            cseq += 1
            send(sock, f"PLAY rtsp://{HOST}/wfd1.0/streamid=0 RTSP/1.0\r\n"
                       f"CSeq: {cseq}\r\nSession: {session}\r\n\r\n")
        elif session is not None and "M7-answered" not in verdict["m_seen"]:
            verdict["m_seen"].append("M7-answered")
            break                        # negotiation done — go count packets
        continue

    m = re.match(r"(\S+)\s+(\S+)\s+RTSP/1\.0", first)
    if not m:
        continue
    method = m.group(1)

    if method == "OPTIONS":              # M1 — and we fire our M2 back first,
        verdict["m_seen"].append("M1")   # the out-of-order habit real TVs have
        cseq += 1
        send(sock, f"OPTIONS * RTSP/1.0\r\nCSeq: {cseq}\r\nRequire: org.wfa.wfd1.0\r\n\r\n")
        respond(sock, headers,
                {"Public": "org.wfa.wfd1.0, SETUP, TEARDOWN, PLAY, PAUSE, GET_PARAMETER, SET_PARAMETER"})
    elif method == "GET_PARAMETER" and "wfd_video_formats" in body:   # M3
        verdict["m_seen"].append("M3")
        respond(sock, headers, body=(
            "wfd_video_formats: 00 00 02 10 000101a1 00000000 00000000 00 0000 0000 11 none none\r\n"
            "wfd_audio_codecs: LPCM 00000002 00, AAC 00000007 00\r\n"
            f"wfd_client_rtp_ports: RTP/AVP/UDP;unicast {RTP_PORT} 0 mode=play\r\n"))
    elif method == "SET_PARAMETER" and "wfd_trigger_method: SETUP" in body:   # M5
        verdict["m_seen"].append("M5")
        respond(sock, headers)
        cseq += 1
        sent_setup = True
        send(sock, f"SETUP rtsp://{HOST}/wfd1.0/streamid=0 RTSP/1.0\r\n"
                   f"CSeq: {cseq}\r\n"
                   f"Transport: RTP/AVP/UDP;unicast;client_port={RTP_PORT}\r\n\r\n")
    elif method == "SET_PARAMETER":      # M4
        verdict["m_seen"].append("M4")
        mm = re.search(r"wfd_video_formats: 00 00 02 10 ([0-9a-f]{8})", body)
        if mm:
            verdict["resolution"] = f"cea-mask:{mm.group(1)}"
        respond(sock, headers)
    elif method == "GET_PARAMETER":      # keep-alive
        respond(sock, headers)

# ── media check: is real MPEG-TS-over-RTP landing on the negotiated port? ──
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.bind((HOST, RTP_PORT))
udp.settimeout(10)
t_end = time.monotonic() + 6
while time.monotonic() < t_end and verdict["rtp_packets"] < 300:
    try:
        pkt = udp.recv(4096)
    except socket.timeout:
        break
    if len(pkt) < 12 or (pkt[0] >> 6) != 2:      # RTP version 2
        continue
    verdict["rtp_packets"] += 1
    verdict["pt"] = pkt[1] & 0x7F
    payload = pkt[12:]
    if len(payload) % 188 == 0 and all(payload[i] == 0x47 for i in range(0, len(payload), 188)):
        verdict["ts_ok"] += 1

# be a polite TV about it
try:
    cseq += 1
    send(sock, f"TEARDOWN rtsp://{HOST}/wfd1.0/streamid=0 RTSP/1.0\r\n"
               f"CSeq: {cseq}\r\nSession: {session or ''}\r\n\r\n")
    sock.close()
except OSError:
    pass

verdict["ok"] = (verdict["m_seen"][:1] == ["M1"]
                 and {"M3", "M4", "M5", "M6-answered", "M7-answered"} <= set(verdict["m_seen"])
                 and verdict["rtp_packets"] > 30
                 and verdict["ts_ok"] == verdict["rtp_packets"]
                 and verdict["pt"] == 33)
print(json.dumps(verdict))
sys.exit(0 if verdict["ok"] else 1)
