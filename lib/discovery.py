# discovery.py — who can we cast to right now?
#
# Two worlds, two watchers, one sink list:
#
#   Chromecast  avahi over D-Bus, browsing _googlecast._tcp. The friendly name
#               is the "fn" TXT record ("Living Room TV"), not the mDNS service
#               name (that one is "Chromecast-<serial>").
#   Miracast    NetworkManager's Wi-Fi P2P device (p2p-dev-*). We ask it to
#               find peers and keep only those whose beacon carries a WFD IE
#               declaring a *sink* role — every Wi-Fi Direct phone/laptop in
#               range also shows up as a peer, and listing those as TVs is how
#               you erode trust in the list.
#
# Both watchers push into a shared registry via callbacks; the daemon owns the
# registry and broadcasts changes. Nothing here blocks: everything rides the
# GLib main loop through Gio's async D-Bus.

from gi.repository import Gio, GLib

AVAHI = "org.freedesktop.Avahi"
NM = "org.freedesktop.NetworkManager"
NM_DEVICE_TYPE_WIFI_P2P = 30


def _wfd_ie_is_sink(ies: bytes):
    """Parse the WFD information elements just enough to answer two things:
    is this peer a sink, and does it say it is available. Subelement 0
    (Device Information) is 6 bytes: info(2) ctrl-port(2) throughput(2);
    device type lives in info bits 0-1 (01 primary sink, 10 source+sink
    hmm — 00 source, 01 primary sink, 10 secondary sink, 11 dual role)."""
    i = 0
    while i + 3 <= len(ies):
        sub_id = ies[i]
        length = int.from_bytes(ies[i + 1:i + 3], "big")
        body = ies[i + 3:i + 3 + length]
        if sub_id == 0 and length >= 6:
            info = int.from_bytes(body[0:2], "big")
            dev_type = info & 0x3
            available = (info >> 4) & 0x3
            return dev_type != 0, available != 0
        i += 3 + length
    return False, False


class ChromecastWatcher:
    """avahi ServiceBrowser → ResolveService for every _googlecast._tcp."""

    def __init__(self, bus, on_found, on_lost):
        self.bus = bus
        self.on_found = on_found
        self.on_lost = on_lost
        self._subs = []

    def start(self):
        try:
            # Server2? Plain Server API is stable everywhere avahi runs.
            path = self.bus.call_sync(
                AVAHI, "/", f"{AVAHI}.Server", "ServiceBrowserNew",
                GLib.Variant("(iissu)", (-1, -1, "_googlecast._tcp", "local", 0)),
                GLib.VariantType("(o)"), 0, -1, None).unpack()[0]
        except GLib.Error as e:
            # no avahi-daemon = no Chromecasts, not a daemon-fatal condition
            print(f"discovery: avahi unavailable ({e.message})", flush=True)
            return
        self._subs.append(self.bus.signal_subscribe(
            AVAHI, f"{AVAHI}.ServiceBrowser", "ItemNew", path, None, 0, self._item_new))
        self._subs.append(self.bus.signal_subscribe(
            AVAHI, f"{AVAHI}.ServiceBrowser", "ItemRemove", path, None, 0, self._item_remove))

    def _item_new(self, bus, sender, path, iface, signal, params):
        interface, protocol, name, stype, domain, flags = params.unpack()
        self.bus.call(
            AVAHI, "/", f"{AVAHI}.Server", "ResolveService",
            GLib.Variant("(iisssiu)", (interface, protocol, name, stype, domain, -1, 0)),
            GLib.VariantType("(iissssisqaayu)"), 0, -1, None,
            self._resolved, name)

    def _resolved(self, bus, res, name):
        try:
            r = bus.call_finish(res).unpack()
        except GLib.Error:
            return                      # gone before we could resolve it
        _, _, sname, _, _, host, addr, port, txt, _ = r
        fn = sname
        for entry in txt:
            s = bytes(entry).decode("utf-8", "replace")
            if s.startswith("fn="):
                fn = s[3:]
        self.on_found({
            "id": f"cc:{sname}",
            "name": fn,
            "kind": "chromecast",
            "addr": addr,
            "port": port,
        })

    def _item_remove(self, bus, sender, path, iface, signal, params):
        _, _, name, _, _, _ = params.unpack()
        self.on_lost(f"cc:{name}")


class MiracastWatcher:
    """NM Wi-Fi P2P peers. Also hands the daemon what it needs to connect:
    the peer's D-Bus object path and the P2P device path."""

    def __init__(self, bus, on_found, on_lost):
        self.bus = bus
        self.on_found = on_found
        self.on_lost = on_lost
        self.device_path = None
        self._subs = []
        self._find_timer = None

    def start(self):
        try:
            devices = self.bus.call_sync(
                NM, "/org/freedesktop/NetworkManager", NM, "GetAllDevices",
                None, GLib.VariantType("(ao)"), 0, -1, None).unpack()[0]
        except GLib.Error as e:
            print(f"discovery: NetworkManager unavailable ({e.message})", flush=True)
            return
        for d in devices:
            if self._prop(d, "org.freedesktop.NetworkManager.Device", "DeviceType") == NM_DEVICE_TYPE_WIFI_P2P:
                self.device_path = d
                break
        if not self.device_path:
            print("discovery: no Wi-Fi P2P device — Miracast sinks won't appear", flush=True)
            return
        di = "org.freedesktop.NetworkManager.Device.WifiP2P"
        self._subs.append(self.bus.signal_subscribe(
            NM, di, "PeerAdded", self.device_path, None, 0,
            lambda *a: self._peer(a[-1].unpack()[0])))
        self._subs.append(self.bus.signal_subscribe(
            NM, di, "PeerRemoved", self.device_path, None, 0,
            lambda *a: self.on_lost(f"p2p:{a[-1].unpack()[0]}")))
        for p in self._prop(self.device_path, di, "Peers") or []:
            self._peer(p)

    def scan(self):
        """One StartFind burst. NM keeps the find alive ~30 s; the QS card
        calls this when it opens, and again on explicit rescan."""
        if not self.device_path:
            return
        self.bus.call(
            NM, self.device_path, "org.freedesktop.NetworkManager.Device.WifiP2P",
            "StartFind", GLib.Variant("(a{sv})", ({},)), None, 0, -1, None,
            lambda b, r: self._call_done(b, r, "StartFind"))

    def _call_done(self, bus, res, what):
        try:
            bus.call_finish(res)
        except GLib.Error as e:
            print(f"discovery: {what} failed ({e.message})", flush=True)

    def _prop(self, path, iface, prop):
        try:
            return self.bus.call_sync(
                NM, path, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (iface, prop)),
                GLib.VariantType("(v)"), 0, -1, None).unpack()[0]
        except GLib.Error:
            return None

    def _peer(self, peer_path):
        pi = "org.freedesktop.NetworkManager.WifiP2PPeer"
        ies = self._prop(peer_path, pi, "WfdIEs")
        if not ies:
            return                       # a phone, a laptop — not a display
        is_sink, available = _wfd_ie_is_sink(bytes(ies))
        if not is_sink:
            return
        name = self._prop(peer_path, pi, "Name") or "Miracast display"
        hw = self._prop(peer_path, pi, "HwAddress") or ""
        self.on_found({
            "id": f"p2p:{peer_path}",
            "name": name,
            "kind": "miracast",
            "hw": hw,
            "peer_path": peer_path,
            "device_path": self.device_path,
            "available": available,
        })
