#!/usr/bin/env python3
"""Export campaign results as static JSON for the results website.

The SQLite store is the working format; this is the published one. They are
deliberately different: a binary DB rewritten every campaign would bloat a git
history, while the site only ever needs aggregates plus one detail file.

Everything written here is self-contained and dependency-free, suitable for
GitHub Pages:

    campaigns.json              one entry per campaign: provenance + headline
    summary/<campaign>.json     per-logic and per-family aggregates
    detail/<campaign>.json.gz   per-file class/wall/peak RSS (gzip; browsers
                                decompress it with DecompressionStream)
    regressions/<campaign>.json diff against the previous campaign
    runs/<campaign>.jsonl.gz    every column of every run -- the archival form
    corpus.jsonl.gz             the corpus index the runs are keyed to

The last two exist so the DB does not have to be published to make the data
complete. Together they carry everything the `run` and `benchmark` tables hold,
as append-only text: a campaign adds a file rather than rewriting 90 MB of
SQLite, and a reviewer can read the diff. detail/ stays because it is the
subset the site charts, small enough to fetch in a page.

Usage:
    export.py --out DIR [--db DB] [--campaign NAME] [--detail-tier full]
"""

import argparse
import gzip
import json
import os
import sqlite3
import statistics
import sys

import outputlog

DEFAULT_DB = os.path.expanduser("~/data/stpbench/results.db")

# Classes that count as the solver having decided the instance.
SOLVED = ("sat", "unsat", "mixed")
# Excluded from headline metrics: STP cannot express these, so counting them as
# failures would misreport a capability gap as a performance result.
EXCLUDED = ("unsupported",)


# The exported JSON is meant to be published. Nothing in it should carry this
# machine's directory layout -- not the corpus root, not which worktree built
# the binary, not where the SAT solver checkouts live.
CORPUS_ROOTS = (os.path.expanduser("~/data/non-incremental"),
                os.path.expanduser("~/data/incremental"),
                os.path.expanduser("~/data"))


def scrub_path(path):
    """Corpus-relative benchmark identity, e.g. non-incremental/QF_BV/fam/x.smt2."""
    if not path:
        return path
    for root in CORPUS_ROOTS:
        parent = os.path.dirname(root.rstrip("/"))
        if path.startswith(parent + "/"):
            return path[len(parent) + 1:]
    return os.path.basename(path)


def scrub_tier(tier):
    """A tier is 'fast', 'full', or the path of an ad-hoc file list.

    The third kind is a local path, so it never travels. Ad-hoc campaigns are
    excluded from the published set anyway (see --tiers); this is the belt to
    that braces, for the case where one is published deliberately.
    """
    return tier if tier in ("fast", "full") else "custom"


def scrub_solvers(solvers):
    """Keep the identity of each linked solver, drop where it lived.

    The sha256 is what actually pins the artefact, and it travels fine; the
    absolute path is local trivia that would leak the machine's layout.
    """
    out = {}
    for name, info in (solvers or {}).items():
        clean = {k: v for k, v in info.items() if k != "path"}
        if "path" in info:
            clean["artefact"] = os.path.basename(info["path"])
        out[name] = clean
    return out


def par2(rows, timeout_s):
    """PAR-2: unsolved instances are charged twice the budget.

    Errors are charged like timeouts, matching the existing track/ harness, so
    a crash cannot look cheaper than a slow success.
    """
    total = 0.0
    for cls, wall in rows:
        if cls in EXCLUDED:
            continue
        if cls in SOLVED and wall is not None:
            total += wall
        else:
            total += 2 * timeout_s
    return total


def aggregate(rows, timeout_s):
    walls = [w for c, w in rows if c in SOLVED and w is not None]
    counted = [r for r in rows if r[0] not in EXCLUDED]
    return {
        "n": len(rows),
        "n_counted": len(counted),
        "solved": sum(1 for c, _ in counted if c in SOLVED),
        "timeout": sum(1 for c, _ in counted if c == "timeout"),
        "memout": sum(1 for c, _ in counted if c == "memout"),
        "error": sum(1 for c, _ in counted if c == "error"),
        "mismatch": sum(1 for c, _ in counted if c == "mismatch"),
        "unsupported": sum(1 for c, _ in rows if c == "unsupported"),
        "par2": round(par2(counted, timeout_s), 1),
        # Time spent on instances that were solved.
        "wall_total": round(sum(walls), 1),
        # Time the machine actually spent, solved or not. A timeout costs the
        # full budget, so on a hard logic this is far larger than wall_total
        # and is the honest answer to "how long did this take".
        "wall_all": round(sum(w for _c, w in rows if w is not None), 1),
        "wall_median": round(statistics.median(walls), 3) if walls else None,
    }


def campaign_rows(conn, cid):
    return conn.execute(
        """SELECT b.mode, b.logic, b.family, b.path, r.class, r.wall_s,
                  r.peak_rss_kb, r.tainted
           FROM run r JOIN benchmark b ON b.id = r.benchmark_id
           WHERE r.campaign_id = ?""", (cid,)).fetchall()


def export_campaign(conn, camp, out_dir, want_detail):
    cid, name, timeout_s = camp["id"], camp["name"], camp["timeout_s"]
    rows = campaign_rows(conn, cid)
    if not rows:
        return None

    flat = [(r["class"], r["wall_s"]) for r in rows]
    by_logic, by_family = {}, {}
    for r in rows:
        key = f"{r['mode']}/{r['logic']}"
        by_logic.setdefault(key, []).append((r["class"], r["wall_s"]))
        by_family.setdefault(f"{key}/{r['family']}", []).append(
            (r["class"], r["wall_s"]))

    peaks = sorted(r["peak_rss_kb"] for r in rows if r["peak_rss_kb"])

    def pct(p):
        return peaks[min(len(peaks) - 1, int(len(peaks) * p))] if peaks else 0

    summary = {
        "campaign": name,
        "overall": aggregate(flat, timeout_s),
        "by_logic": {k: aggregate(v, timeout_s) for k, v in sorted(by_logic.items())},
        "by_family": {k: aggregate(v, timeout_s) for k, v in sorted(by_family.items())},
        "memory_kb": {"p50": pct(0.50), "p90": pct(0.90),
                      "p99": pct(0.99), "max": peaks[-1] if peaks else 0},
        "tainted": sum(1 for r in rows if r["tainted"]),
    }
    _write_json(os.path.join(out_dir, "summary", f"{name}.json"), summary)
    export_failures(conn, camp, out_dir)
    export_runs(conn, camp, out_dir)

    if want_detail:
        detail = {"campaign": name,
                  "columns": ["path", "class", "wall_s", "peak_rss_kb"],
                  "rows": [[scrub_path(r["path"]), r["class"],
                            round(r["wall_s"], 3) if r["wall_s"] is not None else None,
                            r["peak_rss_kb"]] for r in rows]}
        p = os.path.join(out_dir, "detail", f"{name}.json.gz")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with outputlog.open_deterministic(p) as fh:
            json.dump(detail, fh, separators=(",", ":"))
    return summary


def _write_jsonl_gz(path, meta, rows):
    """Header line of metadata, then one compact JSON array per row.

    Rows are arrays rather than objects because the key names repeat 160k
    times otherwise; the header names the columns once. Line-oriented so the
    file streams, and so appending a campaign never rewrites an existing one.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with outputlog.open_deterministic(path) as fh:
        fh.write(json.dumps(meta, separators=(",", ":"), sort_keys=True) + "\n")
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")


RUN_COLUMNS = ["path", "class", "answers", "wall_s", "cpu_s", "peak_rss_kb",
               "exit_code", "term_signal", "oom_killed", "tainted", "attempts",
               "started_utc"]


def export_runs(conn, camp, out_dir):
    """Every run of a campaign, every column -- the archival form.

    This is what makes publishing results.db unnecessary. detail/ drops eight
    fields that matter when a result is being questioned rather than charted:
    cpu_s (wall minus cpu is how staging was shown not to be in the
    measurement), tainted and attempts (whether the timing survived load
    gating), oom_killed with term_signal (a memory kill and a timeout kill are
    both SIGKILL), and answers, which keeps an incremental file's per-query
    verdicts that `class` collapses into one word.
    """
    rows = conn.execute(
        """SELECT b.path, r.class, r.answers_json, r.wall_s, r.cpu_s,
                  r.peak_rss_kb, r.exit_code, r.term_signal, r.oom_killed,
                  r.tainted, r.attempts, r.started_utc
           FROM run r JOIN benchmark b ON b.id = r.benchmark_id
           WHERE r.campaign_id = ?
           ORDER BY b.path""", (camp["id"],)).fetchall()

    def row(r):
        return [scrub_path(r["path"]), r["class"],
                json.loads(r["answers_json"] or "[]"),
                r["wall_s"], r["cpu_s"], r["peak_rss_kb"],
                r["exit_code"], r["term_signal"], bool(r["oom_killed"]),
                bool(r["tainted"]), r["attempts"], r["started_utc"]]

    # The DB joins a run to its saved output by `output_offset`, a per-process
    # counter. It is deliberately not published: it restarted at zero whenever
    # a campaign resumed, so full-001 has two records numbered 0..93007 and the
    # key is ambiguous for that whole overlap. publish.py re-keys the published
    # log by benchmark path, which is unique within a campaign.
    meta = {"campaign": camp["name"], "columns": RUN_COLUMNS, "n": len(rows),
            "outputs": f"outputs/{camp['name']}.jsonl.gz",
            "outputs_key": "path"}
    _write_jsonl_gz(os.path.join(out_dir, "runs", f"{camp['name']}.jsonl.gz"),
                    meta, (row(r) for r in rows))
    return len(rows)


CORPUS_COLUMNS = ["path", "mode", "logic", "family", "n_queries", "expected",
                  "size_bytes", "sha256"]


def export_corpus(conn, out_dir):
    """The benchmark index every run is keyed to.

    Written once per export rather than per campaign: it changes only when the
    corpora do. `expected` is the status each file declares, which is the
    ground truth a mismatch is judged against, and sha256 is what says the file
    behind a path is the same file a year later.
    """
    rows = conn.execute(
        """SELECT path, mode, logic, family, n_queries, expected_json,
                  size_bytes, sha256
           FROM benchmark ORDER BY path""").fetchall()

    def row(r):
        return [scrub_path(r["path"]), r["mode"], r["logic"], r["family"],
                r["n_queries"], json.loads(r["expected_json"] or "[]"),
                r["size_bytes"], r["sha256"]]

    _write_jsonl_gz(os.path.join(out_dir, "corpus.jsonl.gz"),
                    {"columns": CORPUS_COLUMNS, "n": len(rows)},
                    (row(r) for r in rows))
    return len(rows)


def export_failures(conn, camp, out_dir):
    """Every instance the campaign did not solve, grouped by logic.

    Kept separate from detail/: this is the list a person actually opens, it
    is two orders of magnitude smaller than the per-file detail (135 rows for
    a 2000-file campaign against 2000), and it is the only part of the data
    that has to load quickly on a page behind a click.
    """
    rows = conn.execute(
        """SELECT b.logic, b.mode, b.path, b.family, r.class, r.wall_s,
                  r.peak_rss_kb, b.n_queries
           FROM run r JOIN benchmark b ON b.id = r.benchmark_id
           WHERE r.campaign_id = ? AND r.class NOT IN ('sat','unsat','mixed')
           ORDER BY b.logic, b.mode, r.class, b.path""", (camp["id"],)).fetchall()

    by_logic = {}
    for r in rows:
        by_logic.setdefault(r["logic"], []).append([
            scrub_path(r["path"]), r["mode"], r["class"],
            round(r["wall_s"], 1) if r["wall_s"] is not None else None,
            r["peak_rss_kb"], r["family"],
        ])
    out = {
        "campaign": camp["name"],
        "timeout_s": camp["timeout_s"],
        "mem_limit_bytes": camp["mem_limit_bytes"],
        "columns": ["path", "mode", "class", "wall_s", "peak_rss_kb", "family"],
        "by_logic": by_logic,
        "total": len(rows),
    }
    _write_json(os.path.join(out_dir, "failures", f"{camp['name']}.json"), out)
    return len(rows)


def export_regressions(conn, prev, curr, out_dir):
    """Diff two campaigns on the files they share.

    Restricted to the shared set on purpose: a file only one campaign ran says
    nothing about a change in STP, and including it would show manifest edits
    as performance movement.
    """
    def fetch(cid):
        return {r["path"]: r for r in conn.execute(
            """SELECT b.path, r.class, r.wall_s FROM run r
               JOIN benchmark b ON b.id = r.benchmark_id
               WHERE r.campaign_id = ? AND r.tainted = 0""", (cid,))}

    a, b = fetch(prev["id"]), fetch(curr["id"])
    # paths here are keys only; scrub on output below
    shared = set(a) & set(b)
    gained, lost, slower, faster = [], [], [], []
    for p in sorted(shared):
        ca, cb = a[p]["class"], b[p]["class"]
        if ca not in SOLVED and cb in SOLVED:
            gained.append(p)
        elif ca in SOLVED and cb not in SOLVED:
            lost.append(p)
        elif ca in SOLVED and cb in SOLVED:
            wa, wb = a[p]["wall_s"], b[p]["wall_s"]
            if wa and wb and wa > 1.0:      # ignore noise on sub-second files
                ratio = wb / wa
                if ratio > 1.5:
                    slower.append([p, round(wa, 2), round(wb, 2), round(ratio, 2)])
                elif ratio < 0.667:
                    faster.append([p, round(wa, 2), round(wb, 2), round(ratio, 2)])
    out = {
        "from": prev["name"], "to": curr["name"],
        "shared": len(shared),
        "gained": [scrub_path(x) for x in gained],
        "lost": [scrub_path(x) for x in lost],
        "slower": [[scrub_path(r[0])]+r[1:] for r in sorted(slower, key=lambda r: -r[3])[:200]],
        "faster": [[scrub_path(r[0])]+r[1:] for r in sorted(faster, key=lambda r: r[3])[:200]],
        "mismatches": [scrub_path(p) for p in sorted(b) if b[p]["class"] == "mismatch"],
    }
    _write_json(os.path.join(out_dir, "regressions", f"{curr['name']}.json"), out)
    return out


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"), sort_keys=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", required=True, help="output directory for the site data")
    ap.add_argument("--campaign", default=None,
                    help="export only these campaigns (comma-separated)")
    # An ad-hoc campaign -- one run over a hand-written file list -- is an
    # experiment, not a result about STP: 'flatten off over 200 hard instances'
    # solves 5 of 190 and would read as a catastrophic regression next to a
    # full sweep. Published campaigns are the standing tiers unless one is
    # named explicitly.
    ap.add_argument("--tiers", default="fast,full",
                    help="comma-separated tiers to publish, or 'all'")
    ap.add_argument("--detail-tier", default="full",
                    help="write per-file detail for campaigns of this tier "
                         "('all' for every campaign, 'none' for no detail)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"no results DB at {args.db}")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    camps = conn.execute(
        "SELECT * FROM campaign ORDER BY COALESCE(commit_date, started_utc)"
    ).fetchall()
    if args.campaign:
        want = set(args.campaign.split(","))
        missing = want - {c["name"] for c in camps}
        if missing:
            sys.exit("no campaign named " + ", ".join(sorted(missing)))
        camps = [c for c in camps if c["name"] in want]
    elif args.tiers != "all":
        want_tiers = set(args.tiers.split(","))
        skipped = [c["name"] for c in camps if c["tier"] not in want_tiers]
        camps = [c for c in camps if c["tier"] in want_tiers]
        for name in skipped:
            print(f"  skipping {name}: not a published tier")

    index = []
    prev = None
    for c in camps:
        want = (args.detail_tier == "all"
                or (args.detail_tier != "none" and c["tier"] == args.detail_tier))
        s = export_campaign(conn, c, args.out, want)
        if s is None:
            continue
        index.append({
            "name": c["name"],
            "tier": scrub_tier(c["tier"]),
            "started_utc": c["started_utc"],
            # A campaign with no finish time is still running, so its figures
            # are a partial sweep of the corpus. The site says so rather than
            # presenting them as a completed result.
            "finished_utc": c["finished_utc"],
            "complete": c["finished_utc"] is not None,
            "commit_sha": c["commit_sha"],
            "commit_date": c["commit_date"],
            "branch": c["branch"],
            "dirty": bool(c["dirty"]),
            "timeout_s": c["timeout_s"],
            "mem_limit_bytes": c["mem_limit_bytes"],
            "jobs": c["jobs"],
            "solver_flags": c["solver_flags"],
            "notes": c["notes"],
            # Provenance travels with every point, so a step in a chart can be
            # attributed to a SAT-solver bump instead of misread as an STP
            # change -- a stale CaDiCaL silently rewrote months of results here
            # before anyone noticed.
            "binary_sha256": c["binary_sha256"],
            "stp_version": c["stp_version_raw"],
            "compile_defines": c["compile_defines"],
            "solvers": scrub_solvers(json.loads(c["solvers_json"] or "{}")),
            "has_detail": want,
            "headline": s["overall"],
        })
        if prev is not None and prev["tier"] == c["tier"]:
            export_regressions(conn, prev, c, args.out)
        prev = c

    _write_json(os.path.join(args.out, "campaigns.json"), index)
    n_corpus = export_corpus(conn, args.out)
    print(f"exported {len(index)} campaigns and {n_corpus} corpus entries "
          f"to {args.out}")
    for e in index:
        h = e["headline"]
        print(f"  {e['name']:<28} {(e['commit_sha'] or '')[:9]} "
              f"solved {h['solved']}/{h['n_counted']}  PAR-2 {h['par2']:.0f}"
              + ("  MISMATCHES!" if h["mismatch"] else ""))


if __name__ == "__main__":
    main()
