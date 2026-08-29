#!/usr/bin/env bash
# run.sh — the loopback protocol test: daemon as WFD source, the sink sim as
# a Samsung. Verifies the whole M1→M7 negotiation AND that real MPEG-TS RTP
# lands on the negotiated port. No TV, no network, no portal, and — thanks
# to EWE_CAST_FAKE_SOURCE — no rerouting of the developer's live audio.
set -eu
HERE="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$(mktemp)"
trap 'kill $CASTD 2>/dev/null || true; rm -f "$LOG"' EXIT

EWE_CAST_TEST_SINK=127.0.0.1 EWE_CAST_FAKE_SOURCE=1 \
    "$HERE/bin/ewe-castd" >"$LOG" 2>&1 &
CASTD=$!
sleep 2

python3 - <<'EOF'
import os, socket, time
s = socket.socket(socket.AF_UNIX)
s.connect(os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "ewe-cast.sock"))
s.recv(4096)
s.sendall(b'{"cmd":"start","sink":"test:wfd"}\n')
time.sleep(1.5)
EOF

if python3 "$HERE/test/wfd_sink_sim.py"; then
    echo "wfd loopback: OK"
else
    echo "wfd loopback: FAILED — daemon log:" >&2
    cat "$LOG" >&2
    exit 1
fi
