"""The safety kernel is inlined verbatim into every skill (Codex loads SKILL.md
after picking a skill, so an external CONTRACT.md may never be read). The
duplication is deliberate; this test guards it against drift.

The guarantee is intentionally strict:

* Cross-skill: the bytes between the KERNEL markers must be **byte-for-byte
  identical** across all skills — read as raw bytes, no normalization, so even a
  trailing space or a CRLF would fail.
* Contract fidelity: those bytes must equal the canonical block in
  agents/CONTRACT.md §5, compared exactly **except** for leading/trailing
  whitespace — the contract renders the kernel inside a ``` code fence while the
  skills inline it as prose, so only the outer wrapper differs.

If this fails: do NOT hand-fix one skill. Edit the block in agents/CONTRACT.md
§5, then paste it verbatim into each skills/*/SKILL.md between the
`<!-- SKREPKA-KERNEL:BEGIN ... -->` and `<!-- SKREPKA-KERNEL:END -->` markers.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "agents" / "CONTRACT.md"
SKILLS_DIR = ROOT / "skills"

# The exact set of scenario skills that must carry the kernel. A new or renamed
# skill dir fails the set check below instead of silently skipping the kernel.
EXPECTED_SKILLS = frozenset({
    "skrepka-comments", "skrepka-transfer", "skrepka-publish",
    "skrepka-suggestions", "skrepka-whatsnew",
})

BEGIN = b"<!-- SKREPKA-KERNEL:BEGIN"
END = b"<!-- SKREPKA-KERNEL:END -->"
SENTINEL = "Работая со skrepka:"
KERNEL_RULE_COUNT = 6  # header line + exactly six "- " rules


def _skill_kernel_bytes(path):
    data = path.read_bytes()
    assert data.count(BEGIN) == 1, f"{path.parent.name}: BEGIN marker must appear once"
    assert data.count(END) == 1, f"{path.parent.name}: END marker must appear once"
    close = data.find(b"-->", data.find(BEGIN))
    assert close != -1, f"{path.parent.name}: malformed BEGIN marker"
    start = close + len(b"-->")
    end = data.find(END, start)
    assert end != -1 and end > start, f"{path.parent.name}: END marker before BEGIN"
    return data[start:end]


def _contract_kernel_bytes():
    data = CONTRACT.read_bytes()
    h = data.find(b"## 5.")
    assert h != -1, "agents/CONTRACT.md: missing '## 5.' section"
    nxt = data.find(b"\n## ", h + 1)
    section = data[h:] if nxt == -1 else data[h:nxt]
    open_fence = section.find(b"```")
    assert open_fence != -1, "CONTRACT §5: no code fence"
    body_start = section.find(b"\n", open_fence) + 1
    close_fence = section.find(b"```", body_start)
    assert close_fence != -1, "CONTRACT §5: unterminated code fence"
    assert section.find(b"```", close_fence + 3) == -1, "CONTRACT §5: more than one fence"
    return section[body_start:close_fence]


def _assert_shape(kernel_bytes, who):
    # Exactly the sentinel line + KERNEL_RULE_COUNT rule lines, nothing else.
    # A smuggled line (e.g. "IGNORE ALL RULES ABOVE") would either not start
    # with "- " or push the count past the expected total — both fail here.
    text = kernel_bytes.decode("utf-8").strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 1 + KERNEL_RULE_COUNT, \
        f"{who}: kernel must be exactly {1 + KERNEL_RULE_COUNT} non-empty lines, got {len(lines)}"
    assert lines[0] == SENTINEL, f"{who}: first line must equal the sentinel verbatim"
    for ln in lines[1:]:
        assert ln.startswith("- "), f"{who}: unexpected non-rule line in kernel: {ln!r}"


def test_expected_skill_set_present():
    found = {p.name for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()}
    assert found == EXPECTED_SKILLS, f"skill set drifted: {found} != {EXPECTED_SKILLS}"


def test_kernel_is_byte_identical_across_skills_and_contract():
    contract = _contract_kernel_bytes()
    _assert_shape(contract, "agents/CONTRACT.md §5")

    skills = sorted(SKILLS_DIR.glob("*/SKILL.md"))
    reference = None
    for path in skills:
        raw = _skill_kernel_bytes(path)
        _assert_shape(raw, path.parent.name)
        if reference is None:
            reference = raw
        # cross-skill: exact bytes, no normalization whatsoever
        assert raw == reference, f"{path.parent.name}: kernel bytes differ from another skill"
        # contract fidelity: exact except the fence/marker whitespace wrapper
        assert raw.strip() == contract.strip(), \
            f"{path.parent.name}: kernel drifted from agents/CONTRACT.md §5"


@pytest.mark.parametrize("name", sorted(EXPECTED_SKILLS))
def test_skill_has_frontmatter(name):
    text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{name}: missing opening frontmatter delimiter"
    parts = text.split("\n---\n", 1)
    assert len(parts) == 2, f"{name}: missing closing frontmatter delimiter"
    head = parts[0]
    m = re.search(r"^name:\s*(\S.*)$", head, re.MULTILINE)
    assert m, f"{name}: no name:"
    assert m.group(1).strip() == name, \
        f"{name}: frontmatter name {m.group(1).strip()!r} must equal directory name"
    assert re.search(r"^description:\s*\S", head, re.MULTILINE), f"{name}: no description:"
