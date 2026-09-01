# main.py — ewe-castd: the casting brain, headless on purpose (RFC-004).
#
# The shell's Cast card is the face; this daemon is everything else. One
# JSON-lines control socket, one sink registry fed by discovery.py, one
# session at a time driven through a small state machine:
#
#   idle → picking (SharePicker via the portal) → connecting (P2P link or
#   TLS) → waiting/negotiating (WFD RTSP) → starting → streaming → idle
#
# Every transition is broadcast to every connected client, so the QS card
# and the bar icon are always narrating the same truth. The daemon runs as
# a systemd user service and outlives shell restarts — a dropped call
# mid-presentation because the shell hot-reloaded is not acceptable.
#
# Test seams (all env, all off in production):
#   EWE_CAST_FAKE=1          two imaginary sinks in the list (UI work)
#   EWE_CAST_FAKE_SOURCE=1   videotestsrc instead of the portal (no session
#                            UI, no compositor needed)
#   EWE_CAST_TEST_SINK=<ip>  a "miracast" sink with no P2P step — the WFD
#                            engine listens and the sink sim connects from
#                            localhost; this is how the whole RTSP + RTP
#                            path is exercised without a TV

import json
import uuid
import os
import shutil
import subprocess
import sys
import threading

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discovery
import wfd as wfd_mod
import chromecast as cc_mod

RUNTIME = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
SOCK_PATH = os.path.join(RUNTIME, "ewe-cast.sock")
HLS_DIR = os.path.join(RUNTIME, "ewe-cast-hls")
HLS_PORT = 8927
PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
NM = "org.freedesktop.NetworkManager"


def dispatch(fn):
    GLib.idle_add(lambda: (fn(), False)[1])


class PortalScreenCast:
    """The org.freedesktop.portal.ScreenCast dance. Three round-trips, each
    answered by a Response signal on a request object — this is what pops
    the shell's own SharePicker. Ends in on_done(fd, node_id)."""

    def __init__(self, bus, on_done, on_fail):
        self.bus = bus
        self.on_done = on_done
        self.on_fail = on_fail
        self.session = None
        self._n = 0

    def _token(self):
        # globally unique per call — a per-instance counter reset on every
        # cast made the SECOND cast reuse the first session's token, and
        # xdg-desktop-portal answered "Failed to register object vtable
        # (File exists)" → instant "screen selection failed" on every retry
        # (the THIRD real-Samsung field-test catch)
        self._n += 1
        return f"ewecast{os.getpid()}_{self._n}_{uuid.uuid4().hex[:8]}"

    def _expect(self, token, cb):
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        sub = None

        def on_response(bus, s, p, i, sig, params):
            self.bus.signal_unsubscribe(sub)
            code, results = params.unpack()
            if code != 0:
                self.on_fail("cancelled" if code == 1 else "screen selection failed")
            else:
                cb(results)
        sub = self.bus.signal_subscribe(
            None, "org.freedesktop.portal.Request", "Response", path, None, 0, on_response)

    def start(self):
        t = self._token()
        self._expect(t, self._created)
        self.bus.call(
            PORTAL, PORTAL_PATH, "org.freedesktop.portal.ScreenCast", "CreateSession",
            GLib.Variant("(a{sv})", ({"handle_token": GLib.Variant("s", t),
                                      "session_handle_token": GLib.Variant("s", self._token())},)),
            None, 0, -1, None, self._called)

    def _called(self, bus, res):
        try:
            bus.call_finish(res)
        except GLib.Error as e:
            self.on_fail(f"portal error: {e.message}")

    def _created(self, results):
        self.session = results["session_handle"]
        t = self._token()
        self._expect(t, self._selected)
        self.bus.call(
            PORTAL, PORTAL_PATH, "org.freedesktop.portal.ScreenCast", "SelectSources",
            GLib.Variant("(oa{sv})", (self.session, {
                "handle_token": GLib.Variant("s", t),
                "types": GLib.Variant("u", 3),          # monitors | windows
                "multiple": GLib.Variant("b", False),
            })), None, 0, -1, None, self._called)

    def _selected(self, _results):
        t = self._token()
        self._expect(t, self._started)
        self.bus.call(
            PORTAL, PORTAL_PATH, "org.freedesktop.portal.ScreenCast", "Start",
            GLib.Variant("(osa{sv})", (self.session, "", {"handle_token": GLib.Variant("s", t)})),
            None, 0, -1, None, self._called)

    def _started(self, results):
        streams = results.get("streams")
        if not streams:
            self.on_fail("the portal returned no stream")
            return
        node_id = streams[0][0]
        try:
            fdres, fdlist = self.bus.call_with_unix_fd_list_sync(
                PORTAL, PORTAL_PATH, "org.freedesktop.portal.ScreenCast", "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (self.session, {})),
                GLib.VariantType("(h)"), 0, -1, None, None)
            fd = fdlist.get(fdres.unpack()[0])
        except GLib.Error as e:
            self.on_fail(f"pipewire handoff failed: {e.message}")
            return
        self.on_done(fd, node_id)

    def close(self):
        if self.session:
            self.bus.call(PORTAL, self.session, "org.freedesktop.portal.Session", "Close",
                          None, None, 0, -1, None, None)
            self.session = None


class AudioRouter:
    """Port of cast-audio.sh: a null sink the pipeline monitors, everything
    playing moved onto it, self-restoring on stop."""

    SINK = "ewe_cast"

    def __init__(self):
        self.module = None
        self.prev = None
        self.moved = []

    def _pactl(self, *args):
        try:
            return subprocess.run(["pactl", *args], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
        except Exception:
            return ""

    def start(self, reroute=True):
        if not shutil.which("pactl"):
            return False
        self.module = self._pactl("load-module", "module-null-sink",
                                  f"sink_name={self.SINK}",
                                  "sink_properties=device.description=Cast") or None
        if not self.module:
            return False
        if not reroute:                   # tests: sink exists for the pipeline,
            return True                   # the user's audio stays untouched
        self.prev = self._pactl("get-default-sink")
        self._pactl("set-default-sink", self.SINK)
        for line in self._pactl("list", "short", "sink-inputs").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                self.moved.append(parts[0])
                self._pactl("move-sink-input", parts[0], self.SINK)
        return True

    def stop(self):
        if self.prev:
            self._pactl("set-default-sink", self.prev)
            for sid in self.moved:
                self._pactl("move-sink-input", sid, self.prev)
        if self.module:
            self._pactl("unload-module", self.module)
        self.module, self.prev, self.moved = None, None, []


class Daemon:
    def __init__(self):
        Gst.init(None)
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sinks = {}
        self.clients = []
        self.state = "idle"
        self.detail = ""
        self.active = None                # the sink dict we're casting to
        self.pipeline = None
        self.wfd = None
        self.cc = None
        self.portal = None
        self.audio = AudioRouter()
        self.nm_active_path = None
        self.http = None
        self.src_fd = None
        self.src_node = None
        self.cc_watch = discovery.ChromecastWatcher(self.bus, self._found, self._lost)
        self.mc_watch = discovery.MiracastWatcher(self.bus, self._found, self._lost)

    # ── registry ──────────────────────────────────────────────────────────
    def _found(self, sink):
        self.sinks[sink["id"]] = sink
        self._broadcast()

    def _lost(self, sid):
        if self.sinks.pop(sid, None):
            self._broadcast()

    def _sink_list(self):
        return [{k: v for k, v in s.items() if k in ("id", "name", "kind", "available")}
                for s in self.sinks.values()]

    # ── control socket ────────────────────────────────────────────────────
    def serve(self):
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)
        svc = Gio.SocketService.new()
        svc.add_address(Gio.UnixSocketAddress.new(SOCK_PATH),
                        Gio.SocketType.STREAM, Gio.SocketProtocol.DEFAULT, None)
        svc.connect("incoming", self._client_in)
        svc.start()

    def _client_in(self, svc, conn, source):
        conn.get_socket().set_blocking(False)
        self.clients.append(conn)
        self._send(conn, self._status())
        buf = bytearray()
        stream = conn.get_input_stream()

        def more(st, res):
            try:
                data = st.read_bytes_finish(res).get_data()
            except GLib.Error:
                data = b""
            if not data:
                if conn in self.clients:
                    self.clients.remove(conn)
                return
            buf.extend(data)
            while b"\n" in buf:
                line, _, rest = bytes(buf).partition(b"\n")
                buf[:] = rest
                self._command(conn, line)
            stream.read_bytes_async(4096, GLib.PRIORITY_DEFAULT, None, more)
        stream.read_bytes_async(4096, GLib.PRIORITY_DEFAULT, None, more)
        return True

    def _send(self, conn, obj):
        try:
            conn.get_output_stream().write_all((json.dumps(obj) + "\n").encode(), None)
        except GLib.Error:
            if conn in self.clients:
                self.clients.remove(conn)

    def _broadcast(self):
        msg = self._status()
        for c in list(self.clients):
            self._send(c, msg)

    def _status(self):
        return {"event": "status", "state": self.state, "detail": self.detail,
                "sink": ({"id": self.active["id"], "name": self.active["name"],
                          "kind": self.active["kind"]} if self.active else None),
                "sinks": self._sink_list()}

    def _set(self, state, detail=""):
        self.state, self.detail = state, detail
        print(f"state: {state} {detail}", flush=True)
        self._broadcast()

    def _command(self, conn, line):
        try:
            cmd = json.loads(line.decode())
        except ValueError:
            return
        c = cmd.get("cmd")
        if c == "status":
            self._send(conn, self._status())
        elif c == "scan":
            self.mc_watch.scan()
            self._send(conn, self._status())
        elif c == "start":
            self.start(cmd.get("sink", ""))
        elif c == "stop":
            self.stop("stopped", failed=False)

    # ── session ───────────────────────────────────────────────────────────
    def start(self, sid):
        if self.state != "idle":
            self.stop("restarting")
        sink = self.sinks.get(sid)
        if not sink:
            self._set("error", "that display is no longer in range")
            GLib.timeout_add_seconds(3, lambda: (self.state == "error" and self._set("idle"), False)[1])
            return
        self.active = sink
        if os.environ.get("EWE_CAST_FAKE_SOURCE"):
            self._source_ready(None, None)
        else:
            self._set("picking", "choose what to share")
            self.portal = PortalScreenCast(
                self.session_bus,
                lambda fd, node: self._source_ready(fd, node),
                lambda msg: self.stop(msg))
            self.portal.start()

    def _source_ready(self, fd, node):
        self.src_fd, self.src_node = fd, node
        sink = self.active
        if sink["kind"] == "fake":
            self._set("streaming", "pretending, loudly")
            self._pipeline_fake()
        elif sink["kind"] == "chromecast":
            self._set("connecting", f"reaching {sink['name']}")
            self._start_chromecast(sink)
        else:
            self._start_miracast(sink)

    # ── miracast ──────────────────────────────────────────────────────────
    def _start_miracast(self, sink):
        self.wfd = wfd_mod.WfdSource(
            on_state=lambda s, d: self._set(s, d),
            on_ready=self._wfd_play,
            on_teardown=lambda why: self.stop(why, failed=False))
        try:
            self.wfd.start()
        except GLib.Error as e:
            self.stop(f"can't own the WFD port ({e.message}) — is another cast app running?")
            return
        if sink.get("peer_path"):         # a real P2P peer: link up first
            self._set("connecting", f"Wi-Fi Direct link to {sink['name']} — the TV may ask you to allow it")
            self._p2p_connect(sink)
        else:                             # EWE_CAST_TEST_SINK: already routable
            self._set("waiting", "waiting for the TV to dial in")

    # WFD Device Information subelement — WITHOUT this in our P2P frames the
    # TV associates and then just sits there: nothing tells it we are a
    # Miracast SOURCE or where to dial. Found in the first real-Samsung field
    # test (the TV joined the group, took a DHCP lease, and never opened
    # RTSP). id=0x00, len=6; body: devinfo 0x0010 (source, session
    # available), RTSP port 7236 (0x1c44), max throughput 50 Mbps (0x0032).
    WFD_IES = bytes.fromhex("00000600101c440032")

    def _p2p_connect(self, sink):
        conn = {
            "connection": {"type": GLib.Variant("s", "wifi-p2p"),
                           "id": GLib.Variant("s", "ewe-cast"),
                           "autoconnect": GLib.Variant("b", False)},
            "wifi-p2p": {"peer": GLib.Variant("s", sink["hw"]),
                         "wps-method": GLib.Variant("u", 0x4),   # PBC — push-button, what TVs expect
                         "wfd-ies": GLib.Variant("ay", self.WFD_IES)},
        }
        self.bus.call(
            NM, "/org/freedesktop/NetworkManager", NM, "AddAndActivateConnection",
            GLib.Variant("(a{sa{sv}}oo)", (conn, sink["device_path"], sink["peer_path"])),
            GLib.VariantType("(oo)"), 0, -1, None, self._p2p_started)

    def _p2p_started(self, bus, res):
        try:
            _, active = bus.call_finish(res).unpack()
        except GLib.Error as e:
            self.stop(f"Wi-Fi Direct refused: {e.message}")
            return
        self.nm_active_path = active
        sub = None

        def on_props(b, s, p, i, sig, params):
            iface, changed, _ = params.unpack()
            st = changed.get("State")
            if st == 2:                    # ACTIVATED — the radio part is done
                self.bus.signal_unsubscribe(sub)
                if self.state == "connecting":
                    self._set("waiting", "link up — waiting for the TV to dial in")
            elif st in (3, 4):             # DEACTIVATING/DEACTIVATED
                self.bus.signal_unsubscribe(sub)
                if self.state in ("connecting", "waiting"):
                    self.stop("the TV never completed the Wi-Fi Direct handshake — "
                              "open its Screen Mirroring source screen and try again")
        sub = self.bus.signal_subscribe(
            NM, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            active, None, 0, on_props)

    def _wfd_play(self, p):
        audio = bool(p.get("audio")) and self.audio.start(
            reroute=not os.environ.get("EWE_CAST_FAKE_SOURCE"))
        video = self._video_src() + \
            f" ! videorate ! videoscale ! videoconvert " \
            f"! video/x-raw,width={p['width']},height={p['height']}," \
            f"framerate={p['fps']}/1 ! {self._encoder()} " \
            f"! h264parse config-interval=1 ! queue ! mux."
        aac = self._aac()
        aud = (f" pulsesrc device={AudioRouter.SINK}.monitor ! audioconvert ! audioresample "
               f"! audio/x-raw,rate=48000,channels=2 ! {aac} "
               f"! aacparse ! queue ! mux." if audio and aac else "")
        line = (f"mpegtsmux name=mux alignment=7 "
                f"! rtpmp2tpay pt=33 mtu=1400 "
                f"! udpsink host={p['host']} port={p['port']} sync=true {video}{aud}")
        self._pipeline_run(line)
        self._set("streaming", "")

    # ── chromecast ────────────────────────────────────────────────────────
    def _start_chromecast(self, sink):
        os.makedirs(HLS_DIR, exist_ok=True)
        for f in os.listdir(HLS_DIR):
            os.unlink(os.path.join(HLS_DIR, f))
        self._serve_hls()
        audio = self.audio.start()
        video = self._video_src() + \
            f" ! videorate ! videoscale ! videoconvert " \
            f"! video/x-raw,width=1280,height=720,framerate=30/1 " \
            f"! {self._encoder()} ! h264parse ! hls.video"
        aac = self._aac()
        aud = (f" pulsesrc device={AudioRouter.SINK}.monitor ! audioconvert ! audioresample "
               f"! {aac} ! aacparse ! hls.audio" if audio and aac else "")
        line = (f"hlssink2 name=hls target-duration=2 max-files=6 playlist-length=4 "
                f"location={HLS_DIR}/seg%05d.ts playlist-location={HLS_DIR}/ewe.m3u8 {video}{aud}")
        self._pipeline_run(line)
        ip = self._route_ip(sink["addr"])
        self.cc = cc_mod.ChromecastSession(
            sink["addr"], sink.get("port", 8009),
            f"http://{ip}:{HLS_PORT}/ewe.m3u8", dispatch,
            on_state=lambda s, d: self._set(s, d),
            on_dead=lambda why: self.stop(why))
        self.cc.start()

    def _serve_hls(self):
        if self.http:
            return
        import http.server
        import functools
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=HLS_DIR)
        handler.log_message = lambda *a: None
        self.http = http.server.ThreadingHTTPServer(("0.0.0.0", HLS_PORT), handler)
        threading.Thread(target=self.http.serve_forever, daemon=True,
                         name="ewe-cast-http").start()

    def _route_ip(self, dest):
        """Our address as seen from the sink's subnet — what goes in the URL."""
        import socket as s
        probe = s.socket(s.AF_INET, s.SOCK_DGRAM)
        try:
            probe.connect((dest, 1))
            return probe.getsockname()[0]
        finally:
            probe.close()

    # ── pipeline ──────────────────────────────────────────────────────────
    def _video_src(self):
        if os.environ.get("EWE_CAST_FAKE_SOURCE") or self.src_node is None:
            return "videotestsrc is-live=true pattern=smpte"
        return f"pipewiresrc fd={self.src_fd} path={self.src_node} do-timestamp=true"

    def _aac(self):
        # AAC encoder roulette: fdk (bad+libfdk) is the usual Arch resident,
        # faac the fallback; avenc_aac only exists when gst-libav is around
        for e in ("fdkaacenc", "faac", "avenc_aac"):
            if Gst.ElementFactory.find(e):
                return f"{e} bitrate=128000"
        return None

    def _encoder(self):
        # hardware first (any VA driver), x264's zerolatency tune second —
        # both end in byte-stream H.264 the TS mux is happy with
        # constrained-baseline, both paths: the SECOND real-Samsung field-test
        # catch. Unconstrained vah264enc negotiated High with features the
        # TV's Miracast decoder refused — RTSP marched through PLAY and the
        # screen stayed black. CBP is the profile every WFD sink MUST decode.
        if Gst.ElementFactory.find("vah264enc"):
            return ("vah264enc bitrate=8000 key-int-max=60 "
                    "! video/x-h264,profile=constrained-baseline")
        return ("x264enc tune=zerolatency speed-preset=veryfast bitrate=8000 "
                "key-int-max=60 ! video/x-h264,profile=constrained-baseline")

    def _pipeline_fake(self):
        self._pipeline_run(self._video_src()
                           + " ! videoconvert ! x264enc tune=zerolatency bitrate=2000 ! fakesink")

    def _pipeline_run(self, line):
        self._pipeline_stop()
        print(f"pipeline: {line}", flush=True)
        self.pipeline = Gst.parse_launch(line)
        gbus = self.pipeline.get_bus()
        gbus.add_signal_watch()
        gbus.connect("message::error", self._gst_error)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _gst_error(self, bus, msg):
        err, dbg = msg.parse_error()
        self.stop(f"stream broke: {err.message}")

    def _pipeline_stop(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    # ── teardown ──────────────────────────────────────────────────────────
    def stop(self, why="", failed=True):
        was = self.state
        self._pipeline_stop()
        if self.cc:
            self.cc.stop()
            self.cc = None
        if self.wfd:
            self.wfd.stop()
            self.wfd = None
        if self.portal:
            self.portal.close()
            self.portal = None
        if self.nm_active_path:
            self.bus.call(NM, "/org/freedesktop/NetworkManager", NM, "DeactivateConnection",
                          GLib.Variant("(o)", (self.nm_active_path,)), None, 0, -1, None, None)
            self.nm_active_path = None
        self.audio.stop()
        self.active = None
        if failed and was != "idle" and why and why not in ("stopped", "restarting"):
            self._set("error", why)
            GLib.timeout_add_seconds(6, lambda: (self.state == "error" and self._set("idle"), False)[1])
        else:
            self._set("idle")

    # ── go ────────────────────────────────────────────────────────────────
    def run(self):
        self.serve()
        self.cc_watch.start()
        self.mc_watch.start()
        self.mc_watch.scan()
        if os.environ.get("EWE_CAST_FAKE"):
            self._found({"id": "fake:tv", "name": "Living Room TV (fake)", "kind": "fake"})
            self._found({"id": "fake:cc", "name": "Kitchen display (fake)", "kind": "fake"})
        if os.environ.get("EWE_CAST_TEST_SINK"):
            self._found({"id": "test:wfd", "name": "Loopback WFD sink",
                         "kind": "miracast", "hw": "", "peer_path": None,
                         "device_path": None})
        print(f"ewe-castd on {SOCK_PATH}", flush=True)
        loop = GLib.MainLoop()
        # a killed daemon must not strand the user's audio on a dead null
        # sink (the gnd era did exactly that) — restore, then die
        for sig in (2, 15):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig,
                                 lambda: (self.stop("shutdown", failed=False), loop.quit(), False)[2])
        loop.run()


def main():
    Daemon().run()


if __name__ == "__main__":
    main()
