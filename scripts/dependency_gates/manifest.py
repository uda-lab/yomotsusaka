# ---------------------------------------------------------------------------
# VENDORED — do not edit locally.
# Source: t-uda/gate-keeper @ a5123016e4a337b69928c5b9729695d25b1450ab
#         scripts/dependency_gates/manifest.py
# Vendored for the gate-keeper manifest-coverage trial (uda-lab/yomotsusaka#158).
# The validator lives outside the installed gate-keeper package, so it is
# copied verbatim here. Re-sync from the source repo rather than editing.
# ---------------------------------------------------------------------------

"""Loader for the declarative dependency manifest.

Schema: docs/design/dependency-gates.md §3. The loader normalises the
optional ``pairs:`` sugar form into directed ``edges`` before any consumer
sees the result, so callers only ever see the graph form.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_VALID_MODES = frozenset({"affected_set", "stamped"})
_DEFAULT_MODE = "affected_set"

_NODE_REQUIRED = frozenset({"id", "path"})
_NODE_OPTIONAL = frozenset({"anchor", "kind"})
_EDGE_REQUIRED = frozenset({"from", "to", "relation"})
_EDGE_OPTIONAL = frozenset({"mode", "pair_id"})
_PAIR_REQUIRED = frozenset({"id", "source", "target", "relation"})
_PAIR_OPTIONAL = frozenset({"mode"})
_TOP_OPTIONAL = frozenset({"nodes", "edges", "pairs"})


class ManifestError(ValueError):
    """Raised when a manifest fails schema or referential validation."""


@dataclass(frozen=True)
class Node:
    id: str
    path: str
    anchor: str | None = None
    kind: str | None = None


@dataclass(frozen=True)
class Edge:
    from_id: str
    to_id: str
    relation: str
    mode: str = _DEFAULT_MODE
    pair_id: str | None = None


@dataclass(frozen=True)
class Manifest:
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    source_path: Path | None = None
    nodes_by_id: dict[str, Node] = field(default_factory=dict)

    def node(self, node_id: str) -> Node:
        return self.nodes_by_id[node_id]


def load_manifest(path: str | Path) -> Manifest:
    """Read and validate the YAML manifest at *path*."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"manifest {path}: cannot read: {exc}") from exc
    return _parse_manifest(raw, source_path=path)


def parse_manifest(text: str) -> Manifest:
    """Validate a manifest from in-memory YAML text (used by tests)."""
    return _parse_manifest(text, source_path=None)


def _parse_manifest(text: str, *, source_path: Path | None) -> Manifest:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"manifest YAML parse error: {exc}") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ManifestError(f"manifest top-level must be a mapping, got {type(data).__name__}")

    _check_keys(data, set(), _TOP_OPTIONAL, "manifest")

    # Distinguish "key absent / null" from "key present with a wrong type".
    # `data.get("nodes") or []` would silently treat e.g. ``nodes: {}`` as a
    # missing field; we want the type check inside `_parse_nodes` to fire.
    nodes = _parse_nodes(data["nodes"] if data.get("nodes") is not None else [])
    edges_raw = _parse_edges(data["edges"] if data.get("edges") is not None else [], nodes)
    pairs_raw = _parse_pairs(data["pairs"] if data.get("pairs") is not None else [], nodes)

    edges = (*edges_raw, *pairs_raw)
    nodes_by_id = {n.id: n for n in nodes}

    for edge in edges:
        if edge.from_id not in nodes_by_id:
            raise ManifestError(f"edge references unknown node id {edge.from_id!r} in 'from'")
        if edge.to_id not in nodes_by_id:
            raise ManifestError(f"edge references unknown node id {edge.to_id!r} in 'to'")

    return Manifest(
        nodes=tuple(nodes),
        edges=tuple(edges),
        source_path=source_path,
        nodes_by_id=nodes_by_id,
    )


def _parse_nodes(raw: Any) -> list[Node]:
    if not isinstance(raw, list):
        raise ManifestError("'nodes' must be a list")
    seen: set[str] = set()
    out: list[Node] = []
    for i, item in enumerate(raw):
        ctx = f"nodes[{i}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{ctx}: must be a mapping")
        _check_keys(item, _NODE_REQUIRED, _NODE_OPTIONAL, ctx)
        node_id = _expect_nonempty_str(item["id"], f"{ctx}.id")
        path = _expect_nonempty_str(item["path"], f"{ctx}.path")
        anchor = _expect_optional_str(item.get("anchor"), f"{ctx}.anchor")
        kind = _expect_optional_str(item.get("kind"), f"{ctx}.kind")
        if node_id in seen:
            raise ManifestError(f"{ctx}: duplicate node id {node_id!r}")
        seen.add(node_id)
        out.append(Node(id=node_id, path=path, anchor=anchor, kind=kind))
    return out


def _parse_edges(raw: Any, _nodes: list[Node]) -> list[Edge]:
    if not isinstance(raw, list):
        raise ManifestError("'edges' must be a list")
    out: list[Edge] = []
    for i, item in enumerate(raw):
        ctx = f"edges[{i}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{ctx}: must be a mapping")
        _check_keys(item, _EDGE_REQUIRED, _EDGE_OPTIONAL, ctx)
        from_id = _expect_nonempty_str(item["from"], f"{ctx}.from")
        to_id = _expect_nonempty_str(item["to"], f"{ctx}.to")
        relation = _expect_nonempty_str(item["relation"], f"{ctx}.relation")
        mode = _expect_mode(item.get("mode"), f"{ctx}.mode")
        pair_id = _expect_optional_str(item.get("pair_id"), f"{ctx}.pair_id")
        out.append(
            Edge(
                from_id=from_id,
                to_id=to_id,
                relation=relation,
                mode=mode,
                pair_id=pair_id,
            )
        )
    return out


def _parse_pairs(raw: Any, _nodes: list[Node]) -> list[Edge]:
    if not isinstance(raw, list):
        raise ManifestError("'pairs' must be a list")
    out: list[Edge] = []
    for i, item in enumerate(raw):
        ctx = f"pairs[{i}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{ctx}: must be a mapping")
        _check_keys(item, _PAIR_REQUIRED, _PAIR_OPTIONAL, ctx)
        pair_id = _expect_nonempty_str(item["id"], f"{ctx}.id")
        source = _expect_nonempty_str(item["source"], f"{ctx}.source")
        target = _expect_nonempty_str(item["target"], f"{ctx}.target")
        relation = _expect_nonempty_str(item["relation"], f"{ctx}.relation")
        mode = _expect_mode(item.get("mode"), f"{ctx}.mode")
        out.append(
            Edge(
                from_id=source,
                to_id=target,
                relation=relation,
                mode=mode,
                pair_id=pair_id,
            )
        )
        out.append(
            Edge(
                from_id=target,
                to_id=source,
                relation=relation,
                mode=mode,
                pair_id=pair_id,
            )
        )
    return out


def _check_keys(
    item: dict[str, Any],
    required: frozenset[str] | set[str],
    optional: frozenset[str] | set[str],
    ctx: str,
) -> None:
    keys = set(item)
    missing = set(required) - keys
    if missing:
        raise ManifestError(f"{ctx}: missing required fields {sorted(missing)}")
    unknown = keys - set(required) - set(optional)
    if unknown:
        raise ManifestError(f"{ctx}: unknown fields {sorted(unknown)}")


def _expect_nonempty_str(value: Any, ctx: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{ctx}: expected non-empty string, got {value!r}")
    return value


def _expect_optional_str(value: Any, ctx: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestError(f"{ctx}: expected string or null, got {value!r}")
    return value


def _expect_mode(value: Any, ctx: str) -> str:
    if value is None:
        return _DEFAULT_MODE
    if value not in _VALID_MODES:
        raise ManifestError(f"{ctx}: expected one of {sorted(_VALID_MODES)}, got {value!r}")
    return value
