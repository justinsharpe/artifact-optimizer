# artifact-optimizer

Point it at **anything** — an `.md` file, a codebase, a config, an LLM system
prompt, a whole directory — and it runs its full battery and presents every
optimization it can find, **ranked, before any goal is stated**.

If you then state a goal (`--budget 12000`, unify two codebases, …), it either
reaches it while preserving meaning, or tells you the honest floor and why.

## Why this exists (the landscape, surveyed)

No off-the-shelf tool does this. The space, surveyed honestly:

| class | examples | what it does | why it does not fit |
|---|---|---|---|
| prompt-compression research | LLMLingua, AutoCompressor, gist tokens | compresses prompts **into non-human-readable token forms** | targets you must be able to *read, quote, and ratify* are destroyed by design — a law you cannot read is not a law |
| prose linters | Vale, proselint, write-good | **flag** wordiness, run-ons, passive voice | flagging is not fixing; no byte accounting, no rewrite, no goal handling |
| markdown formatters | markdownlint, prettier, mdformat | whitespace and structure only | zero semantic value |
| near-duplicate detection | MinHash, SimHash, shingling | finds duplicates across corpora | finds, never resolves; no notion of which copy is load-bearing |
| the actual practice | point an LLM at the file, loop "make this concise" | works, sometimes | no measurement, no guard against meaning loss, no reversibility |

**Verdict: nothing to buy.** Every existing tool either destroys readability,
stops at diagnosis, or trusts a model's self-report about whether meaning
survived. The pieces are ordinary; the assembly is this tool. Stdlib-only, so
it runs identically on macOS / Linux / Windows and inside any harness.

## The five laws it obeys

1. **Mechanical and semantic are separated by RISK, not convenience.**
   Mechanical transforms are lossless and reversible → safe to auto-apply.
   Semantic transforms change wording → proposed only, never auto-applied.
2. **Standing law is never paraphrased by a machine.** Files carrying ratified-law
   markers are detected and flagged; their semantic tier is report-only.
3. **A goal never overrides meaning.** `--budget N` means *"rewrite so that all
   meaning survives inside N characters"*. A hard cut requires `--truncate`.
4. **Never claim an unmeasured saving.** Every number is a real byte delta from a
   real transform. Cross-file figures are explicitly marked estimated (`~`).
5. **Platform-agnostic.** Stdlib only, py3.8+, no network, no shell assumptions.

## Usage

```bash
# full battery, no goal needed
python3 optimize.py PATH...

# fit to a budget, meaning preserved (the default interpretation)
python3 optimize.py FILE --budget 12000

# write the lossless part (a backup is taken and named by content hash)
python3 optimize.py PATH --apply

# rewrite under a meaning-preservation guard, using any external rewriter
python3 optimize.py FILE --budget 12000 --llm "ollama run qwen2.5"

# convention divergence between two artifacts
python3 optimize.py A.py B.py --unify

# machine-readable
python3 optimize.py DIR --json

# self-check; exit code = number of failures
python3 optimize.py --selftest
```

## The battery

**A — Mechanical (lossless, auto-appliable)**
`path-tilde` · `eol` · `trailing-space` · `space-runs` · `dup-word` ·
`verbose-phrase` · `blank-runs` · `md-heading-space` · `bullet-normalize`

**B — Structural (code-aware, proposed)**
`repeated-literal` · `duplicate-import` · `dead-comment-block` · `long-line`

**C — Semantic (proposed, needs a human eye)**
`run-on-sentence` · `repeated-phrase` · `near-duplicate-paragraph` ·
`duplicate-heading` · `cross-file-duplicate`

## The meaning-preservation guard

When an external rewriter is used (`--llm CMD`), the result is accepted **only if
every protected token survived**: numbers, dates, versions, budgets, paths,
URLs, filenames, CONSTANTS, ticket/artifact ids (`PR-123`, `ISSUE-42`), and law
markers (`RATIFIED`, `PRECEPT`).

A rewrite that drops one is **refused**, the loss is named, and the original is
kept. A compressed law that means something slightly different is worse than a
long one.

## What it does NOT do

- It will not paraphrase ratified law. Ever.
- It will not silently truncate. `--truncate` is explicit and labelled.
- It will not report a saving it did not measure.
- Cross-file de-duplication requires a **per-lane self-sufficiency check** before
  removal: different loaders load different files, so a restatement is
  sometimes load-bearing. Deleting it can silently lose a law from a loader
  that sees one file but not the other.

## Why the guard exists (the defects it caught in its own birth)

Each was caught by the tool's own selftest or its first real run:

1. A "rule-based semantic" stage re-applied already-applied rules and
   **reported a saving it never earned** → replaced with an honest floor.
2. `dead-comment-block` flagged **prose** comments just because they contained
   parentheses → now requires a statement; a noisy detector is an ignored one.
3. `re.findall` returns **capture groups**, not matches: the literal detector
   counted the quote character and reported a phantom saving → now
   `finditer` + `group(0)`.
4. The corpus matcher used whole-paragraph hashing and found nothing; real
   redundancy is shared **phrases** → now 6-word spans, displaying original text.
5. Run-on sentences claimed a byte saving; splitting a sentence removes no
   characters → now `saved=0`, labelled *clarity only*.

## Self-test

```bash
python3 optimize.py --selftest
```

26 checks: the full battery, the meaning-preservation guard (lost number /
path / law marker refused), an honest floor (not silent truncation), a
meaning-destroying rewrite refused, cross-file duplicate found, unify
divergence detected. Exit code = number of failures.

## License

MIT — see [LICENSE](LICENSE).
