"""CLAIMS-POLICY.md as a lint (Bet 15, slice 1): authored copy never claims medicine or borrows a score name.

The policy file is the source of truth: its fenced ``terms <category> [case]`` blocks are the banned lists
(one term per line, a trailing ``*`` is any word ending) and its ``allow`` block is the exact-sentence
allowlist for negations. Scope is authored strings only; contract labels (``contract.py``, ``mcp.json``, the
Rust read paths) are BACKLOG debt and out of scope, as are tests, docs and pitches (see the policy).
"""

import ast
import pathlib
import re

import pytest

from disconect import identity

PROJECTS = pathlib.Path(__file__).resolve().parents[2]
POLICY = PROJECTS / "disconect" / "CLAIMS-POLICY.md"
APP = PROJECTS / "disconect-app"

SCOPE = [
    *sorted((APP / "src").glob("*.ts")),
    APP / "index.html",
    APP / "README.md",
    PROJECTS / "disconect" / "src" / "disconect" / "identity.py",
    PROJECTS / "disconect-core" / "src" / "identity.rs",
    PROJECTS / "disconect" / "README.md",
    APP / "src-tauri" / "src" / "coach" / "prompt.rs",
]

SCORE_CATEGORIES = ("score-marks", "score-names")
VENDOR_FIGURE = ", vendor"  # rule 3: a string naming a vendor's own figure may carry a score name


def parse_policy(text: str):
    """Return (categories, allow): categories maps name -> (case_sensitive, compiled regex)."""
    categories, allow = {}, []
    for info, body in re.findall(r"^```([^\n]+)\n(.*?)^```", text, re.S | re.M):
        words = info.split()
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if words[0] == "allow":
            allow += lines
        elif words[0] == "terms":
            categories[words[1]] = (("case" in words[2:]), _compile(lines, "case" in words[2:]))
    return categories, allow


def _compile(terms, case_sensitive):
    parts = []
    for term in terms:
        words = [re.escape(w.rstrip("*")) + (r"\w*" if w.endswith("*") else "") for w in term.split()]
        parts.append(r"\s+".join(words))
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", 0 if case_sensitive else re.IGNORECASE)


def violations(text: str, categories, allow):
    """Banned hits in ``text`` after the exact allowlisted sentences are removed."""
    for sentence in allow:
        text = text.replace(sentence, " ")
    vendor_figure = VENDOR_FIGURE in text.lower()
    hits = []
    for name, (_case, pattern) in categories.items():
        if vendor_figure and name in SCORE_CATEGORIES:
            continue
        hits += [(name, m.group(0)) for m in pattern.finditer(text)]
    return hits


_TOKENS = {
    ".ts": re.compile(r'"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`|//[^\n]*|/\*.*?\*/', re.S),
    ".rs": re.compile(r'r(#*)"(.*?)"\1|"((?:[^"\\]|\\.)*)"|//[^\n]*|/\*.*?\*/', re.S),
}


def authored_strings(path: pathlib.Path):
    text = path.read_text()
    if path.suffix == ".py":
        return [n.value for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    if path.suffix == ".md":
        return [text]
    if path.suffix == ".html":
        return re.findall(r">([^<>]+)<", text) + re.findall(r'(?:title|alt|placeholder|aria-label)="([^"]*)"', text)
    strings = []
    for m in _TOKENS[path.suffix].finditer(text):
        body = next((g for g in m.groups()[1:] if g) if path.suffix == ".rs" else (g for g in m.groups() if g), None)
        if body:
            strings.append(body)
    return strings


def test_policy_parses_into_the_four_lists_and_an_allowlist():
    categories, allow = parse_policy(POLICY.read_text())
    assert set(categories) == {"medical-verbs", "medical-nouns", "score-marks", "score-names"}
    assert categories["score-names"][0] is True and categories["medical-nouns"][0] is False
    assert allow and identity.DISCLAIMER in allow
    assert len(POLICY.read_text().splitlines()) <= 80, "keep the policy short"


def test_the_disclaimer_is_one_short_sentence_that_says_what_it_is_not():
    assert len(identity.DISCLAIMER.split()) <= 25
    assert "not medical advice" in identity.DISCLAIMER


def test_the_matcher_rejects_a_banned_word_and_accepts_an_allowlisted_sentence():
    categories, allow = parse_policy(POLICY.read_text())
    assert violations("We diagnose your sleep.", categories, allow) == [("medical-verbs", "diagnose")]
    assert violations("Diagnosis", categories, allow), "whole words with an ending wildcard, any case"
    assert violations("It may detect a condition.", categories, allow), "a multi-word term"
    assert violations("Your Readiness today", categories, allow) == [("score-names", "Readiness")]
    assert not violations("recovery in lowercase prose is fine", categories, allow)
    assert not violations("Readiness, vendor", categories, allow), "rule 3"
    assert violations("Body Battery, vendor high", categories, allow) == [] and violations("TSB", categories, allow)
    assert not violations("They describe; they do not diagnose.", categories, allow)
    assert not violations(identity.DISCLAIMER, categories, allow)
    assert violations("They describe; they do not diagnose. We diagnose.", categories, allow), "only the sentence is removed"
    assert not violations("A secure curator.", categories, allow), "no substring hits"


def test_the_extractors_see_strings_and_not_comments():
    probe = pathlib.Path(__file__)  # a .py file: ast sees real constants only
    assert any("CLAIMS-POLICY" in s for s in authored_strings(probe))
    for suffix, source, expect in (
        (".ts", 'const a = "hello"; // a diagnose comment\nconst b = `tick`;', ["hello", "tick"]),
        (".rs", 'const A: &str = "x"; // a diagnose comment\nconst B: &str = r#"y "q""#;', ["x", 'y "q"']),
    ):
        scratch = probe.with_name(f"_probe{suffix}")
        try:
            scratch.write_text(source)
            assert authored_strings(scratch) == expect
        finally:
            scratch.unlink()


@pytest.mark.parametrize("path", SCOPE, ids=lambda p: str(p.relative_to(PROJECTS)))
def test_authored_copy_makes_no_medical_claim_and_borrows_no_score_name(path):
    assert path.exists(), f"{path} is in the lint scope but missing"
    categories, allow = parse_policy(POLICY.read_text())
    offenders = [(name, hit, s.strip()[:80]) for s in authored_strings(path)
                 for name, hit in violations(s, categories, allow)]
    assert not offenders, offenders


def test_the_scope_covers_the_coach_prompt_constant():
    prompt = (APP / "src-tauri" / "src" / "coach" / "prompt.rs").read_text()
    assert re.search(r'pub const SYSTEM_PROMPT: &str = ', prompt)
