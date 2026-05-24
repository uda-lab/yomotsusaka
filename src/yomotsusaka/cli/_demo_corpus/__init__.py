"""Frozen mirror of two canonical redaction-corpus fixtures, shipped as
package data so ``operational_smoke --demo-corpus`` works from an
installed wheel as well as a source checkout.

The bytes here MUST stay byte-identical to the corresponding files under
``tests/fixtures/redaction_corpus/``. A unit test
(``test_demo_corpus_shipped_files_match_test_fixtures``) enforces the
mirror so this package cannot silently drift.
"""
