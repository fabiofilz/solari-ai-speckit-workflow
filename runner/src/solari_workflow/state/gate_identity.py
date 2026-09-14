"""Authoritative Gate Identity and its immutable structured evidence
(third remediation round, M3).

The accepted T029 Fingerprint (`fingerprint/engine.py`) is left
completely unchanged. A Gate Identity COMPOSES it with the two further
identities a checkpoint must be bound to:

    Gate Identity = Fingerprint X
                  + canonical Spec Kit feature/tasks identity
                  + gated main/base identity

`run-codex-gate` captures the complete identity atomically for a gate
and writes it, together with the parsed outcome, as an immutable,
machine-readable JSON evidence file (`runs/audit.py`'s
`write_structured_evidence`, `O_EXCL`, never overwritten). `BlockState`
only CACHES a reference to it - the evidence filename and its SHA-256
digest - alongside its convenience copies (`last_gate`,
`gate_feature_identity`, `gated_main_oid`). `checkpoint` re-reads the
evidence, proves its digest, and requires every cached field to match
it exactly, so editing cached state fields alone can never turn a gate
for Feature A (or main A) into one for Feature B (or main B).

This is provenance validation, not recovery: a missing `.block-state.json`
still fails closed (A4 is unchanged).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from solari_workflow.fingerprint.engine import Fingerprint, fingerprint_from_dict, fingerprint_to_dict
from solari_workflow.runs import audit

GATE_IDENTITY_SCHEMA = "solari-gate-identity/1"
GATE_IDENTITY_EVIDENCE_KIND = "gate-identity"


class GateIdentityError(Exception):
    """The immutable gate evidence does not prove the cached gate identity."""


@dataclass(frozen=True)
class GateIdentity:
    fingerprint: Fingerprint
    feature_identity: str | None
    gated_main_oid: str


def identity_payload(identity: GateIdentity) -> dict:
    """The identity portion of the evidence (also embedded verbatim in the
    gate's prompt/result Markdown for human reviewers)."""
    return {
        "fingerprint": fingerprint_to_dict(identity.fingerprint),
        "feature_identity": identity.feature_identity,
        "gated_main_oid": identity.gated_main_oid,
    }


def write_gate_evidence(
    record: audit.RunRecordPaths, identity: GateIdentity, *, result: str, checkpoint_eligible: bool
) -> tuple[str, str]:
    payload = {
        "schema": GATE_IDENTITY_SCHEMA,
        "audit_stem": record.stem,
        "identity": identity_payload(identity),
        "result": result,
        "checkpoint_eligible": checkpoint_eligible,
    }
    return audit.write_structured_evidence(record, kind=GATE_IDENTITY_EVIDENCE_KIND, payload=payload)


def load_eligible_gate_identity(ai_runs_dir: Path, filename: str | None, sha256: str | None) -> GateIdentity:
    """Read the immutable evidence, prove its digest, and require it to
    record an eligible PASS. Raises :class:`GateIdentityError` otherwise."""
    if filename is None or sha256 is None:
        raise GateIdentityError("no immutable gate identity evidence is referenced by this block's state")
    try:
        data = audit.read_structured_evidence(ai_runs_dir, filename, sha256)
    except audit.StructuredEvidenceError as exc:
        raise GateIdentityError(str(exc)) from exc
    if data.get("schema") != GATE_IDENTITY_SCHEMA:
        raise GateIdentityError(f"gate evidence {filename!r} has unsupported schema {data.get('schema')!r}")
    if data.get("result") != "PASS" or data.get("checkpoint_eligible") is not True:
        raise GateIdentityError(f"gate evidence {filename!r} does not record a checkpoint-eligible PASS")
    identity = data.get("identity")
    try:
        feature_identity = identity["feature_identity"]
        gated_main_oid = identity["gated_main_oid"]
        fingerprint = fingerprint_from_dict(identity["fingerprint"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GateIdentityError(f"gate evidence {filename!r} has a malformed identity: {exc}") from exc
    if feature_identity is not None and not isinstance(feature_identity, str):
        raise GateIdentityError(f"gate evidence {filename!r} has a malformed feature_identity")
    if not isinstance(gated_main_oid, str):
        raise GateIdentityError(f"gate evidence {filename!r} has a malformed gated_main_oid")
    return GateIdentity(fingerprint=fingerprint, feature_identity=feature_identity, gated_main_oid=gated_main_oid)
