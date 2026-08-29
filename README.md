# ewe-cast

**RFC-004 · design accepted, implementation post-Dolly (1.1 target)**

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
        ├─ encode: GStreamer, VA-API H.264 (SW fallback)
        ├─ sink A: Chromecast — avahi discovery + cast-channel v2 (protobuf
        │          over TLS), webrtc/mirroring receiver
        └─ sink B: Miracast — NetworkManager P2P D-Bus + WFD RTSP
                   negotiation (gst-rtsp-server), the part gnd is kept for
                   today (its cast power-save dispatcher hack stays valid)

## Phases

| # | ships | drops |
|---|---|---|
| A | daemon skeleton, discovery, status verbs; QS card lists sinks | — |
| B | Chromecast mirroring end-to-end | — |
| C | Miracast/WFD | gnome-network-displays (+ gtk4/libadwaita from the tree) |

Until C lands, the current gnd path stays as-is — working beats pretty.

## Why a daemon and not a library into the shell

Casting must survive shell restarts (ewe.service respawns on crash — a
dropped call mid-presentation is unacceptable), wants real threads for the
media pipeline, and Komble/future apps may want "cast this file" someday.
Same argument that made ewe-auth a broker.
