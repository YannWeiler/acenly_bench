# ACENLY Bench

Track Python function performance across commits. Catch regressions before they ship.

```
  ACENLY Bench  ·  main@a3f9c1b2  ·  Python 3.12.3

  Function                  Median        P95    vs last commit
  ──────────────────────────────────────────────────────────────
  deduplicate_users         1.24 ms    1.51 ms   ▼ 38.1% faster
  build_index              84.21 ms   97.13 ms   ▲ 12.4% slower  ⚠
  filter_records            0.88 ms    0.92 ms   ~ +0.2%
```

---

## What it does

- **Precise timing** — runs each function in an isolated subprocess with warmup, batch timing, and outlier trimming
- **Regression detection** — compares against the last stored result and flags slowdowns
- **Git hook integration** — blocks pushes automatically when a regression exceeds your threshold
- **History tracking** — stores all runs in a local SQLite database so you can see trends over time

No external services. No accounts. Runs entirely on your machine.

---

## Install

```bash
pip install pyyaml
```

Then copy `bench.py` and `bench_db.py` into your project root.

---

## Quick start

```bash
# Benchmark a specific function right now
python3 bench.py myfile.py::my_function

# Show history for tracked functions
python3 bench.py --history

# High-precision mode (batch timing, 3s measurement window)
python3 bench.py --precise
```

---

## Track functions automatically

Create `acenly.yml` in your project root (see `acenly.example.yml`):

```yaml
benchmark:
  track:
    - file: mymodule/utils.py
      function: process_batch
    - file: mymodule/search.py
      function: find_duplicates

  regression_warn:  0.10   # warn if 10% slower
  regression_block: 0.25   # block push if 25% slower
  noise_floor:      0.05   # ignore changes smaller than 5%
  trials: 5
  warmup: 2
```

Then install the git hook:

```bash
python3 bench.py --install-hooks
```

From now on, every `git push` runs the benchmark automatically. If a function regresses past the block threshold, the push is stopped.

```bash
python3 bench.py --skip-hooks   # bypass when needed
python3 bench.py --uninstall-hooks
```

---

## Options

| Flag | Description |
|------|-------------|
| `file.py::func` | Benchmark a specific function |
| `--compare` | Show diff vs last stored result |
| `--history` | Print run history for tracked functions |
| `--precise` | High-precision mode: adaptive warmup, batch timing |
| `--repeat N` | Run N times, keep the best result |
| `--install-hooks` | Install git pre-push hook |
| `--uninstall-hooks` | Remove git pre-push hook |
| `--skip-hooks` | Run benchmark without enforcing regression block |
| `--hook-mode` | Used internally by the git hook |

---

## How timing works

Each benchmark runs in a **separate subprocess** to avoid interference from the parent process. In normal mode, each function is called `trials` times with `warmup` discarded runs first. In `--precise` mode:

1. **Adaptive warmup** — keeps running until timing variance drops below 3% (CPU caches settled)
2. **Batch calibration** — finds a batch size so each measurement window takes ~50ms, then divides — this eliminates OS scheduler jitter from individual timings
3. **3-second window** — collects ~60 batch measurements, reports the minimum (least OS interference)

Results are stored in `bench.db` alongside your project.

---

## License

MIT
