"""Unit tests for pass 8.9 (prose-promise advisory)."""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from deeppeek.recover.core import FileRecord
from deeppeek.recover.prose_promise import (
    _promised_call_sites,
    _body_includes_call_updates,
    pass8_9_check_prose_promises,
)


def _rec(path, content="", patches=None):
    rec = FileRecord(path=path, state="SINGLE_FULL")
    rec.content = content
    rec.patches = patches if patches is not None else []
    rec.trust = "HIGH"
    return rec


# ---------------------------------------------------------------------------
# _promised_call_sites
# ---------------------------------------------------------------------------

def test_promised_call_sites_comfyui_shape():
    """The exact comfyui.py prose shape: `update the three
    `paste_image(...)` / `paste_batch_as_layers(...)` call sites`,
    with intervening `_paste_result signature and` material between
    the verb and the count.  Pattern A (`fn chain + call sites`)
    extracts both."""
    prose = (
        "And update the `_paste_result` signature and the three "
        "`paste_image(...)` / `paste_batch_as_layers(...)` call "
        "sites inside it so rotation is forwarded:"
    )
    fns = _promised_call_sites(prose)
    assert "paste_image" in fns
    assert "paste_batch_as_layers" in fns


def test_promised_call_sites_bare_count():
    """`update the three call sites` — no function names.  Returns
    empty; the pass skips (no specific target to check)."""
    prose = "And update the three call sites:"
    assert _promised_call_sites(prose) == []


def test_promised_call_sites_of_fn_no_count():
    """`update the call sites of `func`` — count optional; fn
    extracted from the `of `fn`` tail via pattern B.

    Previously documented as a limitation (`[]`); the two-pattern
    fix makes count truly optional."""
    prose = "Update the call sites of `handler`:"
    fns = _promised_call_sites(prose)
    assert fns == ["handler"]


def test_promised_call_sites_count_first():
    """`Change all 4 call sites of `foo`` — count and fn both
    present, fn after "call sites" via pattern B."""
    prose = "Change all 4 call sites of `foo`:"
    fns = _promised_call_sites(prose)
    assert "foo" in fns


def test_promised_call_sites_ignores_no_call_site_word():
    prose = "Update the three files:"
    assert _promised_call_sites(prose) == []


def test_promised_call_sites_requires_update_verb():
    """`the `foo(...)` call sites are reviewed` — descriptive prose
    without an update verb.  No promise; returns empty."""
    prose = "The `foo(...)` call sites are reviewed above:"
    assert _promised_call_sites(prose) == []


# ---------------------------------------------------------------------------
# _body_includes_call_updates
# ---------------------------------------------------------------------------

def test_body_includes_call_updates_yes():
    body = (
        "node = paste_image(doc, images[0], x, y, name,\n"
        "                   rotation=output_rotation)\n"
    )
    assert _body_includes_call_updates(body, ["paste_image"]) is True


def test_body_includes_call_updates_multiline_kwarg():
    body = (
        "paste_image(\n"
        "    doc, images[0], x, y, name,\n"
        "    blend_mode=blend_mode,\n"
        "    rotation=output_rotation,\n"
        ")\n"
    )
    assert _body_includes_call_updates(body, ["paste_image"]) is True


def test_body_includes_call_updates_no():
    body = (
        "def _paste_result(self, doc, result,\n"
        "                  output_rotation: float = 0.0) -> dict:\n"
    )
    assert _body_includes_call_updates(body, ["paste_image"]) is False


def test_body_includes_call_updates_wrong_kwarg():
    body = "paste_image(doc, images[0], x, y, name, blend_mode=bm)\n"
    assert _body_includes_call_updates(body, ["paste_image"]) is False


def test_body_includes_call_updates_substring_fn_not_matched():
    """`paste_image_legacy` should not match `paste_image`."""
    body = "paste_image_legacy(doc, x, y, rotation=r)\n"
    assert _body_includes_call_updates(body, ["paste_image"]) is False


# ---------------------------------------------------------------------------
# pass8_9_check_prose_promises
# ---------------------------------------------------------------------------

def test_advisory_fires_on_signature_only_body():
    """The motivating shape: prose promises 3 call-site updates but
    the fence is a signature-only body."""
    rec = _rec("x.py", content="...", patches=[{
        "idx": 99,
        "body": (
            "    def _paste_result(self, doc, result: GenerationResult,\n"
            "                      output_rotation: float = 0.0) -> dict:\n"
        ),
        "context_before": (
            "And update the `_paste_result` signature and the three "
            "`paste_image(...)` / `paste_batch_as_layers(...)` call "
            "sites inside it so rotation is forwarded:"
        ),
        "applied_by": "deterministic",
    }])
    records = {"x.py": rec}
    n = pass8_9_check_prose_promises(records)
    assert n == 1, "expected one advisory"
    assert any("advisory" in w for w in rec.warnings)
    assert any("paste_image" in w for w in rec.warnings)


def test_advisory_silent_when_call_site_patch_present():
    """If a *separate* patch in the record contains the call-site
    update for one of the promised fns, only the still-missing fn
    triggers the advisory.  Aggregates across patches, checks each
    fn independently."""
    rec = _rec("x.py", content="...", patches=[
        {
            "idx": 99,
            "body": "    def _paste_result(self, ..., output_rotation=0.0):\n",
            "context_before": (
                "And update the `_paste_result` signature and the three "
                "`paste_image(...)` / `paste_batch_as_layers(...)` "
                "call sites inside it:"
            ),
            "applied_by": "deterministic",
        },
        {
            "idx": 100,
            "body": (
                "node = paste_image(doc, images[0], x, y, name,\n"
                "                   rotation=output_rotation)\n"
            ),
            "context_before": "Add the call site update:",
            "applied_by": "deterministic",
        },
    ])
    records = {"x.py": rec}
    n = pass8_9_check_prose_promises(records)
    # paste_image covered; paste_batch_as_layers still missing
    assert n == 1
    assert any("paste_batch_as_layers" in w for w in rec.warnings)


def test_advisory_silent_when_no_promise():
    rec = _rec("x.py", content="...", patches=[{
        "idx": 1,
        "body": "some_function()\n",
        "context_before": "Add a thing:",
        "applied_by": "deterministic",
    }])
    records = {"x.py": rec}
    n = pass8_9_check_prose_promises(records)
    assert n == 0
    assert not any("advisory" in w for w in rec.warnings)


def test_advisory_skips_deleted():
    rec = _rec("x.py", content="...", patches=[{
        "idx": 1,
        "body": "x",
        "context_before": "update the three `f(...)` call sites:",
        "applied_by": "deterministic",
    }])
    rec.is_deleted = True
    records = {"x.py": rec}
    n = pass8_9_check_prose_promises(records)
    assert n == 0


def test_advisory_does_not_demote_trust():
    rec = _rec("x.py", content="...", patches=[{
        "idx": 1,
        "body": "    def foo(self, output_rotation=0.0):\n",
        "context_before": "update the two `bar(...)` call sites:",
        "applied_by": "deterministic",
    }])
    rec.trust = "HIGH"
    records = {"x.py": rec}
    pass8_9_check_prose_promises(records)
    assert rec.trust == "HIGH", (
        "advisory must not demote; it is warn-only")


def main():
    tests = [
        ("promised call sites comfyui shape",
         test_promised_call_sites_comfyui_shape),
        ("promised call sites bare count",
         test_promised_call_sites_bare_count),
        ("promised call sites of fn no count",
         test_promised_call_sites_of_fn_no_count),
        ("promised call sites count first",
         test_promised_call_sites_count_first),
        ("promised call sites ignores no call site word",
         test_promised_call_sites_ignores_no_call_site_word),
        ("promised call sites requires update verb",
         test_promised_call_sites_requires_update_verb),
        ("body includes call updates yes",
         test_body_includes_call_updates_yes),
        ("body includes call updates multiline kwarg",
         test_body_includes_call_updates_multiline_kwarg),
        ("body includes call updates no",
         test_body_includes_call_updates_no),
        ("body includes call updates wrong kwarg",
         test_body_includes_call_updates_wrong_kwarg),
        ("body includes call updates substring fn not matched",
         test_body_includes_call_updates_substring_fn_not_matched),
        ("advisory fires on signature only body",
         test_advisory_fires_on_signature_only_body),
        ("advisory silent when call site patch present",
         test_advisory_silent_when_call_site_patch_present),
        ("advisory silent when no promise",
         test_advisory_silent_when_no_promise),
        ("advisory skips deleted",
         test_advisory_skips_deleted),
        ("advisory does not demote trust",
         test_advisory_does_not_demote_trust),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS: {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL: {name}\n        {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR: {name}\n         {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed}/{len(tests)} prose-promise test(s) failed")
        return 1
    print(f"\nAll {len(tests)} prose-promise tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
