"""Tests for :mod:`yomotsusaka.templates` (Fork 4 of #43).

Pins the template-registry shape and runs each shipped template against
the canonical ``Alice Tan`` fixture, asserting that:

* The template returns a :class:`TemplateResult` carrying only
  :class:`PublicHandle` artifacts.
* The committed artifact's redacted manifest contains no raw private
  value.
* The template's stdout/stderr does not echo raw values.

Per project ``CLAUDE.md``: raw private literals only live inside the
canonical fixture; expected-value assertions must not assert *positive*
presence of raw values.
"""

from __future__ import annotations

from pathlib import Path

from yomotsusaka.boundary import (
    PrivateState,
    PublicHandle,
    parse_locator,
    process_document_request,
)
from yomotsusaka.boundary import ProcessRequest
from yomotsusaka.execution_gateway import ExecutionRequest, ExecutionScope
from yomotsusaka.redactor import Span, redact
from yomotsusaka.templates import (
    TEMPLATES,
    TemplateResult,
    TemplateSpec,
)

from tests._exposure_denylist import CANONICAL_SPANS, CANONICAL_TEXT, RAW_VALUES


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_registry_carries_the_two_shipped_templates() -> None:
    assert "summarise_private_minutes" in TEMPLATES
    assert "generate_letter_from_private_template" in TEMPLATES


def test_registry_entries_are_template_specs() -> None:
    for name, spec in TEMPLATES.items():
        assert isinstance(spec, TemplateSpec), name
        assert spec.name == name
        assert callable(spec.fn)
        assert spec.min_scope is ExecutionScope.PRIVATE_BOUNDARY


def test_registry_is_explicitly_pinned_to_two_templates() -> None:
    """Per §D-6, this PR ships exactly two concrete templates. Any new
    template should land via a follow-up child issue, NOT as a drive-by
    change here. The assertion forces a maintainer touching this file to
    update the registry pin in lock-step."""
    assert set(TEMPLATES.keys()) == {
        "summarise_private_minutes",
        "generate_letter_from_private_template",
    }


# ---------------------------------------------------------------------------
# Canonical fixture setup helpers
# ---------------------------------------------------------------------------


def _setup_canonical_doc(vault_root: Path, doc_id: str) -> tuple[PublicHandle, PrivateState]:
    """Commit the canonical fixture under *doc_id* and return its public
    handle + a manually-constructed PrivateState (so the test does not
    need to flow through the full resolver)."""
    response = process_document_request(
        ProcessRequest(
            doc_id=doc_id,
            raw_text=CANONICAL_TEXT,
            spans=list(CANONICAL_SPANS),
        ),
        vault_root=vault_root,
    )
    handle = response.handle

    # Recover the private dict that the kernel just wrote.
    kernel_spans = [Span(start=s.start, end=s.end, kind=s.kind) for s in CANONICAL_SPANS]
    _, _, private_entries = redact(CANONICAL_TEXT, kernel_spans)

    private_state = PrivateState(
        manifest_path=vault_root / "manifests" / f"{doc_id}.json",
        private_dict_path=vault_root / "private" / f"{doc_id}.json",
        private_entries=private_entries,
    )
    return handle, private_state


# ---------------------------------------------------------------------------
# Template 1: summarise_private_minutes
# ---------------------------------------------------------------------------


def test_summarise_private_minutes_produces_public_artifact(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    handle, private_state = _setup_canonical_doc(vault, "minutes-001")

    spec = TEMPLATES["summarise_private_minutes"]
    request = ExecutionRequest(
        job_name="summarise_private_minutes",
        purpose="weekly-review",
        scope=ExecutionScope.PRIVATE_BOUNDARY,
        inputs={"target_handle": handle.locator},
    )
    result = spec.fn(request, private_state, vault)

    assert isinstance(result, TemplateResult)
    assert len(result.artifact_handles) == 1
    artifact = result.artifact_handles[0]
    assert isinstance(artifact, PublicHandle)
    parsed = parse_locator(artifact.locator)
    assert parsed is not None
    assert parsed.artifact_kind == "manifest"


def test_summarise_private_minutes_artifact_carries_no_raw_values(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    handle, private_state = _setup_canonical_doc(vault, "minutes-002")

    spec = TEMPLATES["summarise_private_minutes"]
    request = ExecutionRequest(
        job_name="summarise_private_minutes",
        purpose="weekly-review",
        scope=ExecutionScope.PRIVATE_BOUNDARY,
        inputs={"target_handle": handle.locator},
    )
    result = spec.fn(request, private_state, vault)

    artifact_locator = result.artifact_handles[0].locator
    parsed = parse_locator(artifact_locator)
    assert parsed is not None
    manifest_path = vault / "manifests" / f"{parsed.opaque_id}.json"
    contents = manifest_path.read_text(encoding="utf-8")
    for raw in RAW_VALUES:
        assert raw not in contents, (
            f"summarise_private_minutes artifact leaked raw value {raw!r}"
        )

    # Stdout / stderr also clean.
    for raw in RAW_VALUES:
        assert raw not in result.stdout
        assert raw not in result.stderr


# ---------------------------------------------------------------------------
# Template 2: generate_letter_from_private_template
# ---------------------------------------------------------------------------


def test_generate_letter_substitutes_then_re_scrubs(tmp_path: Path) -> None:
    """The template takes a redacted ``template_body`` (with placeholders),
    substitutes raw values, and re-scrubs back to redacted form before
    committing. The committed artifact must contain ONLY the keys,
    never the raw values."""
    vault = tmp_path / "vault"
    handle, private_state = _setup_canonical_doc(vault, "letter-001")

    # The body is already-redacted; the template will substitute raw
    # values briefly, then re-scrub.
    body = (
        "Dear <PERSON_a5f4ff58>,\n\n"
        "Your record at <ORG_a73cb456> (ID: <ID_NUMBER_5994471a>) is updated.\n"
    )

    spec = TEMPLATES["generate_letter_from_private_template"]
    request = ExecutionRequest(
        job_name="generate_letter_from_private_template",
        purpose="letter-generation",
        scope=ExecutionScope.PRIVATE_BOUNDARY,
        inputs={"target_handle": handle.locator, "template_body": body},
    )
    result = spec.fn(request, private_state, vault)

    assert isinstance(result, TemplateResult)
    assert len(result.artifact_handles) == 1

    # The committed letter artifact carries the redacted body — keys, no
    # raw values.
    artifact_locator = result.artifact_handles[0].locator
    parsed = parse_locator(artifact_locator)
    assert parsed is not None
    manifest_path = vault / "manifests" / f"{parsed.opaque_id}.json"
    contents = manifest_path.read_text(encoding="utf-8")

    for raw in RAW_VALUES:
        assert raw not in contents, (
            f"generate_letter artifact leaked raw value {raw!r}"
        )
    for key in ("<PERSON_a5f4ff58>", "<ORG_a73cb456>", "<ID_NUMBER_5994471a>"):
        assert key in contents, f"generate_letter artifact missing key {key}"


def test_generate_letter_stdout_carries_no_raw_values(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    handle, private_state = _setup_canonical_doc(vault, "letter-002")

    body = "Hello <PERSON_a5f4ff58>"
    spec = TEMPLATES["generate_letter_from_private_template"]
    request = ExecutionRequest(
        job_name="generate_letter_from_private_template",
        purpose="letter-stdout-test",
        scope=ExecutionScope.PRIVATE_BOUNDARY,
        inputs={"target_handle": handle.locator, "template_body": body},
    )
    result = spec.fn(request, private_state, vault)

    for raw in RAW_VALUES:
        assert raw not in result.stdout
        assert raw not in result.stderr
