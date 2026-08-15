# STP benchmark data

Measurements of [STP](https://github.com/stp/stp) over the SMT-LIB benchmarks
it can read: the exported results, the raw per-run data behind them, the exact
binary that produced each campaign, and the harness that ran it.

The pages that display all this are part of the manual, in the `stp/stp` repo,
and are published at **<https://stp.github.io/stp/benchmarks.html>**. This repo
is published too — at <https://stp.github.io/benchmarks-data/> — so those pages
fetch their data from here. Both sites are served from `stp.github.io`, so the
fetch is same-origin.

Splitting them this way keeps a campaign's output — tens of megabytes, several
times a year — out of the history of the source repo, and lets a new campaign
appear on the website without a commit to STP itself.

## Layout

| path | what |
| --- | --- |
| `data/campaigns.json` | one entry per campaign: provenance and headline figures |
| `data/summary/<c>.json` | per-logic and per-family aggregates |
| `data/failures/<c>.json` | every instance the campaign did not solve, with why |
| `data/detail/<c>.json.gz` | per-file class, wall time and peak RSS |
| `data/runs/<c>.jsonl.gz` | **every column of every run** — the archival form |
| `data/corpus.jsonl.gz` | the benchmark index the runs are keyed to |
| `outputs/<c>.jsonl.gz` | each run's stdout and stderr |
| `binaries/<sha256>.json` | provenance of the binary that produced a campaign: STP commit, compiler, linked SAT solvers, and the release to download it from |
| `scripts/` | the harness: run a campaign, export it, publish it |

`data/` is what the website reads. `runs/`, `corpus.jsonl.gz` and `outputs/`
are for anyone who wants to check a number rather than look at it.

The **binaries themselves are [releases](https://github.com/stp/benchmarks-data/releases)**,
one per distinct binary, tagged `binary-<first 12 of its sha256>`. They are not
committed: 24 MB per campaign that does not delta against the last one would
grow every clone without bound, for a file most readers never fetch. A release
asset costs neither repository size nor the site's 1 GB budget, and the 2 KB of
provenance that says *which* binary a campaign ran stays here in the history
where it belongs.

### The gzipped files

`.jsonl.gz` files are a header line naming the columns, then one compact JSON
array per row:

```console
$ zcat data/runs/full-001.jsonl.gz | head -2
{"campaign":"full-001","columns":["path","class","answers",...],"n":160610,...}
["non-incremental/QF_BV/asp/Labyrinth/laby_22_22_08.lp.smt2","timeout",[],null,...]
```

Rows are arrays because the column names would otherwise repeat 160,610 times.
They are line-oriented so a campaign appends a file instead of rewriting one,
and so a reviewer can read a diff.

The working store is a SQLite database, and it is deliberately **not**
published: rewriting a 90 MB binary file every campaign would bloat this
history for no gain. `data/runs/` plus `data/corpus.jsonl.gz` carry everything
its two tables hold, so the database can be rebuilt from what is here.

## Reproducing a campaign

Everything needed to re-run one byte-identically is published. Take the
`binary_sha256` from `data/campaigns.json`; `binaries/<that sha>.json` records
the STP commit it was built from, the compiler, every SAT solver library it was
linked against — identified by hash, because several builds of one version can
be present on a machine and only one of them was linked — and the
`download_url` of the binary itself:

```bash
sha=$(python3 -c 'import json;print(json.load(open("data/campaigns.json"))[-1]["binary_sha256"])')
url=$(python3 -c "import json;print(json.load(open('binaries/$sha.json'))['download_url'])")
curl -sL -o stp "$url" && chmod +x stp
echo "$sha  stp" | sha256sum -c        # it is the binary or it is not
```

That last line is the point of the hash. A binary that does not match is not
the one the numbers came from, whatever it is.

The corpora themselves are the public SMT-LIB non-incremental and incremental
sets; `data/corpus.jsonl.gz` gives the sha256 of every file, so a local copy
can be checked against the one that was measured.

## Publishing a campaign

From a checkout, with the working store on the same machine:

```bash
./scripts/publish.py            # export, scrub, repair, verify
git checkout -b publish-<name>
git add -A && git commit -m "Publish campaign <name>"
git push -u origin publish-<name> && gh pr create
```

Campaigns land through a pull request like anything else. Publishing is
routine, but it is the step that puts numbers in front of people, and a diff is
the last chance to notice that a campaign is half-finished or that a mismatch
went unremarked.

`publish.py` runs `export.py`, attaches the binary to its release after
checking its hash against the one the campaign recorded, and repairs and
re-keys the output log. Re-running it on unchanged data produces byte-identical
files, so a no-op publish shows an empty diff rather than a few megabytes of
churn.
It scrubs local paths out of everything — the provenance JSON records where the
binary and each solver library sat on the build machine, and every output
record carries the absolute path of its input — then fails rather than pushing
if anything it missed still looks like a local path.

Two rules decide what is published at all. A campaign over an ad-hoc file list
is an experiment, not a result about STP, so only the standing `fast` and
`full` tiers publish. And a campaign whose binary was not archived cannot be
re-run by anyone, so it does not publish either; in practice that means the
dynamically linked baselines, which the runner declines to archive because a
copied dynamic binary resolves `libstp` through RUNPATH back to a build tree
that has since moved on.

The website updates when this repo's Pages build finishes. Nothing in `stp/stp`
has to change.

## Known data damage

**`outputs/full-001.jsonl.gz` holds 154,519 of that campaign's 160,610 runs.**
A reboot interrupted the campaign at run 63,533; the output log was being
appended to as a gzip stream, and the kill left an unterminated member with the
resumed run's records appended after it. Walking the members recovers all but
about 6,000 records — the ones still in the compressor's buffer when the
machine went down. The published file has been repaired into a single clean
member, so `zcat` reads it through.

The results themselves are unaffected: they were committed to the database as
each run finished, and the campaign resumed and completed. Only the retained
stdout of those ~6,000 runs is gone, which costs the ability to re-triage
them without re-running.

Both faults behind that are fixed in `scripts/`: the log is repaired before
anything appends to it, it is flushed on the same boundary as the database
commit, and the per-run key no longer restarts at zero when a campaign resumes.

## Licences

What is written here — the harness, the exported results, this documentation —
is MIT, per `LICENSE`.

The **published binaries are a different matter**. Each is a statically linked
STP, so it embeds the third-party code STP is built from, and redistributing it
carries those licences whatever this repository's own says. They are listed,
with their full text, in
[`LICENSE_COMPONENTS`](https://github.com/stp/stp/blob/master/LICENSE_COMPONENTS)
in the STP source repository: ABC, mimalloc, Bit::Vector, the CVC SMT-LIB
parser, `ankerl::unordered_dense`, CLI11, and — depending on how the binary was
configured — SymFPU and LibBF.

The SAT backend is linked at build time and so is not in that list. Which one a
given binary carries, and at which commit, is recorded in its
`binaries/<sha256>.json`; CaDiCaL and CryptoMiniSat are both MIT.

## The harness

`scripts/README.md` documents it in full — the tiers, the load gating that
decides when a timing is trustworthy, the cgroup memory ceilings, the staging
that keeps a spinning disk out of the measurement, and the methodology rules
that were each learned by getting a measurement wrong first.
