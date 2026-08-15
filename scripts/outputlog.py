#!/usr/bin/env python3
"""Reading and repairing a campaign's solver-output log.

`outputs/<campaign>.jsonl.gz` is written with `gzip.open(..., "at")`, so each
process that touches a campaign appends a fresh gzip member. That is fine until
a process dies mid-member: the file then ends in an unterminated deflate
stream, the next process appends a new member straight after the damaged bytes,
and every standard tool -- `gzip -t`, `zcat`, `gzip.open` -- stops at the seam.
It reports the file as corrupt even though most members are intact.

That is not hypothetical. The reboot during full-001 left exactly this: `zcat`
gives up after 61,511 of 160,610 records, while walking the members recovers
154,519 of them.

So: never read one of these with plain gzip. Walk the members, decode each as
far as it goes, and count what was lost. `repair()` makes the file whole again
by rewriting it as a single clean member, which is also what stops the damage
from compounding the next time a campaign resumes.
"""

import gzip
import io
import json
import os
import zlib

MAGIC = b"\x1f\x8b\x08"


def open_deterministic(path):
    """Text-mode gzip writer whose bytes depend only on the content.

    Plain `gzip.open` stamps the current time and the source filename into the
    header, so re-exporting unchanged data produces a different file every
    time. In a git repository that turns "nothing changed" into a multi-megabyte
    diff, and the churn is indistinguishable from real new data.
    """
    fh = open(path, "wb")
    gz = gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=fh,
                       mtime=0)
    return io.TextIOWrapper(gz, encoding="utf-8", write_through=True)


def _members(data):
    """Yield (offset, decompressed_bytes, clean) for each gzip member.

    A member that ends mid-stream still yields whatever decoded before the
    error, and the walk resumes at the next magic sequence. Those three bytes
    also occur inside compressed data, so a candidate that decodes to nothing
    is junk rather than a member, and is reported as such by yielding it with
    an empty payload -- callers count bytes lost, not members lost.
    """
    pos = 0
    while pos < len(data) - 2:
        if data[pos:pos + 3] != MAGIC:
            nxt = data.find(MAGIC, pos + 1)
            if nxt < 0:
                return
            pos = nxt
            continue
        d = zlib.decompressobj(wbits=31)
        chunks, clean = [], True
        try:
            for i in range(pos, len(data), 1 << 16):
                chunks.append(d.decompress(data[i:i + (1 << 16)]))
                if d.eof:
                    break
        except zlib.error:
            clean = False
        out = b"".join(chunks)
        yield pos, out, clean
        if clean and d.eof:
            # unused_data is everything after this member, so what it consumed
            # is the rest of the buffer minus that.
            pos = len(data) - len(d.unused_data)
        else:
            nxt = data.find(MAGIC, pos + 1)
            if nxt < 0:
                return
            pos = nxt


def read_recovered(path):
    """Return (records, stats) for a possibly damaged log.

    A truncated member can end mid-line, so the last line of a damaged member
    is dropped rather than parsed: half a JSON object is not a record. Lines
    that fail to parse are counted, never guessed at.
    """
    stats = {"members": 0, "damaged_members": 0, "records": 0, "unparsable": 0}
    if not os.path.exists(path):
        return [], stats
    with open(path, "rb") as fh:
        data = fh.read()

    records = []
    for _off, payload, clean in _members(data):
        if not payload:
            continue
        stats["members"] += 1
        if not clean:
            stats["damaged_members"] += 1
        lines = payload.split(b"\n")
        # A clean member ends with a trailing newline, so its last element is
        # empty; a damaged one ends mid-line, so its last element is a partial
        # record. Either way the tail element is not a record.
        for line in lines[:-1]:
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                stats["unparsable"] += 1
    stats["records"] = len(records)
    return records, stats


def is_clean(path):
    """True if the whole file reads through the ordinary gzip path."""
    if not os.path.exists(path):
        return True
    try:
        with gzip.open(path, "rb") as fh:
            while fh.read(1 << 20):
                pass
        return True
    except (OSError, EOFError, zlib.error):
        return False


def repair(path):
    """Rewrite a damaged log as one clean member. Returns stats, or None.

    Called before a campaign appends to an existing log, so a resume after a
    crash cannot bury the damage under a new member. The rewrite goes through a
    temporary file and a rename, because a crash *during the repair* must not
    be able to lose the records the repair was recovering.
    """
    if is_clean(path):
        return None
    records, stats = read_recovered(path)
    tmp = path + ".repair"
    with open_deterministic(tmp) as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    os.replace(tmp, path)
    return stats


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        recs, st = read_recovered(p)
        print(f"{p}: {st['records']} records, {st['members']} members, "
              f"{st['damaged_members']} damaged, clean={is_clean(p)}")
