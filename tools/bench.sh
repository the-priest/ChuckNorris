#!/usr/bin/env bash
# Time to the first painted window frame, and memory at that moment: the
# Python Chuck window vs the Rust Chuck Fleet window, same machine, same
# fresh settings, measured from the shell's clock (process start included).
#   tools/bench.sh [runs]        (needs a display, or: xvfb-run -a tools/bench.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
RUNS="${1:-10}"
PY="${PYTHON:-python3}"
cargo build -q --release -p chuck
BIN="${CARGO_TARGET_DIR:-target}/release/chuck"
TMPH="$(mktemp -d)"
trap 'rm -rf "$TMPH"' EXIT
export HOME="$TMPH" GSK_RENDERER="${GSK_RENDERER:-cairo}"
PYHOOK='
import os, sys, time
sys.path.insert(0, os.getcwd()); sys.argv = ["chucknorris"]
import chucknorris as cn
t0 = float(os.environ["T0"]); done = [False]
present = cn.ChuckWindow.present
def hooked(self):
    def on_map(w):
        def after(_c):
            if done[0]: return
            done[0] = True
            rss = next(l.split()[1] for l in open("/proc/self/status") if l.startswith("VmRSS:"))
            print(f"first-frame {time.time() - t0:.3f} rss-kb {rss}", flush=True)
            w.get_application().quit()
        w.get_frame_clock().connect("after-paint", after)
    self.connect("map", on_map)
    present(self)
cn.ChuckWindow.present = hooked
sys.exit(cn.main())
'
run() {  # $1 = label, rest = command
  local label="$1"; shift
  local f="$TMPH/times"
  : > "$f"
  for _ in $(seq "$RUNS"); do
    T0="$(date +%s.%N)" CHUCK_BENCH_FRAME="$(date +%s.%N)" "$@" 2>/dev/null | grep first-frame >> "$f" || true
  done
  local n; n="$(wc -l < "$f")"
  [ "$n" -gt 0 ] || { echo "$label: no frame reported"; return; }
  local mid=$(( (n + 1) / 2 ))
  local med min max mem
  med="$(awk '{print $2}' "$f" | sort -n | sed -n "${mid}p")"
  min="$(awk '{print $2}' "$f" | sort -n | head -n1)"
  max="$(awk '{print $2}' "$f" | sort -n | tail -n1)"
  mem="$(awk '{m+=$4} END {printf "%.0f", m/NR/1024}' "$f")"
  awk -v l="$label" -v a="$med" -v b="$min" -v c="$max" -v m="$mem" -v n="$n" 'BEGIN {
      printf "%-20s first frame: median %4.0f ms (min %4.0f, max %4.0f)   memory %4d MB   runs %d\n",
      l, 1000*a, 1000*b, 1000*c, m, n }'
}
run "Chuck (Python)" "$PY" -c "$PYHOOK"
run "Chuck Fleet (Rust)" "$BIN"
