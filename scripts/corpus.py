#!/usr/bin/env python3
"""Corpus discovery and manifest building for stpbench.

Walks the local SMT-LIB trees, records each benchmark's identity (logic,
family, size, sha256) and its expected answers, and builds the tier manifests
the runner sweeps.

Expected answers come from `(set-info :status ...)`. Two traps this deliberately
avoids, both of which have produced wrong tables here before:

  * Benchmarks declare variables named like `|foo:status@3|`, so a bare grep for
    ':status' picks up phantom hits. The regex is anchored on `set-info`.
  * Incremental files carry one :status per query, not one per file (one file
    has 202). Statuses are collected as an ordered list, not a scalar.

Usage:
    corpus.py scan     [--db DB] [--jobs N] [--root DIR ...]
    corpus.py manifest [--db DB] --tier fast --out FILE [--target N]
    corpus.py stats    [--db DB]
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.expanduser("~/data/stpbench/results.db")
DEFAULT_ROOTS = [
    os.path.expanduser("~/data/non-incremental"),
    os.path.expanduser("~/data/incremental"),
]


def corpus_relative(path):
    """Strip the local corpus root: '<root>/QF_BV/f/x.smt2' -> 'QF_BV/f/x.smt2',
    keeping the root's own name so 'non-incremental/...' still says which
    corpus. Paths written to a manifest go through this; stpbench.load_tier
    puts the local root back."""
    for root in DEFAULT_ROOTS:
        parent = os.path.dirname(root.rstrip("/"))
        if path.startswith(parent + "/"):
            return path[len(parent) + 1:]
    return path

# Anchored on set-info so that variables named |...:status@3| cannot match.
RE_STATUS = re.compile(rb"set-info\s*:status\s+([a-z]+)")
# (check-sat) and (check-sat-assuming ...); not check-sat-assuming's cousin
# "check-allsat", which STP does not accept anyway.
RE_CHECKSAT = re.compile(rb"\(\s*check-sat(-assuming)?[\s()]")

VALID_STATUS = {b"sat", b"unsat", b"unknown"}

# Bytes held back at each chunk boundary so a token split across two reads is
# still matched whole. Comfortably longer than any pattern above.
CARRY = 256

# Bump whenever extraction changes meaning, so `scan` knows which rows are
# stale. Rows carry the version that produced them; --resume skips only those
# already at the current one, which makes a 20-minute pass restartable instead
# of all-or-nothing.
#   1: initial
#   2: fixed chunk-boundary double-count and truncation (see scan_one)
SCAN_VERSION = 2


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_path(path, root):
    """<root>/<LOGIC>/<family>/... -> (mode, logic, family)."""
    mode = os.path.basename(root.rstrip("/"))
    rel = os.path.relpath(path, root).split(os.sep)
    logic = rel[0] if rel else "?"
    family = rel[1] if len(rel) > 2 else "(root)"
    return mode, logic, family


def scan_one(args):
    """Read a file once: hash it, count queries, collect expected statuses."""
    path, root = args
    try:
        h = hashlib.sha256()
        statuses = []
        n_queries = 0
        size = 0
        # Streaming scan over a rolling overlap, with two independent guards.
        # Both are needed, and getting either wrong corrupts results silently:
        #
        #  * A token straddling a chunk boundary must not be consumed while
        #    truncated -- `:status unsat` cut after `uns` would either be lost
        #    or, worse, matched short. So a match reaching past the safe limit
        #    is deferred to the next round, where the overlap holds it whole.
        #  * The overlap is rescanned, so a match inside it would be counted
        #    twice. Absolute-offset bookkeeping rejects anything already taken.
        #
        # Neither is hypothetical. The double count inflated a 10228-query file
        # to 10229 statuses, shifting the expected-answer list and reporting a
        # false soundness mismatch; the truncation dropped the status of 156
        # QF_BV files whose `:status` happened to land near a 1MB boundary.
        buf = b""
        base = 0            # absolute offset of buf[0]
        taken = {}          # regex id -> absolute offset just past last match
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                h.update(chunk)
                size += len(chunk)
                buf += chunk
                last = not chunk
                limit = len(buf) if last else max(0, len(buf) - CARRY)

                cut = limit
                for rx in (RE_STATUS, RE_CHECKSAT):
                    upto = taken.get(id(rx), 0)
                    for m in rx.finditer(buf):
                        if m.end() > limit:
                            # Truncated. Retry next round, and make sure the
                            # carry reaches back far enough to hold it whole --
                            # a fixed-size carry is not enough, because a match
                            # ending just past the limit starts earlier still.
                            cut = min(cut, m.start())
                            break
                        if base + m.start() < upto:
                            continue            # already counted in the overlap
                        upto = base + m.end()
                        if rx is RE_STATUS:
                            if m.group(1) in VALID_STATUS:
                                statuses.append(m.group(1).decode())
                        else:
                            n_queries += 1
                    taken[id(rx)] = upto

                if last:
                    break
                cut = max(0, cut)
                base += cut
                buf = buf[cut:]

            # Drop this file from the page cache again.
            #
            # Without this the scan pulls 58GB of corpus through the cache and
            # the resulting sustained reclaim drives PSI memory pressure on
            # user@.service past systemd-oomd's threshold (50% for 20s). oomd
            # then kills the largest scopes in that slice -- it took out a
            # browser, gnome-shell and two terminals here before the cause was
            # found. Note free memory looks healthy throughout, because page
            # cache is reclaimable; oomd watches pressure, not free bytes, and
            # fires before the kernel OOM killer, so dmesg stays empty too.
            try:
                os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except (OSError, AttributeError):
                pass
        mode, logic, family = classify_path(path, root)
        return (path, mode, logic, family, n_queries,
                json.dumps(statuses), size, h.hexdigest(), None)
    except OSError as e:
        return (path, None, None, None, 0, "[]", 0, "", str(e))


def iter_files(roots, logics=None):
    for root in roots:
        if not os.path.isdir(root):
            print(f"warning: {root} does not exist, skipping", file=sys.stderr)
            continue
        for dirpath, dirs, names in os.walk(root):
            if logics and dirpath == root:
                dirs[:] = [d for d in dirs if d in logics]
            for n in names:
                if n.endswith(".smt2"):
                    yield (os.path.join(dirpath, n), root)


def connect(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=60)
    with open(os.path.join(HERE, "schema.sql")) as fh:
        conn.executescript(fh.read())
    # CREATE TABLE IF NOT EXISTS will not add columns to a DB made by an older
    # version, so migrate explicitly.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(benchmark)")}
    if "scan_version" not in cols:
        conn.execute("ALTER TABLE benchmark ADD COLUMN scan_version "
                     "INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    return conn


def cmd_scan(args):
    conn = connect(args.db)
    files = list(iter_files(args.roots, set(args.logics) if args.logics else None))
    total = len(files)
    if not args.rescan_all:
        fresh = {r[0] for r in conn.execute(
            "SELECT path FROM benchmark WHERE scan_version = ?", (SCAN_VERSION,))}
        files = [f for f in files if f[0] not in fresh]
        if fresh:
            print(f"resuming: {len(fresh)} of {total} already at scan v"
                  f"{SCAN_VERSION}", flush=True)
    print(f"scanning {len(files)} files with {args.jobs} workers ...", flush=True)
    if not files:
        print("nothing to do")
        return
    done = errors = 0
    with Pool(args.jobs) as pool:
        batch = []
        for row in pool.imap_unordered(scan_one, files, chunksize=64):
            if row[8] is not None:
                errors += 1
                print(f"  read error: {row[0]}: {row[8]}", file=sys.stderr)
                continue
            batch.append(row[:8] + (utcnow(), SCAN_VERSION))
            if len(batch) >= 2000:
                # Commit as we go: a full pass reads ~58GB and takes tens of
                # minutes, so one giant transaction would mean a crash loses
                # everything and nothing is observable while it runs.
                _flush(conn, batch)
                conn.commit()
                batch.clear()
            done += 1
            if done % 5000 == 0:
                print(f"  {done}/{len(files)}", flush=True)
        if batch:
            _flush(conn, batch)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM benchmark").fetchone()[0]
    print(f"scanned {done} files ({errors} unreadable); benchmark table holds {n}")


def _flush(conn, rows):
    conn.executemany(
        """INSERT INTO benchmark
             (path, mode, logic, family, n_queries, expected_json,
              size_bytes, sha256, scanned_utc, scan_version)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET
             mode=excluded.mode, logic=excluded.logic, family=excluded.family,
             n_queries=excluded.n_queries, expected_json=excluded.expected_json,
             size_bytes=excluded.size_bytes, sha256=excluded.sha256,
             scanned_utc=excluded.scanned_utc,
             scan_version=excluded.scan_version""",
        rows,
    )


def cmd_stats(args):
    conn = connect(args.db)
    print(f"{'mode':<16} {'logic':<14} {'files':>7} {'sat':>7} {'unsat':>7} "
          f"{'unknown':>8} {'known%':>7}")
    q = """SELECT mode, logic, COUNT(*), SUM(size_bytes), expected_json
           FROM benchmark GROUP BY mode, logic"""
    # Status tallies need the JSON expanded, so do it in Python.
    rows = conn.execute(
        "SELECT mode, logic, expected_json FROM benchmark").fetchall()
    agg = {}
    for mode, logic, ej in rows:
        a = agg.setdefault((mode, logic), [0, 0, 0, 0])
        a[0] += 1
        for s in json.loads(ej):
            if s == "sat":
                a[1] += 1
            elif s == "unsat":
                a[2] += 1
            else:
                a[3] += 1
    for (mode, logic), (n, sat, unsat, unk) in sorted(agg.items()):
        tot = sat + unsat + unk
        pct = 100.0 * (sat + unsat) / tot if tot else 0.0
        print(f"{mode:<16} {logic:<14} {n:>7} {sat:>7} {unsat:>7} {unk:>8} {pct:>6.1f}%")


def cmd_manifest(args):
    """Stratified sample, frozen to a file.

    Sampling is sqrt-weighted per (mode, logic, family) so that a 20k-file
    family does not drown out a 30-file one — the point of the fast tier is
    coverage of behaviours, not proportional representation of the corpus.

    Selection within a group is by sha256 order, which is stable across runs
    and independent of directory listing order, so re-running with the same
    target reproduces the same manifest.
    """
    conn = connect(args.db)
    groups = {}
    for bid, mode, logic, family, sha in conn.execute(
            "SELECT id, mode, logic, family, sha256 FROM benchmark"):
        groups.setdefault((mode, logic, family), []).append((sha, bid))
    if not groups:
        sys.exit("benchmark table is empty -- run 'corpus.py scan' first")

    # Files worth keeping regardless of the sample: anything that has ever been
    # slow, memory-hungry, or wrong. On a fresh DB this is empty.
    forced = set()
    for (bid,) in conn.execute(
            """SELECT DISTINCT benchmark_id FROM run
               WHERE class IN ('timeout','memout','mismatch','error')
                  OR wall_s > ?""", (args.hard_seconds,)):
        forced.add(bid)

    # Pick the scale that lands closest to the target size.
    def take(scale):
        out = set()
        for key, items in groups.items():
            n = len(items)
            k = min(n, max(1, math.ceil(math.sqrt(n) * scale)))
            out.update(bid for _sha, bid in sorted(items)[:k])
        return out

    lo, hi = 0.01, 50.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if len(take(mid)) < args.target:
            lo = mid
        else:
            hi = mid
    chosen = take((lo + hi) / 2) | forced

    paths = [r[0] for r in conn.execute(
        "SELECT path FROM benchmark WHERE id IN (%s) ORDER BY path"
        % ",".join(str(i) for i in chosen))]
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write(f"# stpbench {args.tier} tier manifest\n")
        fh.write(f"# generated {utcnow()} target={args.target} "
                 f"selected={len(paths)} forced={len(forced)}\n")
        fh.write("# Frozen on purpose: the time series is only comparable while\n"
                 "# this file is unchanged. Regenerating starts a new series.\n")
        fh.write("# Paths are corpus-relative. This file is published, and it\n"
                 "# has to name the same benchmarks on any machine holding the\n"
                 "# SMT-LIB corpora, not just the one that wrote it.\n")
        for p in paths:
            fh.write(corpus_relative(p) + "\n")
    print(f"wrote {len(paths)} paths to {args.out} "
          f"({len(forced)} force-included from prior campaigns)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="walk the corpora into the benchmark table")
    s.add_argument("--root", dest="roots", action="append", default=None)
    s.add_argument("--rescan-all", action="store_true",
                   help="re-read every file even if already at the "
                        "current scan version")
    s.add_argument("--logic", dest="logics", action="append", default=None,
                   help="restrict the scan to these logic dirs")
    # Deliberately low. The corpus is on one spinning disk, so extra workers
    # buy no throughput -- they only multiply page-cache churn, which is what
    # drove systemd-oomd to start killing desktop apps.
    s.add_argument("--jobs", type=int, default=4)
    s.set_defaults(func=cmd_scan)

    m = sub.add_parser("manifest", help="build a frozen tier manifest")
    m.add_argument("--tier", default="fast")
    m.add_argument("--out", default=os.path.join(HERE, "manifests", "fast.txt"))
    m.add_argument("--target", type=int, default=6000)
    m.add_argument("--hard-seconds", type=float, default=10.0,
                   help="prior runs slower than this are always included")
    m.set_defaults(func=cmd_manifest)

    t = sub.add_parser("stats", help="per-logic counts and oracle coverage")
    t.set_defaults(func=cmd_stats)

    args = ap.parse_args()
    if getattr(args, "roots", None) is None:
        args.roots = DEFAULT_ROOTS
    args.func(args)


if __name__ == "__main__":
    main()
