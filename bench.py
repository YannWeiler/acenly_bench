#!/usr/bin/env python3
"""
ACENLY Bench
────────────
Track Python function performance across commits.
Catch regressions before they ship.

Usage:
  python3 bench.py                          # benchmark all tracked functions
  python3 bench.py myfile.py::my_function   # benchmark one specific function
  python3 bench.py --compare                # show diff vs last stored result
  python3 bench.py --history                # show benchmark history
  python3 bench.py --install-hooks          # install git pre-push hook
  python3 bench.py --uninstall-hooks        # remove git hook
  python3 bench.py --hook-mode              # used internally by git hook
  python3 bench.py --skip-hooks             # run benchmark, skip git enforcement
  python3 bench.py --precise                # high-precision mode (batch timing)
  python3 bench.py --repeat N               # repeat N times, keep best result
"""

from __future__ import annotations

import os
import sys
import ast
import time
import stat
import shutil
import subprocess
import statistics
import platform
from pathlib import Path
from typing import Optional

import yaml

from bench_db import (
    init_db,
    save_benchmark,
    get_benchmark_last,
    get_benchmark_history,
)

init_db()

# ── paths ──────────────────────────────────────────────────────────────────
ROOT      = Path(__file__).parent
HOOKS_DIR = ROOT / "hooks"
CFG_FILE  = ROOT / "acenly.yml"
if not CFG_FILE.exists():
    CFG_FILE = ROOT / "acenly.example.yml"

# ── precise mode defaults ──────────────────────────────────────────────────
PRECISE_TRIALS  = 50
PRECISE_WARMUP  = 10
PRECISE_TRIM    = 0.10
PRECISE_TIMEOUT = 30.0

# ── terminal colours ───────────────────────────────────────────────────────
def _c(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"

RED    = lambda t: _c("31", t)
GREEN  = lambda t: _c("32", t)
YELLOW = lambda t: _c("33", t)
CYAN   = lambda t: _c("36", t)
BOLD   = lambda t: _c("1",  t)
DIM    = lambda t: _c("2",  t)
WHITE  = lambda t: _c("97", t)


# ══════════════════════════════════════════════════════════════════════════
# GIT HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _git(*args) -> str:
    try:
        return subprocess.check_output(
            ["git"] + list(args),
            stderr=subprocess.DEVNULL,
            cwd=ROOT,
        ).decode().strip()
    except subprocess.CalledProcessError:
        return ""


def git_commit_hash() -> str:
    return _git("rev-parse", "HEAD") or "unknown"


def git_branch() -> str:
    return _git("branch", "--show-current") or "unknown"


def git_changed_files() -> set[str]:
    out = _git("diff", "HEAD~1", "--name-only")
    return set(out.splitlines()) if out else set()


# ══════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    if not CFG_FILE.exists():
        return {}
    with open(CFG_FILE) as f:
        return yaml.safe_load(f) or {}


def bench_config(cfg: dict) -> dict:
    return cfg.get("benchmark", {})


def tracked_functions(cfg: dict) -> list[dict]:
    return bench_config(cfg).get("track", [])


def regression_warn(cfg: dict) -> float:
    return float(bench_config(cfg).get("regression_warn", 0.10))


def regression_block(cfg: dict) -> float:
    return float(bench_config(cfg).get("regression_block", 0.25))


def noise_floor(cfg: dict) -> float:
    return float(bench_config(cfg).get("noise_floor", 0.05))


def bench_trials(cfg: dict) -> int:
    return int(bench_config(cfg).get("trials", 5))


def bench_warmup(cfg: dict) -> int:
    return int(bench_config(cfg).get("warmup", 2))


# ══════════════════════════════════════════════════════════════════════════
# BENCHMARK RUNNER
# ══════════════════════════════════════════════════════════════════════════

_HARNESS_TEMPLATE = """\
import sys, time, statistics, importlib.util, copy

FILE_PATH = {file_path!r}
FUNC_NAME = {func_name!r}
TRIALS    = {trials}
WARMUP    = {warmup}
TRIM_PCT  = {trim_pct}
PRECISE   = {precise}

spec = importlib.util.spec_from_file_location("_target_mod", FILE_PATH)
mod  = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
func = getattr(mod, FUNC_NAME)

# ── synthetic inputs via introspection ───────────────────────────────────
import inspect, random, string
sig    = inspect.signature(func)
params = list(sig.parameters.values())

def _make_arg(p):
    ann = p.annotation
    if ann == inspect.Parameter.empty:
        n = p.name.lower()
        if any(k in n for k in ("item", "list", "arr", "seq", "data", "elems")):
            return [random.randint(0, 50) for _ in range(200)]
        if any(k in n for k in ("str", "text", "s", "word")):
            return ''.join(random.choices(string.ascii_lowercase, k=100))
        if any(k in n for k in ("n", "count", "size", "k")):
            return 100
        return [random.randint(0, 50) for _ in range(200)]
    if ann in (list, "list"):
        return [random.randint(0, 50) for _ in range(200)]
    if ann in (str, "str"):
        return ''.join(random.choices(string.ascii_lowercase, k=100))
    if ann in (int, "int"):
        return 100
    if ann in (dict, "dict"):
        return {{str(i): i for i in range(100)}}
    return [random.randint(0, 50) for _ in range(200)]

args = tuple(_make_arg(p) for p in params if p.default is inspect.Parameter.empty)

# ── detect arg mutation ───────────────────────────────────────────────────
_MUTATES = False
if args:
    _before = copy.deepcopy(args)
    try:
        func(*copy.deepcopy(args))
    except Exception:
        pass
    try:
        _MUTATES = list(args) != list(_before)
    except Exception:
        _MUTATES = True

# ── warmup ───────────────────────────────────────────────────────────────
def _one_call(fresh=True):
    a = copy.deepcopy(args) if (fresh and args) else args
    func(*a)

if PRECISE:
    _STAB_WIN, _STAB_CV = 20, 0.03
    _recent, _wdl = [], time.perf_counter() + 2.0
    while time.perf_counter() < _wdl:
        a = copy.deepcopy(args) if args else ()
        t0 = time.perf_counter()
        func(*a)
        _recent.append((time.perf_counter() - t0) * 1000)
        if len(_recent) > _STAB_WIN:
            _recent.pop(0)
        if len(_recent) == _STAB_WIN:
            _m = sum(_recent) / _STAB_WIN
            if _m > 0 and statistics.stdev(_recent) / _m < _STAB_CV:
                break
else:
    for _ in range(WARMUP):
        try:
            _one_call()
        except Exception:
            pass

# ── calibrate batch size (precise mode) ──────────────────────────────────
if PRECISE:
    _TARGET_S = 0.050
    _n = 1
    while True:
        _t0 = time.perf_counter()
        for _ in range(_n):
            func(*args)
        _el = time.perf_counter() - _t0
        if _el >= 0.005 or _n >= 500_000:
            BATCH = max(1, int(_n * _TARGET_S / max(_el, 1e-9)))
            BATCH = min(BATCH, 500_000)
            break
        _n = min(_n * 10, 500_000)
else:
    BATCH = 1

# ── measure ───────────────────────────────────────────────────────────────
timings = []

if PRECISE:
    deadline = time.perf_counter() + 3.0
    while time.perf_counter() < deadline:
        if _MUTATES and args:
            _batch_args = [copy.deepcopy(args) for _ in range(BATCH)]
        t0 = time.perf_counter()
        if _MUTATES and args:
            for _a in _batch_args:
                func(*_a)
        else:
            for _ in range(BATCH):
                func(*args)
        per_call_ms = (time.perf_counter() - t0) * 1000 / BATCH
        timings.append(per_call_ms)
    while len(timings) < 20:
        t0 = time.perf_counter()
        for _ in range(BATCH):
            func(*args)
        timings.append((time.perf_counter() - t0) * 1000 / BATCH)
else:
    for _ in range(TRIALS):
        a  = copy.deepcopy(args) if args else ()
        t0 = time.perf_counter()
        func(*a)
        timings.append((time.perf_counter() - t0) * 1000)

# ── stats ─────────────────────────────────────────────────────────────────
n_raw    = len(timings)
sorted_t = sorted(timings)

if PRECISE:
    cut     = max(0, int(n_raw * 0.05))
    trimmed = sorted_t[:-cut] if cut else sorted_t
    mn      = trimmed[0]
    median  = statistics.median(trimmed)
    p95_idx = max(0, int(len(trimmed) * 0.95) - 1)
    p95     = trimmed[p95_idx]
    mx      = trimmed[-1]
    stdev   = statistics.stdev(trimmed) if len(trimmed) > 1 else 0.0
    cv      = (stdev / mn * 100) if mn > 0 else 0.0
    report_median = mn
else:
    cut      = max(1, int(n_raw * TRIM_PCT))
    trimmed  = sorted_t[cut:-cut] if TRIM_PCT > 0 and n_raw > cut * 2 else sorted_t
    mn       = trimmed[0]
    report_median = statistics.median(trimmed)
    median   = report_median
    p95_idx  = max(0, int(len(trimmed) * 0.95) - 1)
    p95      = trimmed[p95_idx]
    mx       = trimmed[-1]
    stdev    = statistics.stdev(trimmed) if len(trimmed) > 1 else 0.0
    cv       = (stdev / median * 100) if median > 0 else 0.0

print(f"RESULT median={{report_median:.6f}} p95={{p95:.6f}} min={{mn:.6f}} max={{mx:.6f}} "
      f"stdev={{stdev:.6f}} cv={{cv:.3f}} trials={{n_raw}} trimmed={{len(trimmed)}}")
"""


def _run_harness_once(harness: str, timeout: float) -> Optional[dict]:
    tmp = ROOT / "_bench_harness_tmp.py"
    tmp.write_text(harness)
    try:
        result = subprocess.run(
            [sys.executable, str(tmp)],
            capture_output=True, text=True,
            timeout=timeout, cwd=ROOT,
        )
        tmp.unlink(missing_ok=True)
        for line in result.stdout.splitlines():
            if line.startswith("RESULT "):
                parts = dict(p.split("=") for p in line[7:].split())
                return {
                    "median_ms": float(parts["median"]),
                    "p95_ms":    float(parts["p95"]),
                    "min_ms":    float(parts["min"]),
                    "max_ms":    float(parts["max"]),
                    "stdev_ms":  float(parts.get("stdev", 0)),
                    "cv_pct":    float(parts.get("cv", 0)),
                    "trials":    int(parts["trials"]),
                    "trimmed":   int(parts.get("trimmed", int(parts["trials"]))),
                }
        return None
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        return None
    except Exception:
        tmp.unlink(missing_ok=True)
        return None


def run_benchmark(
    file_path: str,
    func_name: str,
    trials: int = 5,
    warmup: int = 2,
    timeout: float = 30.0,
    precise: bool = False,
    repeat: int = 1,
) -> Optional[dict]:
    if precise:
        trials  = PRECISE_TRIALS
        warmup  = PRECISE_WARMUP
        timeout = PRECISE_TIMEOUT
        trim    = PRECISE_TRIM
    else:
        trim = 0.0

    harness = _HARNESS_TEMPLATE.format(
        file_path=str(Path(file_path).resolve()),
        func_name=func_name,
        trials=trials,
        warmup=warmup,
        trim_pct=trim,
        precise=precise,
    )

    best = None
    for _ in range(max(1, repeat)):
        r = _run_harness_once(harness, timeout)
        if r is None:
            continue
        if best is None or r["median_ms"] < best["median_ms"]:
            best = r

    if best is None:
        return None
    return {**best, "precise": precise, "repeat": repeat}


# ══════════════════════════════════════════════════════════════════════════
# CORE LOGIC
# ══════════════════════════════════════════════════════════════════════════

def benchmark_function(
    file_path: str,
    func_name: str,
    cfg: dict,
    compare: bool = True,
    hook_mode: bool = False,
    precise: bool = False,
    repeat: int = 1,
) -> dict:
    trials = bench_trials(cfg)
    warmup = bench_warmup(cfg)

    result = run_benchmark(
        file_path, func_name,
        trials=trials, warmup=warmup,
        precise=precise, repeat=repeat,
    )
    if result is None:
        return {"error": True, "file": file_path, "func": func_name}

    commit = git_commit_hash()
    branch = git_branch()
    py_ver = platform.python_version()

    prev = get_benchmark_last(func_name, file_path) if compare else None
    speedup_vs_prev = None
    if prev:
        speedup_vs_prev = prev["median_ms"] / result["median_ms"]

    save_benchmark(
        function_name   = func_name,
        file_path       = file_path,
        commit_hash     = commit,
        branch          = branch,
        median_ms       = result["median_ms"],
        p95_ms          = result["p95_ms"],
        min_ms          = result["min_ms"],
        max_ms          = result["max_ms"],
        trials          = result["trials"],
        speedup_vs_prev = speedup_vs_prev,
        python_version  = py_ver,
    )

    outcome = {
        "error":           False,
        "file":            file_path,
        "func":            func_name,
        "median_ms":       result["median_ms"],
        "p95_ms":          result["p95_ms"],
        "stdev_ms":        result.get("stdev_ms", 0.0),
        "cv_pct":          result.get("cv_pct", 0.0),
        "speedup_vs_prev": speedup_vs_prev,
        "prev_median_ms":  prev["median_ms"] if prev else None,
    }

    nf = noise_floor(cfg)
    if speedup_vs_prev is None:
        outcome["status"] = "baseline"
    elif speedup_vs_prev < (1.0 - nf - regression_block(cfg)):
        outcome["status"] = "regression_block"
    elif speedup_vs_prev < (1.0 - nf - regression_warn(cfg)):
        outcome["status"] = "regression_warn"
    elif speedup_vs_prev > (1.0 + nf):
        outcome["status"] = "improvement"
    else:
        outcome["status"] = "ok"

    return outcome


# ══════════════════════════════════════════════════════════════════════════
# OUTPUT FORMATTING
# ══════════════════════════════════════════════════════════════════════════

def _fmt_ms(ms: float) -> str:
    if ms < 1:
        return f"{ms*1000:.1f} µs"
    if ms < 1000:
        return f"{ms:.3f} ms"
    return f"{ms/1000:.2f} s"


def _fmt_change(speedup: Optional[float]) -> str:
    if speedup is None:
        return DIM("─ baseline")
    pct = (speedup - 1.0) * 100
    if speedup < 0.75:
        return RED(f"▲ {abs(pct):.1f}% SLOWER")
    if speedup < 0.90:
        return YELLOW(f"▲ {abs(pct):.1f}% slower")
    if speedup > 1.10:
        return GREEN(f"▼ {pct:.1f}% faster")
    return DIM(f"~ {pct:+.1f}%")


def print_table(outcomes: list[dict], commit: str, branch: str, precise: bool = False):
    print()
    mode_tag = DIM("  [precise mode]") if precise else ""
    print(BOLD(f"  ACENLY Bench") + DIM(f"  ·  {branch}@{commit[:8]}") + mode_tag)
    print()

    col1 = max(len(o.get("func", "?")) for o in outcomes) + 2
    if precise:
        header = (
            f"  {'Function':<{col1}}  {'Median':>10}  {'P95':>10}  "
            f"{'StdDev':>10}  {'CV%':>6}  {'vs last'}"
        )
    else:
        header = f"  {'Function':<{col1}}  {'Median':>10}  {'P95':>10}  {'vs last commit'}"
    print(DIM(header))
    print(DIM("  " + "─" * (len(header) - 2)))

    any_block = False
    for o in outcomes:
        if o.get("error"):
            print(f"  {RED(o['func']):<{col1}}  {'ERROR':>10}")
            continue

        fn     = o["func"]
        median = _fmt_ms(o["median_ms"])
        p95    = _fmt_ms(o["p95_ms"])
        change = _fmt_change(o.get("speedup_vs_prev"))
        status = o.get("status", "ok")

        fn_col = (
            RED(fn)    if "block" in status else
            YELLOW(fn) if "warn"  in status else
            WHITE(fn)
        )

        if precise:
            stdev   = _fmt_ms(o.get("stdev_ms", 0))
            cv      = f"{o.get('cv_pct', 0):.2f}%"
            cv_pct  = o.get("cv_pct", 0)
            cv_col  = GREEN(cv) if cv_pct < 5 else (YELLOW(cv) if cv_pct < 15 else RED(cv))
            print(f"  {fn_col:<{col1}}  {median:>10}  {p95:>10}  {stdev:>10}  {cv_col:>6}  {change}")
        else:
            print(f"  {fn_col:<{col1}}  {median:>10}  {p95:>10}  {change}")

        if "block" in status:
            any_block = True

    print()
    return any_block


# ══════════════════════════════════════════════════════════════════════════
# HISTORY VIEW
# ══════════════════════════════════════════════════════════════════════════

def print_history(cfg: dict):
    fns = tracked_functions(cfg)
    if not fns:
        print(YELLOW("  No tracked functions in acenly.yml"))
        return

    for entry in fns:
        fp   = entry.get("file", "")
        fn   = entry.get("function", "")
        rows = get_benchmark_history(fn, fp, limit=10)
        if not rows:
            print(DIM(f"\n  {fn}  —  no history yet"))
            continue

        print(f"\n  {BOLD(fn)}  {DIM(fp)}")
        for r in rows:
            ch = _fmt_change(r.get("speedup_vs_prev"))
            ts = r["created_at"][:16].replace("T", " ")
            print(f"    {DIM(ts)}  {_fmt_ms(r['median_ms']):>10}  {ch}  {DIM(r['commit_hash'][:8])}")
    print()


# ══════════════════════════════════════════════════════════════════════════
# GIT HOOKS
# ══════════════════════════════════════════════════════════════════════════

def install_hooks():
    git_dir  = ROOT / ".git"
    if not git_dir.exists():
        print(RED("  Not a git repository."))
        return

    hook_src = HOOKS_DIR / "pre-push.sh"
    hook_dst = git_dir / "hooks" / "pre-push"

    if not hook_src.exists():
        print(RED(f"  Hook script not found: {hook_src}"))
        return

    shutil.copy(hook_src, hook_dst)
    hook_dst.chmod(hook_dst.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    print(GREEN("  ✓ pre-push hook installed"))
    print(DIM("    Benchmarks will run automatically before every push."))
    print(DIM("    Use --skip-hooks to bypass when needed."))


def uninstall_hooks():
    hook_dst = ROOT / ".git" / "hooks" / "pre-push"
    if hook_dst.exists():
        hook_dst.unlink()
        print(GREEN("  ✓ pre-push hook removed"))
    else:
        print(DIM("  No hook installed."))


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════

def main():
    args = sys.argv[1:]
    cfg  = load_config()

    hook_mode    = "--hook-mode"     in args
    compare      = "--compare"       in args or hook_mode
    history      = "--history"       in args
    skip_hooks   = "--skip-hooks"    in args
    do_install   = "--install-hooks" in args
    do_uninstall = "--uninstall-hooks" in args
    precise      = "--precise"       in args

    repeat = 1
    if "--repeat" in args:
        idx = args.index("--repeat")
        try:
            repeat = int(args[idx + 1])
        except (IndexError, ValueError):
            repeat = 5
    elif precise:
        repeat = 5

    _skip_next  = False
    targets_arg = []
    for a in args:
        if _skip_next:
            _skip_next = False
            continue
        if a == "--repeat":
            _skip_next = True
            continue
        if not a.startswith("--"):
            targets_arg.append(a)

    if do_install:
        install_hooks(); return

    if do_uninstall:
        uninstall_hooks(); return

    if history:
        print_history(cfg); return

    if targets_arg:
        targets = []
        for t in targets_arg:
            if "::" in t:
                fp, fn = t.split("::", 1)
            else:
                print(RED(f"  Invalid target format '{t}'. Use file.py::func_name"))
                sys.exit(1)
            targets.append({"file": fp, "function": fn})
    else:
        targets = tracked_functions(cfg)
        if not targets:
            print(YELLOW("  No tracked functions found in acenly.yml"))
            print(DIM("  Add functions under benchmark.track or run:"))
            print(DIM("  python3 bench.py myfile.py::my_function"))
            sys.exit(0)

    if hook_mode and not skip_hooks:
        changed = git_changed_files()
        if changed:
            targets = [t for t in targets if t.get("file", "") in changed]
        if not targets:
            sys.exit(0)

    commit = git_commit_hash()
    branch = git_branch()

    if not hook_mode:
        print()
        print(BOLD("  ACENLY Bench"))
        print(DIM(f"  {branch}@{commit[:8]}  ·  Python {platform.python_version()}"))
        if precise:
            print(YELLOW(f"  ⚡ Precise mode  (repeat={repeat} · 3 s window per run)"))
        print()

    outcomes = []
    for entry in targets:
        fp = entry.get("file", "")
        fn = entry.get("function", "")
        if not hook_mode:
            hint = " (precise)" if precise else ""
            print(DIM(f"  Benchmarking {fn}{hint}..."), end="", flush=True)

        outcome = benchmark_function(
            fp, fn, cfg,
            compare=compare, hook_mode=hook_mode,
            precise=precise, repeat=repeat,
        )
        outcomes.append(outcome)

        if not hook_mode:
            status = outcome.get("status", "error")
            icon   = "✓" if status in ("ok", "baseline", "improvement") else "⚠" if "warn" in status else "✗"
            col    = GREEN if icon == "✓" else YELLOW if icon == "⚠" else RED
            print(f"\r  {col(icon)} {fn}")

    any_block = print_table(outcomes, commit, branch, precise=precise)

    # ── optimizer waitlist (shown once after first successful run) ─────────
    if not hook_mode:
        _flag = ROOT / ".acenly_waitlist_seen"
        if not _flag.exists():
            print()
            print(DIM("  ─────────────────────────────────────────────────────"))
            print(YELLOW("  Found something slow? ACENLY can rewrite it for you."))
            print()
            print(f"  The Optimizer rewrites functions algorithmically —")
            print(f"  O(n²) → O(n), correctness-verified, pure Python output.")
            print()
            print(BOLD("  → acenly.com/optimize") + DIM("  (join the waitlist)"))
            print(DIM("  ─────────────────────────────────────────────────────"))
            print()
            _flag.touch()

    if hook_mode and not skip_hooks and any_block:
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
