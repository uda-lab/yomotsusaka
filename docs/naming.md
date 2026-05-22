# Yomotsusaka Naming Notes

## Purpose

This document records the naming concept for the Yomotsusaka project and its major architectural components.

The project uses motifs from the Yomotsu-Hirasaka episode in Japanese mythology. The naming is not intended as decorative theming. It is used to preserve the conceptual structure of the architecture: boundary, mediation, redaction, restoration, search, validation, and contained private execution.

## Project Name: Yomotsusaka

**Yomotsusaka** is derived from **Yomotsu-Hirasaka**, the slope or boundary between the world of the living and Yomi, the underworld, in the Izanagi and Izanami myth.

In this project, Yomotsusaka represents the boundary between:

- the raw private-data world; and
- the agent-facing operational world.

The name fits the project because the system does not simply delete or hide private information. Instead, it establishes a controlled passage between two domains. Raw private data remains on one side. Redacted manifests, labels, keys, summaries, and handles are exposed on the other side.

The architecture is therefore not a wall in the absolute sense. It is a governed boundary.

## Naming Principles

The names below are **conceptual codenames**, not mandatory implementation names. They are intended for documentation, issue titles, epics, milestones, and architecture discussions.

Source code should prefer explicit engineering names such as `search_gateway`, `restoration_api`, `execution_gateway`, `redactor`, and `validator`. Mythological names should not make the implementation harder to understand.

Component names should follow these principles:

- Prefer concepts from the Yomotsu-Hirasaka episode over widely used generic mythological names.
- Use names to clarify architectural responsibility, not merely to decorate modules.
- Avoid names that are already heavily overloaded in ordinary Japanese usage or common software naming.
- Keep names short enough for code, issue titles, and subsystem references.
- Preserve replaceability: names describe roles, not specific implementations.

## Component Names

### Ifuya — Redacted Search Gateway

**Ifuya** refers to the place-name tradition around **Ifuya / Iya**, associated with the Yomotsu-Hirasaka setting.

In the architecture, Ifuya is the redacted search gateway.

Role:

- accepts search queries from agents or users;
- resolves private query terms inside the private boundary when necessary;
- searches redacted manifests and indexes;
- returns redacted hits, handles, labels, and keys;
- does not expose raw private values by default.

Why the name fits:

Ifuya functions as the searchable entrance to the boundary zone. It helps locate relevant artifacts without crossing directly into raw private data.

### Kukuri — Restoration API Gateway

**Kukuri** evokes the idea of mediation, binding, and reconciliation. It is associated with the figure of Kukurihime in later mythological interpretation, who appears in contexts of mediation between separated parties.

In the architecture, Kukuri is the restoration API gateway.

Role:

- mediates access from keys to private values;
- allows controlled restoration when operationally necessary;
- logs requests, reasons, scopes, and callers;
- keeps the private dictionary inaccessible as a direct filesystem or database mount.

Why the name fits:

Kukuri connects separated domains without collapsing the boundary. It allows private data to be restored through an explicit interface rather than by ambient access.

### Chikaeshi — Private Execution Gateway

**Chikaeshi** refers to **Chigaeshi / Chikaeshi**, associated with the act of blocking or turning back the path at the Yomotsu-Hirasaka boundary, especially through the mythic boundary stone.

In the architecture, Chikaeshi is the private execution gateway.

Role:

- receives a private-data computation request;
- validates the job specification;
- runs the job in an isolated container or controlled execution environment;
- grants the job limited private-data access;
- returns only status, scrubbed logs, and artifact handles to the agent.

Why the name fits:

Private execution is allowed inside the boundary, but raw private results are not allowed to flow freely back into the agent context. Chikaeshi expresses this "turning back" of direct exposure.

### Chibiki — Redaction and Keying Boundary

**Chibiki** refers to **Chibiki-no-Iwa**, the huge stone placed at Yomotsu-Hirasaka to separate the two worlds.

In the architecture, Chibiki is the redaction and keying boundary.

Role:

- converts private spans into stable keys;
- creates redacted documents;
- emits public manifests;
- writes private dictionary entries;
- separates raw values from agent-facing representations.

Why the name fits:

Chibiki is the central separator. It is the component that makes the private/public split concrete.

### Kamuzumi — Validation and Leakage Guard

**Kamuzumi** refers to **Okamuzumi / Ōkamu-zumi**, the name given to the peaches used by Izanagi to drive away pursuers from Yomi in the myth.

In the architecture, Kamuzumi is the validation and leakage guard.

Role:

- checks public artifacts before commit;
- verifies JSON schema and placeholder consistency;
- performs lightweight leakage scans;
- flags suspicious output;
- prevents obviously unsafe artifacts from entering the agent-facing index.

Why the name fits:

Kamuzumi is a protective element at the boundary. It does not define the whole security model, but it helps repel obvious leakage before it reaches the public side.

## Suggested Conceptual Mapping

The initial codebase should prefer engineering names in source code. The mythological names are stable conceptual aliases used in documentation and planning.


Recommended public documentation mapping:

```text
Yomotsusaka : whole project
Ifuya       : redacted search gateway
Kukuri      : restoration API gateway
Chikaeshi   : private execution gateway
Chibiki     : redaction/keying boundary
Kamuzumi    : validation/leakage guard
```

Recommended initial Python module mapping:

```text
search_gateway.py      # conceptual alias: Ifuya
restoration_api.py     # conceptual alias: Kukuri
execution_gateway.py   # conceptual alias: Chikaeshi
redactor.py            # conceptual alias: Chibiki
validator.py           # conceptual alias: Kamuzumi
```

Recommended CLI surface:

```text
yomotsusaka search      # not: yomotsusaka ifuya
yomotsusaka restore     # not: yomotsusaka kukuri
yomotsusaka execute     # not: yomotsusaka chikaeshi
yomotsusaka redact      # not: yomotsusaka chibiki
yomotsusaka validate    # not: yomotsusaka kamuzumi
```

This keeps the source tree readable while preserving the conceptual names in documentation, issue titles, and architecture discussions.

## Naming Caution

The names should not be allowed to obscure the architecture. If a mythological name becomes confusing in implementation work, prefer the engineering role name in code and use the mythological name only in documentation or planning.

The project should remain understandable to contributors who do not know Japanese mythology.

## Summary

Yomotsusaka is the boundary system.

- Ifuya helps agents find redacted knowledge.
- Kukuri mediates restoration.
- Chikaeshi contains private execution.
- Chibiki performs the separation into keys and redacted artifacts.
- Kamuzumi validates and guards the public side.

Together, these names encode the central design idea: private data is not erased, but it is moved behind a controlled boundary where access becomes explicit, mediated, and inspectable.
