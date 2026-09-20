# E5 derivation — what the dependency table survives

`docs/DESIGN.md` §2.6 names the derivation stack. That table was written at E0/E2
from reading, before anything resolved it. Resolving it changes two rows and
deletes a third. Done before writing a line of E5, because the point of picking
libraries instead of writing them is lost if the library costs more than the
code would have.

Method: `uv pip compile` on each candidate alone, counting the transitive
closure and flagging the heavy members (`torch`, `transformers`,
`sentence-transformers`, `scipy`, `scikit-learn`, `nvidia-*`). Licence from
PyPI metadata. Determinism by running it.

| Candidate | Version | Licence | Closure | Heavy | Verdict |
|---|---|---|---|---|---|
| `sumy` | 0.13.0 | Apache-2.0 | 21 | 0 | **keep, with a change** |
| `model2vec` | 0.9.0 | MIT | 23 | 0 | keep (already in `hybrid`) |
| `graphifyy` | 0.9.65 | Apache-2.0 (in the wheel; see below) | 30 | 0 | keep |
| `keybert` | 0.9.0 | MIT | **42** | **5** | **rejected** |
| `yake` | 0.7.3 | LGPLv3 / GPLv3 classifier | 8 | 0 | **rejected on licence** |

## KeyBERT is out

DESIGN says "KeyBERT + Model2Vec backend, fixed seed", on the understanding that
the Model2Vec backend is what keeps it light. It does not: `pip install keybert`
requires `sentence-transformers`, which requires `torch` and `transformers`,
whatever backend you later hand it. Forty-two packages and several gigabytes to
rank keyphrases, in a product whose pitch is that it is small, local, and
deterministic. The backend choice cannot undo the install.

```
keybert>=0.9 -> 42 packages, 5 heavy   (torch, transformers, sentence-transformers, scikit-learn, scipy)
sumy>=0.11   -> 21 packages, 0 heavy
```

`yake` is the obvious light replacement — 8 packages, no ML stack — and it is
disqualified on licence, not on merit: PyPI says `LGPLv3` in the license field
and carries the `GNU General Public License v3 (GPLv3)` classifier. The two do
not agree with each other, and neither agrees with shipping inside an Apache-2.0
project without argument. An ambiguous licence is a reason on its own; we are
not going to be the ones who resolve it.

**Open decision for E5:** key *phrases* may simply not ship. Key *ideas* —
extractive sentences via LexRank — cover most of what the phrase list was for,
and the honest move when a feature costs a gigabyte is to ask whether anyone
wanted it. If it does ship, `model2vec` alone (MIT, 23 packages, already earned
its place in the `hybrid` extra) plus the ~20 lines of MMR that KeyBERT's value
actually consists of is the shape to consider — and that is one of the few
places in this project where writing the code beats taking the library.

## sumy runs offline — but only if we keep nltk out of the path

The risk with `sumy` is not its licence or its weight, it is `nltk`: the default
`sumy.nlp.tokenizers.Tokenizer` downloads the `punkt` models on first use. A
network fetch at first run would break the offline claim in §2.9 for real.

It is avoidable. sumy's summarizers do not want a tokenizer, they want a
document; and `Sentence` wants an object with exactly two methods. Supplying our
own makes `nltk` an install-time dependency that is never imported on the
derivation path:

```python
class Tok:
    def to_sentences(self, text):
        return tuple(s for s in re.split(r"(?<=[.!?])\s+", text) if s)

    def to_words(self, sent):
        return tuple(re.findall(r"[a-z0-9']+", sent.lower()))


doc = ObjectDocumentModel([Paragraph([Sentence(s, tok) for s in tok.to_sentences(text)])])
```

Verified by running it in a clean 3.13 venv with `sumy` and `numpy` installed and
no nltk data present:

```
LexRank:  no-nltk OK, deterministic=True
TextRank: no-nltk OK, deterministic=True
```

Determinism checked the cheap way — three runs, identical output — which is the
in-process half. The cross-process half belongs in the E5 suite under two
`PYTHONHASHSEED`s, the same way E1's determinism test had to be rebuilt once it
turned out to be comparing an object with itself.

`LexRank` also hard-requires `numpy`, which the table did not say:

```
ValueError: LexRank summarizer requires NumPy. Please, install it by command 'pip install numpy'.
```

`numpy` is already in the `hybrid` extra, so this is an accounting correction,
not a new dependency — but `derive` has to declare it rather than inherit it by
luck.

**Open decision for E5:** twenty-one packages for two summarizers is still a lot,
and the two we want are a few hundred lines of Apache-2.0 we are entitled to
vendor with attribution — which this project has done once already, for
`jsonl.py` from fable (§2.5b, THIRD_PARTY.md). Vendoring LexRank and TextRank
would leave `derive` needing only `numpy`. Against that: a vendored copy stops
receiving upstream fixes, and `jsonl.py` was vendored because we needed to
*change* it, which is a better reason than size. Not decided here.

## graphify: checked against the artifact, not the note — and it holds

DESIGN §2.6 records graphify as Apache-2.0, verified at E2 from the repository.
E5 is where the first line of gitmemory code comes to depend on it, and §2.6
says attribution enters THIRD_PARTY.md at exactly that moment — so the check was
redone against the artifact we will actually install rather than carried forward
from a note.

PyPI's JSON `info.license` for `graphifyy` 0.9.65 is empty and it carries no
licence classifier, which is what raised the question. The wheel answers it: the
metadata sets **`License-Expression: Apache-2.0`** (PEP 639, which supersedes
both of the fields PyPI's JSON was reporting on), and `dist-info/licenses/`
ships `LICENSE`, `LICENSE-MIT`, and `NOTICE`. Nothing is wrong here; the old
field simply is not populated any more.

Worth carrying forward for THIRD_PARTY.md: graphify ships a **NOTICE** recording
that portions were contributed under MIT before relicensing and remain available
under those terms, with the MIT text retained. We depend on graphify rather than
redistribute it, so Apache-2.0 §4(d) does not bind us — but the attribution is
cheap, accurate, and the reason this project keeps a THIRD_PARTY.md at all.

```
License-Expression: Apache-2.0
graphifyy-0.9.65.dist-info/licenses/{LICENSE, LICENSE-MIT, NOTICE}
```

## What this changes in DESIGN.md

Nothing yet — the table is corrected when E5 lands and the choices are made, not
in advance of them. This file is the evidence those choices will be made from.
