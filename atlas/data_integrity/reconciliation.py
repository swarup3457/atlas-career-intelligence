"""Reconciliation: fold ingestion records into the canonical SQLite store.

Given validated, identity-resolved records, reconciliation computes the
diff against the durable canonical layer (Phase 0.9 tables) and applies it
**atomically and idempotently**:

* new identity            -> canonical job created (status ACTIVE/CLOSED)
* seen-again identity      -> observation appended, canonical ``last_seen`` advanced
* closed observation       -> status transition ACTIVE -> CLOSED (+ history)
* reposted (closed->active)-> status transition CLOSED -> ACTIVE (+ history)
* quarantined disposition  -> written to ``quarantine``, never canonicalized

Idempotency is structural: canonical rows are keyed by identity and guarded
by a content hash; observations and history rows use deterministic IDs, so
re-running the exact same reconciliation is a no-op (every record reports
``unchanged``). All writes happen inside one :meth:`StateStore.transaction`
so a mid-way failure rolls the whole batch back.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from atlas.data_integrity.identity import IdentityResolution
from atlas.data_integrity.records import IngestionRecord
from atlas.data_integrity.statuses import CanonicalStatus, apply_transition
from atlas.data_integrity.validation import Action, ValidationReport
from atlas.persistence.sqlite import StateStore

# Fields that define a job's canonical *content* (identity-stable). Changing
# any of these is a genuine update; changing anything else (timestamps,
# freshness, source) is just a new observation.
_CONTENT_FIELDS = (
    "company",
    "job_id",
    "role",
    "location",
    "work_mode",
    "experience",
    "company_type",
    "match_score",
)


def _hash(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(repr(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def _content_hash(record: IngestionRecord) -> str:
    payload = {f: record.value(f) for f in _CONTENT_FIELDS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _observed_status(record: IngestionRecord) -> CanonicalStatus:
    # Closed_or_Rejected sheet, or an explicit closed live-status, means CLOSED.
    sheet = record.provenance.sheet_name.lower()
    live = str(record.value("live_status") or "").casefold()
    if "closed" in sheet or "rejected" in sheet:
        return CanonicalStatus.CLOSED
    if live in ("closed", "expired", "filled"):
        return CanonicalStatus.CLOSED
    if live == "" and not record.has("live_status"):
        return CanonicalStatus.ACTIVE
    return CanonicalStatus.ACTIVE


@dataclass
class RecordDisposition:
    record_id: str
    identity_key: Optional[str]
    outcome: str  # created | updated | unchanged | closed | reposted | quarantined | skipped
    status: Optional[str] = None
    observation_added: bool = False


@dataclass
class ReconciliationResult:
    di_run_id: str
    source_file: str
    record_count: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    closed: int = 0
    reposted: int = 0
    quarantined: int = 0
    skipped: int = 0
    observations_added: int = 0
    status_changes: int = 0
    dispositions: list[RecordDisposition] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "di_run_id": self.di_run_id,
            "source_file": self.source_file,
            "record_count": self.record_count,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "closed": self.closed,
            "reposted": self.reposted,
            "quarantined": self.quarantined,
            "skipped": self.skipped,
            "observations_added": self.observations_added,
            "status_changes": self.status_changes,
        }


class Reconciler:
    """Applies a validated, identity-resolved batch to the canonical store."""

    def __init__(self, store: StateStore, entity_types: tuple[str, ...] = ("job",)):
        self.store = store
        self.entity_types = entity_types

    def reconcile(
        self,
        di_run_id: str,
        source_file: str,
        records: list[IngestionRecord],
        validation: ValidationReport,
        identity: IdentityResolution,
        *,
        source_hash: Optional[str] = None,
        mapping_version: str = "1",
    ) -> ReconciliationResult:
        result = ReconciliationResult(di_run_id=di_run_id, source_file=source_file)
        quarantined_ids = set(validation.quarantined) | set(validation.rejected)

        target = [r for r in records if r.entity_type in self.entity_types]
        result.record_count = len(target)

        with self.store.transaction():
            self.store.start_data_integrity_run(
                di_run_id, source_file, source_hash=source_hash, mapping_version=mapping_version
            )
            # Quarantined records first — they never reach canonical.
            for rec in records:
                if rec.record_id in quarantined_ids:
                    action = validation.worst_action(rec.record_id)
                    reason = self._primary_reason(rec.record_id, validation)
                    self.store.add_quarantine(
                        quarantine_id=_hash("q", di_run_id, rec.record_id)[:24],
                        record_id=rec.record_id,
                        reason_code=reason,
                        entity_type=rec.entity_type,
                        identity_key=rec.identity_key,
                        severity="ERROR" if action == Action.QUARANTINE else "CRITICAL",
                        action=action.name,
                        detail={"identity_key": rec.identity_key},
                    )
                    result.quarantined += 1
                    if rec.entity_type in self.entity_types:
                        result.dispositions.append(
                            RecordDisposition(rec.record_id, rec.identity_key, "quarantined")
                        )

            # Group by identity so a batch containing several rows for the
            # same job is deterministic and idempotent: one primary record
            # drives canonical content/status; the rest only add observations.
            groups: dict[str, list[IngestionRecord]] = {}
            loose: list[IngestionRecord] = []
            for rec in target:
                if rec.record_id in quarantined_ids:
                    continue
                if rec.identity_key is None:
                    loose.append(rec)
                else:
                    groups.setdefault(rec.identity_key, []).append(rec)

            for key in sorted(groups):
                members = sorted(groups[key], key=lambda r: r.record_id)
                primary = members[0]
                # Closure is authoritative within a batch: if ANY member of
                # the identity cluster observes CLOSED (e.g. a Closed_or_Rejected
                # row contradicting an Active All_Jobs row), the canonical job
                # is closed. This is deterministic and idempotent.
                group_status = (
                    CanonicalStatus.CLOSED
                    if any(_observed_status(m) == CanonicalStatus.CLOSED for m in members)
                    else None
                )
                result.dispositions.append(
                    self._reconcile_one(
                        di_run_id, primary, result, is_primary=True,
                        observed_override=group_status,
                    )
                )
                for secondary in members[1:]:
                    result.dispositions.append(
                        self._reconcile_one(di_run_id, secondary, result, is_primary=False)
                    )

            for rec in loose:
                result.skipped += 1
                result.dispositions.append(
                    RecordDisposition(rec.record_id, None, "skipped")
                )

            summary = result.summary()
            self.store.complete_data_integrity_run(di_run_id, summary)

        return result

    # ------------------------------------------------------------------
    def _reconcile_one(
        self,
        di_run_id: str,
        rec: IngestionRecord,
        result: ReconciliationResult,
        is_primary: bool = True,
        observed_override: Optional[CanonicalStatus] = None,
    ) -> RecordDisposition:
        canonical_id = rec.identity_key
        if canonical_id is None:
            result.skipped += 1
            return RecordDisposition(rec.record_id, None, "skipped")

        chash = _content_hash(rec)
        observed = observed_override or _observed_status(rec)

        # Secondary members of an identity cluster contribute observations
        # only — they must never overwrite canonical content, otherwise
        # conflicting duplicates would flip-flop and break idempotency.
        if not is_primary:
            existing = self.store.get_canonical_job(canonical_id)
            obs_id = _hash("obs", canonical_id, rec.record_id, chash, observed.value)[:24]
            added = self.store.add_observation(
                obs_id,
                canonical_id,
                rec.record_id,
                source_file=rec.provenance.source_file,
                sheet_name=rec.provenance.sheet_name,
                discovery_source=rec.value("discovery_source") or rec.value("source"),
                observed_status=observed.value,
                observed_at=rec.value("last_verified") or rec.value("first_seen"),
                content_hash=chash,
                provenance=rec.provenance.to_dict(),
            )
            if added:
                result.observations_added += 1
            result.unchanged += 1
            return RecordDisposition(
                rec.record_id, canonical_id, "observation",
                status=(existing["current_status"] if existing else observed.value),
                observation_added=added,
            )

        existing = self.store.get_canonical_job(canonical_id)
        prior_status = (
            CanonicalStatus(existing["current_status"]) if existing is not None else None
        )

        outcome = self.store.upsert_canonical_job(
            canonical_id,
            entity_type=rec.entity_type,
            company=rec.value("company"),
            job_id=rec.value("job_id"),
            role=rec.value("role"),
            location=rec.value("location"),
            status=observed.value if existing is None else existing["current_status"],
            first_seen=rec.value("first_seen"),
            last_seen=rec.value("last_verified") or rec.value("first_seen"),
            match_score=rec.value("match_score"),
            source_url=rec.value("source_url"),
            official_apply_url=rec.value("official_apply_url"),
            content_hash=chash,
            payload=rec.normalized_dict(),
        )
        if outcome == "created":
            result.created += 1
        elif outcome == "updated":
            result.updated += 1
        else:
            result.unchanged += 1

        # Observation (idempotent on deterministic id).
        obs_id = _hash("obs", canonical_id, rec.record_id, chash, observed.value)[:24]
        added = self.store.add_observation(
            obs_id,
            canonical_id,
            rec.record_id,
            source_file=rec.provenance.source_file,
            sheet_name=rec.provenance.sheet_name,
            discovery_source=rec.value("discovery_source") or rec.value("source"),
            observed_status=observed.value,
            observed_at=rec.value("last_verified") or rec.value("first_seen"),
            content_hash=chash,
            provenance=rec.provenance.to_dict(),
        )
        if added:
            result.observations_added += 1

        # Status transition handling (only when the observed state differs).
        final_status = observed
        disp_outcome = outcome
        if prior_status is None:
            # brand new canonical row: seed history with UNKNOWN -> observed
            if self._maybe_transition(
                di_run_id, canonical_id, CanonicalStatus.UNKNOWN, observed, "initial-observation"
            ):
                result.status_changes += 1
            if observed == CanonicalStatus.CLOSED:
                result.closed += 1
                disp_outcome = "closed"
        else:
            if observed != prior_status:
                if self._maybe_transition(
                    di_run_id, canonical_id, prior_status, observed,
                    self._transition_reason(prior_status, observed),
                ):
                    result.status_changes += 1
                self.store.set_canonical_status(canonical_id, observed.value)
                if prior_status == CanonicalStatus.CLOSED and observed == CanonicalStatus.ACTIVE:
                    result.reposted += 1
                    disp_outcome = "reposted"
                elif observed == CanonicalStatus.CLOSED:
                    result.closed += 1
                    disp_outcome = "closed"

        return RecordDisposition(
            rec.record_id, canonical_id, disp_outcome, status=final_status.value,
            observation_added=added,
        )

    def _maybe_transition(
        self,
        di_run_id: str,
        canonical_id: str,
        from_status: CanonicalStatus,
        to_status: CanonicalStatus,
        reason: str,
    ) -> bool:
        # Validate legality; illegal transitions are recorded as QUARANTINED
        # canonical status rather than crashing the batch.
        transition = apply_transition(
            canonical_id, from_status, to_status, reason, axis="canonical"
        )
        history_id = _hash(
            "hist", canonical_id, from_status.value, to_status.value, reason
        )[:24]
        return self.store.record_status_change(
            history_id,
            canonical_id,
            to_status.value,
            from_status=from_status.value,
            reason=reason,
            axis="canonical",
            context=transition.context,
        )

    @staticmethod
    def _transition_reason(prior: CanonicalStatus, observed: CanonicalStatus) -> str:
        if prior == CanonicalStatus.CLOSED and observed == CanonicalStatus.ACTIVE:
            return "repost-detected"
        if observed == CanonicalStatus.CLOSED:
            return "closure-observed"
        return f"{prior.value}->{observed.value}"

    @staticmethod
    def _primary_reason(record_id: str, validation: ValidationReport) -> str:
        codes = [f.code for f in validation.findings if f.record_id == record_id]
        # deterministic worst-first
        priority = {
            "FORMULA_INJECTION": 0,
            "OUT_OF_RANGE_HIGH": 1,
            "OUT_OF_RANGE_LOW": 1,
            "REQUIRED_MISSING": 2,
        }
        codes.sort(key=lambda c: (priority.get(c, 99), c))
        return codes[0] if codes else "QUARANTINED"
