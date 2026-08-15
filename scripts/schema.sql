-- stpbench results store.
--
-- One SQLite file accumulates every campaign. It is the working store, not the
-- published artefact: export.py derives the small static JSON the website
-- reads. Keeping those separate matters because a binary DB rewritten per
-- campaign would bloat a git history badly, while 160k rows per campaign is
-- more than a committed text file wants to carry.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- The corpus. Populated by corpus.py scan; one row per benchmark file.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS benchmark (
    id          INTEGER PRIMARY KEY,
    path        TEXT    NOT NULL UNIQUE,
    mode        TEXT    NOT NULL,   -- 'non-incremental' | 'incremental'
    logic       TEXT    NOT NULL,   -- QF_BV, QF_ABV, ...
    family      TEXT    NOT NULL,   -- first path component below the logic dir
    n_queries   INTEGER NOT NULL,   -- number of (check-sat) / (check-sat-assuming)
    -- JSON array of 'sat'|'unsat'|'unknown', one entry per query. Shorter than
    -- n_queries when the file gives fewer :status lines than it has queries.
    expected_json TEXT  NOT NULL,
    size_bytes  INTEGER NOT NULL,
    sha256      TEXT    NOT NULL,
    scanned_utc TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS benchmark_group ON benchmark (mode, logic, family);

-- ---------------------------------------------------------------------------
-- One campaign = one binary swept over one tier.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaign (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL UNIQUE,
    started_utc     TEXT    NOT NULL,
    finished_utc    TEXT,
    -- what was measured
    commit_sha      TEXT,
    commit_date     TEXT,
    branch          TEXT,
    dirty           INTEGER,
    build_dir       TEXT,
    -- how it was measured
    tier            TEXT    NOT NULL,
    timeout_s       INTEGER NOT NULL,
    mem_limit_bytes INTEGER NOT NULL,
    jobs            INTEGER NOT NULL,
    solver_flags    TEXT    NOT NULL,
    load_threshold  REAL    NOT NULL,
    host            TEXT    NOT NULL,
    notes           TEXT,
    -- provenance of the exact binary (see stpbench.py:provenance)
    binary_sha256     TEXT,
    binary_static     INTEGER,
    stp_version_raw   TEXT,
    compile_defines   TEXT,
    solvers_json      TEXT,   -- {name: {path, sha256, version, git_sha}}
    provenance_method TEXT
);

-- ---------------------------------------------------------------------------
-- One row per (campaign, benchmark).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run (
    id            INTEGER PRIMARY KEY,
    campaign_id   INTEGER NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    benchmark_id  INTEGER NOT NULL REFERENCES benchmark(id),
    -- sat|unsat|mixed|timeout|memout|error|unsupported|mismatch
    class         TEXT    NOT NULL,
    answers_json  TEXT    NOT NULL,   -- answers actually printed, in order
    wall_s        REAL,               -- NULL when tainted: the timing is void
    cpu_s         REAL,
    peak_rss_kb   INTEGER,
    exit_code     INTEGER,
    term_signal   INTEGER,
    oom_killed    INTEGER NOT NULL DEFAULT 0,
    tainted       INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 1,
    started_utc   TEXT,
    output_offset INTEGER,            -- record index in outputs/<campaign>.jsonl.gz
    UNIQUE (campaign_id, benchmark_id)
);
CREATE INDEX IF NOT EXISTS run_campaign ON run (campaign_id, class);
CREATE INDEX IF NOT EXISTS run_benchmark ON run (benchmark_id);

-- Memory prediction for the scheduler, and cost order: the most recent clean
-- measurement of each benchmark, whichever campaign produced it.
CREATE VIEW IF NOT EXISTS benchmark_cost AS
SELECT benchmark_id,
       MAX(peak_rss_kb)                    AS max_peak_rss_kb,
       MAX(CASE WHEN oom_killed THEN 1 ELSE 0 END) AS ever_memout,
       AVG(wall_s)                         AS avg_wall_s,
       MAX(wall_s)                         AS max_wall_s
FROM run
WHERE tainted = 0 AND peak_rss_kb IS NOT NULL
GROUP BY benchmark_id;
