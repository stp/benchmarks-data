#!/usr/bin/env python3
"""Check the published tree is internally consistent. Run in CI before deploy.

The website reads campaigns.json and then fetches the files it names, so a
campaign listed without its summary is a 404 on a live page rather than a
failed build. This is also where a local path would be caught if it survived
publish.py -- once a deploy has happened, taking it back means rewriting a
public history.

No arguments; run from a checkout.
"""

import gzip
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    errs = []
    index_path = os.path.join(REPO, "data", "campaigns.json")
    if not os.path.exists(index_path):
        sys.exit("data/campaigns.json is missing: nothing to publish")
    with open(index_path) as fh:
        campaigns = json.load(fh)
    if not campaigns:
        errs.append("data/campaigns.json lists no campaigns")

    for c in campaigns:
        name = c["name"]
        required = [f"data/summary/{name}.json", f"data/failures/{name}.json"]
        if c.get("has_detail"):
            required.append(f"data/detail/{name}.json.gz")
        for rel in required:
            if not os.path.exists(os.path.join(REPO, rel)):
                errs.append(f"{name}: campaigns.json names it, but {rel} is missing")

        # The binary lives on a release, so what has to be here is the
        # provenance that identifies it and says where to fetch it. A campaign
        # naming a binary nobody can locate is not reproducible, which is the
        # one property the whole provenance apparatus exists to provide.
        sha = c.get("binary_sha256")
        if sha:
            prov_path = os.path.join(REPO, "binaries", f"{sha}.json")
            if not os.path.exists(prov_path):
                errs.append(f"{name}: no binaries/{sha[:12]}….json for the "
                            "binary it names")
            else:
                with open(prov_path) as fh:
                    prov = json.load(fh)
                if not prov.get("download_url"):
                    errs.append(f"{name}: binaries/{sha[:12]}….json has no "
                                "download_url, so the binary cannot be found")
                elif prov.get("binary_sha256") != sha:
                    errs.append(f"{name}: binaries/{sha[:12]}….json records a "
                                "different binary than the campaign did")

    # Every gzipped file must read through plain gzip. An output log appended
    # to across a crash does not, and that is exactly the state this repo must
    # never publish.
    for root, dirs, files in os.walk(REPO):
        # Prune rather than filter: a scan that walks into .git reads pack
        # files, and one that walks into __pycache__ reports byte-compiled
        # copies of these very scripts as path leaks.
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if not f.endswith(".gz"):
                continue
            p = os.path.join(root, f)
            try:
                with gzip.open(p, "rb") as fh:
                    while fh.read(1 << 20):
                        pass
            except Exception as e:
                errs.append(f"{os.path.relpath(p, REPO)}: not readable as gzip ({e})")

    # Belt to publish.py's braces.
    for root, dirs, files in os.walk(REPO):
        # Prune rather than filter: a scan that walks into .git reads pack
        # files, and one that walks into __pycache__ reports byte-compiled
        # copies of these very scripts as path leaks.
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith(".stp"):
                continue
            p = os.path.join(root, f)
            if os.path.relpath(p, REPO) in ("scripts/publish.py",
                                            "scripts/export.py",
                                            "scripts/check-published.py"):
                continue        # they name the roots they strip
            opener = gzip.open if f.endswith(".gz") else open
            try:
                with opener(p, "rt", errors="replace") as fh:
                    for line in fh:
                        if "/home/" in line:
                            errs.append(f"{os.path.relpath(p, REPO)}: "
                                        "contains a local path")
                            break
            except Exception:
                continue        # binary or unreadable; not our business here

    if errs:
        for e in errs:
            print("error: " + e, file=sys.stderr)
        sys.exit(1)
    print(f"{len(campaigns)} campaign(s), all files present and readable")


if __name__ == "__main__":
    main()
