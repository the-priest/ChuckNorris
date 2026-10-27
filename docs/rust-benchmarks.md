# Benchmarks: the Rust fleet vs the Python window

Numbers from one machine (a cloud container: Xvfb with GTK's cairo renderer,
Ubuntu 24.04, GTK 4.14, libadwaita 1.5, Python 3.12). Absolute times are
different on a desktop with a GPU renderer; the comparison is the point. Both
scripts are in the repository so anyone can rerun them.

## Start: time to the first painted frame

`tools/bench.sh 10` (under a display, or `xvfb-run -a tools/bench.sh`) starts
each window ten times with a fresh `$HOME`, and stops the clock when GTK's
frame clock reports the first paint. The process start is included.

| window | first frame (median of 10) | min – max | memory then |
|---|---|---|---|
| Chuck (Python) | 427 ms | 408 – 464 ms | 146 MB |
| Chuck Fleet (Rust) | 213 ms | 205 – 248 ms | 117 MB |

Most of the memory on both sides is GTK and libadwaita themselves.

## The fleet's own cost

`cargo run --release -p chuck-runtime --example fleet_bench -- N` runs a whole
job with the model replaced by a script, so only Chuck's own work is timed:
the Lead writes criteria and a plan and assigns N tasks; each worker gets its
own copy of a 200-file project, writes, verifies and submits; the Lead re-runs
each verification on the worker's copy, merges, verifies the whole result, and
finishes through the gate and the critic.

| workers | wall time | model calls | peak memory |
|---|---|---|---|
| 0 (the Lead alone) | 64 ms | 2 | 9 MB |
| 1 | 74 ms | 6 | 10 MB |
| 4 | 322 ms | 9 | 13 MB |
| 8 | 360 ms | 13 | 16 MB |

Each extra agent costs under 1 MB. With a real model, a single call takes
seconds, so the fleet's overhead disappears into the model's latency; what the
architecture buys is that eight workers really run at once, on eight threads.
