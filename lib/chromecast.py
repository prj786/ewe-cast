# chromecast.py — cast-channel v2 client, no dependencies.
#
# A Chromecast speaks length-prefixed protobuf (CastMessage) over TLS :8009.
# The message has six scalar fields, so we hand-encode the wire format
# rather than shipping a protobuf stack for one struct. What we run on top:
#
#   virtual connection  urn:...tp.connection  CONNECT to the receiver, then a
#                       second CONNECT to the app's transportId once launched
#   heartbeat           urn:...tp.heartbeat   PING every 5 s, or it drops us
#   receiver            urn:...receiver       LAUNCH the Default Media
#                       Receiver (CC1AD845)
#   media               urn:...media          LOAD our local HLS URL
#
# Honesty about latency: the Default Media Receiver buffers an HLS live
# stream several seconds deep. This is a watch-together path, not the
# real-time mirror — that is Miracast's job (wfd.py). Google's true mirror
# protocol (Cast Streaming / openscreen) is a future milestone.

import json
import socket
import ssl
import struct
import threading

DEFAULT_RECEIVER = "CC1AD845"
NS_CONN = "urn:x-cast:com.google.cast.tp.connection"
NS_BEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECV = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"


def _varint(n):
    out = b""
    while True:
        b7 = n & 0x7F
        n >>= 7
        out += bytes([b7 | (0x80 if n else 0)])
        if not n:
            return out


def _field(tag, wire, payload):
    return _varint((tag << 3) | wire) + payload


def encode(source, dest, namespace, payload: str) -> bytes:
    msg = (_field(1, 0, _varint(0))                                  # protocol_version = 0
           + _field(2, 2, _varint(len(source.encode())) + source.encode())
           + _field(3, 2, _varint(len(dest.encode())) + dest.encode())
           + _field(4, 2, _varint(len(namespace.encode())) + namespace.encode())
           + _field(5, 0, _varint(0))                                # payload_type = STRING
           + _field(6, 2, _varint(len(payload.encode())) + payload.encode()))
    return struct.pack(">I", len(msg)) + msg


def decode(msg: bytes):
    """Just the fields we read back: namespace (4) and payload (6)."""
    i, out = 0, {}
    while i < len(msg):
        key = 0
        shift = 0
        while True:
            b = msg[i]; i += 1
            key |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        tag, wire = key >> 3, key & 7
        if wire == 0:
            v = 0
            shift = 0
            while True:
                b = msg[i]; i += 1
                v |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
        elif wire == 2:
            ln = 0
            shift = 0
            while True:
                b = msg[i]; i += 1
                ln |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            v = msg[i:i + ln]; i += ln
        else:
            break
        out[tag] = v
    return (out.get(4, b"").decode("utf-8", "replace"),
            out.get(6, b"").decode("utf-8", "replace"))


class ChromecastSession:
    """Connect → launch → load, then keep the heartbeat going. Runs its own
    reader thread (blocking TLS socket); all callbacks are marshalled back
    to the GLib main loop by the caller-provided `dispatch`."""

    def __init__(self, addr, port, media_url, dispatch, on_state, on_dead):
        self.addr, self.port = addr, port
        self.media_url = media_url
        self.dispatch = dispatch          # fn(callable) → run on main loop
        self.on_state = on_state
        self.on_dead = on_dead
        self.sock = None
        self.transport = None             # the launched app's transportId
        self.req = 0
        self.alive = False

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="ewe-cast-cc").start()

    def stop(self):
        self.alive = False
        try:
            if self.sock and self.transport:
                self._send("sender-0", self.transport, NS_CONN, {"type": "CLOSE"})
                self._send("sender-0", "receiver-0", NS_RECV,
                           {"type": "STOP", "requestId": self._rid()})
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

    def _rid(self):
        self.req += 1
        return self.req

    def _send(self, src, dst, ns, obj):
        self.sock.sendall(encode(src, dst, ns, json.dumps(obj)))

    def _run(self):
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE   # device certs are self-signed by design
            raw = socket.create_connection((self.addr, self.port), timeout=10)
            self.sock = ctx.wrap_socket(raw)
            self.alive = True
            self._send("sender-0", "receiver-0", NS_CONN, {"type": "CONNECT"})
            self._send("sender-0", "receiver-0", NS_RECV,
                       {"type": "LAUNCH", "appId": DEFAULT_RECEIVER, "requestId": self._rid()})
            self.dispatch(lambda: self.on_state("connecting", "waking the Chromecast app"))
            self.sock.settimeout(5)
            buf = b""
            import time
            last_ping = 0
            while self.alive:
                now = time.monotonic()
                if now - last_ping > 5:
                    self._send("sender-0", "receiver-0", NS_BEAT, {"type": "PING"})
                    last_ping = now
                try:
                    chunk = self.sock.recv(8192)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 4:
                    ln = struct.unpack(">I", buf[:4])[0]
                    if len(buf) < 4 + ln:
                        break
                    ns, payload = decode(buf[4:4 + ln])
                    buf = buf[4 + ln:]
                    self._message(ns, payload)
        except Exception as e:
            if self.alive:
                self.dispatch(lambda: self.on_dead(f"Chromecast connection failed: {e}"))
                return
        self.dispatch(lambda: self.on_dead("the Chromecast closed the session"))

    def _message(self, ns, payload):
        try:
            msg = json.loads(payload or "{}")
        except ValueError:
            return
        t = msg.get("type", "")
        if t == "PING":
            self._send("sender-0", "receiver-0", NS_BEAT, {"type": "PONG"})
        elif t == "RECEIVER_STATUS" and not self.transport:
            for app in (msg.get("status", {}).get("applications") or []):
                if app.get("appId") == DEFAULT_RECEIVER:
                    self.transport = app["transportId"]
                    self._send("sender-0", self.transport, NS_CONN, {"type": "CONNECT"})
                    self._send("sender-0", self.transport, NS_MEDIA, {
                        "type": "LOAD", "requestId": self._rid(),
                        "media": {"contentId": self.media_url,
                                  "contentType": "application/x-mpegURL",
                                  "streamType": "LIVE"},
                        "autoplay": True,
                    })
                    self.dispatch(lambda: self.on_state("starting", "Chromecast is tuning in"))
        elif t == "MEDIA_STATUS":
            for st in msg.get("status") or []:
                if st.get("playerState") == "PLAYING":
                    self.dispatch(lambda: self.on_state("streaming", ""))
        elif t == "LAUNCH_ERROR":
            self.dispatch(lambda: self.on_dead("the Chromecast refused to launch the player"))
