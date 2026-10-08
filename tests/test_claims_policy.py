"""CLAIMS-POLICY.md as a lint: authored copy never claims medicine or borrows a score name.

The policy file is the source of truth: its fenced ``terms <category> [case]`` blocks are the banned lists
(one term per line, a trailing ``*`` is any word ending) and its ``allow`` block is the exact-sentence
allowlist for negations. Scope is authored strings only; contract labels (``contract.py``, ``mcp.json``, the
Rust read paths) are known debt and out of scope, as are tests, docs and design notes (see the policy).
"""

import ast
import json
import pathlib
import re

import pytest

from disconect import identity

import monorepo

POLICY = monorepo.PROJECT / "CLAIMS-POLICY.md"
APP = monorepo.APP

SCOPE = [
    monorepo.PROJECT / "src" / "disconect" / "identity.py",
    monorepo.PROJECT / "README.md",
]
if monorepo.PRESENT:   # the shell and the Rust core carry authored copy too; they are linted beside this repository
    SCOPE += [
        *sorted((APP / "src").glob("*.ts")),
        APP / "index.html",
        APP / "README.md",
        monorepo.CRATE / "src" / "identity.rs",
        # every coach module: its fixed error and badge words reach the UI through the commands
        *sorted((APP / "src-tauri" / "src" / "coach").glob("*.rs")),
    ]

# rule 3 (2026-10-06): a vendor's own figure is labelled in plain words + ", vendor"; no exemption from the score lists


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
    hits = []
    for name, (_case, pattern) in categories.items():
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
    assert violations("Readiness, vendor", categories, allow) == [("score-names", "Readiness")], "rule 3: no exemption"
    assert violations("Body Battery, vendor high", categories, allow) and violations("TSB", categories, allow)
    assert not violations("Daily preparedness, vendor", categories, allow) and not violations("Rest time, vendor", categories, allow)
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


@pytest.mark.parametrize("path", SCOPE, ids=lambda p: str(p.relative_to(monorepo.PROJECT.parent)))
def test_authored_copy_makes_no_medical_claim_and_borrows_no_score_name(path):
    assert path.exists(), f"{path} is in the lint scope but missing"
    categories, allow = parse_policy(POLICY.read_text())
    offenders = [(name, hit, s.strip()[:80]) for s in authored_strings(path)
                 for name, hit in violations(s, categories, allow)]
    assert not offenders, offenders


@monorepo.needs_monorepo
def test_the_scope_covers_the_coach_prompt_constant():
    prompt = (APP / "src-tauri" / "src" / "coach" / "prompt.rs").read_text()
    assert re.search(r'pub const SYSTEM_PROMPT: &str = ', prompt)


def _policy_terms(category: str):
    blocks = re.findall(r"^```terms " + category + r"[^\n]*\n(.*?)^```", POLICY.read_text(), re.S | re.M)
    assert len(blocks) == 1, category
    return [line.strip() for line in blocks[0].splitlines() if line.strip()]


def _ts_table(name: str):
    """The keys of an identity.ts score table: each key is `mark("head", "tail")`, joined back together."""
    text = (APP / "src" / "identity.ts").read_text()
    body = re.search(r"export const " + name + r": \[string, string\]\[\] = \[(.*?)\n\];", text, re.S).group(1)
    pairs = re.findall(r'\[mark\("([^"]*)", "([^"]*)"\), "([^"]+)"\]', body)
    return [(head + tail, plain) for head, tail, plain in pairs]


@monorepo.needs_monorepo
def test_the_ts_neutralizer_table_has_exactly_the_policy_score_lists():
    marks, names = _ts_table("SCORE_MARKS"), _ts_table("SCORE_NAMES")
    assert [k for k, _ in marks] == _policy_terms("score-marks")
    assert [k for k, _ in names] == _policy_terms("score-names")
    assert len(marks) + len(names) == 9
    categories, allow = parse_policy(POLICY.read_text())
    for _key, plain in marks + names:
        assert not violations(plain, categories, allow), plain  # a plain-words form is itself clean copy
        assert not re.search(r"readiness|recovery", plain, re.I), plain


@monorepo.needs_monorepo
def test_the_ts_neutralizer_source_spells_no_score_name_and_no_maker_name():
    text = (APP / "src" / "identity.ts").read_text()
    # the metric labels above the neutralizer are in plain words too (rule 3), and the neutralizer may not spell a name
    neutralizer = text.split("what the coach's words are scrubbed with", 1)[1]
    code = "\n".join(line for line in neutralizer.splitlines() if not line.strip().startswith("//"))
    for key, _ in _ts_table("SCORE_MARKS") + _ts_table("SCORE_NAMES"):
        assert key not in code, f"{key} is spelled whole in the neutralizer"
    assert not re.search(r"garmin", text, re.I)


def test_the_ts_neutralizer_behaves(tmp_path):
    """Compile identity.ts with the project's own tsc and run neutralText under node, if both are there."""
    import shutil
    import subprocess

    tsc = APP / "node_modules" / ".bin" / "tsc"
    node = shutil.which("node")
    if not tsc.exists() or not node:
        pytest.skip("node or the project's tsc is not installed")
    subprocess.run([str(tsc), str(APP / "src" / "identity.ts"), "--target", "es2022", "--module", "esnext",
                    "--outDir", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "identity.mjs").write_text((tmp_path / "identity.js").read_text())
    maker = "gar" + "min"
    cases = {
        f"{maker} Connect and {maker.upper()}": "{vendor} and {vendor}",
        f"{maker}  connect": "{vendor}  connect",
        f"{maker} connectx": "{vendor}x",
        "gar" + "m\u0131n \u0130x GARM\u0130N": "{vendor} \u0130x {vendor}",
        "Your Training Read" + "iness is up. Body Bat" + "tery low; Recov" + "ery score fine.":
            "Your daily preparedness is up. Energy level low; rest estimate fine.",
        "Recov" + "ery matters. Not the recovery word, nor TS" + "SB": "Rest matters. Not the recovery word, nor TS" + "SB",
        "TS" + "S and C" + "TL and A" + "TL and T" + "SB": "Training stress total and long-term training load and "
                                                       "short-term training load and training balance",
        "plain text stays": "plain text stays",
    }
    script = ("import { neutralText } from './identity.mjs';"
              "const cases = JSON.parse(process.argv[1]);"
              "console.log(JSON.stringify(Object.keys(cases).map((k) => neutralText(k))));")
    run = subprocess.run([node, "--input-type=module", "-e", script, json.dumps(cases)], cwd=tmp_path,
                         check=True, capture_output=True, text=True)
    assert json.loads(run.stdout) == list(cases.values())
