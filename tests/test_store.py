"""E2: the segment store, generations, and the contiguity proof.

The gate for this epoch is "a fuzzer cannot produce an undetected hole", so the
centrepiece is `test_fuzz_*`: build a valid store, corrupt it the way a disk,
a neighbouring tool, or an attacker would, and require `verify` to notice every
single time. A test that only checks the happy path would pass against a
`verify` that returns `[]` unconditionally.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from gitmemory import redact, store

LINE = b'{"type":"user","uuid":"u%d","message":{"role":"user","content":"hello %d"}}\n'


def transcript(path: str, n: int, start: int = 0) -> int:
    """Append `n` JSONL lines. Returns the new size."""
    with open(path, "ab") as fh:
        for i in range(start, start + n):
            fh.write(LINE % (i, i))
    return os.path.getsize(path)


@pytest.fixture
def home(tmp_path):
    """A store home that is not inside any work tree (this repo is one)."""
    h = tmp_path / "store"
    h.mkdir()
    return str(h)


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "src" / "sess.jsonl"
    p.parent.mkdir()
    return str(p)


def manifest(home: str, gen: int = 0, agent="claude-code", sid="sess") -> dict:
    return json.loads(Path(home, "sessions", agent, sid, f"g{gen:02d}.json").read_text())


# --------------------------------------------------------------------------- #
# the proof itself
# --------------------------------------------------------------------------- #


def test_first_capture_tiles_from_zero(home, src):
    size = transcript(src, 20)
    cap = store.capture(src, "claude-code", "sess", home=home)

    assert cap.generation == 0
    assert cap.appended == size
    man = manifest(home)
    assert [(s["start"], s["end"]) for s in man["segments"]] == [(0, size)]
    assert man["file_sha256"] == hashlib.sha256(Path(src).read_bytes()).hexdigest()
    assert store.verify(home) == []


def test_appends_become_new_segments_that_tile(home, src):
    a = transcript(src, 10)
    store.capture(src, "claude-code", "sess", home=home)
    b = transcript(src, 10, start=10)
    cap = store.capture(src, "claude-code", "sess", home=home)

    assert cap.generation == 0 and cap.diverged is None
    man = manifest(home)
    assert [(s["start"], s["end"]) for s in man["segments"]] == [(0, a), (a, b)]
    assert man["size"] == b
    assert store.verify(home) == []


def test_stored_bytes_equal_bytes_written_once(home, src):
    """The point of segments over snapshots: no duplication (DESIGN.md §2.4)."""
    for i in range(6):
        transcript(src, 10, start=i * 10)
        store.capture(src, "claude-code", "sess", home=home)

    raw = os.path.join(home, "raw")
    stored = sum(
        os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(raw) for f in fs
    )
    assert stored == os.path.getsize(src), "segments duplicated bytes; snapshots in disguise"


def test_a_stranger_can_check_it_with_cat_and_shasum(home, src):
    """DESIGN.md §2.5 promises a one-liner with no Python. Run that exact one-liner."""
    transcript(src, 40)
    store.capture(src, "claude-code", "sess", home=home)
    transcript(src, 40, start=40)
    store.capture(src, "claude-code", "sess", home=home)

    man_rel = "sessions/claude-code/sess/g00.json"
    proof = subprocess.run(
        f"cat $(jq -r '.segments|sort_by(.start)|.[].path' {man_rel}) | shasum -a 256",
        shell=True, cwd=home, capture_output=True, text=True, check=True,
    )
    assert proof.stdout.split()[0] == manifest(home)["file_sha256"]


def test_nothing_new_rewrites_nothing(home, src):
    transcript(src, 10)
    store.capture(src, "claude-code", "sess", home=home)
    path = os.path.join(home, "sessions", "claude-code", "sess", "g00.json")
    before = Path(path).read_bytes()

    cap = store.capture(src, "claude-code", "sess", home=home)

    assert cap.appended == 0 and cap.segment is None
    assert Path(path).read_bytes() == before, "a no-op capture would commit a diff"


def test_empty_source_is_recorded_not_skipped(home, src):
    Path(src).write_bytes(b"")
    store.capture(src, "claude-code", "sess", home=home)
    man = manifest(home)
    assert man["segments"] == [] and man["size"] == 0
    assert man["file_sha256"] == store.EMPTY_SHA256
    assert store.verify(home) == []


def test_capture_is_byte_identical_across_runs(home, src, tmp_path):
    transcript(src, 25)
    other = str(tmp_path / "store2")
    os.mkdir(other)
    store.capture(src, "claude-code", "sess", home=home)
    store.capture(src, "claude-code", "sess", home=other)
    a = os.path.join(home, "sessions", "claude-code", "sess", "g00.json")
    b = os.path.join(other, "sessions", "claude-code", "sess", "g00.json")
    assert Path(a).read_bytes() == Path(b).read_bytes()


def test_compact_boundaries_accumulate_within_a_generation(home, src):
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home, boundaries=[128])
    transcript(src, 5, start=5)
    store.capture(src, "claude-code", "sess", home=home, boundaries=[512, 128])
    assert manifest(home)["compact_boundaries"] == [128, 512]


# --------------------------------------------------------------------------- #
# generations: what happens after divergence (DESIGN.md §2.5a)
# --------------------------------------------------------------------------- #


def test_in_place_rewrite_seals_a_generation_instead_of_losing_it(home, src):
    """The fable-pruner case. Equal-length edit, so offsets are untouched."""
    transcript(src, 12)
    store.capture(src, "claude-code", "sess", home=home)
    sealed = Path(os.path.join(home, manifest(home)["segments"][0]["path"])).read_bytes()

    data = bytearray(Path(src).read_bytes())
    data[10:14] = b"XXXX"  # same length: only the hash can see this
    Path(src).write_bytes(bytes(data))
    transcript(src, 3, start=12)

    cap = store.capture(src, "claude-code", "sess", home=home)

    assert cap.generation == 1 and cap.diverged
    assert manifest(home, 0)["segments"][0]["path"] != manifest(home, 1)["segments"][0]["path"]
    g00_seg = Path(home, manifest(home, 0)["segments"][0]["path"])
    assert g00_seg.read_bytes() == sealed, "the pre-rewrite bytes are what no other tool keeps"
    assert manifest(home, 1)["diverged_from"]["at_byte"] == manifest(home, 0)["size"]
    assert store.verify(home) == []


def test_truncation_forks_a_generation(home, src):
    transcript(src, 20)
    store.capture(src, "claude-code", "sess", home=home)
    os.truncate(src, 100)

    cap = store.capture(src, "claude-code", "sess", home=home)

    assert cap.generation == 1 and "shorter" in cap.diverged
    assert store.verify(home) == []


def test_generations_keep_stacking(home, src):
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    for gen in range(1, 4):
        # Different content each round. Rewriting 5 lines to 6 *identical* first
        # lines is a byte-level append and must not fork — the store was right
        # and the first version of this fixture was wrong.
        Path(src).write_bytes(b"")
        transcript(src, 5 + gen, start=100 * gen)
        assert store.capture(src, "claude-code", "sess", home=home).generation == gen
    assert store.verify(home) == []
    assert manifest(home, 3)["prev_manifest_sha256"] == hashlib.sha256(
        Path(os.path.join(home, "sessions", "claude-code", "sess", "g02.json")).read_bytes()
    ).hexdigest()


def test_generation_zero_may_not_claim_an_ancestor(home, src):
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    path = os.path.join(home, "sessions", "claude-code", "sess", "g00.json")
    man = json.loads(Path(path).read_text())
    man["diverged_from"] = {"generation": -1, "at_byte": 0, "prev_file_sha256": "x"}
    Path(path).write_text(json.dumps(man))
    assert any("descend" in p for p in store.verify(home))


def test_a_broken_generation_chain_is_caught(home, src):
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    Path(src).write_bytes(b"")
    transcript(src, 7, start=900)
    assert store.capture(src, "claude-code", "sess", home=home).generation == 1

    path = os.path.join(home, "sessions", "claude-code", "sess", "g01.json")
    man = json.loads(Path(path).read_text())
    man["diverged_from"]["prev_file_sha256"] = "0" * 64
    Path(path).write_text(json.dumps(man))
    assert any("diverged_from" in p for p in store.verify(home))


# --------------------------------------------------------------------------- #
# hostile input
# --------------------------------------------------------------------------- #


def test_home_inside_a_work_tree_is_refused(tmp_path):
    outer = tmp_path / "repo"
    (outer / ".git").mkdir(parents=True)
    inner = outer / "sub" / ".gitmemory"
    inner.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="work tree"):
        store.resolve_home(str(inner))


@pytest.mark.parametrize("bad", ["..", "../escape", "a/b", ".hidden", "", "x" * 200, "-rf"])
def test_path_components_are_validated(home, src, bad):
    transcript(src, 2)
    with pytest.raises(ValueError, match="unsafe"):
        store.capture(src, "claude-code", bad, home=home)
    with pytest.raises(ValueError, match="unsafe"):
        store.capture(src, bad, "sess", home=home)


def test_a_segment_path_escaping_the_store_is_refused_not_read(home, src, tmp_path):
    """A manifest from a pulled repo is untrusted data, not a map to follow anywhere."""
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)

    path = os.path.join(home, "sessions", "claude-code", "sess", "g00.json")
    man = json.loads(Path(path).read_text())
    man["segments"][0]["path"] = os.path.relpath(str(outside), home)
    Path(path).write_text(json.dumps(man))

    assert any("escapes the store" in p for p in store.verify(home))


def test_source_shrinking_mid_read_abandons_the_capture(home, src, monkeypatch):
    """The pruner race Gemini flagged: the file is rewritten while we stream it."""
    size = transcript(src, 200)
    monkeypatch.setattr(store, "CHUNK", 512)

    class Shrinking:
        def __init__(self, fh):
            self._fh, self._read = fh, 0

        def read(self, n=-1):
            data = self._fh.read(n)
            self._read += len(data)
            if self._read > size // 2 and os.path.getsize(src) > size // 4:
                os.truncate(src, size // 4)  # a neighbour rewrites it under us
            return data

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

    real_open = builtins.open
    monkeypatch.setattr(
        store,
        "open",
        lambda p, m="r", **kw: (
            Shrinking(real_open(p, m, **kw)) if p == src else real_open(p, m, **kw)
        ),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="shrank"):
        store.capture(src, "claude-code", "sess", home=home)

    seg_dir = os.path.join(home, "raw", "claude-code", "sess", "g00")
    assert os.listdir(seg_dir) == [], "a half-written segment survived the abort"
    assert not os.path.exists(os.path.join(home, "sessions", "claude-code", "sess", "g00.json"))


# --------------------------------------------------------------------------- #
# the E2 gate: no undetected hole
# --------------------------------------------------------------------------- #


def _build(home: str, src: str, chunks: int = 5) -> None:
    for i in range(chunks):
        transcript(src, 8, start=i * 8)
        store.capture(src, "claude-code", "sess", home=home)
    assert store.verify(home) == []


MUTATIONS = [
    "drop_segment_file",
    "truncate_segment",
    "extend_segment",
    "flip_byte_in_place",
    "drop_manifest_entry",
    "punch_hole",
    "overlap_segments",
    "forge_file_sha256",
    "forge_size",
    "stray_file",
    "swap_segment_contents",
]


def _mutate(home: str, which: str, rng: random.Random) -> None:
    path = os.path.join(home, "sessions", "claude-code", "sess", "g00.json")
    man = json.loads(Path(path).read_text())
    segs = man["segments"]
    i = rng.randrange(len(segs))
    full = os.path.join(home, segs[i]["path"])

    if which == "drop_segment_file":
        os.unlink(full)
    elif which == "truncate_segment":
        os.truncate(full, max(0, os.path.getsize(full) - 1 - rng.randrange(8)))
    elif which == "extend_segment":
        with open(full, "ab") as fh:
            fh.write(b"x")
    elif which == "flip_byte_in_place":
        data = bytearray(Path(full).read_bytes())
        j = rng.randrange(len(data))
        data[j] ^= 0xFF
        Path(full).write_bytes(bytes(data))
    elif which == "drop_manifest_entry":
        segs.pop(i)
    elif which == "punch_hole":
        segs[i]["start"] += 1
    elif which == "overlap_segments":
        segs[i]["end"] += 1
    elif which == "forge_file_sha256":
        man["file_sha256"] = "0" * 64
    elif which == "forge_size":
        man["size"] += rng.randrange(1, 100)
    elif which == "stray_file":
        Path(os.path.dirname(full), "000000000000-000000000001.jsonl").write_bytes(b"?")
    elif which == "swap_segment_contents":
        j = (i + 1) % len(segs)
        if j == i:
            segs[i]["sha256"] = "0" * 64
        else:
            other = os.path.join(home, segs[j]["path"])
            a, b = Path(full).read_bytes(), Path(other).read_bytes()
            Path(full).write_bytes(b)
            Path(other).write_bytes(a)
    Path(path).write_text(json.dumps(man))


@pytest.mark.parametrize("which", MUTATIONS)
def test_every_known_mutation_is_detected(tmp_path, which):
    home, src = str(tmp_path / "h"), str(tmp_path / "s.jsonl")
    os.mkdir(home)
    _build(home, src)
    _mutate(home, which, random.Random(0))
    assert store.verify(home), f"{which} produced an undetected hole"


def test_fuzz_no_mutation_slips_through(tmp_path):
    """Seeded, so a failure is reproducible rather than a flake."""
    template, src = str(tmp_path / "t"), str(tmp_path / "s.jsonl")
    os.mkdir(template)
    _build(template, src, chunks=6)

    rng = random.Random(20260920)
    for n in range(150):
        home = str(tmp_path / f"f{n}")
        shutil.copytree(template, home)
        which = rng.choice(MUTATIONS)
        _mutate(home, which, rng)
        assert store.verify(home), f"iteration {n}: {which} produced an undetected hole"
        shutil.rmtree(home)


# --------------------------------------------------------------------------- #
# the egress gate
# --------------------------------------------------------------------------- #

SECRETS = {
    "anthropic_api_key": b"sk-ant-api03-" + b"A1b2_-" * 15,
    "private_key_block": b"-----BEGIN OPENSSH PRIVATE KEY-----",
    "aws_access_key_id": b"AKIAIOSFODNN7EXAMPLE",
    "github_token": b"ghp_" + b"a" * 36,
    "slack_token": b"xoxb-123456789012-abcdefghijkl",
    "google_api_key": b"AIza" + b"b" * 35,
    "stripe_secret": b"sk_live_" + b"c" * 24,
}


@pytest.mark.parametrize("name,blob", sorted(SECRETS.items()))
def test_high_confidence_secrets_block_egress(name, blob):
    found = redact.scan_bytes(b"prefix " + blob + b" suffix", "t.jsonl")
    assert name in {f.detector for f in found}, f"{name} would have been pushed"
    assert all(f.tier == "high" for f in found if f.detector == name)


def test_a_finding_never_carries_the_secret():
    blob = b"sk-ant-api03-" + b"A1b2_-" * 15
    for f in redact.scan_bytes(blob):
        assert blob[4:].decode() not in str(f), "the gate published what it found"


def test_our_own_manifests_do_not_trip_the_gate(home, src):
    """A gate that fires on every sha256 is a gate nobody leaves on."""
    transcript(src, 30)
    store.capture(src, "claude-code", "sess", home=home)
    ok, findings = redact.gate(
        [os.path.join(d, f) for d, _, fs in os.walk(home) for f in fs]
    )
    assert ok and findings == [], f"false positives on a clean store: {findings}"


def test_suspect_tier_reports_without_blocking(tmp_path):
    noisy = tmp_path / "notes.md"
    noisy.write_bytes(b'password = "hunter2hunter2hunter2"\n')
    ok, findings = redact.gate([str(noisy)])
    assert ok, "a suspect-tier match must not block egress on its own"
    assert [f.detector for f in findings] == ["assigned_secret"]


def test_push_is_denied_by_default(home):
    allowed, why = redact.push_allowed(home, "origin")
    assert not allowed and "opt-in" in why


@pytest.mark.parametrize(
    "cfg,expect",
    [
        ('[remote.origin]\nurl = "git@h:r.git"\n', "allow_push"),
        ("[remote.other]\nurl = \"x\"\nallow_push = true\n", "no [remote.origin]"),
        ('[remote.origin]\nallow_push = true\n', "no url"),
    ],
)
def test_push_stays_denied_until_every_field_is_set(home, cfg, expect):
    Path(os.path.join(home, "config.toml")).write_text(cfg)
    allowed, why = redact.push_allowed(home, "origin")
    assert not allowed and expect in why


def test_push_allowed_only_when_explicitly_opted_in(home):
    Path(os.path.join(home, "config.toml")).write_text(
        '[remote.origin]\nurl = "git@h:r.git"\nallow_push = true\n'
    )
    allowed, url = redact.push_allowed(home, "origin")
    assert allowed and url == "git@h:r.git"


# --------------------------------------------------------------------------- #
# adapter + store, end to end through the CLI
# --------------------------------------------------------------------------- #

COMPACTED = [
    b'{"type":"user","uuid":"u1","sessionId":"s","message":{"role":"user","content":"one"}}\n',
    b'{"type":"system","subtype":"compact_boundary","uuid":"c1","sessionId":"s"}\n',
    b'{"type":"user","uuid":"u2","sessionId":"s","isCompactSummary":true,'
    b'"message":{"role":"user","content":"summary"}}\n',
]


def test_cli_capture_records_the_compaction_boundary_the_adapter_found(tmp_path, capsys):
    from gitmemory.__main__ import main

    home, src = str(tmp_path / "h"), str(tmp_path / "s.jsonl")
    os.mkdir(home)
    Path(src).write_bytes(b"".join(COMPACTED))

    assert main(["--home", home, "capture", src, "--session-id", "sess"]) == 0
    assert manifest(home)["compact_boundaries"] == [len(COMPACTED[0])]
    assert main(["--home", home, "verify"]) == 0


def test_cli_verify_exits_nonzero_on_a_hole(tmp_path):
    from gitmemory.__main__ import main

    home, src = str(tmp_path / "h"), str(tmp_path / "s.jsonl")
    os.mkdir(home)
    _build(home, src, chunks=3)
    _mutate(home, "drop_segment_file", random.Random(1))
    assert main(["--home", home, "verify"]) == 1


def test_cli_push_refuses_with_no_config(tmp_path, capsys):
    from gitmemory.__main__ import main

    home = str(tmp_path / "h")
    os.mkdir(home)
    assert main(["--home", home, "push"]) == 1
    assert "refusing to push" in capsys.readouterr().err
