"""
Validator — post-redaction quality checks.

Plugin boundary: real implementations can call Presidio, LLM Guard, or a
custom rule engine.  The MVP implementation here enforces the privacy
invariants required to make the vault boundary trustworthy:

1. No raw ``PrivateDictEntry.original_value`` substring leaks into the
   redacted manifest text.
2. Every entity placeholder and every private-dictionary key is actually
   present in the redacted text.
3. The set of entity keys equals the set of private-dictionary keys.
4. Every key matches the canonical ``<KIND_<8 hex>>`` placeholder shape.
5. Each entity's ``kind`` agrees with the prefix embedded in its key, and
   likewise for private-dictionary entries.
"""

from __future__ import annotations

import logging
import re

from yomotsusaka.schemas import DocumentManifest, EntityKind, PrivateDictEntry

logger = logging.getLogger(__name__)

# Mirrors ``redactor._make_key`` — ``<KIND_<8 hex>>`` where KIND is one of
# the EntityKind values.  Kept here as a local literal so validation does
# not import a private regex from the redactor module.
_PLACEHOLDER_PATTERN = re.compile(
    r"^<(PERSON|ORG|LOCATION|DATE|ID_NUMBER|FINANCIAL|HEALTH|CUSTOM)_[0-9a-f]{8}>$"
)


class ValidationError(Exception):
    """Raised when a manifest fails a validation rule."""


class Validator:
    """
    Checks that a :class:`~yomotsusaka.schemas.DocumentManifest` does not
    contain residual private data and that its placeholders are internally
    consistent with the supplied private dictionary.

    Override :meth:`validate` in a subclass to integrate Presidio, LLM
    Guard, or a custom rule engine.
    """

    def validate(
        self,
        manifest: DocumentManifest,
        private_dict: list[PrivateDictEntry],
    ) -> None:
        """
        Raise :class:`ValidationError` if *manifest* or *private_dict*
        violates any MVP privacy invariant.

        Parameters
        ----------
        manifest:
            Redacted manifest emitted by the pipeline.
        private_dict:
            Vault-side mapping from redacted key to original value.  Raw
            values are inspected here to verify they have been removed
            from ``manifest.redacted_text``; they MUST NOT be logged or
            re-emitted in any failure message.
        """
        redacted_text = manifest.redacted_text

        # 1. Raw-value leakage: any non-empty original_value appearing as
        #    a substring of the redacted text means redaction failed.
        for entry in private_dict:
            if entry.original_value and entry.original_value in redacted_text:
                # Do not include the raw value in the error message.
                raise ValidationError(
                    f"raw private value leaked into redacted text "
                    f"for key {entry.key!r}"
                )

        # 2a. Every entity placeholder must appear in the redacted text.
        for entity in manifest.entities:
            if entity.redacted_key not in redacted_text:
                raise ValidationError(
                    f"entity key {entity.redacted_key!r} is absent from "
                    "redacted_text"
                )

        # 2b. Every private-dictionary key must appear in the redacted text.
        for entry in private_dict:
            if entry.key not in redacted_text:
                raise ValidationError(
                    f"private_dict key {entry.key!r} is absent from "
                    "redacted_text"
                )

        # 3. Entity-key set must equal private-dictionary-key set.
        entity_keys = {e.redacted_key for e in manifest.entities}
        private_keys = {p.key for p in private_dict}
        if entity_keys != private_keys:
            missing_in_private = entity_keys - private_keys
            missing_in_entities = private_keys - entity_keys
            raise ValidationError(
                "entity keys and private_dict keys disagree: "
                f"in entities only={sorted(missing_in_private)!r}, "
                f"in private_dict only={sorted(missing_in_entities)!r}"
            )

        # 4 & 5. Per-key shape + kind/prefix agreement on both sides.
        for entity in manifest.entities:
            self._check_key_shape(entity.redacted_key, entity.kind, source="entity")
        for entry in private_dict:
            self._check_key_shape(entry.key, entry.kind, source="private_dict")

        logger.debug(
            "Validator: manifest %s passed all MVP checks (%d entities)",
            manifest.doc_id,
            len(manifest.entities),
        )

    @staticmethod
    def _check_key_shape(key: str, kind: EntityKind, *, source: str) -> None:
        match = _PLACEHOLDER_PATTERN.match(key)
        if match is None:
            raise ValidationError(
                f"{source} key {key!r} does not match the canonical "
                "<KIND_<8 hex>> placeholder shape"
            )
        prefix_kind = match.group(1)
        if prefix_kind != kind.value:
            raise ValidationError(
                f"{source} key {key!r} prefix kind {prefix_kind!r} "
                f"disagrees with declared EntityKind {kind.value!r}"
            )
