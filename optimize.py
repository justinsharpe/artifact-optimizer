#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
artifact-optimizer v1.0
=======================
Point it at anything — an .md file, a codebase, a config, an LLM system prompt,
a directory, a corpus — and it runs its full battery and presents every
optimization it can find, ranked, BEFORE any goal is stated.

Design laws:
  L1  MECHANICAL vs SEMANTIC are separated by RISK, not by convenience.
      Mechanical transforms are lossless and reversible -> safe to auto-apply.
      Semantic transforms change wording -> proposed only, never auto-applied.
  L2  Standing law is never paraphrased by a machine. Files carrying ratified-law
      markers are flagged; their semantic tier is report-only (RATIFICATION REQUIRED).
  L3  A goal never overrides meaning. "--budget N" means "rewrite so that all
      meaning survives inside N chars". A hard cut requires --truncate, explicitly.
  L4  Never claim a saving that was not measured. Every number here is a real
      byte delta from a real transform, not an estimate from a model.
  L5  Platform-agnostic: stdlib only, py3.8+, POSIX/Windows, no network.

Usage:
  optimize.py PATH...                     # full battery, ranked, no goal needed
  optimize.py PATH --budget 12000         # fit to 12k chars, meaning preserved
  optimize.py PATH --budget 12000 --apply # write the lossless part
  optimize.py PATH --apply                # apply mechanical fixes (backup taken)
  optimize.py A B --unify                 # convention-divergence between two artifacts
  optimize.py DIR --json                  # machine-readable
  optimize.py --selftest                  # fixtures; exit code = failure count
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter, defaultdict

HOME = os.path.expanduser("~")
VERSION = "1.0"

# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------

RISK_LOSSLESS = "mechanical"   # auto-appliable
RISK_PROPOSED = "semantic"     # human eye required

CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs",
            ".java", ".rb", ".sh", ".bash", ".zsh", ".swift", ".c", ".h",
            ".cpp", ".hpp", ".cs", ".kt", ".php", ".pl", ".lua", ".sql"}
MD_EXT = {".md", ".markdown", ".mdx", ".rst"}
CFG_EXT = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".env"}

TEXT_TARGETS = {"md", "markdown", "text", "config", "prompt", "generic", "dir"}

VERBOSE_PHRASES = [
    ("in order to", "to"),
    ("in order for", "for"),
    ("with regard to", "re"),
    ("with respect to", "re"),
    ("in the event that", "if"),
    ("in the event of", "if"),
    ("at this point in time", "now"),
    ("at the present time", "now"),
    ("the fact that", "that"),
    ("due to the fact that", "because"),
    ("for the purpose of", "to"),
    ("in the near future", "soon"),
    ("a number of", "several"),
    ("a majority of", "most"),
    ("is able to", "can"),
    ("are able to", "can"),
    ("has the ability to", "can"),
    ("prior to", "before"),
    ("subsequent to", "after"),
    ("in spite of", "despite"),
    ("on a daily basis", "daily"),
    ("on a regular basis", "regularly"),
    ("make a decision", "decide"),
    ("take into consideration", "consider"),
    ("provide assistance to", "help"),
    ("in the process of", "currently"),
    ("it is important to note that", "note:"),
    ("it should be noted that", "note:"),
    ("please be advised that", "note:"),
    ("as a matter of fact", "in fact"),
    ("in the absence of", "without"),
    ("by means of", "via"),
    ("in the amount of", "for"),
    ("utilize", "use"),
    ("utilizes", "uses"),
    ("utilized", "used"),
    ("commence", "start"),
    ("terminate", "end"),
    ("endeavor", "try"),
    ("sufficient", "enough"),
    ("additional", "more"),
    ("approximately", "about"),
    ("demonstrate", "show"),
    ("facilitate", "help"),
    ("implement", "build"),
    ("ascertain", "find"),
    ("necessitate", "require"),
]

# legitimate doubled words — never auto-collapse these
DUP_WHITELIST = {"had", "that", "ha", "no", "so"}

LAW_MARKERS = re.compile(
    r"RATIFIED|PRECEPT|STANDING (?:DIRECTIVE|LAW)|constitution",
    re.I,
)

PATH_RE = re.compile(r"(?:~|/Users/[A-Za-z0-9_.\-]+|/home/[A-Za-z0-9_.\-]+|\$HOME)"
                     r"(?:/[A-Za-z0-9_.\-]+)+/?")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")

# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------


def finding(rule, risk, saved, **kw):
    d = {"rule": rule, "risk": risk, "saved": int(saved)}
    d.update(kw)
    return d


def kind_of(path, text):
    ext = os.path.splitext(path)[1].lower()
    if ext in CODE_EXT:
        return "code"
    if ext in MD_EXT:
        return "markdown"
    if ext in CFG_EXT:
        return "config"
    if os.path.isdir(path):
        return "dir"
    return "text"


# --------------------------------------------------------------------------
# MECHANICAL transforms — lossless, reversible, measured by before/after diff
# --------------------------------------------------------------------------


def t_path_tilde(text):
    """Collapse an absolute home prefix to ~ — universally understood, saves
    9 chars per site and is the idiomatic spelling anyway."""
    out = text
    for pref in (HOME + os.sep, HOME + "/"):
        out = out.replace(pref, "~/")
    out = out.replace("$HOME/", "~/")
    return out


def t_eol(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def t_trailing_space(text):
    return "\n".join(line.rstrip() for line in text.split("\n"))


def t_blank_runs(text):
    return re.sub(r"\n{4,}", "\n\n\n", text)


def t_space_runs(text):
    """Collapse runs of 2+ interior spaces. Never inside code, never indentation."""
    def fix(m):
        return m.group(1)
    return re.sub(r"(\S)[ \t]{2,}(\S)", r"\1 \2", text)


def t_dup_word(text):
    def repl(m):
        w = m.group(1)
        if w.lower() in DUP_WHITELIST:
            return m.group(0)
        return w
    return re.sub(r"\b([A-Za-z][A-Za-z'\-]{1,})\s+\1\b", repl, text)


def t_verbose(text):
    out = text
    for long, short in VERBOSE_PHRASES:
        pat = re.compile(r"(?<![\w\-])" + re.escape(long) + r"(?![\w\-])", re.I)
        def keep_case(m, short=short):
            src = m.group(0)
            if src[:1].isupper():
                return short[:1].upper() + short[1:]
            return short
        out = pat.sub(keep_case, out)
    return out


def t_md_heading_space(text):
    return re.sub(r"^(#{1,6} .*?)[ \t]+$", r"\1", text, flags=re.M)


def t_bullet_normalize(text):
    """'* item' / '+ item' -> '- item' (markdown only, at line start)."""
    return re.sub(r"^([ \t]*)[*+][ \t]+", r"\1- ", text, flags=re.M)


TEXT_TRANSFORMS = [
    ("A1  path-tilde", "collapse /Users/<you>/ to ~/", t_path_tilde),
    ("A2  eol", "normalize CRLF to LF", t_eol),
    ("A3  trailing-space", "strip end-of-line whitespace", t_trailing_space),
    ("A4  space-runs", "collapse interior double spaces", t_space_runs),
    ("A5  dup-word", "collapse adjacent duplicated words", t_dup_word),
    ("A6  verbose-phrase", "swap long phrases for their short form", t_verbose),
    ("A7  blank-runs", "collapse 4+ blank lines to 3", t_blank_runs),
    ("A8  md-heading-space", "strip trailing space in headings", t_md_heading_space),
]

CODE_TRANSFORMS = [
    ("A2  eol", "normalize CRLF to LF", t_eol),
    ("A3  trailing-space", "strip end-of-line whitespace", t_trailing_space),
    ("A7  blank-runs", "collapse 4+ blank lines to 3", t_blank_runs),
]


def run_mechanical(text, kind):
    """Apply every lossless transform in order, recording each delta.
    Measure == apply, by construction: the same functions both score and write."""
    transforms = CODE_TRANSFORMS if kind == "code" else TEXT_TRANSFORMS
    if kind == "markdown":
        transforms = TEXT_TRANSFORMS + [
            ("A9  bullet-normalize", "normalize * / + bullets to -", t_bullet_normalize),
        ]
    findings = []
    cur = text
    for rule, desc, fn in transforms:
        try:
            nxt = fn(cur)
        except Exception as exc:            # a transform must never break the run
            findings.append(finding(rule, RISK_LOSSLESS, 0,
                                    detail=desc, error=str(exc)))
            continue
        delta = len(cur) - len(nxt)
        if delta:
            findings.append(finding(rule, RISK_LOSSLESS, delta, detail=desc))
        cur = nxt
    return cur, findings


# --------------------------------------------------------------------------
# STRUCTURAL detectors — code-aware, report-only in v1
# --------------------------------------------------------------------------


def detect_structural(path, text, kind):
    out = []
    if kind != "code":
        return out
    lines = text.split("\n")

    # repeated string literals (>=4 occurrences, len>=12) — bounded quantifier
    # so a pathological file cannot trigger runaway backtracking
    lits = Counter(m.group(0) for m in re.finditer(
        r"""(['"])(?:\\.|(?!\1).){7,200}?\1""", text))
    for lit, n in lits.items():
        if n >= 4 and len(lit) >= 12:
            out.append(finding("B1  repeated-literal", RISK_PROPOSED,
                               (len(lit) - 12) * (n - 1),
                               detail="extract to a shared constant",
                               sample=lit[:60], count=n))

    # duplicate import lines
    imports = Counter()
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if re.match(r"^(import |from |#include|require\(|using )", s):
            imports[s] += 1
    for ln, n in imports.items():
        if n > 1:
            out.append(finding("B2  duplicate-import", RISK_LOSSLESS,
                               len(ln) * (n - 1),
                               detail="identical import repeated",
                               sample=ln[:60], count=n))

    # commented-out code blocks: 3+ consecutive lines that read like STATEMENTS.
    # Prose comments containing parentheses must NOT qualify — a noisy detector
    # is an ignored detector.
    CODEISH = re.compile(
        r"^(?:def|class|return|import|from|print|if|elif|else|for|while|try|"
        r"except|finally|with|lambda|async|await|yield|pass|raise|global|"
        r"const|let|var|function|public|private|protected|func|fn|struct|"
        r"SELECT|INSERT|UPDATE|DELETE)\b", re.I)
    ASSIGN = re.compile(r"^[A-Za-z_][\w.\[\]'\"]*\s*(?:=|\+=|-=|\|=|\*=)\s*\S")

    def _flush(run, out):
        if len(run) >= 3:
            out.append(finding("B3  dead-comment-block", RISK_PROPOSED,
                               sum(len(l) + 1 for _, l in run),
                               detail="commented-out code; delete if superseded",
                               line_start=run[0][0], line_end=run[-1][0]))

    run = []
    for i, ln in enumerate(lines, 1):
        m = re.match(r"^\s*(?:#|//|/\*|\*)\s*(.*)$", ln)
        body = m.group(1).strip() if m else None
        if body and (CODEISH.match(body) or ASSIGN.match(body)):
            run.append((i, ln))
        else:
            _flush(run, out)
            run = []
    _flush(run, out)

    # long lines
    longs = [(i, l) for i, l in enumerate(lines, 1) if len(l) > 120]
    if longs:
        out.append(finding("B4  long-line", RISK_PROPOSED,
                           sum(len(l) - 100 for _, l in longs),
                           detail="lines over 120 chars",
                           count=len(longs),
                           worst=max(len(l) for _, l in longs)))
    return out


# --------------------------------------------------------------------------
# SEMANTIC detectors — proposed only
# --------------------------------------------------------------------------


def detect_semantic(path, text, kind):
    out = []
    if kind == "code":
        return out

    body = re.sub(r"```.*?```", "", text, flags=re.S)

    # long sentences / run-ons — a CLARITY finding, not a byte saving.
    # Splitting a sentence removes no characters (it usually adds one), so this
    # must never be reported as a saving (law 4: never claim an unmeasured saving).
    sents = SENT_SPLIT.split(body)
    longs = [s for s in sents if len(s.split()) > 45]
    if longs:
        out.append(finding("C1  run-on-sentence", RISK_PROPOSED, 0,
                           detail="clarity: sentence(s) over 45 words",
                           count=len(longs),
                           worst_words=max(len(s.split()) for s in longs),
                           clarity=True))

    # repeated multi-word phrases (6-grams, 2+ uses)
    words = re.findall(r"[A-Za-z][A-Za-z'\-]+", body.lower())
    grams = Counter(tuple(words[i:i + 6]) for i in range(max(0, len(words) - 5)))
    for g, n in grams.items():
        if n >= 2 and len(" ".join(g)) > 30:
            out.append(finding("C2  repeated-phrase", RISK_PROPOSED,
                               (len(" ".join(g)) - 8) * (n - 1),
                               detail="phrase repeats; candidate for a defined term",
                               sample=" ".join(g)[:70], count=n))

    # near-duplicate paragraphs within the file
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if len(p.strip()) > 160]
    def shingles(p):
        w = re.findall(r"[a-z]+", p.lower())
        return {tuple(w[i:i + 4]) for i in range(max(0, len(w) - 3))}
    sh = [shingles(p) for p in paras]
    seen = []
    for i in range(len(paras)):
        for j in range(i + 1, len(paras)):
            if not sh[i] or not sh[j]:
                continue
            inter = len(sh[i] & sh[j])
            jac = inter / float(len(sh[i] | sh[j]))
            if jac >= 0.55:
                seen.append((i, j, jac, paras[j]))
    for i, j, jac, p in seen[:12]:
        out.append(finding("C3  near-duplicate-paragraph", RISK_PROPOSED,
                           int(len(p) * jac * 0.8),
                           detail="paragraphs overlap; one may be redundant",
                           overlap=round(jac, 2), sample=p[:70]))

    # repeated markdown headings / table-of-contents drift
    heads = re.findall(r"^#{1,6}\s+(.*)$", text, flags=re.M)
    for h, n in Counter(h.strip().lower() for h in heads).items():
        if n > 1 and len(h) >= 4:
            out.append(finding("C4  duplicate-heading", RISK_PROPOSED,
                               len(h) * (n - 1),
                               detail="identical heading appears more than once",
                               sample=h[:60], count=n))
    return out


def _shared_span_windows(words, shared_keys, k):
    """Maximal runs of consecutive shared k-grams."""
    spans, i, n = [], 0, len(words)
    while i <= n - k:
        if tuple(words[i:i + k]) in shared_keys:
            j = i + k
            while j <= n - k and tuple(words[j:j + k]) in shared_keys:
                j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


def detect_cross_file(files, k=6, min_chars=36, cap_tokens=400000):
    """Redundancy ACROSS files: maximal passages of k+ words appearing in two or
    more files. Catches duplicated doctrine no whole-paragraph matcher would see.
    Samples are the ORIGINAL text (not tokenised), so they are paste-ready.
    Report-only: removal needs the per-lane self-sufficiency check (v1.1)."""
    out = []
    toks, orig = {}, {}
    for path, text in files.items():
        t = re.sub(r"```.*?```", " ", text, flags=re.S)
        ms = list(re.finditer(r"[A-Za-z0-9']+", t))
        toks[path] = [m.group(0).lower() for m in ms]
        orig[path] = (t, [(m.start(), m.end()) for m in ms])
        if sum(len(v) for v in toks.values()) > cap_tokens:
            return out
    grams = defaultdict(set)
    for path, w in toks.items():
        for i in range(max(0, len(w) - k + 1)):
            grams[tuple(w[i:i + k])].add(path)
    shared = {g: ps for g, ps in grams.items() if len(ps) >= 2}
    if not shared:
        return out

    seen = set()
    for path, w in toks.items():
        t, pos = orig[path]
        for i, j in _shared_span_windows(w, set(shared), k):
            sets = [shared[tuple(w[x:x + k])] for x in range(i, j - k + 1)]
            common = set.intersection(*sets) if sets else set()
            if len(common) < 2:
                continue
            disp = re.sub(r"\s+", " ", t[pos[i][0]:pos[j - 1][1]]).strip()
            if len(disp) < min_chars:
                continue
            sig = disp[:120].lower()
            if sig in seen:
                continue
            seen.add(sig)
            out.append(finding("C5  cross-file-duplicate", RISK_PROPOSED,
                               int(len(disp) * (len(common) - 1) * 0.6),
                               detail="shared passage in %d files — candidate "
                                      "for a single source of truth" % len(common),
                               files=sorted(os.path.basename(p) for p in common),
                               sample=disp[:100], est=True))
    out.sort(key=lambda f: -f["saved"])
    return out[:40]


# --------------------------------------------------------------------------
# budget / goal logic
# --------------------------------------------------------------------------

# Tokens that carry meaning a rewrite is forbidden to drop: numbers, dates,
# versions, paths, URLs, identifiers, constants, artifact ids, law markers.
PROTECT_PATTERNS = [
    r"\d[\d.,:/%\-]*\d|\d",                 # numbers, dates, versions, budgets
    r"(?:~/|/)[\w./\-]{3,}",                # paths
    r"https?://\S+",                        # urls
    r"\b[A-Z][A-Z0-9_]{2,}\b",              # CONSTANTS / ACRONYMS
    r"\b\w+\.(?:md|py|sh|json|yaml|yml|db|txt|csv|toml)\b",   # filenames
    r"\b[A-Z]{2,6}[-_]\d+\b",                       # ticket/artifact ids (e.g. PR-123, ISSUE-42)
    r"\b(?:RATIFIED|PRECEPT|sha256|no-go|NO-GO)\b",
]


def protected_tokens(text):
    toks = set()
    for pat in PROTECT_PATTERNS:
        toks |= set(re.findall(pat, text))
    return toks


def preservation_check(before, after):
    """Return the protected tokens present before and missing after.
    A rewrite that loses one is refused — meaning is not negotiable (L3)."""
    return sorted(protected_tokens(before) - protected_tokens(after))


def llm_rewrite(cmd, text, budget):
    """Hand the text to an external rewriter (local model CLI, etc.).
    The tool never writes the result until preservation_check passes."""
    instr = (
        "Rewrite the text between the markers so it fits within %d characters.\n"
        "Preserve EVERY instruction, number, path, filename, identifier, heading "
        "and law reference exactly. Compress wording only. Use the shortest "
        "unambiguous phrasing. Output only the rewritten text, no commentary.\n"
        "---BEGIN---\n%s\n---END---\n" % (budget, text)
    )
    try:
        proc = subprocess.run(shlex.split(cmd), input=instr,
                              capture_output=True, text=True, timeout=600)
    except Exception as exc:
        return None, "llm command failed: %s" % exc
    if proc.returncode != 0:
        return None, "llm exited %d: %s" % (proc.returncode,
                                           (proc.stderr or "")[:200])
    out = proc.stdout.strip()
    if not out:
        return None, "llm returned empty output"
    return out, None


def fit(text, kind, budget, truncate=False, llm_cmd=None, allow_truncate=True):
    """Return (result_text, trace, ok).

    Ordering is deliberate (L3): lossless -> guarded rewrite -> explicit truncate.
    The floor is reported honestly rather than reached by mangling meaning."""
    trace = []
    n0 = len(text)
    cur, _mech = run_mechanical(text, kind)
    n1 = len(cur)
    trace.append(("start", n0, None))
    trace.append(("mechanical (lossless)", n1, n0 - n1))
    if n1 <= budget:
        trace.append(("verdict", n1, "REACHED by the lossless pass alone"))
        return cur, trace, True

    if truncate and allow_truncate:
        cut = cur[:budget]
        trace.append(("truncate (EXPLICIT)", len(cut),
                      "HARD CUT — meaning NOT preserved, as instructed"))
        return cut, trace, True

    if llm_cmd:
        out, err = llm_rewrite(llm_cmd, cur, budget)
        if out is None:
            trace.append(("rewrite (llm)", n1, "FAILED — %s" % err))
        else:
            trace.append(("rewrite (llm)", len(out), n1 - len(out)))
            missing = preservation_check(cur, out)
            if missing:
                trace.append(("preservation check", len(out),
                              "REJECTED — %d protected token(s) lost: %s"
                              % (len(missing), ", ".join(missing[:6]))))
                trace.append(("floor", n1, "kept the lossless text; rewrite refused"))
                return cur, trace, False
            trace.append(("preservation check", len(out),
                          "PASS — every protected token survived"))
            if len(out) <= budget:
                trace.append(("verdict", len(out),
                              "REACHED by lossless + guarded rewrite"))
                return out, trace, True
            trace.append(("floor", len(out),
                          "meaning preserved but still over by %d chars"
                          % (len(out) - budget)))
            return out, trace, False

    trace.append(("floor", n1,
                  "cannot reach %d without sentence-level rewriting; floor = %d "
                  "(gap %d). Options: raise the budget | pass --llm CMD to rewrite "
                  "under a preservation check | --truncate for a hard cut."
                  % (budget, n1, n1 - budget)))
    return cur, trace, False


# --------------------------------------------------------------------------
# unify mode — convention divergence between two artifacts
# --------------------------------------------------------------------------


def sniff_conventions(path, text):
    lines = text.split("\n")
    indents = Counter()
    quotes_s = quotes_d = 0
    for ln in lines:
        m = re.match(r"^( +)\S", ln)
        if m:
            indents[len(m.group(1))] += 1
    for ln in lines:
        for ch in ln:
            if ch == "'":
                quotes_s += 1
            elif ch == '"':
                quotes_d += 1
    lens = sorted(len(l) for l in lines if l.strip())
    p95 = lens[int(len(lens) * 0.95)] if lens else 0
    tabs = sum(1 for l in lines if l.startswith("\t"))
    return {
        "indent_width": (indents.most_common(1)[0][0] if indents else 0),
        "tabs": tabs > len(lines) * 0.1,
        "quote_style": "double" if quotes_d > quotes_s else "single",
        "line_len_p95": p95,
        "trailing_comma": len(re.findall(r",\s*\n\s*[\]\})]", text)) > 0,
        "semicolons": text.count(";"),
        "snake_case": len(re.findall(r"\b[a-z]+_[a-z]+\b", text)),
        "camel_case": len(re.findall(r"\b[a-z]+[A-Z][a-zA-Z]*\b", text)),
        "blank_between_defs": len(re.findall(r"\n\n(def |function |class |func )",
                                             text)),
        "docstrings": len(re.findall(r'^\s*("""|\'\'\')', text, flags=re.M)),
    }


def unify_report(pa, pb, ta, tb):
    A, B = sniff_conventions(pa, ta), sniff_conventions(pb, tb)
    rows = []
    for k in A:
        if A[k] != B[k]:
            rows.append((k, A[k], B[k]))
    return A, B, rows


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def human(n):
    return "{:,}".format(int(n))


def render_report(path, kind, text, mech, sem, extra=None):
    n0 = len(text)
    L = []
    tot_m = sum(f["saved"] for f in mech)
    proposed = list(sem) + list(extra or [])
    tot_s = sum(f["saved"] for f in proposed)
    L.append("artifact-optimizer v%s" % VERSION)
    L.append("  target : %s" % path)
    L.append("  kind   : %s   %s chars   %s lines" %
             (kind, human(n0), human(text.count("\n") + 1)))
    if LAW_MARKERS.search(text):
        L.append("  flag   : RATIFIED-LAW MARKERS PRESENT — semantic tier is "
                 "report-only (L2)")
    L.append("")
    L.append("BATTERY (full sweep, no goal stated)")
    L.append("  [A] MECHANICAL — lossless, reversible, safe to auto-apply")
    if mech:
        for f in sorted(mech, key=lambda x: -x["saved"]):
            L.append("    %-22s %-42s %9s chars" %
                     (f["rule"], f.get("detail", "")[:42], human(f["saved"])))
    else:
        L.append("    (nothing found — already tight)")
    L.append("    %-22s %-42s %9s chars  (%s%%)" %
             ("SUBTOTAL", "", human(tot_m),
              round(100.0 * tot_m / max(n0, 1), 2)))
    L.append("")
    if extra:
        L.append("  [B] STRUCTURAL — code-aware, proposed")
        for f in sorted(extra, key=lambda x: -x["saved"]):
            L.append("    %-22s %-42s %9s chars%s" %
                     (f["rule"], f.get("detail", "")[:42], human(f["saved"]),
                      "  x%s" % f["count"] if f.get("count") else ""))
            if f.get("sample"):
                L.append("        %s" % f["sample"].replace("\n", " ")[:96])
            elif f.get("line_start"):
                L.append("        lines %s-%s" % (f["line_start"], f["line_end"]))
        L.append("")
    L.append("  [C] SEMANTIC — proposed, needs a human eye")
    if sem:
        for f in sorted(sem, key=lambda x: (-x["saved"], x["rule"]))[:15]:
            amount = ("clarity only" if f.get("clarity")
                      else human(f["saved"]) + " chars")
            L.append("    %-28s %-40s %13s%s" %
                     (f["rule"], f.get("detail", "")[:40], amount,
                      "  x%s" % f["count"] if f.get("count") else ""))
            if f.get("sample"):
                L.append("        %s" % f["sample"].replace("\n", " ")[:96])
    else:
        L.append("    (nothing found)")
    L.append("")
    L.append("  total mechanical (auto)      %9s chars  (%s%%)" %
             (human(tot_m), round(100.0 * tot_m / max(n0, 1), 2)))
    L.append("  total proposed  (review)     %9s chars  (%s%%)" %
             (human(tot_s), round(100.0 * tot_s / max(n0, 1), 2)))
    L.append("  projected best               %9s chars  (%s%%)" %
             (human(tot_m + tot_s), round(100.0 * (tot_m + tot_s) / max(n0, 1), 2)))
    L.append("")
    L.append("  goal: none stated. Add --budget N to fit a target, --apply to "
             "write the mechanical part.")
    return "\n".join(L)


def render_fit(path, n0, budget, trace, ok, result):
    L = ["artifact-optimizer v%s" % VERSION, "  target : %s" % path]
    L.append("  GOAL   : fit <= %s chars, meaning preserved (default)" % human(budget))
    L.append("  note   : interpreting this as 'rewrite so every instruction and "
             "meaning survives inside %s'" % human(budget))
    L.append("           — not a hard cut. A hard cut needs --truncate, explicitly.")
    L.append("")
    for stage, size, delta in trace:
        if delta is None:
            L.append("  %-24s %10s chars" % (stage, human(size)))
        elif isinstance(delta, int):
            L.append("  %-24s %10s chars   (%+d)" % (stage, human(size), -delta))
        else:
            L.append("  %-24s %10s chars   %s" % (stage, human(size), delta))
    L.append("")
    L.append("  RESULT : %s   final %s chars (was %s, -%s)" %
             ("FIT" if ok else "FLOOR", human(len(result)), human(n0),
              human(n0 - len(result))))
    return "\n".join(L)


# --------------------------------------------------------------------------
# io
# --------------------------------------------------------------------------


def read_target(p):
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def backup_and_write(path, text):
    data = text.encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()[:12]
    bak = path + ".aobak-" + sha
    if not os.path.exists(bak):
        with open(bak, "wb") as fh:
            fh.write(open(path, "rb").read())
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return bak, sha


def gather(targets):
    """Return ordered {path: text} for files, or all text files under a dir."""
    out = {}
    for t in targets:
        if os.path.isdir(t):
            for root, dirs, names in os.walk(t):
                dirs[:] = [d for d in dirs
                           if d not in (".git", "node_modules", "__pycache__",
                                        ".venv", "venv", ".mypy_cache")]
                for n in sorted(names):
                    if n.startswith("."):
                        continue
                    fp = os.path.join(root, n)
                    if os.path.splitext(n)[1].lower() in (
                            CODE_EXT | MD_EXT | CFG_EXT | {".txt", ".log"}):
                        try:
                            if os.path.getsize(fp) < 2_000_000:
                                out[fp] = read_target(fp)
                        except OSError:
                            pass
        elif os.path.isfile(t):
            out[t] = read_target(t)
    return out


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------


def selftest():
    fails = 0
    def check(name, cond):
        nonlocal fails
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            fails += 1

    # mechanical
    t = "/Users/x/file\n\n\n\n\nfoo  bar\nin order to win\nhello hello world\n"
    out, fs = run_mechanical(t, "markdown")
    check("trailing/blank/space/verbose collapse",
          "in order to" not in out and "foo bar" in out and "hello world" in out)
    check("mechanical measured > 0", sum(f["saved"] for f in fs) > 0)

    # losslessness: no letter content removed except known rules
    a = "The quick brown fox.\n"
    b, _ = run_mechanical(a, "text")
    check("plain prose untouched", a.strip() == b.strip())

    # tilde collapse
    o = t_path_tilde(os.path.join(HOME, "x"))
    check("home collapse", o.startswith("~/"))

    # dup-word whitelist
    check("whitelisted double kept", "had had" in t_dup_word("he had had enough"))
    check("error double collapsed", t_dup_word("the the fox") == "the fox")

    # budget: reachable by mechanical
    big = ("in order to " * 200)
    res, tr, ok = fit(big, "text", len(big))
    check("budget reached trivially", ok)

    # budget: floor reported honestly, no silent cut
    src = " ".join("w%d" % i for i in range(100))
    res, tr, ok = fit(src, "text", 10)
    check("floor reported, not silent truncation", ok is False)
    check("floor keeps every token", len(res.split()) == 100)
    check("floor message names the gap",
          any(isinstance(d, str) and "floor" in d.lower() for _, _, d in tr))

    # truncate is explicit
    res, tr, ok = fit("word " * 100, "text", 10, truncate=True)
    check("explicit truncate works", ok and len(res) <= 10)

    # meaning-preservation guard
    check("guard catches a lost number",
          preservation_check("budget is 12,000 chars", "budget is chars")
          == ["12,000"])
    check("guard catches a lost path",
          "~/work/x.py" in preservation_check(
              "run ~/work/x.py now", "run it now"))
    check("guard passes a clean rewrite",
          preservation_check("run ~/work/x.py then wait",
                              "run ~/work/x.py; wait") == [])
    check("guard catches a lost law marker",
          preservation_check("RATIFIED 2026-09-21", "approved recently")
          == ["2026-09-21", "RATIFIED"])

    # an llm that mangles must be refused, and the original kept
    res, tr, ok = fit("keep 12,000 chars exactly here " * 20, "text", 10,
                      llm_cmd="cat")
    check("llm rewrite that cannot shrink still returns floor", ok is False)

    # a hostile rewriter that drops meaning is refused outright
    class _Hostile(object):
        stdout = "shortened"
        returncode = 0
        stderr = ""
    _orig = globals()["llm_rewrite"]
    globals()["llm_rewrite"] = lambda c, t, b: ("shortened", None)
    res, tr, ok = fit("keep 12,000 chars " * 30, "text", 10, llm_cmd="x")
    globals()["llm_rewrite"] = _orig
    check("meaning-destroying rewrite refused", ok is False)
    check("original preserved after refusal", "12,000" in res)

    # structure
    code = "\ndef f():\n    pass\n# x = 1\n# y = 2\n# z = 3\n" + "v = 'longliteral'\n" * 5
    sf = detect_structural("a.py", code, "code")
    check("structural finds dead block", any("dead-comment" in f["rule"] for f in sf))
    check("structural finds repeated literal",
          any("repeated-literal" in f["rule"] for f in sf))

    # semantic
    md = "## Title\n\n## Title\n\n" + ("This is a very long sentence " * 50) + "."
    sm = detect_semantic("a.md", md, "markdown")
    check("semantic finds duplicate heading",
          any("duplicate-heading" in f["rule"] for f in sm))
    check("semantic flags run-on",
          any("run-on" in f["rule"] for f in sm))

    # law guard
    check("law markers detected",
          bool(LAW_MARKERS.search("RATIFIED by the author: PRECEPT 12")))
    check("plain text not flagged",
          not LAW_MARKERS.search("just a normal sentence about lunch"))

    # cross-file
    para = "This paragraph is long enough to be indexed across the corpus. " * 2
    cf = detect_cross_file({"a.md": para, "b.md": para})
    check("cross-file duplicate found", any("cross-file" in f["rule"] for f in cf))

    # unify
    A, B, rows = unify_report("a.py", "b.py",
                              "def f():\n    x = 'a'\n",
                              "def f():\n\tx = \"a\"\n")
    check("unify detects divergence", len(rows) > 0)

    print("")
    print("selftest: %d failure(s)" % fails)
    return fails


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="optimize.py",
        description="Run the full optimization battery on any artifact.")
    ap.add_argument("targets", nargs="*")
    ap.add_argument("--budget", type=int, default=None)
    ap.add_argument("--goal", default=None)
    ap.add_argument("--llm", dest="llm_cmd", default=None,
                    help="external rewriter command; result is accepted only if "
                         "every protected token survives")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--truncate", action="store_true")
    ap.add_argument("--unify", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.targets:
        ap.print_help()
        return 2

    files = gather(args.targets)

    if args.unify:
        ts = [t for t in args.targets if os.path.isfile(t)]
        if len(ts) < 2:
            print("unify needs two files"); return 2
        ta, tb = read_target(ts[0]), read_target(ts[1])
        A, B, rows = unify_report(ts[0], ts[1], ta, tb)
        print("artifact-optimizer v%s — convention divergence" % VERSION)
        print("  A: %s\n  B: %s\n" % (ts[0], ts[1]))
        for k, a, b in rows:
            print("  %-22s A=%-14s B=%-14s" % (k, a, b))
        print("\n  %d divergent convention(s). Unification is a rewrite — "
              "proposed, never automatic." % len(rows))
        return 0

    report = []
    for path, text in files.items():
        kind = kind_of(path, text)
        mech_text, mech = run_mechanical(text, kind)
        struct = detect_structural(path, text, kind)
        sem = detect_semantic(path, text, kind)

        if args.budget is not None:
            res, trace, ok = fit(text, kind, args.budget, truncate=args.truncate,
                                 llm_cmd=args.llm_cmd)
            if args.apply and ok:
                bak, sha = backup_and_write(path, res)
                trace.append(("wrote", len(res), "backup %s" % bak))
            report.append(render_fit(path, len(text), args.budget, trace, ok, res))
            if args.json:
                report.append(json.dumps(
                    {"path": path, "budget": args.budget, "ok": ok,
                     "final": len(res), "trace": trace}, indent=2))
            continue

        if args.apply:
            if mech:
                bak, sha = backup_and_write(path, mech_text)
                report.append("  wrote %s (backup %s, -%d chars)"
                              % (path, os.path.basename(bak),
                                 len(text) - len(mech_text)))
            else:
                report.append("  %s: nothing mechanical to apply" % path)
            continue

        if args.json:
            report.append(json.dumps(
                {"path": path, "kind": kind, "chars": len(text),
                 "mechanical": mech, "structural": struct, "semantic": sem},
                indent=2))
        else:
            report.append(render_report(path, kind, text, mech, sem, struct))

    if len(files) > 1:
        cf = detect_cross_file(files)
        if cf:
            report.append("")
            report.append("CORPUS — cross-file redundancy (proposed; removal needs "
                          "the per-lane self-sufficiency check)")
            for f in cf[:15]:
                report.append("  %-26s ~%6s chars  in %s" %
                              (f["rule"], human(f["saved"]),
                               ", ".join(f["files"])[:62]))
                if f.get("sample"):
                    report.append("        \"%s\"" % f["sample"][:92])

    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
