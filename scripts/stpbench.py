#!/usr/bin/env python3
"""stpbench -- sweep the SMT-LIB corpora with one STP binary and record the run.

Produces one campaign: a git-commit-keyed measurement of answer, wall time and
peak memory for every benchmark in a tier, with the raw solver output retained
so answers can be re-checked later.

What this does that the older bash harnesses do not:

  * peak RSS per run (wait4 rusage) -- nothing here has ever recorded it, and
    it is what lets later campaigns schedule memory-hungry files serially;
  * a real memory ceiling via cgroup v2. ulimit -v is wrong for STP because
    stp-bin links mimalloc and over-reserves address space;
  * foreign-load monitoring, so a run disturbed by unrelated work on the box is
    discarded and retried instead of silently polluting the series;
  * binary provenance, including which SAT solver build was actually linked --
    a stale deps/cadical once invalidated months of numbers here.

Usage:
    stpbench.py run --binary BIN [--build-dir DIR] [--tier fast|full|FILE] ...
    stpbench.py provenance --binary BIN [--build-dir DIR]
    stpbench.py summary [--campaign NAME]
"""

import argparse
import atexit
import ctypes
import glob
import gzip
import hashlib
import json
import os
import re
import resource
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque

import outputlog
from datetime import datetime, timezone

try:
    _LIBC = ctypes.CDLL("libc.so.6", use_errno=True)
except OSError:                                     # pragma: no cover
    _LIBC = None
PR_SET_PDEATHSIG = 1

# Production stack limit. ABC's recursive CNF code overflows the 8MB default,
# and measuring at 8MB reports harness crashes as if they were properties of
# the solver -- that mistake invalidated every pre-2026-08-14 track/ number.
STACK_LIMIT_BYTES = 80000 * 1024


LIVE_CHILDREN = set()   # pids of running solvers, for shutdown cleanup


def kill_live_children(*_args):
    """Belt to PR_SET_PDEATHSIG's braces, for an orderly stop."""
    for pid in list(LIVE_CHILDREN):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    LIVE_CHILDREN.clear()


def sweep_stale_state():
    """Remove slot cgroups and tmpfs staging left by dead campaigns.

    Named after the owning pid, so anything whose pid is gone is debris.
    """
    for path in glob.glob(os.path.join(Slot.BASE, "stpbench-*-*.scope")):
        try:
            pid = int(os.path.basename(path).split("-")[1])
        except (IndexError, ValueError):
            continue
        if not os.path.isdir(f"/proc/{pid}"):
            try:
                os.rmdir(path)
            except OSError:
                pass    # still populated; leave it rather than guess
    for path in glob.glob("/dev/shm/stpbench-*"):
        try:
            pid = int(os.path.basename(path).split("-")[1])
        except (IndexError, ValueError):
            continue
        if not os.path.isdir(f"/proc/{pid}"):
            shutil.rmtree(path, ignore_errors=True)


def child_setup():
    """Run in the child between fork and exec.

    PR_SET_PDEATHSIG is the important part: it tells the kernel to SIGKILL this
    solver the moment the orchestrator dies, whatever kills it -- Ctrl-C,
    systemctl stop, systemd-oomd, or a session teardown. Without it solvers
    survive as orphans reparented to init, because each one is moved into its
    own cgroup and so is not covered by anything that kills the parent's
    cgroup. Eleven such orphans once kept running long past their -k budget and
    showed up as 11 cores of "foreign" load, tainting the next campaign.
    """
    try:
        resource.setrlimit(resource.RLIMIT_STACK,
                           (STACK_LIMIT_BYTES, STACK_LIMIT_BYTES))
    except (ValueError, OSError):
        pass
    if _LIBC is not None:
        _LIBC.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.expanduser("~/data/stpbench/results.db")
DATA_ROOT = os.path.expanduser("~/data/stpbench")
CLK_TCK = os.sysconf("SC_CLK_TCK")

# Answers are read from stdout only. STP exits 0 on syntax errors and even on a
# rejected set-logic (which it then solves anyway, stp#861), so exit codes
# cannot classify a run -- a prior sweep here got a whole logic backwards that
# way.
RE_ANSWER = re.compile(r"^(sat|unsat)$", re.M)
RE_TIMEDOUT = re.compile(r"^Timed Out\.$", re.M)
# All three per-query verdict forms, in order. See classify().
RE_VERDICT = re.compile(r"^(sat|unsat|Timed Out\.)$", re.M)


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_tsv(name):
    out = []
    path = os.path.join(HERE, "manifests", name)
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                out.append((parts[0], parts[1]))
    return out


LOGIC_FLAGS = {k: v.split() for k, v in load_tsv("logic-flags.tsv")}
UNSUPPORTED = load_tsv("unsupported-patterns.tsv")


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def is_static(binary):
    try:
        out = subprocess.run(["file", "-L", binary], capture_output=True,
                             text=True, timeout=30).stdout
        if "statically linked" in out:
            return True
        if "dynamically linked" in out:
            return False
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def dep_version(lib_path):
    """Identify a linked SAT-solver artefact.

    The library file's sha256 is the authoritative identity; the VERSION file
    and git SHA are the human-readable labels. This matters because there are
    several CaDiCaL checkouts on a typical box here and CMake's choice is
    machine-global via ~/.cmake/packages -- a green build proves nothing about
    which one was linked, and a stale one silently rewrote months of results.
    """
    info = {"path": lib_path}
    if not lib_path or not os.path.exists(lib_path):
        info["missing"] = True
        return info
    info["sha256"] = sha256_file(lib_path)
    info["mtime"] = datetime.fromtimestamp(
        os.path.getmtime(lib_path), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Walk up looking for a VERSION file and a git checkout.
    d = os.path.dirname(os.path.abspath(lib_path))
    for _ in range(4):
        vf = os.path.join(d, "VERSION")
        if "version" not in info and os.path.isfile(vf):
            with open(vf) as fh:
                info["version"] = fh.read().strip()
        if os.path.isdir(os.path.join(d, ".git")):
            try:
                info["git_sha"] = subprocess.run(
                    ["git", "-C", d, "rev-parse", "HEAD"], capture_output=True,
                    text=True, timeout=20).stdout.strip()
                info["git_describe"] = subprocess.run(
                    ["git", "-C", d, "describe", "--tags", "--always", "--dirty"],
                    capture_output=True, text=True, timeout=20).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
            break
        d = os.path.dirname(d)
    return info


CACHE_LIB_VARS = {
    "cadical": "CADICAL_LIBRARY",
    "minisat": "MINISAT_LIBRARY",
    "cryptominisat": "CRYPTOMINISAT5_LIBRARIES",
    "libbf": "LIBBF_LIBRARY",
    "riss": "RISS_LIBRARY",
}


def provenance(binary, build_dir, source_dir=None):
    binary = os.path.abspath(binary)
    prov = {"binary": binary, "binary_sha256": sha256_file(binary),
            "binary_static": is_static(binary)}

    ver = subprocess.run([binary, "--version"], capture_output=True, text=True,
                         timeout=60).stdout
    prov["stp_version_raw"] = ver.strip()
    m = re.search(r"STP version SHA string\s+(\S+)", ver)
    prov["stp_sha"] = m.group(1) if m else None
    m = re.search(r"COMPILE_DEFINES\s*=\s*(.*?)\s*\|", ver)
    prov["compile_defines"] = m.group(1).strip() if m else ""
    prov["has_libbf"] = "STP_HAVE_LIBBF" in ver
    prov["has_fp"] = "STP_ENABLE_FLOATING_POINT" in ver

    # SAT solver identity, primary source: STP reports the linked solvers'
    # own version strings since #863 ("STP SAT solvers cadical 3.0.1"). That is
    # self-describing, so it still works for an archived binary with no build
    # tree beside it.
    solvers = {}
    methods = []
    m = re.search(r"^STP SAT solvers (.*)$", ver, re.M)
    if m and m.group(1).strip() != "none":
        methods.append("version-string")
        for entry in m.group(1).split(","):
            bits = entry.split()
            if bits:
                solvers[bits[0]] = {"version": " ".join(bits[1:]) or None}

    # Secondary source: the build tree names the exact artefact, which pins the
    # identity harder than a version string can -- several CaDiCaL checkouts
    # here report the same version. Note we read the *resolved* library path
    # from the cache, never the *_DIR hint: find_library searches
    # CMAKE_PREFIX_PATH before HINTS, which is how CADICAL_DIR got shadowed
    # here before.
    cache = os.path.join(build_dir, "CMakeCache.txt") if build_dir else None
    if cache and os.path.exists(cache):
        methods.append("cmakecache")
        text = open(cache, errors="replace").read()
        for name, var in CACHE_LIB_VARS.items():
            mm = re.search(rf"^{var}:[A-Z]+=(.*)$", text, re.M)
            if mm and mm.group(1).strip():
                info = dep_version(mm.group(1).strip())
                info.update(solvers.get(name, {}))
                solvers[name] = info
    prov["provenance_method"] = "+".join(methods) or "none"
    prov["solvers"] = solvers

    # Source-tree identity. Derived from the build tree, not from where this
    # script happens to live: the harness sits outside the checkouts, and there
    # are many of them here, so the only reliable link from a binary back to
    # its source is the cache CMake wrote when configuring it.
    src = source_dir
    if not src and cache and os.path.exists(cache):
        m = re.search(r"^STP_SOURCE_DIR:[A-Z]+=(.*)$",
                      open(cache, errors="replace").read(), re.M)
        if m:
            src = m.group(1).strip()
    if not src:
        src = os.path.dirname(os.path.dirname(HERE))
    def git(*a):
        try:
            return subprocess.run(["git", "-C", src] + list(a),
                                  capture_output=True, text=True,
                                  timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    prov["commit_sha"] = git("rev-parse", "HEAD")
    prov["commit_date"] = git("show", "-s", "--format=%cI", "HEAD")
    prov["branch"] = git("rev-parse", "--abbrev-ref", "HEAD")
    prov["dirty"] = bool(git("status", "--porcelain", "--untracked-files=no"))

    # The SHA baked into the binary is captured by CMake at *configure* time
    # (CMakeLists.txt get_git_head_revision -> GitSHA1.cpp.in), not at build
    # time. Configure, switch branch, rebuild, and the binary reports a commit
    # it does not contain -- which is exactly how this build ended up claiming
    # 2d33ac34 while holding master's code. Keying a time series on that would
    # silently attribute results to the wrong commit, so both are recorded and
    # a disagreement is surfaced rather than resolved.
    prov["stp_sha_matches_worktree"] = (
        bool(prov["stp_sha"]) and prov["stp_sha"] == prov["commit_sha"])
    return prov


def archive_binary(prov):
    d = os.path.join(DATA_ROOT, "binaries")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, prov["binary_sha256"] + ".stp")
    if not os.path.exists(dst):
        shutil.copy2(prov["binary"], dst)
        os.chmod(dst, 0o555)
    with open(dst[:-4] + ".json", "w") as fh:
        json.dump(prov, fh, indent=2, sort_keys=True)
    return dst


# ---------------------------------------------------------------------------
# Load monitoring
# ---------------------------------------------------------------------------

class LoadMonitor(threading.Thread):
    """Tracks CPU load that is *not* ours.

    Raw loadavg is useless for this: with 20 workers running it reports 20 and
    says nothing about interference. So we subtract our own children's CPU from
    the machine total. Children that exit between samples would otherwise make
    our share appear to shrink and fake a load spike, so their final CPU (from
    rusage) is accumulated into a running total rather than lost.
    """

    def __init__(self, threshold, interval=1.0, window=1800):
        super().__init__(daemon=True)
        self.threshold = threshold
        self.interval = interval
        self.samples = deque(maxlen=int(window / interval))
        self.lock = threading.Lock()
        self.pids = set()
        self.reaped_cpu_s = 0.0
        self.current = 0.0
        self.mem_available_kb = 0
        # Not _stop: threading.Thread._stop is an internal method, and
        # shadowing it makes threading._after_fork raise in every forked
        # child ("'Event' object is not callable").
        self._stopped = threading.Event()

    def register(self, pid):
        with self.lock:
            self.pids.add(pid)

    def unregister(self, pid, cpu_s):
        with self.lock:
            self.pids.discard(pid)
            self.reaped_cpu_s += cpu_s

    def _machine_busy_s(self):
        with open("/proc/stat") as fh:
            parts = fh.readline().split()
        vals = [int(v) for v in parts[1:11]]
        idle = vals[3] + vals[4]          # idle + iowait
        return (sum(vals) - idle) / CLK_TCK

    def _our_live_cpu_s(self):
        total = 0.0
        with self.lock:
            pids = list(self.pids)
        # The orchestrator's own CPU counts as ours, not as interference. It is
        # not negligible -- page-cache preloading, gzip of the output log and
        # sqlite writes all land here -- and leaving it out taints runs on a
        # quiet box, burning retries for no reason.
        pids.append(os.getpid())
        for pid in pids:
            try:
                with open(f"/proc/{pid}/stat") as fh:
                    f = fh.read().rsplit(") ", 1)[1].split()
                # fields 11..14 after the comm field: utime, stime, cutime, cstime
                total += (int(f[11]) + int(f[12]) + int(f[13]) + int(f[14])) / CLK_TCK
            except (OSError, IndexError, ValueError):
                continue
        return total

    def _mem_available_kb(self):
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1])
        except OSError:
            pass
        return 0

    def run(self):
        prev_busy = self._machine_busy_s()
        with self.lock:
            prev_ours = self.reaped_cpu_s
        prev_ours += self._our_live_cpu_s()
        prev_t = time.monotonic()
        while not self._stopped.wait(self.interval):
            now = time.monotonic()
            busy = self._machine_busy_s()
            with self.lock:
                reaped = self.reaped_cpu_s
            ours = reaped + self._our_live_cpu_s()
            dt = max(1e-6, now - prev_t)
            foreign = max(0.0, (busy - prev_busy) - (ours - prev_ours)) / dt
            self.current = foreign
            self.mem_available_kb = self._mem_available_kb()
            with self.lock:
                self.samples.append((prev_t, now, foreign))
            prev_busy, prev_ours, prev_t = busy, ours, now

    def quiet_now(self):
        return self.current <= self.threshold

    def was_disturbed(self, t0, t1):
        """True if [t0, t1] saw sustained foreign load, by time-weighted mean.

        Deliberately not "any sample exceeded the threshold": that criterion is
        not scale-invariant. A 300s run spans ~300 samples, so on a machine
        anyone is using the probability that none of them spikes approaches 1,
        and every long run gets tainted and retried. Measured: 24 of 24 runs
        tainted, and an ETA of 60 days for what should be a two-day sweep.

        The mean is the right question anyway. A momentary spike inside a
        five-minute solve is noise; sustained competition for cores is what
        actually corrupts the timing. At short durations the mean is dominated
        by whatever overlapped, so a one-second spike in a two-second run still
        taints it -- the criterion degrades gracefully in both directions.
        """
        total = weighted = 0.0
        with self.lock:
            for s0, s1, foreign in self.samples:
                lo, hi = max(s0, t0), min(s1, t1)
                if hi > lo:
                    total += hi - lo
                    weighted += foreign * (hi - lo)
        if total <= 0:
            # Run finished inside one sampling interval; use the latest value.
            return self.current > self.threshold
        return (weighted / total) > self.threshold

    def stop(self):
        self._stopped.set()


# ---------------------------------------------------------------------------
# cgroup slots
# ---------------------------------------------------------------------------

class Slot:
    """A reusable cgroup v2 directory giving one worker a hard memory ceiling.

    The `memory` controller is delegated to the user manager, so we can create
    these directly without systemd-run and without root. The kernel here
    predates memory.peak, so peak RSS comes from wait4 rusage instead; what the
    cgroup provides is the ceiling and, via memory.events, the ability to tell
    an OOM kill apart from a timeout kill (both arrive as SIGKILL).
    """

    BASE = (f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/"
            f"user@{os.getuid()}.service/app.slice")

    # Parent cgroup holding every slot, with an AGGREGATE ceiling.
    #
    # Per-slot limits alone are not containment: 12 slots x 30GB is 360GB of
    # potential demand on a 62GB machine. The scheduler's reservations are only
    # predictions, and on a first campaign nothing is measured, so unknown
    # files are assumed to need 1GB while they can actually grow to 30. That
    # gap let this harness drive the machine into real memory pressure, and
    # systemd-oomd responded by killing a browser and a file-sync daemon.
    #
    # With a parent limit the kernel contains the campaign as a whole: if the
    # solvers collectively exceed the budget it OOM-kills one of THEM, inside
    # this subtree, and the desktop is never a candidate.
    parent_path = None

    @classmethod
    def create_parent(cls, budget_bytes):
        cls.parent_path = os.path.join(cls.BASE, f"stpbench-{os.getpid()}.scope")
        os.makedirs(cls.parent_path, exist_ok=True)
        with open(os.path.join(cls.parent_path, "memory.max"), "w") as fh:
            fh.write(str(budget_bytes))
        try:
            with open(os.path.join(cls.parent_path, "memory.swap.max"), "w") as fh:
                fh.write("0")
        except OSError:
            pass
        # Children need the memory controller delegated down to them.
        with open(os.path.join(cls.parent_path, "cgroup.subtree_control"), "w") as fh:
            fh.write("+memory")
        return cls.parent_path

    @classmethod
    def destroy_parent(cls):
        if cls.parent_path:
            try:
                os.rmdir(cls.parent_path)
            except OSError:
                pass

    def __init__(self, index, mem_limit_bytes):
        base = self.parent_path or self.BASE
        self.path = os.path.join(base, f"slot-{index}.scope"
                                 if self.parent_path
                                 else f"stpbench-{os.getpid()}-{index}.scope")
        os.makedirs(self.path, exist_ok=True)
        self._write("memory.max", str(mem_limit_bytes))
        self._write("memory.swap.max", "0")   # swapping would wreck timings

    def _write(self, name, value):
        with open(os.path.join(self.path, name), "w") as fh:
            fh.write(value)

    def adopt(self, pid):
        self._write("cgroup.procs", str(pid))

    def oom_count(self):
        try:
            with open(os.path.join(self.path, "memory.events")) as fh:
                for line in fh:
                    k, v = line.split()
                    if k == "oom_kill":
                        return int(v)
        except OSError:
            pass
        return 0

    def close(self):
        try:
            os.rmdir(self.path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Running one benchmark
# ---------------------------------------------------------------------------

class Result:
    # t0/t1 are epoch seconds, for the record; m0/m1 are the monotonic clock,
    # which is what the load samples use. Keeping the two apart matters: mixing
    # them silently disabled the taint check, since monotonic values are tiny
    # next to epoch ones and the overlap test could never be true.
    __slots__ = ("cls", "answers", "wall_s", "cpu_s", "peak_rss_kb", "exit_code",
                 "term_signal", "oom_killed", "stdout", "stderr",
                 "t0", "t1", "m0", "m1")


def classify(bench, stdout, stderr, timed_out_hard, oom_killed):
    """Answer/verdict classification, from stdout only.

    Order matters: a wrong answer is a soundness alarm and outranks everything,
    including a run that also hit the wall.
    """
    # A timed-out query still OCCUPIES A POSITION in the verdict sequence: STP
    # prints "Timed Out." for it and carries on to the next check-sat. Matching
    # only sat/unsat drops that line, shifts every later answer left by one,
    # and reports a mismatch on a correct run -- observed on a 298-query
    # incremental file whose query 242 timed out. Capture all three verdict
    # forms so positions stay aligned with the expected list.
    answers = ["unknown" if v.startswith("Timed") else v
               for v in RE_VERDICT.findall(stdout)]
    expected = json.loads(bench["expected_json"])

    for i, a in enumerate(answers):
        if a == "unknown":
            continue
        if i < len(expected) and expected[i] in ("sat", "unsat") and a != expected[i]:
            return "mismatch", answers
    if oom_killed:
        return "memout", answers
    if timed_out_hard or "unknown" in answers:
        return "timeout", answers

    blob = stdout + "\n" + stderr
    if "(error" in stdout or "STP Error:" in stderr:
        for pattern, _reason in UNSUPPORTED:
            if pattern in blob:
                return "unsupported", answers
        # STP accepts the QF_AUFBV logic name but has no theory of
        # uninterpreted functions, so its parse failures are a capability gap
        # rather than a bug. Only 9 of 75 local files are readable at all.
        if bench["logic"] == "QF_AUFBV":
            return "unsupported", answers
        return "error", answers
    if not answers:
        return "error", answers
    if bench["n_queries"] and len(answers) < bench["n_queries"]:
        # Fewer verdicts than queries: the run was cut off rather than paced.
        return "timeout" if timed_out_hard else "error", answers
    uniq = set(answers)
    if len(uniq) == 1:
        return uniq.pop(), answers
    return "mixed", answers


def stage(cfg, slot_index, bench):
    """Put the input somewhere a spinning disk cannot reach mid-run.

    The corpus lives on a 7200rpm disk. Reading it inside the timed region
    would be ruinous for the easy corpus -- median wall there is ~14ms, and one
    seek is milliseconds -- so the input is always made resident first.

    Two levels:
      * preload: read the file so it sits in page cache. Cheap, but page cache
        is reclaimable, and a full campaign runs 20 jobs against a 30GB ceiling,
        so it can in principle be evicted between the preload and STP's parse.
      * shm: copy to tmpfs and run from there. Not reclaimable, so the window
        closes entirely. Only worth it for small files, which is nearly all of
        them; a file big enough to matter is attached to a run long enough for
        one read not to.

    Returns the path to run, and a temp path to remove afterwards (or None).
    """
    path = bench["path"]
    if cfg.stage == "shm" and bench["size_bytes"] <= cfg.stage_max_bytes:
        d = f"/dev/shm/stpbench-{os.getpid()}"
        try:
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, f"slot{slot_index}.smt2")
            with open(path, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
                drop_cache(src)
            return tmp, tmp
        except OSError:
            pass    # shm full or unavailable -- fall back to a plain preload
    try:
        with open(path, "rb") as fh:
            while fh.read(1 << 22):
                pass
    except OSError:
        pass
    return path, None


def drop_cache(fh):
    """Evict a file from the page cache.

    A campaign reads the whole corpus, and leaving 58GB of it cached generates
    exactly the sustained reclaim that makes systemd-oomd start killing other
    things in the user slice. With shm staging the original is dead weight the
    moment it has been copied, so drop it immediately.
    """
    try:
        os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    except (OSError, AttributeError):
        pass


def run_benchmark(cfg, slot, monitor, bench, slot_index=0):
    flags = list(cfg.flags) + LOGIC_FLAGS.get(bench["logic"], [])
    # Staging happens before the timer starts, so the disk read is never part
    # of a measurement.
    path, tmp = stage(cfg, slot_index, bench)

    # No shell wrapper: child_setup() applies the stack limit and the
    # parent-death signal directly, so the solver is the only process and
    # wait4's rusage describes it alone.
    cmd = [cfg.binary, "-k", str(cfg.timeout_s)] + flags + [path]

    res = Result()
    res.oom_killed = False
    oom_before = slot.oom_count()
    hard_killed = threading.Event()

    with tempfile.TemporaryFile("w+b") as fo, tempfile.TemporaryFile("w+b") as fe:
        res.t0 = time.time()
        res.m0 = t0 = time.monotonic()
        proc = subprocess.Popen(cmd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
                                start_new_session=True, close_fds=True,
                                preexec_fn=child_setup)
        LIVE_CHILDREN.add(proc.pid)
        try:
            slot.adopt(proc.pid)
        except OSError:
            pass  # process may already have exited
        monitor.register(proc.pid)

        def kill():
            hard_killed.set()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass

        timer = threading.Timer(cfg.timeout_s + cfg.grace_s, kill)
        timer.start()
        try:
            _pid, status, ru = os.wait4(proc.pid, 0)
        except BaseException:
            # Never leave a pid registered: the monitor would keep counting it
            # as ours forever and under-report foreign load from then on.
            monitor.unregister(proc.pid, 0.0)
            LIVE_CHILDREN.discard(proc.pid)
            raise
        finally:
            timer.cancel()
        res.m1 = time.monotonic()
        res.wall_s = res.m1 - t0
        res.t1 = time.time()
        proc.returncode = -(status & 0x7f) if (status & 0x7f) else (status >> 8)
        res.cpu_s = ru.ru_utime + ru.ru_stime
        res.peak_rss_kb = ru.ru_maxrss
        res.term_signal = status & 0x7f
        res.exit_code = status >> 8
        monitor.unregister(proc.pid, res.cpu_s)
        LIVE_CHILDREN.discard(proc.pid)

        fo.seek(0); fe.seek(0)
        res.stdout = fo.read(cfg.max_output_bytes).decode("utf-8", "replace")
        res.stderr = fe.read(cfg.max_output_bytes).decode("utf-8", "replace")

    res.oom_killed = slot.oom_count() > oom_before
    res.cls, res.answers = classify(bench, res.stdout, res.stderr,
                                    hard_killed.is_set(), res.oom_killed)
    if tmp:
        # tmpfs is RAM: leaving staged copies around would eat the budget the
        # solver needs.
        try:
            os.unlink(tmp)
        except OSError:
            pass
    else:
        # Preload path: the run is over, so stop holding the file in cache.
        try:
            with open(bench["path"], "rb") as fh:
                drop_cache(fh)
        except OSError:
            pass
    return res


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

class Job:
    __slots__ = ("bench", "reserve_kb", "solo", "attempts", "memout_retried")

    def __init__(self, bench, reserve_kb, solo):
        self.bench = bench
        self.reserve_kb = reserve_kb
        self.solo = solo
        self.attempts = 0
        self.memout_retried = False


class Scheduler:
    """Memory-aware admission with a drain-for-solo rule.

    Reservations come from the largest peak RSS any prior campaign measured for
    that file, so the first campaign runs blind at a default and every later one
    schedules on real data -- which is the whole reason peak memory is recorded.

    A file that needs more than the solo threshold, or that has ever been OOM
    killed, runs alone. When such a job reaches the head of the queue the
    scheduler stops admitting new work and lets the running set drain, rather
    than letting big jobs starve behind an endless stream of small ones.
    """

    def __init__(self, jobs, budget_kb, monitor):
        self.pending = deque(jobs)
        self.budget_kb = budget_kb
        self.monitor = monitor
        self.cv = threading.Condition()
        self.reserved_kb = 0
        self.running = 0
        self.solo_running = False
        self.closed = False

    def _pick_locked(self):
        if not self.pending:
            return None
        if self.solo_running:
            return None
        head = self.pending[0]
        if head.solo:
            # Drain mode: admit nothing else until the box is ours alone.
            if self.running == 0:
                self.pending.popleft()
                self.solo_running = True
                self.reserved_kb += head.reserve_kb
                self.running += 1
                return head
            return None
        for i, job in enumerate(self.pending):
            if job.solo:
                break   # preserve drain ordering
            if self.reserved_kb + job.reserve_kb <= self.budget_kb:
                del self.pending[i]
                self.reserved_kb += job.reserve_kb
                self.running += 1
                return job
        return None

    def acquire(self):
        with self.cv:
            while True:
                if self.closed:
                    return None
                if not self.pending and self.running == 0:
                    return None
                if self.monitor.quiet_now():
                    job = self._pick_locked()
                    if job is not None:
                        return job
                self.cv.wait(0.5)

    def release(self, job, requeue, make_solo=False):
        """Return a slot. make_solo promotes the job to the serial tail.

        Promotion happens here, under the lock, and only after solo_running has
        been cleared for the state the job was *acquired* with -- flipping
        job.solo before releasing would clear a flag this job never set.
        """
        with self.cv:
            self.reserved_kb -= job.reserve_kb
            self.running -= 1
            if job.solo:
                self.solo_running = False
            if make_solo:
                job.solo = True
                job.reserve_kb = self.budget_kb   # nothing may run alongside
            if requeue:
                self.pending.append(job)
            self.cv.notify_all()

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def remaining(self):
        with self.cv:
            return len(self.pending) + self.running


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------

class Recorder:
    """DB + compressed output log. Serialised; the workers are I/O bound."""

    def __init__(self, db, campaign_id, name):
        self.db = db
        self.campaign_id = campaign_id
        self.lock = threading.Lock()
        d = os.path.join(DATA_ROOT, "outputs")
        os.makedirs(d, exist_ok=True)
        self.out_path = os.path.join(d, f"{name}.jsonl.gz")
        # A previous process may have been killed mid-write. Appending to the
        # damaged stream is what makes it unreadable to every standard tool, so
        # rewrite it whole before adding to it.
        st = outputlog.repair(self.out_path)
        if st:
            print(f"repaired {self.out_path}: kept {st['records']} records "
                  f"from {st['members']} members "
                  f"({st['damaged_members']} damaged)")
        self.out = gzip.open(self.out_path, "at", encoding="utf-8")
        # Continue the numbering rather than restarting it. This counter used
        # to be per-process, so a resumed campaign wrote a second record 0 and
        # every offset in the overlap addressed two different outputs --
        # reclassify builds a dict on this key and would silently pair a run
        # with another run's output. full-001 was recorded that way.
        row = self.db.execute(
            "SELECT MAX(output_offset) FROM run WHERE campaign_id = ?",
            (campaign_id,)).fetchone()
        self.index = (row[0] + 1) if row and row[0] is not None else 0
        self.n = 0
        self.last_commit = time.monotonic()

    def record(self, bench, res, tainted, attempts):
        with self.lock:
            offset = self.index
            self.index += 1
            self.out.write(json.dumps({
                "i": offset, "path": bench["path"], "class": res.cls,
                "stdout": res.stdout, "stderr": res.stderr}) + "\n")
            self.db.execute(
                """INSERT INTO run (campaign_id, benchmark_id, class, answers_json,
                       wall_s, cpu_s, peak_rss_kb, exit_code, term_signal,
                       oom_killed, tainted, attempts, started_utc, output_offset)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(campaign_id, benchmark_id) DO UPDATE SET
                       class=excluded.class, answers_json=excluded.answers_json,
                       wall_s=excluded.wall_s, cpu_s=excluded.cpu_s,
                       peak_rss_kb=excluded.peak_rss_kb,
                       exit_code=excluded.exit_code,
                       term_signal=excluded.term_signal,
                       oom_killed=excluded.oom_killed, tainted=excluded.tainted,
                       attempts=excluded.attempts,
                       output_offset=excluded.output_offset""",
                (self.campaign_id, bench["id"], res.cls, json.dumps(res.answers),
                 None if tainted else res.wall_s, res.cpu_s, res.peak_rss_kb,
                 res.exit_code, res.term_signal, int(res.oom_killed),
                 int(tainted), attempts,
                 datetime.fromtimestamp(res.t0, timezone.utc).strftime(
                     "%Y-%m-%dT%H:%M:%SZ"), offset))
            self.n += 1
            # Commit on a timer as well as a count. A campaign runs for hours,
            # and a count-only rule means a slow tier shows an empty DB for a
            # long time and loses everything since the last boundary if the
            # process dies -- which on this box it can, via systemd-oomd.
            now = time.monotonic()
            if self.n % 50 == 0 or now - self.last_commit > 30:
                self.db.commit()
                # Flush the log on the same boundary as the DB, so the two
                # cannot disagree by more than one commit window. Without this
                # the log trails the DB by a whole zlib buffer, which is how
                # the reboot during full-001 cost ~6,000 records that the DB
                # had already recorded.
                self.out.flush()
                self.out.flush()
                self.last_commit = now

    def flush(self):
        """Commit from outside record().

        record() only fires its timer when a run finishes, so with twelve
        300-second runs in flight the DB can lag by minutes and lose up to one
        slot-full on a kill. The progress loop calls this instead.
        """
        with self.lock:
            self.db.commit()
            self.out.flush()
            self.last_commit = time.monotonic()

    def close(self):
        with self.lock:
            self.db.commit()
            self.out.close()


CORPUS_PARENT = os.path.expanduser("~/data")


def resolve_corpus_path(entry):
    """A manifest's corpus-relative entry -> this machine's absolute path."""
    if os.path.isabs(entry):
        return entry
    return os.path.join(CORPUS_PARENT, entry)


def load_tier(conn, tier):
    if tier == "full":
        rows = conn.execute("SELECT * FROM benchmark").fetchall()
    else:
        path = tier if os.path.exists(tier) else os.path.join(
            HERE, "manifests", f"{tier}.txt")
        if not os.path.exists(path):
            sys.exit(f"no such tier or manifest: {tier}")
        # Manifest entries are corpus-relative so the file can be published
        # and still name the same benchmarks elsewhere; the DB keys on the
        # local absolute path. Absolute entries are taken as-is, which keeps
        # ad-hoc file lists and manifests written before this change working.
        wanted = [resolve_corpus_path(l.strip()) for l in open(path)
                  if l.strip() and not l.startswith("#")]
        rows = []
        cur = conn.cursor()
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            q = "SELECT * FROM benchmark WHERE path IN (%s)" % ",".join("?" * len(chunk))
            rows.extend(cur.execute(q, chunk).fetchall())
        if len(rows) != len(wanted):
            print(f"warning: manifest lists {len(wanted)} paths but only "
                  f"{len(rows)} are in the DB -- rerun corpus.py scan",
                  file=sys.stderr)
    return rows


def cmd_run(args):
    # Solvers must never outlive this process; see child_setup().
    atexit.register(kill_live_children)
    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(_sig, lambda *_a: sys.exit(143))
        except (ValueError, OSError):
            pass
    sweep_stale_state()
    # Workers write through Recorder, which serialises on its own lock, so
    # sharing one connection across threads is safe here.
    conn = sqlite3.connect(args.db, timeout=120, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with open(os.path.join(HERE, "schema.sql")) as fh:
        conn.executescript(fh.read())

    prov = provenance(args.binary, args.build_dir, args.source_dir)

    # Is this a resume? Look the campaign up before the provenance guards,
    # because a resume asks a different question. What matters then is "is this
    # the same binary the campaign started with", not "does the binary match
    # whatever the worktree happens to be now" -- the worktree can have moved to
    # an unrelated branch since, and the campaign is already pinned to its own
    # commit. Comparing binary sha256 is also a stronger check than comparing
    # git SHAs.
    resume_name = args.name or ""
    prior = conn.execute("SELECT * FROM campaign WHERE name = ?",
                         (resume_name,)).fetchone() if resume_name else None
    if prior is not None:
        if prior["binary_sha256"] != prov["binary_sha256"]:
            sys.exit(
                f"refusing to resume campaign {resume_name} with a different "
                f"binary.\n  campaign was run with {prior['binary_sha256']}\n"
                f"  this binary is    {prov['binary_sha256']}\n"
                f"Resuming with another binary would mix two solvers into one "
                f"data point. Use a new --name, or point --binary at the "
                f"archived copy under {os.path.join(DATA_ROOT, 'binaries')}.")
        print(f"resuming {resume_name}: binary matches the campaign's "
              f"({prov['binary_sha256'][:12]}), commit {(prior['commit_sha'] or '')[:12]}")
    if not prov["binary_static"] and not args.allow_dynamic:
        sys.exit("refusing a dynamically linked binary: it resolves libstp via "
                 "RUNPATH back to its build tree, so the archived copy would "
                 "silently change as that tree is rebuilt. Build with "
                 "-DBUILD_SHARED_LIBS=OFF -DSTATICCOMPILE=ON, or pass "
                 "--allow-dynamic for a throwaway run.")
    if prior is None and not prov["stp_sha_matches_worktree"]:
        msg = (f"binary reports SHA {prov['stp_sha']} but the worktree is at "
               f"{prov['commit_sha']}. The baked SHA is captured at cmake "
               f"configure time, so this binary was configured on one commit "
               f"and built on another -- reconfigure and rebuild before "
               f"recording a campaign against a commit.")
        if not args.allow_sha_mismatch:
            sys.exit("refusing: " + msg + "\n(pass --allow-sha-mismatch to override)")
        print("warning: " + msg, file=sys.stderr)
        # The worktree demonstrably did not produce this binary, so recording
        # its commit would attribute the campaign to code that never ran. The
        # binary's own baked SHA is the better answer; keep the worktree's
        # under a separate key so the disagreement stays visible.
        prov["worktree_commit_sha"] = prov["commit_sha"]
        prov["commit_sha"] = prov["stp_sha"] or prov["commit_sha"]
        prov["commit_date"] = None
    if not prov["has_libbf"]:
        print("warning: binary lacks STP_HAVE_LIBBF -- the three *LRA logics "
              "(381 files) will mostly fail to parse and be recorded as "
              "unsupported for this campaign.", file=sys.stderr)
    # Only static binaries are worth archiving: a copied dynamic one resolves
    # libstp via RUNPATH back to a build tree that will change underneath it,
    # so the archive would not reproduce anything. Say so rather than skipping
    # quietly -- a campaign with no archived binary cannot be re-run later, and
    # that is a property of the data point people need to know about.
    archived = archive_binary(prov) if prov["binary_static"] else None
    if archived is None:
        print("warning: binary is not static, so it has NOT been archived. "
              "This campaign records the sha256 but cannot be reproduced "
              "byte-identically later; treat it as a validation run rather "
              "than a time-series anchor.", file=sys.stderr)
    print(f"binary   {prov['binary']}")
    print(f"  sha256 {prov['binary_sha256']}  static={prov['binary_static']}")
    print(f"  stp    {prov['stp_sha']}  branch={prov['branch']} dirty={prov['dirty']}")
    for name, info in prov["solvers"].items():
        print(f"  {name:<14} {info.get('version') or info.get('git_describe') or '?'}"
              f"  {info.get('path')}")
    if archived:
        print(f"  archived -> {archived}")
    if args.provenance_only:
        return

    name = args.name or f"{utcnow().replace(':', '').replace('-', '')}-{args.tier}"
    cur = conn.execute("SELECT id FROM campaign WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        campaign_id = row["id"]
        print(f"resuming campaign {name} (id={campaign_id})")
    else:
        campaign_id = conn.execute(
            """INSERT INTO campaign (name, started_utc, commit_sha, commit_date,
                   branch, dirty, build_dir, tier, timeout_s, mem_limit_bytes,
                   jobs, solver_flags, load_threshold, host, notes,
                   binary_sha256, binary_static, stp_version_raw,
                   compile_defines, solvers_json, provenance_method)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, utcnow(), prov["commit_sha"], prov["commit_date"],
             prov["branch"], int(prov["dirty"]), args.build_dir, args.tier,
             args.timeout, args.mem_limit_gb * (1 << 30), args.jobs,
             args.flags, args.max_foreign_load, socket.gethostname(),
             args.notes, prov["binary_sha256"], int(prov["binary_static"]),
             prov["stp_version_raw"], prov["compile_defines"],
             json.dumps(prov["solvers"]), prov["provenance_method"])).lastrowid
        conn.commit()

    benches = load_tier(conn, args.tier)
    done = {r[0] for r in conn.execute(
        "SELECT benchmark_id FROM run WHERE campaign_id=? AND tainted=0",
        (campaign_id,))}
    todo = [b for b in benches if b["id"] not in done]
    print(f"campaign {name}: {len(benches)} benchmarks, {len(done)} already done, "
          f"{len(todo)} to run")
    if not todo:
        conn.execute("UPDATE campaign SET finished_utc=? WHERE id=?",
                     (utcnow(), campaign_id))
        conn.commit()
        return

    cost = {r["benchmark_id"]: r for r in conn.execute("SELECT * FROM benchmark_cost")}
    solo_kb = args.solo_threshold_gb * (1 << 20)
    default_kb = args.default_reserve_mb * 1024

    def make_job(b):
        c = cost.get(b["id"])
        peak = c["max_peak_rss_kb"] if c and c["max_peak_rss_kb"] else None
        reserve = max(int((peak or default_kb) * 1.5), 256 * 1024)
        solo = bool((peak and peak > solo_kb) or (c and c["ever_memout"]))
        return Job(b, min(reserve, args.mem_limit_gb * (1 << 20)), solo)

    # Longest known first shortens the tail; unknown-cost files keep a stable
    # deterministic order so a rerun schedules identically.
    def sort_key(b):
        c = cost.get(b["id"])
        return (-(c["max_wall_s"] if c and c["max_wall_s"] else 0.0), b["path"])

    jobs = [make_job(b) for b in sorted(todo, key=sort_key)]
    jobs.sort(key=lambda j: j.solo)   # solo jobs last, drained into at the end

    budget_kb = int(args.mem_budget_gb * (1 << 20))
    monitor = LoadMonitor(args.max_foreign_load)
    monitor.start()
    sched = Scheduler(jobs, budget_kb, monitor)
    rec = Recorder(conn, campaign_id, name)

    cfg = argparse.Namespace(binary=args.binary, timeout_s=args.timeout,
                             grace_s=args.grace, flags=shlex.split(args.flags),
                             max_output_bytes=args.max_output_kb * 1024,
                             stage=args.stage,
                             stage_max_bytes=args.stage_max_mb * (1 << 20))

    stats = {"done": 0, "tainted": 0, "mismatch": 0}
    stats_lock = threading.Lock()
    t_start = time.time()

    def worker(index):
        slot = Slot(index, args.mem_limit_gb * (1 << 30))
        try:
            while True:
                job = sched.acquire()
                if job is None:
                    return
                job.attempts += 1
                try:
                    res = run_benchmark(cfg, slot, monitor, job.bench, index)
                except Exception as e:                      # keep the sweep alive
                    print(f"worker error on {job.bench['path']}: {e}",
                          file=sys.stderr)
                    sched.release(job, requeue=False)
                    continue
                disturbed = monitor.was_disturbed(res.m0, res.m1)
                retry = disturbed and job.attempts < args.retries
                # A memout while 11 neighbours are running is not evidence the
                # file needs more than the ceiling -- it may just have been
                # squeezed. Re-run it alone at the end of the campaign, where
                # it gets the whole budget, and only then believe the result.
                defer_solo = (res.cls == "memout" and not job.solo
                              and not job.memout_retried)
                if defer_solo:
                    job.memout_retried = True
                # A tainted run still carries a valid answer and peak memory --
                # only the timing is void -- so it is recorded either way, and
                # overwritten if the retry succeeds.
                rec.record(job.bench, res, tainted=disturbed, attempts=job.attempts)
                with stats_lock:
                    stats["done"] += 1
                    if disturbed:
                        stats["tainted"] += 1
                    if res.cls == "mismatch":
                        stats["mismatch"] += 1
                        print(f"MISMATCH {job.bench['path']} -> {res.answers}",
                              file=sys.stderr)
                sched.release(job, requeue=(retry or defer_solo),
                              make_solo=defer_solo)
        finally:
            slot.close()

    # Hard aggregate ceiling for the whole campaign, so memory pressure can
    # never reach the desktop. See Slot.create_parent.
    try:
        Slot.create_parent(int(args.mem_budget_gb * (1 << 30)))
        print(f"aggregate memory ceiling: {args.mem_budget_gb:.0f} GB "
              f"across all {args.jobs} slots")
    except OSError as e:
        print(f"warning: could not create the aggregate memory cgroup ({e}); "
              f"slots are limited individually only, so {args.jobs} jobs could "
              f"together demand {args.jobs * args.mem_limit_gb} GB",
              file=sys.stderr)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(args.jobs)]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(5)
            rec.flush()
            with stats_lock:
                d, ta, mm = stats["done"], stats["tainted"], stats["mismatch"]
            left = sched.remaining()
            rate = d / max(1e-6, time.time() - t_start)
            eta = left / rate / 60 if rate > 0 else 0
            print(f"\r{d} done, {left} left, {ta} tainted, {mm} mismatch, "
                  f"foreign load {monitor.current:.1f}, ETA {eta:.0f}m   ",
                  end="", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted -- closing cleanly, rerun to resume")
        sched.close()
    print()
    for t in threads:
        t.join(timeout=args.timeout + args.grace + 30)
    monitor.stop()
    Slot.destroy_parent()
    shutil.rmtree(f"/dev/shm/stpbench-{os.getpid()}", ignore_errors=True)
    conn.execute("UPDATE campaign SET finished_utc=? WHERE id=?",
                 (utcnow(), campaign_id))
    rec.close()
    print(f"outputs -> {rec.out_path}")
    print_summary(conn, campaign_id)


def print_summary(conn, campaign_id):
    print("\nclass breakdown:")
    for cls, n in conn.execute(
            "SELECT class, COUNT(*) FROM run WHERE campaign_id=? "
            "GROUP BY class ORDER BY COUNT(*) DESC", (campaign_id,)):
        print(f"  {cls:<12} {n}")
    row = conn.execute(
        """SELECT COUNT(*), SUM(tainted), MAX(peak_rss_kb), AVG(wall_s)
           FROM run WHERE campaign_id=?""", (campaign_id,)).fetchone()
    print(f"  total {row[0]}, tainted {row[1] or 0}, "
          f"max peak RSS {(row[2] or 0)/1048576:.1f} GB, "
          f"mean wall {row[3] or 0:.2f}s")
    mm = conn.execute("SELECT COUNT(*) FROM run WHERE campaign_id=? AND "
                      "class='mismatch'", (campaign_id,)).fetchone()[0]
    if mm:
        print(f"\n  *** {mm} MISMATCHES -- a wrong answer is a soundness bug "
              f"and invalidates this campaign ***")


def cmd_reclassify(args):
    """Re-derive each run's class from its retained output.

    The whole point of keeping stdout is that a classifier fix does not cost a
    re-run. A campaign is hours; re-reading its output is seconds.
    """
    conn = sqlite3.connect(args.db, timeout=120)
    conn.row_factory = sqlite3.Row
    camp = conn.execute("SELECT * FROM campaign WHERE name=?",
                        (args.campaign,)).fetchone()
    if not camp:
        sys.exit(f"no campaign named {args.campaign}")
    path = os.path.join(DATA_ROOT, "outputs", f"{args.campaign}.jsonl.gz")
    if not os.path.exists(path):
        sys.exit(f"no retained output at {path}")

    # A campaign killed mid-write (reboot, oomd, Ctrl-C) leaves a truncated
    # member, and the resume appends past it: plain gzip stops at the seam and
    # reports far less than survived. outputlog walks the members instead.
    #
    # Keyed by path, not by the `i` field: that counter restarted at zero in
    # every process before this was fixed, so in a resumed campaign one `i`
    # names two outputs, and this dict would keep whichever was written last --
    # silently re-classifying runs from another run's output. A benchmark path
    # appears once per campaign.
    records, st = outputlog.read_recovered(path)
    outs = {d.get("path"): (d.get("stdout", ""), d.get("stderr", ""))
            for d in records}
    if st["damaged_members"] or st["unparsable"]:
        print(f"warning: output log damaged; recovered {st['records']} records "
              f"from {st['members']} members ({st['damaged_members']} damaged, "
              f"{st['unparsable']} unparsable lines). Runs with no surviving "
              f"output keep their recorded class.", file=sys.stderr)

    rows = conn.execute(
        """SELECT r.id AS run_id, r.class AS old_class,
                  r.term_signal, r.oom_killed,
                  b.expected_json, b.logic, b.n_queries, b.path
           FROM run r JOIN benchmark b ON b.id = r.benchmark_id
           WHERE r.campaign_id = ?""", (camp["id"],)).fetchall()

    changes, updates = {}, []
    for row in rows:
        if row["path"] not in outs:
            continue
        so, se = outs[row["path"]]
        # timed_out_hard was not persisted; SIGKILL without an OOM is the
        # harness's hard kill.
        hard = bool(row["term_signal"] == 9 and not row["oom_killed"])
        cls, answers = classify(row, so, se, hard, bool(row["oom_killed"]))
        if cls != row["old_class"]:
            changes[(row["old_class"], cls)] = \
                changes.get((row["old_class"], cls), 0) + 1
            if args.verbose:
                print(f"  {row['old_class']} -> {cls}  {row['path']}")
        updates.append((cls, json.dumps(answers), row["run_id"]))

    if not args.dry_run:
        conn.executemany("UPDATE run SET class=?, answers_json=? WHERE id=?",
                         updates)
        conn.commit()
    print(f"{len(updates)} runs re-read"
          f"{' (dry run)' if args.dry_run else ''}")
    for (old, new), n in sorted(changes.items(), key=lambda kv: -kv[1]):
        print(f"  {old:<12} -> {new:<12} {n}")
    if not changes:
        print("  no class changes")


def cmd_summary(args):
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    if args.campaign:
        row = conn.execute("SELECT id FROM campaign WHERE name=?",
                           (args.campaign,)).fetchone()
        if not row:
            sys.exit(f"no campaign named {args.campaign}")
        print_summary(conn, row["id"])
        return
    for r in conn.execute("SELECT * FROM campaign ORDER BY started_utc"):
        n = conn.execute("SELECT COUNT(*) FROM run WHERE campaign_id=?",
                         (r["id"],)).fetchone()[0]
        print(f"{r['name']:<32} {(r['commit_sha'] or '')[:9]} {r['tier']:<8} "
              f"{n:>7} runs  {r['started_utc']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="sweep a tier with one binary")
    r.add_argument("--binary", required=True)
    r.add_argument("--build-dir", default=None,
                   help="build tree of that binary, for SAT solver provenance")
    r.add_argument("--source-dir", default=None)
    r.add_argument("--tier", default="fast", help="fast | full | path to a manifest")
    r.add_argument("--name", default=None)
    r.add_argument("--timeout", type=int, default=300)
    r.add_argument("--grace", type=int, default=30,
                   help="hard kill this many seconds after the soft budget")
    r.add_argument("--mem-limit-gb", type=int, default=30)
    r.add_argument("--mem-budget-gb", type=float, default=30.0,
                   help="total memory the campaign may reserve at once. Kept to "
                        "about half of RAM on purpose: systemd-oomd kills the "
                        "highest-pressure scopes in the user slice once memory "
                        "pressure holds above 50%, and it will take the desktop "
                        "with it. Leave the rest of RAM as headroom.")
    r.add_argument("--solo-threshold-gb", type=float, default=8.0)
    r.add_argument("--default-reserve-mb", type=int, default=1024,
                   help="assumed peak for files never measured before")
    r.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 8) - 4))
    r.add_argument("--max-foreign-load", type=float, default=3.0,
                   help="cores of non-stpbench CPU that taints a run. 3 of 24 "
                        "cores is about the documented +/-4%% noise floor for "
                        "this box, so ordinary desktop use does not taint "
                        "every run while real interference still does.")
    r.add_argument("--retries", type=int, default=3)
    r.add_argument("--max-output-kb", type=int, default=256)
    r.add_argument("--stage", choices=("shm", "preload"), default="shm",
                   help="how the input is made resident before timing starts. "
                        "'shm' copies it to tmpfs, which page cache eviction "
                        "cannot touch; 'preload' just warms the page cache. "
                        "The corpus is on a spinning disk, so one of these is "
                        "always done.")
    r.add_argument("--stage-max-mb", type=int, default=64,
                   help="files larger than this are preloaded, not copied to "
                        "tmpfs, so staging cannot eat the solver's memory")
    # One quoted string rather than nargs="*": argparse reads a following
    # "--cadical" as an option, not as a value, so the list form cannot express
    # the flags this actually needs.
    r.add_argument("--flags", default="--cadical",
                   help="solver flags as one quoted string, "
                        "e.g. --flags \'--cadical --flattening 0\'")
    r.add_argument("--notes", default=None)
    r.add_argument("--allow-dynamic", action="store_true")
    r.add_argument("--allow-sha-mismatch", action="store_true",
                   help="proceed when the binary's baked SHA differs from the "
                        "worktree HEAD (configure-time vs build-time skew)")
    r.add_argument("--provenance-only", action="store_true")
    r.set_defaults(func=cmd_run)

    p = sub.add_parser("provenance", help="print binary provenance and exit")
    p.add_argument("--binary", required=True)
    p.add_argument("--build-dir", default=None)
    p.add_argument("--source-dir", default=None)
    p.set_defaults(func=lambda a: print(json.dumps(
        provenance(a.binary, a.build_dir, a.source_dir), indent=2, sort_keys=True)))

    rc = sub.add_parser("reclassify",
                        help="re-derive classes from retained output (no re-run)")
    rc.add_argument("--campaign", required=True)
    rc.add_argument("--dry-run", action="store_true")
    rc.add_argument("--verbose", action="store_true")
    rc.set_defaults(func=cmd_reclassify)

    s = sub.add_parser("summary", help="campaign list or one campaign's breakdown")
    s.add_argument("--campaign", default=None)
    s.set_defaults(func=cmd_summary)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
