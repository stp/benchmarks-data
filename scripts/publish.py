#!/usr/bin/env python3
"""Assemble the publishable tree in this repo from the local working store.

    ./scripts/publish.py [--repo DIR] [--data-root DIR] [--campaign NAME]

One command per campaign, run from a checkout of stp/benchmarks-data:

  data/       every exported JSON, via export.py
  outputs/    each campaign's solver output, repaired and re-keyed by path
  binaries/   the exact binary that produced the campaign, plus provenance

Three things happen here that export.py does not do, all of them about the
difference between a working store and a published one:

1. **Paths are scrubbed.** The provenance JSON records where the binary and
   each SAT-solver library sat on the machine that built them, and every record
   in the output log carries the absolute path of its input. None of that is
   anyone else's business, and it is not information: the sha256 beside it is
   what actually identifies the artefact.

2. **Output logs are repaired.** See outputlog.py -- a killed campaign leaves a
   log that standard gzip gives up on early. It is rewritten here as one clean
   member so what is published can be read with `zcat`.

3. **The binary is verified before it is copied.** Its sha256 has to match the
   name it is archived under and the hash the campaign recorded, or the two
   halves of the claim "this binary produced these numbers" have come apart.
"""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys

import outputlog

DEFAULT_DATA_ROOT = os.path.expanduser("~/data/stpbench")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The same roots export.py strips, for the same reason.
CORPUS_ROOTS = (os.path.expanduser("~/data/non-incremental"),
                os.path.expanduser("~/data/incremental"),
                os.path.expanduser("~/data"))


def scrub_path(path):
    if not path:
        return path
    for root in CORPUS_ROOTS:
        parent = os.path.dirname(root.rstrip("/"))
        if path.startswith(parent + "/"):
            return path[len(parent) + 1:]
    return os.path.basename(path)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def publish_outputs(data_root, repo, name):
    """Repair, scrub and copy one campaign's output log.

    Re-keyed by benchmark path: the `i` field is a per-process counter that
    restarted at zero on resume, so it names two records in any campaign that
    was interrupted. Dropping it costs nothing -- runs/<campaign>.jsonl.gz is
    keyed by path too.
    """
    src = os.path.join(data_root, "outputs", f"{name}.jsonl.gz")
    if not os.path.exists(src):
        print(f"  outputs: none at {src}")
        return None
    records, st = outputlog.read_recovered(src)
    dst = os.path.join(repo, "outputs", f"{name}.jsonl.gz")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    seen = set()
    dupes = 0
    with gzip.open(dst, "wt", encoding="utf-8") as fh:
        for d in records:
            p = scrub_path(d.get("path"))
            if p in seen:
                # A retried run writes a second record for the same benchmark.
                # The later one is the one the DB kept, so it wins.
                dupes += 1
            seen.add(p)
            fh.write(json.dumps({"path": p, "class": d.get("class"),
                                 "stdout": d.get("stdout", ""),
                                 "stderr": d.get("stderr", "")},
                                separators=(",", ":")) + "\n")
    note = (f"  outputs: {st['records']} records from {st['members']} members"
            + (f", {st['damaged_members']} damaged" if st['damaged_members'] else "")
            + (f", {dupes} retried" if dupes else ""))
    print(note)
    return {"records": st["records"], "damaged_members": st["damaged_members"],
            "unparsable": st["unparsable"], "retried": dupes}


def publish_binary(data_root, repo, sha):
    """Copy the archived binary and its provenance, with the paths taken out."""
    if not sha:
        print("  binary: campaign recorded none (dynamic build?)")
        return None
    src = os.path.join(data_root, "binaries", f"{sha}.stp")
    if not os.path.exists(src):
        print(f"  binary: MISSING from the archive ({sha[:12]})")
        return None

    got = sha256_file(src)
    if got != sha:
        sys.exit(f"binary {src} hashes to {got}, archived as {sha} -- refusing "
                 "to publish a binary that is not the one the campaign ran")

    dst = os.path.join(repo, "binaries", f"{sha}.stp")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.exists(dst):
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o755)

    prov_src = os.path.join(data_root, "binaries", f"{sha}.json")
    if os.path.exists(prov_src):
        with open(prov_src) as fh:
            prov = json.load(fh)
        # Where it sat on the build machine is not part of its identity.
        prov.pop("binary", None)
        prov.pop("build_dir", None)
        for s in (prov.get("solvers") or {}).values():
            s.pop("path", None)
        with open(os.path.join(repo, "binaries", f"{sha}.json"), "w") as fh:
            json.dump(prov, fh, indent=2, sort_keys=True)
            fh.write("\n")
    print(f"  binary: {sha[:12]} ({os.path.getsize(dst) / 1e6:.1f} MB), hash verified")
    return sha


def publishable(db, data_root, tiers):
    """Campaigns of a published tier whose binary is in the archive.

    A campaign nobody can re-run is not a result anyone can check, and the
    whole apparatus around provenance here exists to make the numbers
    checkable. In practice this excludes the dynamically linked baselines: the
    runner declines to archive those, because a copied dynamic binary resolves
    libstp through RUNPATH back to a build tree that has since moved on.
    """
    import sqlite3
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    want = set(tiers.split(","))
    names = []
    for c in conn.execute("SELECT name, tier, binary_sha256 FROM campaign "
                          "ORDER BY COALESCE(commit_date, started_utc)"):
        if c["tier"] not in want:
            print(f"  skipping {c['name']}: not a published tier")
            continue
        sha = c["binary_sha256"]
        if not sha or not os.path.exists(
                os.path.join(data_root, "binaries", f"{sha}.stp")):
            print(f"  skipping {c['name']}: no archived binary, so the "
                  "campaign cannot be reproduced")
            continue
        names.append(c["name"])
    conn.close()
    return names


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=REPO, help="checkout to write into")
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--campaign", default=None,
                    help="publish only this campaign (default: every "
                         "published-tier campaign)")
    ap.add_argument("--tiers", default="fast,full")
    args = ap.parse_args()

    db = os.path.join(args.data_root, "results.db")
    export = os.path.join(os.path.dirname(os.path.abspath(__file__)), "export.py")
    cmd = [sys.executable, export, "--out", os.path.join(args.repo, "data"),
           "--db", db]
    if args.campaign:
        cmd += ["--campaign", args.campaign]
    else:
        names = publishable(db, args.data_root, args.tiers)
        if not names:
            sys.exit("no campaign is publishable")
        cmd += ["--campaign", ",".join(names)]
    print("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True)

    with open(os.path.join(args.repo, "data", "campaigns.json")) as fh:
        campaigns = json.load(fh)

    for c in campaigns:
        print(f"{c['name']}:")
        publish_outputs(args.data_root, args.repo, c["name"])
        publish_binary(args.data_root, args.repo, c.get("binary_sha256"))

    # Anything the scrubbers missed is a leak, and it is cheaper to fail here
    # than to rewrite a public history.
    leaked = []
    for sub in ("data", "outputs", "binaries"):
        d = os.path.join(args.repo, sub)
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.endswith(".stp"):
                    continue        # a binary legitimately contains anything
                p = os.path.join(root, f)
                opener = gzip.open if f.endswith(".gz") else open
                with opener(p, "rt", errors="replace") as fh:
                    for line in fh:
                        if "/home/" in line or "/tmp/" in line:
                            leaked.append(p)
                            break
    if leaked:
        sys.exit("local paths leaked into: " + ", ".join(sorted(set(leaked))))
    print("no local paths in the published tree")


if __name__ == "__main__":
    main()
