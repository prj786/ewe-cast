# wfd.py — a Wi-Fi Display (Miracast) SOURCE, the real-time path.
#
# Roles, because they are backwards from intuition: after the Wi-Fi Direct
# link is up, the SOURCE (us) listens on TCP 7236 and the TV connects to us
# (verified against gnome-network-displays, which does exactly this with
# gst-rtsp-server). Over that one TCP connection RTSP requests then flow in
# BOTH directions — we interrogate and configure the sink (M1, M3, M4, M5),
# the sink drives the actual media setup at us like a normal RTSP client
# (M2, M6 SETUP, M7 PLAY). The media itself is not RTSP-interleaved: it is
# plain RTP/UDP, MPEG-TS (H.264 + AAC) at pt 33, to the client_port the sink
# names in SETUP.
#
# We implement RTSP by hand rather than bending gst-rtsp-server into a
# source: the WFD exchange is nonstandard enough (server-initiated requests,
# wfd_* parameter bodies, trigger methods) that owning the ~200 lines of
# text protocol is simpler and more debuggable than subclassing C vfuncs
# through PyGObject. GStreamer's job here is only the pipeline.
#
# The negotiation, by the numbers:
#   M1  us→TV   OPTIONS (Require: org.wfa.wfd1.0)      "do you speak WFD?"
#   M2  TV→us   OPTIONS                                 same question back
#   M3  us→TV   GET_PARAMETER video/audio/ports         "what can you do?"
#   M4  us→TV   SET_PARAMETER formats + presentation URL "we'll do this"
#   M5  us→TV   SET_PARAMETER wfd_trigger_method: SETUP  "your move"
#   M6  TV→us   SETUP  → we answer with a session id
#   M7  TV→us   PLAY   → pipeline starts, picture on glass
#   M16 us→TV   GET_PARAMETER keep-alive every 25 s (sinks drop silent
#               sources at 30-60 s)
#
# Some sinks send their OPTIONS before answering ours (gnome-network-displays
# has a debug line for exactly this) — the parser therefore treats requests
# and responses as one interleaved stream and never assumes turn order.

import re
import socket
from gi.repository import GLib

RTSP_PORT = 7236
# The RTP source port we ANNOUNCE in the SETUP Transport header. The
# fifth real-Samsung field catch: we declared it, then streamed from a
# random ephemeral port — strict sinks filter RTP by the announced
# source port, so the session stayed healthy while zero media arrived
# (gnome-network-displays works because gst-rtsp-server binds it).
# udpsink must bind-port= this exact value.
SERVER_RTP_PORT = 15550
KEEPALIVE_S = 25

# CEA resolution bitmap (wfd_video_formats), the entries we are willing to
# send. Higher index = preferred. 30 fps is the WFD sweet spot; 60p pushes
# many TVs' decoders and the Wi-Fi link for no visible win on a desktop.
CEA_MODES = [
    (0,  640, 480, 60),    # bit 0 is mandatory-supported by every sink
    (2,  720, 576, 50),
    (1,  720, 480, 60),
    (16, 1920, 1080, 25),
    (8, 1920, 1080, 30),
    # 720p30 preferred (last = wins): the fourth real-Samsung field lesson.
    # The payload was proven valid (captured off the wire and decoded to a
    # perfect frame) while the TV stayed black — 1080p at 8 Mbps over a
    # shared 2.4 GHz channel can lose too many packets to assemble a frame.
    # Commercial Miracast sources start at 720p for exactly this reason;
    # 1080p becomes a setting once link stats prove the air is clean.
    (5, 1280, 720, 30),
]


def pick_video_mode(sink_cea_mask):
    """Highest mode the sink's CEA bitmap admits; bit 0 is the safety net."""
    chosen = CEA_MODES[0]
    for bit, w, h, fps in CEA_MODES:
        if sink_cea_mask & (1 << bit):
            chosen = (bit, w, h, fps)
    return chosen


class WfdSource:
    """One listener, one sink at a time. Owns the RTSP conversation; hands
    the daemon `on_ready(params)` when it is time to start the pipeline and
    `on_state(state, detail)` for everything the UI should narrate."""

    def __init__(self, on_state, on_ready, on_teardown):
        self.on_state = on_state          # (str state, str detail)
        self.on_ready = on_ready          # ({host, port, width, height, fps, audio})
        self.on_teardown = on_teardown
        self.service = None
        self.conn = None                  # GSocketConnection of THE sink
        self.buf = b""
        self.cseq = 0
        self.pending = {}                 # cseq → callback(status, headers, body)
        self.session_id = "173620"        # any token; some sinks echo it badly, keep it digits
        self.peer_ip = None
        self.keepalive = None
        self.sink_rtp_port = None
        self.sink_caps = {}

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self):
        from gi.repository import Gio
        if self.service:
            return
        self.service = Gio.SocketService.new()
        self.service.add_inet_port(RTSP_PORT, None)
        self.service.connect("incoming", self._incoming)
        self.service.start()

    def stop(self):
        if self.conn:
            try:
                self._send_request("TEARDOWN", "rtsp://localhost/wfd1.0/streamid=0",
                                   {"Session": self.session_id})
            except Exception:
                pass
            try:
                self.conn.close(None)
            except Exception:
                pass
            self.conn = None
        if self.keepalive:
            GLib.source_remove(self.keepalive)
            self.keepalive = None
        if self.service:
            self.service.stop()
            self.service.close()
            self.service = None

    # ── plumbing ──────────────────────────────────────────────────────────
    def _incoming(self, service, conn, source):
        if self.conn is not None:
            conn.close(None)              # one TV at a time
            return True
        self.conn = conn
        self.peer_ip = conn.get_remote_address().get_address().to_string()
        self.on_state("negotiating", f"TV connected from {self.peer_ip}")
        self.buf = b""
        stream = conn.get_input_stream()
        self._read_more(stream)
        # M1 — open the conversation
        self._send_request("OPTIONS", "*", {"Require": "org.wfa.wfd1.0"},
                           cb=self._m1_done)
        return True

    def _read_more(self, stream):
        stream.read_bytes_async(4096, GLib.PRIORITY_DEFAULT, None, self._on_read, stream)

    def _on_read(self, stream, res, user_stream):
        try:
            data = stream.read_bytes_finish(res).get_data()
        except GLib.Error:
            data = b""
        if not data:
            if self.conn:                 # TV hung up
                self.conn = None
                self.on_teardown("the TV closed the connection")
            return
        self.buf += data
        while self._consume_one():
            pass
        self._read_more(user_stream)

    def _consume_one(self):
        """Parse one complete RTSP message (request or response) off the
        buffer. Returns False when more bytes are needed."""
        if b"\r\n\r\n" not in self.buf:
            return False
        head, rest = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("utf-8", "replace").split("\r\n")
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        clen = int(headers.get("content-length", "0") or "0")
        if len(rest) < clen:
            return False
        body = rest[:clen].decode("utf-8", "replace")
        self.buf = rest[clen:]
        first = lines[0]
        self._wire("<<", head.decode("utf-8", "replace") + "\r\n" + body)
        if first.startswith("RTSP/1.0"):
            m = re.match(r"RTSP/1\.0\s+(\d+)", first)
            status = int(m.group(1)) if m else 0
            cseq = headers.get("cseq", "")
            cb = self.pending.pop(cseq, None)
            if cb:
                cb(status, headers, body)
        else:
            m = re.match(r"(\S+)\s+(\S+)\s+RTSP/1\.0", first)
            if m:
                self._handle_request(m.group(1), m.group(2), headers, body)
        return True

    def _write(self, text):
        if self.conn:
            self.conn.get_output_stream().write_all(text.encode(), None)

    @staticmethod
    def _wire(direction, text):
        """full RTSP wire log — black screens hide in single header fields,
        so the journal carries every message verbatim (they are tiny)"""
        print(f"rtsp {direction} | " + " \\n ".join(text.rstrip().splitlines()), flush=True)

    def _send_request(self, method, url, headers=None, body="", cb=None):
        self.cseq += 1
        h = {"CSeq": str(self.cseq)}
        h.update(headers or {})
        if body:
            h["Content-Type"] = "text/parameters"
            h["Content-Length"] = str(len(body))
        msg = f"{method} {url} RTSP/1.0\r\n" \
            + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n" + body
        if cb:
            self.pending[str(self.cseq)] = cb
        self._wire(">>", msg)
        self._write(msg)

    def _respond(self, req_headers, status="200 OK", headers=None, body=""):
        h = {"CSeq": req_headers.get("cseq", "0")}
        h.update(headers or {})
        if body:
            h["Content-Type"] = "text/parameters"
            h["Content-Length"] = str(len(body))
        out = (f"RTSP/1.0 {status}\r\n"
               + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n" + body)
        self._wire(">>", out)
        self._write(out)

    # ── the sink's requests (M2, M6, M7, teardown) ────────────────────────
    def _handle_request(self, method, url, headers, body):
        if method == "OPTIONS":           # M2
            self._respond(headers, headers={
                "Public": "org.wfa.wfd1.0, GET_PARAMETER, SET_PARAMETER, SETUP, PLAY, PAUSE, TEARDOWN"})
        elif method == "SETUP":           # M6 — the transport answer we've been waiting for
            m = re.search(r"client_port=(\d+)", headers.get("transport", ""))
            self.sink_rtp_port = int(m.group(1)) if m else 1028
            # port PAIRS, rtp-rtcp: the sixth real-Samsung field catch. gnd
            # works because gst-rtsp-server runs a real RTP session with
            # RTCP sender reports on client_port+1 — Samsung renderers use
            # the SR to establish the presentation clock and will decode
            # forever WITHOUT PRESENTING if none arrive. Black glass, healthy
            # session. We now declare and serve the same pair.
            self._respond(headers, headers={
                "Session": f"{self.session_id};timeout=60",
                "Transport": (f"RTP/AVP/UDP;unicast;"
                              f"client_port={self.sink_rtp_port}-{self.sink_rtp_port + 1};"
                              f"server_port={SERVER_RTP_PORT}-{SERVER_RTP_PORT + 1}")})
        elif method == "PLAY":            # M7 — roll tape
            self._respond(headers, headers={"Session": self.session_id, "Range": "npt=now-"})
            self.on_state("starting", "the TV asked for the stream")
            self.on_ready({
                "host": self.peer_ip,
                "port": self.sink_rtp_port,
                **self.sink_caps,
            })
            self.keepalive = GLib.timeout_add_seconds(KEEPALIVE_S, self._keepalive)
        elif method == "TEARDOWN":
            self._respond(headers)
            self.on_teardown("the TV ended the session")
        elif method == "GET_PARAMETER":   # sink-side keep-alive
            self._respond(headers)
        elif method == "SET_PARAMETER":
            # sinks report wfd_idr_request (picture loss) and similar here;
            # answering 200 is enough for v0, a future pipeline hook can
            # force a keyframe on wfd_idr_request
            self._respond(headers)
        else:
            self._respond(headers, status="405 Method Not Allowed")

    # ── our side of the script (M1 → M3 → M4 → M5) ───────────────────────
    def _m1_done(self, status, headers, body):
        # M3: ask capabilities. Keep the ask minimal — every extra parameter
        # is one more line a quirky firmware can choke on.
        self._send_request(
            "GET_PARAMETER", "rtsp://localhost/wfd1.0", {},
            "wfd_video_formats\r\nwfd_audio_codecs\r\nwfd_client_rtp_ports\r\n",
            cb=self._m3_done)

    def _m3_done(self, status, headers, body):
        cea_mask = 0x1                    # mandatory bit 0
        audio = None
        for line in body.split("\r\n"):
            if line.startswith("wfd_video_formats:"):
                # native, pref-mode, profile, level, CEA, VESA, HH, ...
                parts = line.split(":", 1)[1].strip().split()
                # first format block: fields 4 (0-indexed) is the CEA bitmap
                # in "xx xx xx xx CEA VESA HH ..." layout; be forgiving —
                # firmware whitespace varies wildly
                hexes = [p for p in parts if re.fullmatch(r"[0-9a-fA-F]{2,8}", p)]
                if len(hexes) >= 5:
                    cea_mask = int(hexes[4], 16) or 0x1
            elif line.startswith("wfd_audio_codecs:"):
                if "AAC" in line:
                    audio = "aac"
                elif "LPCM" in line:
                    audio = "lpcm"
            elif line.startswith("wfd_client_rtp_ports:"):
                m = re.search(r"(\d{3,5})\s+\d+\s+mode=play", line)
                if m:
                    self.sink_rtp_port = int(m.group(1))
        bit, w, h, fps = pick_video_mode(cea_mask)
        self.sink_caps = {"width": w, "height": h, "fps": fps, "audio": audio}
        self.on_state("negotiating", f"TV speaks {w}x{h}@{fps}" + (f" + {audio}" if audio else ""))
        # M4: commit the choice. Presentation URL names US — the sink connects
        # its media session to this (it already has our IP from TCP).
        local_ip = self.conn.get_local_address().get_address().to_string()
        audio_line = "wfd_audio_codecs: AAC 00000001 00\r\n" if audio == "aac" else \
                     "wfd_audio_codecs: LPCM 00000002 00\r\n" if audio == "lpcm" else ""
        self._send_request(
            "SET_PARAMETER", "rtsp://localhost/wfd1.0", {},
            f"wfd_video_formats: 00 00 02 10 {1 << bit:08x} 00000000 00000000 00 0000 0000 11 none none\r\n"
            + audio_line
            + f"wfd_presentation_URL: rtsp://{local_ip}/wfd1.0/streamid=0 none\r\n"
            + f"wfd_client_rtp_ports: RTP/AVP/UDP;unicast {self.sink_rtp_port or 1028} 0 mode=play\r\n",
            cb=self._m4_done)

    def _m4_done(self, status, headers, body):
        # M5: hand the sink the ball — it must now SETUP against us.
        self._send_request("SET_PARAMETER", "rtsp://localhost/wfd1.0", {},
                           "wfd_trigger_method: SETUP\r\n")

    def _keepalive(self):
        if not self.conn:
            self.keepalive = None
            return False
        self._send_request("GET_PARAMETER", "rtsp://localhost/wfd1.0", {})
        return True
