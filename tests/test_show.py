# SPDX-FileCopyrightText: 2026 The gitmemory authors
# SPDX-License-Identifier: Apache-2.0
"""`gitmemory show <address>`: the turn behind a line `recall` printed.

`recall` cuts a turn to 160 characters and hands back an address. Until `show`,
the rest of the turn was segment arithmetic done by hand under `raw/`. These
tests hold the three things that make the address worth printing: it resolves
from the store and not from the index, it never becomes a path, and nothing in
a transcript can make the output say a different address than the one it is.
"""

from __future__ import annotations

import json
import os

import pytest
from test_index import AKIA, PEM, PEM_BODY, assistant, user, write

from gitmemory import redact, store
from gitmemory.__main__ import main


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "store"
    h.mkdir()
    return str(h)


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "src" / "sess.jsonl"
    p.parent.mkdir()
    return str(p)


def size(obj: dict) -> int:
    """One JSONL line's length, newline included: the next turn's offset."""
    return len(json.dumps(obj).encode()) + 1


def show(home: str, capsys, *argv: str) -> tuple[int, str, str]:
    code = main(["--home", home, "show", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_show_prints_the_whole_turn_recall_cut_at_160(home, src, capsys):
    long = "the marmoset rule: " + "never retry the upload " * 20 + "END-OF-TURN"
    write(src, [user("u1", long) | {"timestamp": "2026-09-28T10:00:00Z"}])
    store.capture(src, "claude-code", "sess", home=home)
    assert main(["--home", home, "index"]) == 0
    capsys.readouterr()
    assert main(["--home", home, "recall", "marmoset"]) == 0
    out = capsys.readouterr().out
    assert "END-OF-TURN" not in out
    code, out, err = show(home, capsys, out.split()[2])
    assert (code, err) == (0, "")
    assert out == f"> claude-code/sess/g00@0  2026-09-28T10:00Z  user\n  text\n    {long}\n"


def test_show_needs_no_index(home, src, capsys):
    write(src, [user("u1", "aardvark")])
    store.capture(src, "claude-code", "sess", home=home)
    code, out, _ = show(home, capsys, "claude-code/sess/g00@0")
    assert code == 0 and "    aardvark\n" in out
    assert not os.path.exists(os.path.join(home, "index"))


def test_show_resolves_a_turn_that_straddles_a_segment_cut(home, src, capsys):
    first, second = user("u1", "first"), user("u2", "the peculiar marmoset")
    write(src, [first, second])
    with open(src, "rb") as fh:
        whole = fh.read()
    with open(src, "wb") as fh:
        fh.write(whole[: size(first) + 10])  # the cut lands inside the second line
    store.capture(src, "claude-code", "sess", home=home)
    with open(src, "wb") as fh:
        fh.write(whole)
    store.capture(src, "claude-code", "sess", home=home)
    assert len(store.sessions(home)[0].segments) == 2
    code, out, _ = show(home, capsys, f"claude-code/sess/g00@{size(first)}")
    assert code == 0
    assert out.splitlines()[0].startswith(f"> claude-code/sess/g00@{size(first)} ")
    assert "    the peculiar marmoset" in out.splitlines()


def test_show_context_prints_neighbours_in_order_and_marks_the_target(home, src, capsys):
    turns = [user(f"u{i}", f"turn-{i}") for i in range(5)]
    write(src, turns)
    store.capture(src, "claude-code", "sess", home=home)
    at = size(turns[0]) + size(turns[1])
    code, out, _ = show(home, capsys, f"claude-code/sess/g00@{at}", "--context", "1")
    assert code == 0
    heads = [ln for ln in out.splitlines() if not ln.startswith(" ")]
    assert [h[0] for h in heads] == ["-", ">", "-"]
    assert [ln.strip() for ln in out.splitlines() if ln.startswith("    ")] == [
        "turn-1",
        "turn-2",
        "turn-3",
    ]


def test_an_offset_inside_a_turn_shows_that_turn_at_its_start(home, src, capsys):
    write(src, [user("u1", "aardvark"), user("u2", "marmoset")])
    store.capture(src, "claude-code", "sess", home=home)
    start = size(user("u1", "aardvark"))
    code, out, _ = show(home, capsys, f"claude-code/sess/g00@{start + 5}")
    assert code == 0 and out.startswith(f"> claude-code/sess/g00@{start} ")
    assert "marmoset" in out and "aardvark" not in out


def test_a_subagent_turn_is_headed_subagent_not_user(home, src, capsys):
    """A sidechain's user-role text is the parent agent's brief, not the user's
    word, so the header says whose it is. Same column, so a reader that splits
    the header still finds the role where it was."""
    brief = user("u1", "review the upload code") | {"isSidechain": True}
    write(src, [brief, assistant("a2", [{"type": "text", "text": "done"}])])
    store.capture(src, "claude-code", "sess", home=home)
    code, out, _ = show(home, capsys, "claude-code/sess/g00@0", "--context", "1")
    assert code == 0
    heads = [ln for ln in out.splitlines() if not ln.startswith(" ")]
    assert heads == [
        "> claude-code/sess/g00@0  undated            subagent:user",
        f"- claude-code/sess/g00@{size(brief)}  undated            assistant",
    ]


@pytest.mark.parametrize(
    "address",
    [
        "../../etc/passwd@0",
        "claude-code/../g00@0",
        "claude-code/.git/g00@0",
        "claude-code/sess\n/g00@0",
        "claude-code/sess/g00@\u0663",  # ARABIC-INDIC DIGIT THREE: int() reads it as 3
        "claude-code/sess/g00",
        "claude-code/sess@0",
        "/etc/sess/g00@0",
    ],
)
def test_a_malformed_or_escaping_address_is_not_an_address(home, src, capsys, address):
    write(src, [user("u1", "aardvark")])
    store.capture(src, "claude-code", "sess", home=home)
    code, out, err = show(home, capsys, address)
    assert (code, out) == (2, "") and err.startswith("not an address: ")
    assert err.count("\n") == 1


def test_an_address_the_store_does_not_hold_is_an_answer_exit_1(home, src, capsys):
    write(src, [user("u1", "aardvark")])
    store.capture(src, "claude-code", "sess", home=home)
    code, out, err = show(home, capsys, "claude-code/sess/g01@0")
    assert (code, out, err) == (1, "", "claude-code/sess/g01: not in the store\n")
    code, out, err = show(home, capsys, "claude-code/sess/g00@99999")
    assert (code, out, err) == (1, "", "claude-code/sess/g00: no turn at byte 99999\n")


def test_a_credential_in_an_address_is_masked_in_the_error(home, src, capsys):
    """A session id is whatever `capture --session-id` was given, and these
    pass the path-component rule. The error masks the address as it was typed:
    `identity()` lowercases, and an AWS key id lowercased is one no detector
    knows, so masking the store's spelling printed it whole."""
    write(src, [user("u1", "aardvark")])
    store.capture(src, "claude-code", "sess", home=home)
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    code, _, err = show(home, capsys, f"claude-code/{token}/g00@0")
    assert code == 1 and "ghp_" in err and token.lower() not in err.lower()
    key = "AKIAZZZZQQQQWWWW1234"  # synthetic, right shape
    code, _, err = show(home, capsys, f"claude-code/{key}/g00@0")
    assert code == 1 and "AKIA" in err and key.lower() not in err.lower(), err


def test_a_transcript_cannot_forge_a_show_header_or_reach_the_terminal(home, src, capsys):
    """Only a header starts in column 0, so a line that looks like one has to
    have come from `show`. Two forgeries got past the first version: a tool
    name with a newline in it printed a header of its own, and `splitlines()`
    broke on a carriage return before `safe_text` could escape it."""
    forged = "> claude-code/forged/g00@0  2026-01-01T00:00Z  user"
    text = f"needle \x1b[2K\r{forged}\n{forged}\u2028{forged}\x85{forged}"
    tool = {"type": "tool_use", "id": "t", "name": f"Bash\n{forged}", "input": {"command": "ls"}}
    write(src, [user("u1", text), assistant("a2", [tool])])
    store.capture(src, "claude-code", "sess", home=home)
    code, out, _ = show(home, capsys, "claude-code/sess/g00@0", "--context", "1")
    assert code == 0
    assert not any(c in out for c in "\x1b\r\u2028\x85")
    assert out.split("\n")[2] == "    needle \\x1b[2K\\x0d" + forged
    heads = [ln for ln in out.splitlines() if not ln.startswith(" ")]
    assert len(heads) == 2 and not any("forged" in h for h in heads), heads


def test_two_manifests_claiming_one_address_are_refused(home, src, capsys):
    """`verify` flags a manifest whose declared identity is not its path. Until
    it has been run, which of the two is the turn is not something `show` can
    know, so it does not pick one."""
    write(src, [user("u1", "aardvark")])
    store.capture(src, "claude-code", "sess", home=home)
    store.capture(src, "claude-code", "other", home=home)
    other = os.path.join(home, "sessions", "claude-code", "other", "g00.json")
    with open(other) as fh:
        manifest = json.load(fh)
    manifest["session_id"] = "sess"
    with open(other, "w") as fh:
        json.dump(manifest, fh)
    code, out, err = show(home, capsys, "claude-code/sess/g00@0")
    assert (code, out) == (1, "") and "claimed by 2 manifests" in err


def test_a_captured_show_result_is_an_echo_the_skill_lists(home, tmp_path, capsys):
    """A session that ran `show` was captured too, with the whole turn in a
    tool result dated after it, so `recall` finds the turn again under the
    echo's own address. The skill tells an agent to run `show`, so it has to
    tell it how to know the echo: by the header at the front of its text."""
    first, later = str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")
    rule = user("u1", "the marmoset rule: never retry the upload")
    write(first, [rule | {"timestamp": "2026-09-01T10:00:00Z"}])
    store.capture(first, "claude-code", "a", home=home)
    code, said, _ = show(home, capsys, "claude-code/a/g00@0")
    call = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gitmemory show"}}
    result = {"type": "tool_result", "tool_use_id": "t1", "content": said}
    echo = user("u3", "") | {"timestamp": "2026-10-01T10:00:00Z"}
    echo["message"]["content"] = [result]
    write(later, [assistant("a2", [call]), echo])
    store.capture(later, "claude-code", "b", home=home)
    assert code == 0 and main(["--home", home, "index"]) == 0
    capsys.readouterr()
    assert main(["--home", home, "recall", "marmoset"]) == 0
    heads = [ln.split(None, 4) for ln in capsys.readouterr().out.splitlines()]
    assert [h[2].split("@")[0] for h in heads] == ["claude-code/a/g00", "claude-code/b/g00"]
    assert heads[1][4].startswith("> claude-code/a/g00@0 "), heads
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "skills", "recall", "SKILL.md")) as fh:
        echoes = fh.read().split("\n## Echoes\n")[1].split("\n## ")[0]
    assert "`gitmemory show ...`" in echoes and "`> `" in echoes, echoes
    # The header's shape, not "a quote or a bullet that names an address":
    # a real turn can open with either and cite one further on.
    assert "`- ` immediately followed by an `<agent>/" in " ".join(echoes.split()), echoes


def test_show_on_no_store_and_a_negative_context_exit_2(tmp_path, capsys):
    code, _, err = show(str(tmp_path / "absent"), capsys, "claude-code/sess/g00@0")
    assert code == 2 and err.startswith("not a store: ")
    with pytest.raises(SystemExit) as done:
        main(["--home", str(tmp_path), "show", "claude-code/sess/g00@0", "-C", "-1"])
    assert done.value.code == 2


def test_show_redact_masks_a_whole_turn_and_the_default_prints_it(home, src, capsys):
    """What `recall --redact` masked one line of, whole. A private key's body is
    the lines under its header, so a block is masked before it is split into
    lines, never one line at a time. The address and a tool's name are masked
    too, and the address stays one field, as recall's does."""
    ghp = "ghp_" + "a" * 36  # lowercase, because the store case-folds a session id
    text = f"the deploy key:\n{PEM}\nand {AKIA} for staging"
    tool = {"type": "tool_use", "id": "t", "name": ghp, "input": {}}
    write(src, [user("u1", text), assistant("a2", [tool])])
    store.capture(src, "claude-code", ghp, home=home)
    address = f"claude-code/{ghp}/g00@0"
    code, out, _ = show(home, capsys, address, "-C", "1")
    assert code == 0 and PEM_BODY in out and AKIA in out and out.count(ghp) == 3, out
    code, out, err = show(home, capsys, address, "-C", "1", "--redact")
    assert (code, err) == (0, "")
    undated = "undated".ljust(17)
    assert out.splitlines() == [
        f"> claude-code/ghp_…[40bytes]/g00@0  {undated}  user",
        "  text",
        "    the deploy key:",
        f"    ----…[{len(PEM)} bytes]",
        "    and AKIA…[20 bytes] for staging",
        f"- claude-code/ghp_…[40bytes]/g00@{size(user('u1', text))}  {undated}  assistant",
        "  tool_use ghp_…[40 bytes]",
    ], out
    assert ghp[4:20] not in out and AKIA[4:10] not in out and PEM_BODY[:8] not in out, out


def test_show_redact_joins_a_key_header_a_line_break_split(home, src, capsys):
    """`-----BEGIN RSA\\nPRIVATE KEY-----` is no header until the line is
    flattened. recall flattens a turn before it masks; show keeps the breaks, so
    its mask flattens a copy itself and masks what that finds in the text as
    written, leaving the lines around the key as they were. [E7b L3-F1]"""
    split = PEM.replace("RSA PRIVATE", "RSA\nPRIVATE", 1)
    text = f"first words\n\n  {split}\nlast words"
    assert redact.mask(text) == text, "premise: nothing matches with the break in"
    write(src, [user("u1", text)])
    store.capture(src, "claude-code", "sess", home=home)
    code, out, _ = show(home, capsys, "claude-code/sess/g00@0", "--redact")
    assert code == 0
    assert out.splitlines()[2:] == [
        "    first words",
        "    ",
        f"      ----…[{len(split)} bytes]",
        "    last words",
    ], out
