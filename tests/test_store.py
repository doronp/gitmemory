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
    stored = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(raw) for f in fs)
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
        shell=True,
        cwd=home,
        capture_output=True,
        text=True,
        check=True,
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


def test_a_boundary_outside_the_bytes_is_refused_rather_than_recorded(home, src):
    """An offset the store cannot resolve is not a fact about this generation.

    The daemon re-stats the source around the parse to avoid producing one, but
    that is a mitigation with a window in it, and the store used to write down
    whatever it was handed: a boundary past EOF, or a negative one, became a
    permanent entry that `verify` called clean and the next capture carried
    forward for the life of the session. [E4, review: Gemini r3 §4]
    """
    transcript(src, 5)
    size = os.path.getsize(src)

    cap = store.capture(
        src, "claude-code", "sess", home=home, boundaries=[-1, 0, 8, size, size + 1]
    )

    assert manifest(home)["compact_boundaries"] == [0, 8, size], "an unresolvable offset survived"
    assert cap.dropped_boundaries == 2, "the drop was silent"


def test_a_bad_boundary_already_in_a_manifest_heals_on_the_next_capture(home, src):
    """The filter is on the union, not on the argument, so a store written by a
    version that had no floor stops carrying its bad offsets forward."""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    path = Path(home, "sessions", "claude-code", "sess", "g00.json")
    man = json.loads(path.read_text())
    man["compact_boundaries"] = [8, 10**9]
    path.write_text(json.dumps(man))

    transcript(src, 5, start=5)
    cap = store.capture(src, "claude-code", "sess", home=home)

    assert manifest(home)["compact_boundaries"] == [8]
    assert cap.dropped_boundaries == 1


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
    assert (
        manifest(home, 3)["prev_manifest_sha256"]
        == hashlib.sha256(
            Path(os.path.join(home, "sessions", "claude-code", "sess", "g02.json")).read_bytes()
        ).hexdigest()
    )


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
    ok, findings = redact.gate([os.path.join(d, f) for d, _, fs in os.walk(home) for f in fs])
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
        ('[remote.other]\nurl = "x"\nallow_push = true\n', "no [remote.origin]"),
        ("[remote.origin]\nallow_push = true\n", "no url"),
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


# --------------------------------------------------------------------------- #
# E2 standalone review: tests for the twenty confirmed findings
#
# Every test below existed as a mutant that survived the 451-test suite. The
# fixes shipped first and broke exactly one test, which is itself the finding:
# a suite that does not notice a session lock, a case fold, an orphan-adoption
# pass and a gate that now raises was not testing those surfaces at all.
# --------------------------------------------------------------------------- #

import threading  # noqa: E402 - kept with the block it serves

GHP = b"ghp_" + b"A" * 36
AKIA = b"AKIAZZZZQQQQWWWW1234"


def _seal(home: str, man: dict, gen: int = 0) -> None:
    """Write a crafted manifest back without repairing what it now contradicts."""
    Path(home, "sessions", "claude-code", "sess", f"g{gen:02d}.json").write_text(json.dumps(man))


def _sorted_segs(man: dict) -> list[dict]:
    return sorted(man["segments"], key=lambda s: s["start"])


# --- the gate has to be able to say no ------------------------------------- #


def test_the_gate_says_no_to_a_credential(tmp_path):
    """[E2] Not one of 451 tests made `gate` return False. Five mutants lived."""
    p = tmp_path / "leak.txt"
    p.write_bytes(b"token=" + GHP + b"\n")

    ok, findings = redact.gate([str(p)])

    assert ok is False, "a github token is a HIGH finding and must block egress"
    assert [f.detector for f in findings] == ["github_token"]
    assert findings[0].tier == "high"


def test_the_gate_scans_every_path_not_just_the_first(tmp_path):
    """[E2] `paths[0]`-only and `any`->`all` both survived the old suite."""
    clean, dirty = tmp_path / "a.txt", tmp_path / "b.txt"
    clean.write_bytes(b"nothing here\n")
    dirty.write_bytes(GHP + b"\n")

    assert redact.gate([str(clean), str(dirty)])[0] is False
    assert redact.gate([str(dirty), str(clean)])[0] is False


def test_the_gate_refuses_to_attest_to_nothing():
    """[E2] `not any([])` is True, which is how a filter bug became a bypass."""
    with pytest.raises(ValueError, match="nothing to scan"):
        redact.gate([])

    ok, findings = redact.gate([], allow_empty=True)
    assert (ok, findings) == (True, []), "an explicit caller may still opt in"


def test_a_suspect_finding_reports_but_does_not_block(tmp_path):
    """Two tiers, or the gate is either useless or unpassable."""
    p = tmp_path / "cfg.py"
    p.write_bytes(b'password = "hunter2hunter2"\n')

    ok, findings = redact.gate([str(p)])
    assert ok is True
    assert [f.tier for f in findings] == ["suspect"]


def test_a_finding_never_carries_the_value_it_found(tmp_path):
    """The gate's own docstring: printing the secret publishes it."""
    p = tmp_path / "leak.txt"
    p.write_bytes(GHP + b"\n")

    (f,) = redact.gate([str(p)])[1]
    assert GHP.decode() not in str(f)
    assert "ghp_" in f.shape and "40 bytes" in f.shape


def test_a_secret_in_a_session_id_is_caught_before_it_becomes_a_tree_entry(tmp_path):
    """[E2] `_SAFE_RE` happily accepts a 103-character API key as a directory."""
    d = tmp_path / GHP.decode()
    d.mkdir()
    (d / "g00.json").write_bytes(b"{}\n")

    ok, findings = redact.gate([str(d / "g00.json")])
    assert ok is False
    assert any(f.detector == "github_token" for f in findings)


def test_a_credential_split_across_two_segments_is_caught(home, src):
    """[E2] The store cuts wherever EOF was; what leaves is the concatenation."""
    transcript(src, 4)
    with open(src, "ab") as fh:
        fh.write(b'{"k":"' + AKIA[:10])
    store.capture(src, "claude-code", "sess", home=home)
    with open(src, "ab") as fh:
        fh.write(AKIA[10:] + b'"}\n')
    store.capture(src, "claude-code", "sess", home=home)

    groups = store.segment_groups(home)
    assert len(groups[0]) == 2
    for path in groups[0]:
        assert redact.scan_path(path) == [], "neither half matches on its own"

    ok, findings = redact.gate([], groups, allow_empty=True)
    assert ok is False, "the bytes that leave are the concatenation"
    seam = [f for f in findings if " + " in f.path]
    assert [f.detector for f in seam] == ["aws_access_key_id"]


def test_a_hostile_manifest_field_cannot_opt_a_segment_run_out_of_the_seam_scan(home, src):
    """[E3] `sessions()` reads metadata a pushed manifest supplies, and a reader
    that dropped a row for a bad *metadata* field would let that manifest
    exclude itself from `segment_groups` — and with it, the seam window. Every
    segment is still scanned individually by the file walk, so the loss is
    exactly the straddling secret, which is the one the group scan exists for.
    """
    transcript(src, 4)
    with open(src, "ab") as fh:
        fh.write(b'{"k":"' + AKIA[:10])
    store.capture(src, "claude-code", "sess", home=home)
    with open(src, "ab") as fh:
        fh.write(AKIA[10:] + b'"}\n')
    store.capture(src, "claude-code", "sess", home=home)
    _seal(home, manifest(home) | {"agent": "../evil", "generation": "one", "size": None})

    groups = store.segment_groups(home)
    assert len(groups) == 1 and len(groups[0]) == 2
    ok, _ = redact.gate([], groups, allow_empty=True)
    assert ok is False


def test_a_seam_finding_is_not_reported_twice(home, src):
    """Only matches that genuinely span a cut belong to the seam."""
    transcript(src, 4)
    with open(src, "ab") as fh:
        fh.write(b'{"k":"' + AKIA + b'"}\n')
    store.capture(src, "claude-code", "sess", home=home)
    transcript(src, 4, start=4)
    store.capture(src, "claude-code", "sess", home=home)

    findings = redact.scan_group(store.segment_groups(home)[0])
    assert [f.detector for f in findings] == ["aws_access_key_id"]
    assert " + " not in findings[0].path, "wholly inside one segment, not a seam"


def test_a_remote_url_password_is_never_printed(tmp_path):
    """[E2] The gate used to print the credential it was handed, on success."""
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.toml").write_text(
        '[remote.origin]\nurl = "https://me:s3cr3t-pw@github.com/me/p.git"\nallow_push = true\n'
    )

    allowed, why = redact.push_allowed(str(home), "origin")
    assert allowed is True
    assert "s3cr3t-pw" not in why
    assert why == "https://github.com/me/p.git"
    assert redact.safe_url("git@github.com:me/p.git") == "git@github.com:me/p.git"


# --- the CLI push path, which the old suite never reached ------------------ #


@pytest.mark.parametrize("name", ["store", ".gitmemory"])
def test_cli_push_is_blocked_by_the_gate(tmp_path, capsys, name):
    """[E2] `".git" not in d` is a substring test, and the default home is
    `~/.gitmemory` — so every file was filtered out, the gate scanned nothing,
    and push was allowed. Parametrised on the name that caused it."""
    from gitmemory.__main__ import main

    home, source = tmp_path / name, tmp_path / "s.jsonl"
    home.mkdir()
    (home / "config.toml").write_text(
        '[remote.origin]\nurl = "git@github.com:me/p.git"\nallow_push = true\n'
    )
    transcript(str(source), 4)
    with open(source, "ab") as fh:
        fh.write(b'{"k":"' + GHP + b'"}\n')
    assert main(["--home", str(home), "capture", str(source), "--session-id", "sess"]) == 0
    capsys.readouterr()

    assert main(["--home", str(home), "push"]) == 1
    err = capsys.readouterr().err
    assert "the redaction gate found credentials" in err
    assert "gate passed" not in err
    assert GHP.decode() not in err


@pytest.mark.parametrize("name", ["store", ".gitmemory"])
def test_cli_push_scans_files_that_are_not_segments(tmp_path, capsys, name):
    """[E2] Segments reach the gate as ordered groups, so a secret in the raw
    tree is caught even by a broken file filter. Everything else in the store —
    manifests, config, derived output — only reaches it through the walk."""
    from gitmemory.__main__ import main

    home, source = tmp_path / name, tmp_path / "s.jsonl"
    home.mkdir()
    (home / "config.toml").write_text(
        '[remote.origin]\nurl = "git@github.com:me/p.git"\nallow_push = true\n'
    )
    transcript(str(source), 8)
    assert main(["--home", str(home), "capture", str(source), "--session-id", "sess"]) == 0
    (home / "derived").mkdir()
    (home / "derived" / "notes.md").write_bytes(b"key: " + GHP + b"\n")
    capsys.readouterr()

    assert main(["--home", str(home), "push"]) == 1
    assert "the redaction gate found credentials" in capsys.readouterr().err


def test_cli_push_reaches_the_gate_on_a_clean_store(tmp_path, capsys):
    """The negative control: without it, "blocked" could just mean "crashed"."""
    from gitmemory.__main__ import main

    home, source = tmp_path / ".gitmemory", tmp_path / "s.jsonl"
    home.mkdir()
    (home / "config.toml").write_text(
        '[remote.origin]\nurl = "git@github.com:me/p.git"\nallow_push = true\n'
    )
    transcript(str(source), 8)
    assert main(["--home", str(home), "capture", str(source), "--session-id", "sess"]) == 0
    capsys.readouterr()

    assert main(["--home", str(home), "push"]) == 1  # E2 has no transport yet
    err = capsys.readouterr().err
    assert "gate passed" in err
    assert "found credentials" not in err


# --- verify's checks, each deletable while the suite stayed green ---------- #


def test_the_tiling_check_is_not_deletable(home, src):
    """[E2] A self-consistent manifest with a 5-byte overlap passed every other
    check, so `verify`'s central invariant could be removed with 451 green."""
    _build(home, src, chunks=2)
    man = manifest(home)
    segs = _sorted_segs(man)
    segs[1]["start"] -= 5
    segs[1]["end"] -= 5
    man["segments"], man["size"] = segs, segs[1]["end"]
    _seal(home, man)

    problems = store.verify(home)
    assert [p for p in problems if "overlap at byte" in p], problems
    assert len(problems) == 1, f"the craft should trip exactly one check: {problems}"


def test_the_segment_length_check_is_not_deletable(home, src):
    """[E2] A short-ranged segment tiles, hashes, and totals correctly."""
    _build(home, src, chunks=2)
    man = manifest(home)
    segs = _sorted_segs(man)
    for key in ("end",):
        segs[0][key] -= 5
    segs[1]["start"] -= 5
    segs[1]["end"] -= 5
    man["segments"], man["size"] = segs, segs[1]["end"]
    _seal(home, man)

    problems = store.verify(home)
    assert [p for p in problems if "manifest says" in p], problems
    assert len(problems) == 1, f"the craft should trip exactly one check: {problems}"


def test_the_per_segment_hash_check_is_not_deletable(home, src):
    """[E2] Repairing only `file_sha256` after an edit left every other check
    happy, so the per-segment digest was dead weight the suite never exercised."""
    _build(home, src, chunks=2)
    man = manifest(home)
    segs = _sorted_segs(man)
    target = Path(home, segs[0]["path"])
    data = bytearray(target.read_bytes())
    data[10] ^= 0x20
    target.write_bytes(bytes(data))
    whole = hashlib.sha256()
    for s in segs:
        whole.update(Path(home, s["path"]).read_bytes())
    man["file_sha256"] = whole.hexdigest()
    _seal(home, man)

    problems = store.verify(home)
    assert [p for p in problems if "content changed" in p], problems
    assert len(problems) == 1, f"the craft should trip exactly one check: {problems}"


def test_a_misfiled_manifest_is_caught(home, src):
    """[E2] `generation` drives every path below it; a mismatch must stop there.

    Three problems, not one, since E4 added the reverse sweep: renaming the
    manifest out of the way is also what leaves `raw/.../g00` with nothing
    attesting it, and that is a true and separate fact about each of the two
    segments in there. What the count is still guarding is the original point —
    that a bad generation is not then *used*, producing a cascade of invented
    paths under `g01`.

    One finding per file rather than one per directory: the sweep now walks the
    whole of `raw/` instead of a pinned depth, and a walk has no directory to
    aggregate to. [E4, review: store 2]
    """
    _build(home, src, chunks=2)
    d = Path(home, "sessions", "claude-code", "sess")
    (d / "g00.json").rename(d / "g01.json")

    problems = store.verify(home)
    assert [p for p in problems if "filed as g01.json" in p], problems
    unattested = [p for p in problems if "g00/" in p and "no manifest speaks" in p]
    assert len(unattested) == 2, problems
    assert len(problems) == 3, f"a bad generation must not be fed to a path: {problems}"


def test_a_dotfile_in_a_generation_directory_is_a_stray(home, src):
    """[E2] `glob("*")` skips dotfiles, so `.incoming.*` — real unattested
    transcript bytes — was the one artefact the stray check could not see."""
    _build(home, src, chunks=2)
    seg_dir = Path(home, "raw", "claude-code", "sess", "g00")
    (seg_dir / ".evil.jsonl").write_bytes(b'{"smuggled":true}\n')

    assert [p for p in store.verify(home) if ".evil.jsonl" in p]


def test_one_unparseable_manifest_does_not_hide_tampering_elsewhere(tmp_path, monkeypatch):
    """[E2] A crash in the sweep suppressed every later session's report and
    blamed the exception, which reads as "verify is broken", not "store is".

    The manifest below is well-formed JSON that survives every internal check
    and then raises in `os.path.join` — the shape the internal `except` clauses
    are not for. Sessions are swept in sorted order, so `aaa` crashes first."""
    home = str(tmp_path / "store")
    os.mkdir(home)
    for sid in ("aaa", "zzz"):
        source = str(tmp_path / f"{sid}.jsonl")
        transcript(source, 8)
        store.capture(source, "claude-code", sid, home=home)

    bad = manifest(home, sid="aaa")
    Path(home, "sessions", "claude-code", "aaa", "g00.json").write_text(json.dumps(bad))
    tampered = _sorted_segs(manifest(home, sid="zzz"))[0]["path"]
    Path(home, tampered).write_bytes(b"replaced\n")

    # Simulate a crash inside _verify_manifest
    orig_scandir = os.scandir

    def mock_scandir(path):
        if "aaa" in str(path) and "raw" in str(path):
            raise TypeError("simulated crash")
        return orig_scandir(path)

    monkeypatch.setattr(os, "scandir", mock_scandir)

    problems = store.verify(home)
    assert [p for p in problems if "aaa/g00.json" in p and "unverifiable manifest" in p], problems
    assert [p for p in problems if "zzz/g00.json" in p], (
        "the crashing manifest must not mask the real corruption behind it"
    )


def test_an_unreadable_manifest_is_one_problem_not_a_stop(home, src):
    _build(home, src, chunks=1)
    Path(home, "sessions", "claude-code", "sess", "g00.json").write_text("{not json")
    assert [p for p in store.verify(home) if "unreadable manifest" in p]


def test_a_manifest_that_is_not_an_object_is_a_problem_not_a_crash(home, src):
    _build(home, src, chunks=1)
    _seal(home, [])  # type: ignore[arg-type]
    assert [p for p in store.verify(home) if "not an object" in p]


def test_a_manifest_with_a_malformed_segment_entry_is_a_problem(home, src):
    _build(home, src, chunks=1)
    man = manifest(home)
    man["segments"] = [{"start": "0", "end": 10, "path": "x", "sha256": "y"}]
    _seal(home, man)
    assert [p for p in store.verify(home) if "malformed segment" in p]


# --- durability, which is invisible to a functional assertion -------------- #


def test_durability_is_not_optional(home, src, monkeypatch):
    """[E2] Removing the atomic rename and every fsync left 451/451 green: the
    bytes land either way, and only a crash tells the difference. So assert the
    syscalls, which is the only observable a passing process has."""
    fsynced: list[int] = []
    replaced: list[tuple[str, str]] = []
    real_fsync, real_replace = os.fsync, os.replace

    monkeypatch.setattr(os, "fsync", lambda fd: (fsynced.append(fd), real_fsync(fd))[1])
    monkeypatch.setattr(
        os, "replace", lambda a, b: (replaced.append((str(a), str(b))), real_replace(a, b))[1]
    )
    transcript(src, 8)
    store.capture(src, "claude-code", "sess", home=home)

    dsts = [b for _, b in replaced]
    assert any(d.endswith(".jsonl") for d in dsts), "the segment was not published atomically"
    assert any(d.endswith("g00.json") for d in dsts), "the manifest was not published atomically"
    assert all(a != b for a, b in replaced)
    assert len(fsynced) >= 4, f"file and directory fsyncs, both publications: {len(fsynced)}"


# --- concurrency and crash recovery ---------------------------------------- #


def test_concurrent_captures_never_leave_the_store_unverifiable(home, src):
    """[E2] Two captures racing published overlapping segments and one of the
    two manifests, leaving a store that `verify` calls broken forever."""
    transcript(src, 4)
    errors: list[BaseException] = []
    done = threading.Event()

    def grow() -> None:
        try:
            for i in range(1, 250):
                transcript(src, 1, start=i)
        finally:
            done.set()

    def snap() -> None:
        try:
            while not done.is_set():
                store.capture(src, "claude-code", "sess", home=home)
            store.capture(src, "claude-code", "sess", home=home)
        except BaseException as exc:  # noqa: BLE001 - the test is what it is
            errors.append(exc)

    threads = [threading.Thread(target=grow)] + [threading.Thread(target=snap) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    assert store.verify(home) == []
    assert manifest(home)["size"] == os.path.getsize(src)


def test_a_crashed_capture_is_adopted_on_the_next_run(home, src):
    """[E2] Segment published, process killed before the manifest. Nothing
    reclaimed it, and no command existed that could."""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    base = manifest(home)["size"]
    end = transcript(src, 5, start=5)
    with open(src, "rb") as fh:
        fh.seek(base)
        tail = fh.read()
    seg_dir = Path(home, "raw", "claude-code", "sess", "g00")
    (seg_dir / f"{base:012d}-{end:012d}.jsonl").write_bytes(tail)

    assert store.verify(home) != [], "the wedged state the crash leaves"
    store.capture(src, "claude-code", "sess", home=home)
    assert store.verify(home) == [], "the next capture finishes what the crash started"
    man = manifest(home)
    assert man["size"] == end
    assert man["file_sha256"] == hashlib.sha256(Path(src).read_bytes()).hexdigest()
    assert man["generation"] == 0, "adoption continues the generation, it does not fork"


def test_an_orphan_holding_pruned_bytes_is_kept_not_overwritten(home, src):
    """[E2] The fable-pruner case. The orphan is the only surviving copy of the
    pre-rewrite bytes, so deleting it is the exact loss generations exist to
    prevent — and `os.replace` onto the colliding name is deleting it."""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    base = manifest(home)["size"]

    original = b'{"type":"user","content":"ORIGINAL-%02d"}\n' % 0 * 5
    with open(src, "ab") as fh:
        fh.write(original)
    end = os.path.getsize(src)
    seg_dir = Path(home, "raw", "claude-code", "sess", "g00")
    (seg_dir / f"{base:012d}-{end:012d}.jsonl").write_bytes(original)

    # the pruner rewrites those bytes in place, same length, different content
    with open(src, "r+b") as fh:
        fh.seek(base)
        fh.write(b'{"type":"user","content":"REDACTED-%02d"}\n' % 0 * 5)

    store.capture(src, "claude-code", "sess", home=home)

    assert store.verify(home) == []
    assert manifest(home, gen=1)["diverged_from"]["at_byte"] == end
    kept = b"".join(Path(home, s["path"]).read_bytes() for s in _sorted_segs(manifest(home, gen=0)))
    assert b"ORIGINAL" in kept, "the only copy of the pruned bytes"
    assert b"REDACTED" in Path(src).read_bytes()


def test_a_colliding_segment_is_never_overwritten(home, src):
    """[E2] Adoption takes a whole orphan; a *torn* one it correctly refuses,
    and the next capture then produces that exact filename. `os.replace` would
    destroy the partial evidence of what the crash was doing."""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    base = manifest(home)["size"]
    end = transcript(src, 5, start=5)
    seg_dir = Path(home, "raw", "claude-code", "sess", "g00")
    torn = seg_dir / f"{base:012d}-{end:012d}.jsonl"
    torn.write_bytes(b"x" * (end - base - 3))  # short: never fully published

    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        store.capture(src, "claude-code", "sess", home=home)
    assert torn.read_bytes() == b"x" * (end - base - 3), "the torn segment survived"
    assert [p for p in store.verify(home) if "unrecorded file" in p]


def test_a_torn_temp_file_is_swept_not_adopted(home, src):
    """`.incoming.*` never became a segment; removing it is what the killed
    process would have done, and it is what `verify` cannot see."""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    seg_dir = Path(home, "raw", "claude-code", "sess", "g00")
    (seg_dir / ".incoming.4242.beef").write_bytes(b"half a line")
    assert [p for p in store.verify(home) if ".incoming" in p]

    transcript(src, 5, start=5)
    store.capture(src, "claude-code", "sess", home=home)
    assert store.verify(home) == []
    assert not (seg_dir / ".incoming.4242.beef").exists()


# --- identity: agent, session, and source ---------------------------------- #


def test_an_agent_name_differing_only_in_case_is_one_session(home, src):
    """[E2] APFS and NTFS are case-insensitive and case-preserving, so two
    manifests named one directory; `verify` compares strings and stayed red."""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    transcript(src, 5, start=5)
    cap = store.capture(src, "CLAUDE-Code", "SESS", home=home)

    assert store.verify(home) == []
    assert "claude-code/sess" in cap.manifest_path.replace(os.sep, "/")
    assert manifest(home)["size"] == os.path.getsize(src)


def test_a_session_id_refuses_a_second_source(home, src, tmp_path):
    """[E2] Two transcripts in one session "diverged" past each other forever,
    re-copying both in full on every capture — unbounded duplication that
    `verify` calls clean, which is DESIGN.md §2.4 inverted."""
    other = str(tmp_path / "src" / "other.jsonl")
    transcript(src, 5)
    transcript(other, 5)
    store.capture(src, "claude-code", "sess", home=home)

    with pytest.raises(ValueError, match="pass a distinct --session-id"):
        store.capture(other, "claude-code", "sess", home=home)


def test_the_session_tag_is_wide_enough_that_the_refusal_stays_unreachable(tmp_path):
    """A narrow tag turns a refusal into a transcript that stops being captured.

    `test_a_session_id_refuses_a_second_source` above is the floor under a
    collision, and under the watcher that floor costs a session: there is no
    `--session-id` to pass, so the loser is simply never captured again. At 32
    bits the odds reached 50% around 77,000 transcripts sharing a stem, which is
    not a number a long-lived agent directory is safe from. Width is the only
    thing standing between a documented refusal and a silent stop, so it is
    asserted rather than left to a comment. [E4, review: Gemini r3 §5]
    """
    a = str(tmp_path / "a" / "session.jsonl")
    b = str(tmp_path / "b" / "session.jsonl")

    assert store.session_id_for(a) == store.session_id_for(a), "not stable for one path"
    assert store.session_id_for(a) != store.session_id_for(b), "the path is not in the name"
    tag = store.session_id_for(a).rsplit("-", 1)[1]
    assert len(tag) == 16, f"the tag narrowed to {len(tag) * 4} bits"


def test_cli_session_ids_do_not_collide_on_basename(tmp_path):
    """The default id is where that collision actually came from."""
    from gitmemory.__main__ import main

    home = tmp_path / "h"
    home.mkdir()
    sources = []
    for proj in ("projA", "projB"):
        d = tmp_path / proj
        d.mkdir()
        p = d / "session.jsonl"
        transcript(str(p), 10)
        sources.append(str(p))

    for _ in range(3):
        for p in sources:
            assert main(["--home", str(home), "capture", p, "--no-parse"]) == 0

    stored = sum(f.stat().st_size for f in (home / "raw").rglob("*.jsonl"))
    written = sum(os.path.getsize(p) for p in sources)
    assert stored == written, "each byte copied exactly once, not once per capture"
    assert store.verify(str(home)) == []


def test_a_symlinked_home_does_not_bypass_the_work_tree_guard(tmp_path):
    """[E2] `abspath` does not resolve symlinks, so a link outside a repo
    pointing into one defeated the check that keeps raw out of git."""
    outer = tmp_path / "repo"
    (outer / ".git").mkdir(parents=True)
    real = outer / "store"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    with pytest.raises(RuntimeError, match="work tree"):
        store.resolve_home(str(link))


# --- malformed input must never cost the bytes ----------------------------- #


def _hostile_jsonl(path) -> None:
    """Two lines `json` refuses in ways that are not JSONDecodeError."""
    with open(path, "wb") as fh:
        fh.write(LINE % (0, 0))
        fh.write(b'{"deep":' + b"[" * 30_000 + b"]" * 30_000 + b"}\n")  # RecursionError
        fh.write(b'{"huge":' + b"9" * 5000 + b"}\n")  # ValueError, 4300-digit limit
        fh.write(LINE % (1, 1))


def test_iter_records_survives_a_line_json_refuses(tmp_path):
    """[E2] `except json.JSONDecodeError` let both of these escape the reader,
    so one malformed line made a whole session uncapturable."""
    from gitmemory import jsonl

    source = tmp_path / "s.jsonl"
    _hostile_jsonl(source)
    errors: list[Exception] = []
    records = list(jsonl.iter_records(str(source), on_error=lambda _n, e: errors.append(e)))

    assert [r.lineno for r in records] == [1, 4], "reading resumed after each bad line"
    assert {type(e).__name__ for e in errors} == {"RecursionError", "ValueError"}


def test_a_line_json_cannot_parse_does_not_cost_the_capture(tmp_path):
    """The bytes are the product; parsing is only how boundaries are found."""
    from gitmemory.__main__ import main

    home, source = tmp_path / "h", tmp_path / "s.jsonl"
    home.mkdir()
    _hostile_jsonl(source)

    assert main(["--home", str(home), "capture", str(source), "--session-id", "sess"]) == 0
    assert manifest(str(home))["size"] == os.path.getsize(source)
    assert store.verify(str(home)) == []


def test_the_console_script_target_exists():
    """`pyproject.toml` named `gitmemory.cli:main`, and there is no `cli`
    module — so `pip install gitmemory; gitmemory` was an ImportError. Nothing
    in a test suite imports through an entry point, so nothing caught it."""
    import importlib
    import tomllib

    with open(Path(__file__).parent.parent / "pyproject.toml", "rb") as fh:
        target = tomllib.load(fh)["project"]["scripts"]["gitmemory"]
    module, _, attr = target.partition(":")
    assert callable(getattr(importlib.import_module(module), attr))


# --- the trust root: a manifest is untrusted data -------------------------- #


def rewrite(home: str, mutate) -> str:
    """Put the g00 manifest through `mutate` and write it back. Returns its path."""
    path = os.path.join(home, "sessions", "claude-code", "sess", "g00.json")
    man = json.loads(Path(path).read_text())
    mutate(man)
    Path(path).write_text(json.dumps(man))
    return path


def two_segments(home, src) -> None:
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    transcript(src, 5, start=10)
    store.capture(src, "claude-code", "sess", home=home)


def test_a_reordered_manifest_is_read_in_offset_order(home, src):
    """`verify` sorted by start and `sessions` did not, so a manifest whose
    segments were swapped passed the proof and then handed every caller a
    concatenation that was never the transcript."""
    two_segments(home, src)
    rewrite(home, lambda m: m["segments"].reverse())

    assert store.verify(home) == [], "verify sorts, so it still sees a clean tiling"
    (stored,) = store.sessions(home)
    assert [os.path.basename(p) for p in stored.segments] == sorted(
        os.path.basename(s["path"]) for s in manifest(home)["segments"]
    )


def test_a_segment_path_outside_the_store_fails_the_read(home, src):
    """Not a skip. `segment_groups` feeds the egress gate, so a run dropped for
    naming an outside path is a run the seam scan never sees."""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    rewrite(home, lambda m: m["segments"][0].update(path="../escaping-path.jsonl"))

    with pytest.raises(store.EscapingSegment, match="escapes the store"):
        store.sessions(home)


def test_a_truncated_manifest_is_skipped_not_raised(home, src):
    """`json.JSONDecodeError` is a `ValueError`, so re-raising ValueError out of
    `sessions` to report an escaping path made the ordinary half-written
    manifest a full disk leaves behind cost the whole store."""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    store.capture(src, "claude-code", "other", home=home)
    path = os.path.join(home, "sessions", "claude-code", "sess", "g00.json")
    Path(path).write_text(Path(path).read_text()[:23])

    assert [s.session_id for s in store.sessions(home)] == ["other"]


def test_a_malformed_segment_entry_costs_its_generation_not_one_segment(home, src):
    """Skipping just the entry leaves a run one segment short, which reads as a
    shorter transcript rather than a broken one: every offset past the gap
    silently addresses the wrong bytes."""
    two_segments(home, src)
    rewrite(home, lambda m: m["segments"].__setitem__(0, "not-a-dict"))

    assert store.sessions(home) == []
    assert any("malformed segment entry" in p for p in store.verify(home))


def test_a_segment_entry_with_an_unsortable_start_costs_only_its_generation(home, src):
    """`sessions` has to call this entry malformed for the same reason `verify`
    does: ordering by a start it cannot read is ordering by nothing, and the
    two readers disagreeing about what a valid manifest is has been the shape
    of every bug in this file."""
    two_segments(home, src)
    store.capture(src, "claude-code", "other", home=home)
    rewrite(home, lambda m: m["segments"][0].update(start="0"))

    assert [s.session_id for s in store.sessions(home)] == ["other"]
    assert any("malformed segment entry" in p for p in store.verify(home))


def test_a_credential_split_across_three_segments_is_caught_at_the_seams(home, src):
    """No single segment holds the key, and no single file scan can see it."""
    transcript(src, 2)
    for piece in (b'{"k":"' + AKIA[:5], AKIA[5:15], AKIA[15:] + b'"}\n'):
        with open(src, "ab") as fh:
            fh.write(piece)
        store.capture(src, "claude-code", "sess", home=home)

    groups = store.segment_groups(home)
    assert len(groups[0]) == 3
    assert all(redact.scan_path(p) == [] for p in groups[0]), "no one segment matches"

    ok, findings = redact.gate([], groups, allow_empty=True)
    assert ok is False, "but the bytes that would leave are the concatenation"
    assert [f.detector for f in findings if " + " in f.path] == ["aws_access_key_id"]


def test_a_manifest_naming_an_unsafe_agent_is_reported_not_trusted(home, src):
    """`agent` and `session_id` are path components everywhere else in the
    store, and `verify` was the one reader that took them on trust."""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    rewrite(home, lambda m: m.update(agent="/absolute-path-unsafe"))

    assert any("unsafe agent name" in p for p in store.verify(home))


def test_an_escaping_segment_path_stops_orphan_adoption(home, src):
    """Adoption rebuilds a run from the manifest, so it is a second reader of
    the same untrusted list and needs the same guard."""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    size = manifest(home)["size"]
    rewrite(home, lambda m: m["segments"][0].update(path="../escaping-file"))
    orphan = Path(home, "raw", "claude-code", "sess", "g00", f"{size:012d}-{size + 10:012d}.jsonl")
    orphan.write_bytes(b"0123456789")

    with pytest.raises(store.EscapingSegment, match="escapes the store"):
        store.capture(src, "claude-code", "sess", home=home)


@pytest.mark.parametrize("bad", ["10", True, None, [], {}])
def test_a_manifest_field_of_the_wrong_type_is_refused_not_crashed_on(home, src, bad):
    """The next capture reads `size` to know where to tile from. A string there
    used to reach arithmetic as a TypeError, which is not a report — and `True`
    is the one that gets through a bare `isinstance(val, int)` and tiles the
    next segment from byte 1."""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    rewrite(home, lambda m: m.update(size=bad))

    with pytest.raises(ValueError, match="size"):
        store.capture(src, "claude-code", "sess", home=home)


def test_a_terminal_escape_in_a_transcript_does_not_reach_the_terminal(home, src, capsys):
    """Recall prints bytes an attacker chose. `\\x1b[2K\\r` erases the line it
    was found on, which is how a hit hides the hit above it."""
    from gitmemory.__main__ import main

    transcript(src, 5)
    with open(src, "ab") as fh:
        fh.write(
            b'{"type":"user","uuid":"u99","sessionId":"sess","message":'
            b'{"role":"user","content":"needle \\u001b[2K \\r"}}\n'
        )
    assert main(["--home", home, "capture", src, "--session-id", "sess"]) == 0
    assert main(["--home", home, "index"]) == 0
    capsys.readouterr()

    assert main(["--home", home, "recall", "needle"]) == 0
    out = capsys.readouterr().out
    assert "needle" in out
    assert "\x1b" not in out and "\r" not in out
    assert "\\x1b" in out


def test_the_store_is_owner_only_on_disk(home, src):
    """The store holds whatever the transcript held, which on a shared box is
    the same argument as `~/.ssh`."""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)

    for root, _dirs, files in os.walk(home):
        if ".git" in root.split(os.sep):
            continue
        if root != home:
            assert os.stat(root).st_mode & 0o777 == 0o700, root
        for name in files:
            assert os.stat(os.path.join(root, name)).st_mode & 0o777 == 0o600, name


# --- E4 review round ---------------------------------------------------------


def test_adoption_reports_itself_so_the_repair_can_reach_git(home, src):
    """Adoption changes the store without copying a byte, and `appended` is 0.

    The watcher's "should I commit?" test read `appended` alone, so crash
    recovery repaired the store on disk and the repair was never committed — and
    if the session had ended, never would be. Two reviewers found this
    independently, which is why the field is separate rather than folded into
    `appended`. [E4, review: store-contract 4, concurrency 4]
    """
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    base = manifest(home)["size"]
    end = transcript(src, 5, start=5)
    with open(src, "rb") as fh:
        fh.seek(base)
        tail = fh.read()
    Path(home, "raw", "claude-code", "sess", "g00", f"{base:012d}-{end:012d}.jsonl").write_bytes(
        tail
    )

    cap = store.capture(src, "claude-code", "sess", home=home)
    assert cap.adopted, "the pass that finished the crash has to say so"
    assert cap.appended == 0, "adoption copies nothing; that is the whole problem"


def test_a_capture_that_adopts_nothing_says_so(home, src):
    """The negative half. `adopted` gates a commit, so a stuck True commits every pass."""
    transcript(src, 5)
    assert not store.capture(src, "claude-code", "sess", home=home).adopted
    transcript(src, 5, start=5)
    assert not store.capture(src, "claude-code", "sess", home=home).adopted
    assert not store.capture(src, "claude-code", "sess", home=home).adopted


def test_identity_is_the_name_the_store_actually_files_under(home, src):
    """One capital letter made every session look new on every pass.

    `capture` case-folds both components before touching the disk, and
    `_recorded` reads the folded names back out of the manifests — so a watcher
    keying its "have I captured this already?" bookkeeping on the raw names
    never found its own entry, and re-captured the whole transcript every tick.
    Measured at 454 ms per tick on a 20 MB transcript. [E4, review: store-contract 3]
    """
    assert store.identity("Claude-Code", "SESS") == ("claude-code", "sess")
    transcript(src, 5)
    cap = store.capture(src, "Claude-Code", "SESS", home=home)
    agent, session = store.identity("Claude-Code", "SESS")
    assert Path(cap.manifest_path) == Path(home, "sessions", agent, session, "g00.json")
    with pytest.raises(ValueError):
        store.identity("../escape", "sess")


def test_a_session_with_no_manifest_at_all_is_not_invisible_to_the_proof(home, src):
    """The manifest-driven sweep cannot see a session that has zero manifests.

    Reachable and permanent: crash on a session's *first* capture, before g00 is
    written, then let the transcript go away. `discover()` never yields it
    again, so `_adopt_orphans` never runs, so the manifest is never written —
    while `git add --all` stages the orphaned segment on the next pass that
    commits for some other session. Real transcript bytes, in git for ever, that
    `verify` called clean. [E4, review: concurrency 5]
    """
    transcript(src, 5)
    store.capture(src, "claude-code", "kept", home=home)
    orphan = Path(home, "raw", "claude-code", "gone", "g00")
    orphan.mkdir(parents=True)
    (orphan / "000000000000-000000000100.jsonl").write_bytes(b"real transcript bytes\n")

    problems = store.verify(home)
    assert [p for p in problems if "gone/g00" in p and "no manifest" in p], problems


def test_an_empty_generation_directory_is_not_a_finding(home, src):
    """An empty directory attests to nothing and is not evidence of loss."""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    Path(home, "raw", "claude-code", "later", "g00").mkdir(parents=True)
    assert store.verify(home) == []


def test_the_reverse_sweep_cannot_abort_the_report(home, src, monkeypatch):
    """`verify` is the proof command, so every part of it survives a broken store.

    The first version of the sweep above was unguarded, which put back the exact
    abort an E2 regression test exists for: one unreadable directory and the
    whole report — including the manifest findings already collected — was
    replaced by a traceback. [E4]
    """
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    Path(home, "raw", "claude-code", "sess", "g00", "stray.jsonl").write_bytes(b"x\n")

    real = store.glob

    def exploding(pattern, *a, **kw):
        if os.path.join("raw", "*", "*") in pattern:
            raise TypeError("simulated crash")
        return real(pattern, *a, **kw)

    monkeypatch.setattr(store, "glob", exploding)
    problems = store.verify(home)
    assert [p for p in problems if "stray" in p], "the manifest findings survive"
    assert [p for p in problems if "unverifiable" in p], problems


def test_a_manifest_is_checked_against_where_it_lives_not_what_it_claims(home, src):
    """`seg_dir` used to be built from the manifest's own `agent`/`session_id`.

    Those are attacker-controlled strings in a file the proof is meant to check,
    so a manifest could name another session's directory and be verified against
    *those* bytes — the proof reading the wrong evidence and passing. The
    location on disk is the one part of a manifest nobody can forge by editing
    it. [E4, review: store-contract]
    """
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    path = Path(home, "sessions", "claude-code", "sess", "g00.json")
    man = json.loads(path.read_text())
    man["session_id"] = "elsewhere"
    path.write_text(json.dumps(man))

    problems = store.verify(home)
    assert [p for p in problems if "declares claude-code/elsewhere" in p], problems


# --- E4 review round: the eight store findings ----------------------------- #


def test_an_orphan_in_a_forked_generation_is_adopted(home, src):
    """Adoption looked only in the newest *manifested* generation's directory.

    A capture that forks is killed in the same publish-then-manifest window as
    any other, but its segment lands one generation higher than anything a
    manifest names — so `_adopt_orphans` computed `gen` from `prior[-1]`, looked
    in `g00/`, found nothing, and returned. The next capture then forked again,
    built the same `g01/000000000000-NNN.jsonl` name, and hit `refusing to
    overwrite an existing segment`. Permanently: every later capture repeats it.
    [E4, review: store 1]
    """
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    prev = manifest(home)
    # Same length, different bytes: the next capture must fork.
    data = Path(src).read_bytes().replace(b"hello", b"HELLO")
    assert len(data) == prev["size"]
    Path(src).write_bytes(data)
    g01 = Path(home, "raw", "claude-code", "sess", "g01")
    g01.mkdir(parents=True)
    (g01 / f"{0:012d}-{len(data):012d}.jsonl").write_bytes(data)

    assert store.verify(home) != [], "the wedged state the crash leaves"
    store.capture(src, "claude-code", "sess", home=home)
    assert store.verify(home) == [], "the next capture finishes what the crash started"
    man = manifest(home, gen=1)
    assert man["size"] == len(data)
    assert man["diverged_from"] == {
        "generation": 0,
        "at_byte": prev["size"],
        "prev_file_sha256": prev["file_sha256"],
    }
    assert man["file_sha256"] == hashlib.sha256(data).hexdigest()


def test_unattested_bytes_anywhere_under_raw_are_a_finding(home, src):
    """The reverse sweep was pinned to `raw/*/*/g*`, so bytes at any other depth
    were invisible to it and `git add --all` committed them. [E4, review: store 2]"""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    Path(home, "raw", "claude-code", "sess", "leftover.jsonl").write_bytes(b"real bytes\n")
    Path(home, "raw", "loose.jsonl").write_bytes(b"real bytes\n")

    problems = store.verify(home)
    assert [p for p in problems if "leftover.jsonl" in p], problems
    assert [p for p in problems if "loose.jsonl" in p], problems


def test_a_stray_file_in_the_sessions_tree_is_a_finding(home, src):
    """There was no stray check on `sessions/` at all — the tree that *is* the
    proof. [E4, review: store 2]"""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    Path(home, "sessions", "claude-code", "sess", "notes.txt").write_bytes(b"x\n")
    Path(home, "sessions", "loose.json").write_bytes(b"{}\n")

    problems = store.verify(home)
    assert [p for p in problems if "notes.txt" in p], problems
    assert [p for p in problems if "loose.json" in p], problems


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("size", 370.0),
        ("file_sha256", 12),
        ("compact_boundaries", {}),
        ("source_path", 7),
    ],
)
def test_verify_applies_the_floor_the_next_capture_will_apply(home, src, field, bad):
    """A manifest `verify` calls clean must be one a capture can use.

    `verify` never applied `_FIELDS`, so `size: 370.0` passed every check it has
    — `370 != 370.0` is False — while `_check_manifest` raises on it at the top
    of the next capture. Clean proof, dead session, and no command that says
    which. [E4, review: store 4]
    """
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    assert manifest(home)["size"] == 370, "the float below has to equal the real size"
    rewrite(home, lambda m: m.__setitem__(field, bad))

    assert [p for p in store.verify(home) if field in p], store.verify(home)


def test_a_boundary_that_is_not_an_offset_cannot_wedge_the_store(home, src):
    """`_check_manifest` typed the container and not its elements, and the
    element reached arithmetic.

    `compact_boundaries: ["x"]` survives the writer's floor, and `0 <= "x"`
    raises TypeError — *after* `os.replace` has published the segment. So the
    capture leaves an orphan, the next capture's adoption copies the same list
    into the manifest it writes, and every capture after that dies in the same
    place. Permanent, and `verify` called the store clean throughout.
    [E4, review: store 3]
    """
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    rewrite(home, lambda m: m.__setitem__("compact_boundaries", ["x"]))
    transcript(src, 5, start=5)

    store.capture(src, "claude-code", "sess", home=home)
    assert store.verify(home) == []
    assert manifest(home)["compact_boundaries"] == [], "the bad offset healed rather than wedged"


def test_verify_does_not_report_a_live_capture_as_corruption(home, src):
    """`verify` read the manifest, then hashed, then listed the directory — with
    no lock, so a segment published in between read as an unrecorded file.

    A watcher and a `verify` in another terminal is the ordinary case. The
    reviewer measured 79 of 82 concurrent runs reporting a healthy store
    corrupt. [E4, review: store 5]
    """
    transcript(src, 200)
    store.capture(src, "claude-code", "sess", home=home)
    stop = threading.Event()
    failed: list[BaseException] = []

    def grow():
        i = 200
        while not stop.is_set():
            try:
                transcript(src, 5, start=i)
                store.capture(src, "claude-code", "sess", home=home)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                failed.append(exc)
                return
            i += 5

    writer = threading.Thread(target=grow)
    writer.start()
    try:
        problems = [p for _ in range(30) for p in store.verify(home)]
    finally:
        stop.set()
        writer.join()
    assert failed == [], failed
    assert problems == [], problems[:5]


def test_a_generation_with_no_segments_is_still_the_live_one(home, src):
    """`sessions()` dropped a run of zero segments, which is a real state: a
    source truncated to nothing forks, and the fork has no bytes yet.

    Every reader then saw the *sealed* g00 as the live generation — stale
    history presented as current, with nothing saying so. [E4, review: store 6]
    """
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    Path(src).write_bytes(b"")
    cap = store.capture(src, "claude-code", "sess", home=home)
    assert (cap.generation, cap.size) == (1, 0)

    assert [s.generation for s in store.sessions(home)] == [0, 1]
    assert store.verify(home) == []


def test_a_boundary_offered_when_nothing_grew_is_not_discarded(home, src):
    """The no-op early return skipped the boundary merge entirely, so a
    compaction that appended no bytes lost its only record. [E4, review: store 7]"""
    size = transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    cap = store.capture(src, "claude-code", "sess", home=home, boundaries=[size])

    assert cap.appended == 0
    assert manifest(home)["compact_boundaries"] == [size]
    assert store.verify(home) == []


def test_a_no_op_capture_still_writes_nothing_when_there_is_nothing_to_write(home, src):
    """The other side of the fix: a genuine no-op must stay a no-op, or every
    idle pass produces a commit. [E4, review: store 7]"""
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    path = Path(home, "sessions", "claude-code", "sess", "g00.json")
    before = path.read_bytes(), path.stat().st_mtime_ns

    store.capture(src, "claude-code", "sess", home=home)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_an_abandoned_manifest_temp_is_swept(home, src):
    """`_write_atomic`'s temp is the one crash artefact nothing removed.

    The segment half is swept by the next capture; this half stayed on disk for
    ever, and the GITIGNORE comment claiming both were swept was wrong about it.
    [E4, review: store 8]
    """
    transcript(src, 5)
    store.capture(src, "claude-code", "sess", home=home)
    litter = Path(home, "sessions", "claude-code", "sess", "g00.json.tmp.4242.7")
    litter.write_bytes(b'{"half":')
    assert [p for p in store.verify(home) if litter.name in p], "reported while it is there"

    transcript(src, 5, start=5)
    store.capture(src, "claude-code", "sess", home=home)
    assert not litter.exists()
    assert store.verify(home) == []
