# stpbench

Sweeps the local SMT-LIB corpora with one STP binary and records answer, wall
time and peak memory per benchmark, keyed to a git commit, so successive
campaigns form a performance time series. `publish.py` turns the accumulated
results into the static files this repository serves.

What this measures that a flag A/B does not: every supported logic, incremental
as well as non-incremental, peak memory, a real memory ceiling, load-aware
scheduling, and retained solver output.

| script | does |
| --- | --- |
| `corpus.py` | index the corpora; freeze a tier manifest |
| `stpbench.py` | run a campaign; summarise, resume, re-classify one |
| `export.py` | the DB → the published JSON |
| `publish.py` | export, then scrub, repair and verify the whole tree |
| `outputlog.py` | read and repair a campaign's output log |
| `check-published.py` | what CI runs before a deploy |

## Quick start

```bash
# 1. Index the corpora (one pass; re-run when the corpora change)
./corpus.py scan

# 2. Freeze a fast-tier sample
./corpus.py manifest --tier fast --target 6000

# 3. Run a campaign (binary must be static -- see Provenance)
./stpbench.py run --binary /path/to/static/stp --build-dir /path/to/build \
    --tier fast --timeout 300

# 4. Inspect, then publish
./stpbench.py summary
./publish.py                    # writes ../data, ../outputs, ../binaries
```

Then commit and push: the website picks the new campaign up from this repo's
Pages build, with no change to `stp/stp`. The repository README covers what
publishing does and does not include.

## Tiers

| tier | what | roughly |
| --- | --- | --- |
| `fast` | frozen stratified sample, `manifests/fast.txt` | ~6k files, ~1 h |
| `full` | every indexed benchmark | ~160k files, ~1 day |
| *path* | any file listing benchmark paths | — |

The fast manifest is stratified by (mode, logic, family) with sqrt weighting so
a 20k-file family cannot drown out a 30-file one, and it force-includes every
file that has previously been slow, memory-hungry or wrong. **It is frozen on
purpose**: the series is only comparable while the manifest is unchanged, so
regenerating it starts a new series. Say so in the campaign notes if you do.

## Methodology rules

Every one of these was learned by getting a measurement wrong first:

- **Never classify by exit code.** STP exits 0 on syntax errors and even on a
  rejected `set-logic`, which it then solves anyway (stp#861). Everything here
  parses stdout. A prior sweep that trusted `$?` reported a whole logic
  backwards.
- **Wall clock is only comparable at equal load.** That is what the load
  monitor is for; see below. Do not compare campaigns run at different `--jobs`.
- **Any mismatch invalidates the campaign.** A printed answer contradicting the
  file's `(set-info :status)` is a soundness bug, not a data point.
- **`ulimit -s 80000`** is applied to every run. ABC's recursive CNF code
  overflows the 8 MB default, and measuring at 8 MB reports harness crashes as
  properties of the solver — this invalidated every pre-2026-08-14 `track/`
  measurement.
- **`--array-equality` is not optional** for the array logics; without it ~30%
  of QF_ABV silently errors. Per-logic flags live in `manifests/logic-flags.tsv`.
- **`-s` / `--print-functionstat` changes the code path.** Never in a timed run.

## Load gating and taint

Raw `loadavg` is useless here: with 20 workers it reports 20 and says nothing
about interference. The monitor instead computes **foreign** CPU each second —
machine-busy minus our own children minus the orchestrator itself — and:

- pauses dispatch while foreign load exceeds `--max-foreign-load` (default 1.0
  core);
- marks any run whose lifetime overlapped an over-threshold sample as
  **tainted**, discards its timing, and requeues it (up to `--retries`, default
  3; after that it is recorded tainted so the campaign can finish).

A tainted run still carries a valid answer and peak memory — only the timing is
void — so it is recorded either way and overwritten if a retry succeeds.

Two clock notes, both of which were bugs before they were rules: load samples
and run lifetimes must use the *same* clock (`time.monotonic()`, not epoch), and
the orchestrator's own CPU counts as ours, not as interference.

## The corpus is on a spinning disk

`~/data` is a 7200rpm drive (`/` is an SSD but has no room for a 58 GB corpus).
One seek is milliseconds and the median easy benchmark solves in ~14 ms, so a
read inside the timed region would dominate the measurement.

Every run therefore makes its input resident *before* the timer starts:

- `--stage shm` (default) copies the file to tmpfs and runs it from there.
  tmpfs pages are not reclaimable, so nothing can evict the input between
  staging and the parse — which is the gap a plain page-cache warm leaves open
  when 20 jobs are running against a 30 GB ceiling.
- `--stage preload` just reads the file to warm the page cache.
- Files over `--stage-max-mb` (default 64) are preloaded rather than copied, so
  staging cannot eat memory the solver needs. A file that big is attached to a
  run long enough that one read does not matter.

Measured effect, sub-50 ms runs: `wall - cpu` averages 0.3 ms, max 1 ms. The
disk is not in the measurement. Copying the whole corpus to SSD was rejected —
it does not fit, and it would not improve on this.

## Memory

STP has no `--max-memory`, and `ulimit -v` is the wrong tool because stp-bin
links mimalloc and over-reserves address space. Each worker instead gets a
cgroup v2 scope with `memory.max` and `memory.swap.max = 0` (swapping would
wreck timing fidelity). The `memory` controller is delegated to the user
manager, so this needs neither root nor systemd-run.

Peak RSS comes from `wait4()` rusage, because this kernel predates
`memory.peak`. The cgroup's role is the ceiling plus `memory.events`, which is
how a memory kill is told apart from a timeout kill — both arrive as SIGKILL.

Scheduling uses that history: each file reserves 1.5× the largest peak any prior
campaign measured (default 1 GB when unmeasured), the campaign keeps total
reservations under `--mem-budget-gb`, and anything above `--solo-threshold-gb`
or previously OOM-killed runs alone, with the queue drained first so big jobs
cannot starve behind small ones. So the first campaign runs blind and every
later one schedules on real data.

## Provenance

A campaign refuses to start unless the binary is **statically linked**. A copied
shared-lib binary resolves `libstp` through RUNPATH back to whatever tree built
it, so an archived copy would silently change as that tree is rebuilt.
`--allow-dynamic` overrides this for throwaway runs.

Recorded per campaign, and carried into the exported JSON so a step in a chart
can be attributed rather than guessed at:

- the binary, archived to `~/data/stpbench/binaries/<sha256>.stp`, so any
  historical point can be re-run byte-identically;
- `stp --version` verbatim: STP SHA, compile defines, and — since #863 — the
  linked SAT solvers' own version strings;
- each linked solver library resolved from the build tree's `CMakeCache.txt`,
  with its path, sha256, `VERSION` and git SHA. This is not paranoia: there are
  several CaDiCaL checkouts on a typical box here, CMake's choice is
  machine-global via `~/.cmake/packages`, and a stale `deps/cadical` once meant
  months of `--cadical` numbers were really measuring a 2023 solver.

**The SHA baked into the binary is captured at cmake *configure* time**, not
build time. Configure, switch branch, rebuild, and the binary reports a commit
it does not contain. The runner compares it against the worktree HEAD and
refuses on disagreement (`--allow-sha-mismatch` overrides).

Build campaign binaries with LibBF on, or the three `*LRA` logics (381 files)
mostly fail to parse and are recorded `unsupported`:

```
-DUSE_LIBBF=ON -DLIBBF_DIR=<path to deps/libbf> -DENABLE_FLOATING_POINT=ON
-DBUILD_SHARED_LIBS=OFF -DSTATICCOMPILE=ON -DCMAKE_BUILD_TYPE=Release
```

## Result classes

| class | meaning |
| --- | --- |
| `sat` / `unsat` | decided; all queries agreed |
| `mixed` | incremental file whose queries had differing verdicts |
| `timeout` | hit the budget (`Timed Out.` or hard kill) |
| `memout` | cgroup OOM kill |
| `error` | crashed, or printed an error with no answer |
| `unsupported` | a documented STP capability gap, not a failure |
| `mismatch` | contradicted `(set-info :status)` — a soundness alarm |

`unsupported` is excluded from headline metrics but still run and recorded, so
the site shows the gap honestly rather than hiding it. The patterns are in
`manifests/unsupported-patterns.tsv`; raw output is always retained, so anything
mis-triaged can be re-classified without re-running the campaign.

## Storage

| where | what |
| --- | --- |
| `~/data/stpbench/results.db` | SQLite, all campaigns (working store) |
| `~/data/stpbench/outputs/<campaign>.jsonl.gz` | every run's stdout/stderr |
| `~/data/stpbench/binaries/` | archived binaries + provenance JSON |

All of it lives on the data disk on purpose: `/` is nearly full, and a full
campaign's output log is not small.

`export.py` writes the published form — `campaigns.json`, per-campaign
summaries, gzipped per-file detail, the full per-run archive, the corpus index,
and a regression diff against the previous campaign of the same tier. That diff
is restricted to files both campaigns ran, so editing a manifest cannot
masquerade as a performance change.

The DB is never published. `runs/<campaign>.jsonl.gz` and `corpus.jsonl.gz`
carry everything its two tables hold, as append-only text: a campaign adds a
file rather than rewriting 90 MB of SQLite, and the result can be reviewed in a
diff.

### The output log is append-per-process

`outputs/<campaign>.jsonl.gz` gets a new gzip member from every process that
touches the campaign. A process killed mid-member — reboot, oomd, Ctrl-C —
leaves an unterminated stream, and the next append lands straight after the
damaged bytes, at which point `gzip -t`, `zcat` and `gzip.open` all stop at the
seam and report far less than survived. Read these with `outputlog.py`, which
walks the members, and never with plain gzip. `Recorder` repairs the file
before appending to it, so the damage cannot compound; the repair is also what
`publish.py` relies on to publish a log that `zcat` can read.

The published log is keyed by benchmark path, not by the DB's `output_offset`.
That counter used to restart at zero in each process, so a resumed campaign
numbered two different records `0` — see the README's note on `full-001`, which
was recorded that way.

## Resuming

Runs are committed as they complete. Re-running with the same `--name` skips
everything already done and clean, so a campaign spanning a day survives a
Ctrl-C, a reboot, or a machine that got busy.
