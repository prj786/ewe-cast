# ewe-cast

**RFC-004 · phases A+B built (2026-08-30), Miracast proven against a loopback
sink — first real-TV field test pending**

Casting without the foreign app: today ewe's Cast card spawns
gnome-network-displays — a gtk4/libadwaita window in a DE that deliberately
has neither, driven by SIGTERM. The screen-sharing *plumbing* is already
ours (xdg-desktop-portal + PipeWire + the shell's own SharePicker); only
the sink protocols live in that app. `ewe-castd` moves them into a headless
daemon so the whole flow lives in Quick Settings.

## The user flow (all in Control Center)

1. Cast card → expands to a sink list (live scan: Miracast peers via
   NetworkManager P2P, Chromecasts via avahi).
2. Pick a sink → the shell's **SharePicker** opens (same one screen-share
   uses): output / window / region.
3. Sharing. The card shows the sink name + a stop button. No window ever.

## Architecture

    Quick Settings (Cast card)
        │  qs ipc / D-Bus verbs: scan · sinks · start <sink> · stop · status
    ewe-castd  (Rust daemon, own repo = this one)
        ├─ source: xdg-desktop-portal ScreenCast → PipeWire stream
        ├─ encode: GStreamer, VA-API H.264 (vah264enc, x264 fallback)
        ├─ sink A: Miracast — NM Wi-Fi P2P D-Bus + a hand-rolled WFD RTSP
        │          source (we LISTEN on 7236, the TV dials in — verified
        │          against gnd's behavior). RTP/UDP MPEG-TS, pt 33. This is
        │          the real-time path.
        └─ sink B: Chromecast — avahi discovery + cast-channel v2 (protobuf
                   hand-encoded, TLS 8009), Default Media Receiver playing a
                   local HLS stream (seconds of latency — honest; Google's
                   true mirroring protocol is a future milestone)

    One implementation note vs. the original RFC: the daemon is Python on
    GLib, not Rust. The media heavy lifting is all C (GStreamer); what's left
    is IO-bound protocol logic, and Python-with-GLib is the house pattern
    (ewe-auth, ewe-conf) with zero new dependencies on an ewe install. A Rust
    rewrite stays on the table if profiling ever demands it.

## Testing without a TV

    test/run.sh — the daemon plays WFD source against test/wfd_sink_sim.py,
    a loopback Samsung impersonator: full M1→M7 negotiation, then it counts
    RTP packets and checks every payload is clean 188-byte-aligned MPEG-TS.
    EWE_CAST_FAKE=1 adds imaginary sinks for shell UI work; what remains
    untestable indoors is only the P2P radio handshake and real-firmware
    quirks — exactly the part the shell's journal narrator diagnoses.

## Phases

| # | ships | status |
|---|---|---|
| A | daemon, discovery, control socket; QS card lists sinks, bar icon | **built + tested** (loopback, nested shell) |
| B | Miracast WFD source + Chromecast control/HLS | **built**; WFD loopback-tested, real hardware pending |
| C | field-proven on a real Samsung + a real Chromecast | ← the gate for dropping gnome-network-displays |

Until C, gnd stays installed behind `qs ipc call cast legacy` — the escape
hatch if a real TV meets a v0 bug mid-presentation.

## Why a daemon and not a library into the shell

Casting must survive shell restarts (ewe.service respawns on crash — a
dropped call mid-presentation is unacceptable), wants real threads for the
media pipeline, and Komble/future apps may want "cast this file" someday.
Same argument that made ewe-auth a broker.
