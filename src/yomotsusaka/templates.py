"""Template registry for the Chikaeshi execution gateway (#43).

This module defines the closed-set library of template jobs the
:func:`yomotsusaka.boundary.execute_request` dispatcher will accept. Each
template is a pure-Python function executed inside the private-boundary
trust zone (it MAY import :mod:`yomotsusaka.pipeline`, :mod:`commit`,
:mod:`restoration_api`) and returns a :class:`TemplateResult` whose
``artifact_handles`` are :class:`PublicHandle` objects — i.e. the dispatcher
returns only opaque locators back to the caller.

Registry shape
--------------

A module-level :data:`TEMPLATES` dict maps ``template_name`` →
:class:`TemplateSpec`. New templates self-register at module import via
``TEMPLATES[name] = TemplateSpec(...)``. No plugin discovery, no entry
points — adding a template requires a deliberate PR edit, which keeps the
exposure-contract scan in lock-step with the registry.

This PR ships two concrete templates:

1. ``summarise_private_minutes`` — read a committed manifest, produce a
   redacted summary, commit it as a new public artifact.
2. ``generate_letter_from_private_template`` — substitute redacted keys in
   a caller-supplied template body with raw values from the resolved
   private dictionary, then run the scrubber inverted (raw → keys) on the
   result before committing the public artifact.

Out of scope for #43 (deferred to a follow-up child issue):
``render_private_pdf``, ``fill_private_form``, ``export_private_table_view``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from yomotsusaka.boundary import (
    PrivateState,
    PublicHandle,
    build_locator,
)
from yomotsusaka.execution_gateway import ExecutionRequest, ExecutionScope
from yomotsusaka.pipeline import process_document
from yomotsusaka.redactor import Span
from yomotsusaka.schemas import DocumentManifest, EntityKind
from yomotsusaka.scrubber import scrub_stream

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TemplateResult:
    """Return value of a template function.

    Attributes
    ----------
    artifact_handles:
        Public handles for any artifacts the template committed during
        execution. Each handle must be a :class:`PublicHandle` whose
        locator parses via :func:`boundary.parse_locator`.
    stdout:
        Free-form stdout-equivalent text. The dispatcher runs this
        through :func:`yomotsusaka.scrubber.scrub_stream` against the
        resolved ``private_dict`` before returning it on the
        :class:`ExecutionResponse`.
    stderr:
        Free-form stderr-equivalent text. Same scrubber pass as
        ``stdout``.
    """

    artifact_handles: tuple[PublicHandle, ...] = ()
    stdout: str = ""
    stderr: str = ""


# Callable signature for a template implementation. The dispatcher always
# invokes templates with ``(request, private_state, vault_root)``; templates
# that need additional context read it out of ``request.inputs``.
TemplateFn = Callable[[ExecutionRequest, PrivateState, Path], TemplateResult]


@dataclass(frozen=True)
class TemplateSpec:
    """One entry of the :data:`TEMPLATES` registry.

    Attributes
    ----------
    name:
        Registry key, must match ``ExecutionRequest.job_name``.
    fn:
        Template implementation; receives the request, the resolved
        :class:`PrivateState`, and the vault root.
    min_scope:
        Minimum :class:`ExecutionScope` required to invoke this template.
        The dispatcher returns
        :data:`ExecutionFailureReason.ScopeDenied` when the caller's
        scope is strictly weaker. For MVP the only meaningful gate is
        ``PRIVATE_BOUNDARY`` (which the dispatcher rejects for
        ordinary-agent callers).
    description:
        Short human description; surfaced in error messages and the
        ``docs/chikaeshi.md`` reference table.
    requires_locator_input:
        When ``True``, the template requires ``request.inputs`` to carry a
        ``"target_handle"`` key whose value is an opaque locator string.
        The dispatcher validates this before invoking the template; an
        omitted or malformed value surfaces as
        :data:`ExecutionFailureReason.SchemaInvalid`.
    """

    name: str
    fn: TemplateFn
    min_scope: ExecutionScope
    description: str
    requires_locator_input: bool = True
    # Reserved for future per-template input schemas; carried as a free-form
    # dict so a future PR can constrain ``ExecutionRequest.inputs`` per
    # template without breaking the registry shape.
    input_schema: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers shared by template implementations
# ---------------------------------------------------------------------------


def _load_manifest(vault_root: Path, opaque_id: str) -> DocumentManifest:
    """Load a committed :class:`DocumentManifest` from the vault.

    Used by templates that need to read the redacted text of the source
    artifact. The path is constructed from the opaque id; callers must
    only invoke this for artifacts they have already resolved.
    """
    manifest_path = vault_root / "manifests" / f"{opaque_id}.json"
    return DocumentManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )


def _new_artifact_id(template_name: str, source_id: str) -> str:
    """Generate a deterministic-ish artifact id for a template output.

    Combines the template name and a uuid suffix so collisions across
    repeated invocations are vanishingly unlikely while preserving a
    human-readable lineage hint. The result is constrained to the
    opaque-id charset (``[A-Za-z0-9._-]{1,128}``).
    """
    import uuid

    # Replace any character outside the opaque-id charset with '-'.
    safe_template = "".join(c if c.isalnum() else "-" for c in template_name)
    safe_source = "".join(c if c.isalnum() else "-" for c in source_id)
    suffix = uuid.uuid4().hex[:12]
    out = f"{safe_template}-{safe_source}-{suffix}"
    # Cap at 128 chars; pipeline._validate_doc_id enforces it.
    return out[:128]


# ---------------------------------------------------------------------------
# Template 1: summarise_private_minutes
# ---------------------------------------------------------------------------


def _summarise_private_minutes(
    request: ExecutionRequest,
    private_state: PrivateState,
    vault_root: Path,
) -> TemplateResult:
    """Read a committed manifest and produce a redacted summary artifact.

    The summary text is the first 240 characters of the manifest's
    ``redacted_text`` followed by an inventory of every private-dictionary
    key. The summary is committed as a fresh public artifact whose handle
    is returned to the caller.

    The template reads ``private_state.private_entries`` to enumerate the
    keys but never echoes ``original_value`` into the summary text. The
    dispatcher's scrubber pass would catch a regression here.
    """
    target_locator = request.inputs.get("target_handle")
    if not isinstance(target_locator, str):
        # Defensive — the dispatcher already validates this. Raise a
        # ValueError to surface a clear failure path in tests that bypass
        # the dispatcher.
        raise ValueError("target_handle missing or not a string")

    from yomotsusaka.boundary import parse_locator

    parsed = parse_locator(target_locator)
    if parsed is None:
        raise ValueError("target_handle does not parse as a locator")

    manifest = _load_manifest(vault_root, parsed.opaque_id)
    snippet = manifest.redacted_text[:240]
    keys = sorted({entry.key for entry in private_state.private_entries})
    summary_text = snippet + "\n\nentities: " + ", ".join(keys)

    # Commit the summary as a new public artifact. No spans (the summary
    # is already redacted-only by construction).
    summary_doc_id = _new_artifact_id("summarise-private-minutes", parsed.opaque_id)
    handle = process_document(
        doc_id=summary_doc_id,
        raw_text=summary_text,
        spans=[],
        vault_root=vault_root,
    )

    public = PublicHandle(
        locator=build_locator(
            exposure_class="agent_redacted",
            artifact_kind="manifest",
            opaque_id=handle.doc_id,
        )
    )
    return TemplateResult(
        artifact_handles=(public,),
        stdout=f"summarised {len(keys)} entit(ies) into doc {summary_doc_id}",
        stderr="",
    )


# ---------------------------------------------------------------------------
# Template 2: generate_letter_from_private_template
# ---------------------------------------------------------------------------


def _generate_letter_from_private_template(
    request: ExecutionRequest,
    private_state: PrivateState,
    vault_root: Path,
) -> TemplateResult:
    """Substitute redacted keys in a caller-supplied template body.

    The caller supplies ``inputs["template_body"]``: a redacted text
    containing ``<KIND_xxxxxxxx>`` placeholder keys. The template
    substitutes each key with its corresponding ``original_value`` from
    the resolved private dictionary, then runs the scrubber **inverted**
    (raw → keys) on the result so the artifact committed back to the
    vault is redacted again.

    This template exercises the "raw values touched in private process,
    only redacted artifact returned to agent" guarantee: the rendered
    raw-value text exists only inside this function's local variable,
    and the committed artifact carries the redacted form.
    """
    target_locator = request.inputs.get("target_handle")
    if not isinstance(target_locator, str):
        raise ValueError("target_handle missing or not a string")
    template_body = request.inputs.get("template_body")
    if not isinstance(template_body, str) or not template_body:
        raise ValueError("template_body missing or not a non-empty string")

    from yomotsusaka.boundary import parse_locator

    parsed = parse_locator(target_locator)
    if parsed is None:
        raise ValueError("target_handle does not parse as a locator")

    # Build a (key → raw value) map from the resolved private_state. We
    # use longest-key-first to avoid prefix collisions (though keys are
    # all of the canonical ``<KIND_<8 hex>>`` shape so this is mostly
    # belt-and-braces).
    key_to_raw = {
        entry.key: entry.original_value
        for entry in private_state.private_entries
        if entry.original_value
    }

    # Render the letter by substituting each key with the raw value. This
    # is the one place inside this function where raw values touch a
    # local variable; the committed artifact is the re-scrubbed form.
    rendered = template_body
    for key in sorted(key_to_raw.keys(), key=len, reverse=True):
        rendered = rendered.replace(key, key_to_raw[key])

    # Re-scrub: produce the redacted version of the rendered letter by
    # walking the raw values back into keys. We deliberately use the same
    # scrubber that the dispatcher will apply to stdout/stderr — it is
    # the project's single source of truth for raw-value masking.
    redacted_letter = scrub_stream(rendered, list(private_state.private_entries))

    # Commit the redacted letter as a fresh public artifact.
    letter_doc_id = _new_artifact_id("letter", parsed.opaque_id)

    # We pre-detect spans of every key that survived the scrub so the
    # manifest's ``entities`` list reflects the rendered letter's
    # placeholders. Use simple linear scan: keys are short and the
    # template body is bounded.
    spans: list[Span] = []
    for entry in private_state.private_entries:
        idx = 0
        while True:
            found = redacted_letter.find(entry.key, idx)
            if found == -1:
                break
            spans.append(
                Span(
                    start=found,
                    end=found + len(entry.key),
                    kind=entry.kind,
                )
            )
            idx = found + len(entry.key)

    # The pipeline expects the raw text to be the un-redacted form and
    # spans pointing into it. We pass the redacted letter (which the
    # caller supplied as a template that is already redacted) and an
    # empty spans list — the manifest will carry the same redacted text
    # without re-redacting. This keeps the artifact contents identical
    # to ``redacted_letter`` and avoids round-tripping through the
    # redactor (which would attempt to re-key already-redacted text).
    handle = process_document(
        doc_id=letter_doc_id,
        raw_text=redacted_letter,
        spans=[],
        vault_root=vault_root,
    )

    public = PublicHandle(
        locator=build_locator(
            exposure_class="agent_redacted",
            artifact_kind="manifest",
            opaque_id=handle.doc_id,
        )
    )
    return TemplateResult(
        artifact_handles=(public,),
        stdout=f"rendered letter with {len(key_to_raw)} substitution(s)",
        stderr="",
    )


# ---------------------------------------------------------------------------
# Registry (binding — the dispatcher's closed-set acceptance list)
# ---------------------------------------------------------------------------


TEMPLATES: dict[str, TemplateSpec] = {}
"""Module-level registry of every template the dispatcher will accept.

Keys are the canonical template names. Adding a template requires
appending a :class:`TemplateSpec` entry to this dict in the same module
that defines the template function, so the dispatcher and the registry
stay in lock-step.
"""

TEMPLATES["summarise_private_minutes"] = TemplateSpec(
    name="summarise_private_minutes",
    fn=_summarise_private_minutes,
    min_scope=ExecutionScope.PRIVATE_BOUNDARY,
    description=(
        "Reads a committed manifest and emits a redacted summary "
        "(snippet + key inventory) as a fresh public artifact."
    ),
)

TEMPLATES["generate_letter_from_private_template"] = TemplateSpec(
    name="generate_letter_from_private_template",
    fn=_generate_letter_from_private_template,
    min_scope=ExecutionScope.PRIVATE_BOUNDARY,
    description=(
        "Substitutes raw private values into a caller-supplied redacted "
        "template body, re-scrubs the result, and commits the redacted "
        "rendering as a fresh public artifact."
    ),
)


# Keep EntityKind in scope to avoid an unused-import lint flag without
# weakening the public surface; templates may need it in future revisions.
_ = EntityKind


__all__ = [
    "TEMPLATES",
    "TemplateFn",
    "TemplateResult",
    "TemplateSpec",
]
