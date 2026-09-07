"""Atlas CompanyRegistry (Phase 1A.5).

Persistent, run-independent company identity + relationship operations built
on the existing StateStore (no second state store). Enforces deterministic,
idempotent registration and — critically — **merge safety**: two records are
only unified on strong evidence (same official domain, or an explicitly
registered alias). Similar names with different domains, or genuinely
ambiguous input, are never auto-merged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from atlas.company.identity import (
    canonical_company_name,
    company_identity_key,
    derive_company_id,
    derive_relationship_id,
    normalize_domain,
)
from atlas.company.models import (
    Company,
    CompanyObservation,
    CompanySourceRelationship,
    CompanyStatus,
    RelationshipState,
    SourceConfidence,
    SourceDiscoveryObservation,
)


@dataclass(frozen=True)
class CompanyMatch:
    """Result of resolving an observation to an existing company."""

    company_id: Optional[str]
    confidence: SourceConfidence
    ambiguous: bool
    reason: str

    @property
    def matched(self) -> bool:
        return self.company_id is not None and not self.ambiguous


class CompanyRegistry:
    def __init__(self, store):
        self.store = store

    # -- identity resolution (merge-safe) ----------------------------------
    def resolve(self, name: str, official_domain: Optional[str] = None) -> CompanyMatch:
        domain = normalize_domain(official_domain)
        key = company_identity_key(name)

        # 1. Official-domain identity is the strongest signal.
        if domain:
            row = self.store.find_company_by_domain(domain)
            if row is not None:
                return CompanyMatch(row["company_id"], SourceConfidence.CONFIRMED, False, "official-domain match")

        # 2. Explicit alias registration.
        if key:
            alias_company = self.store.find_company_id_by_alias_key(key)
            if alias_company is not None:
                return CompanyMatch(alias_company, SourceConfidence.STRONG, False, "explicit-alias match")

        # 3. Canonical identity key.
        rows = self.store.find_companies_by_identity_key(key) if key else []
        if not rows:
            return CompanyMatch(None, SourceConfidence.UNKNOWN, False, "no existing company")

        if domain:
            non_conflicting = [r for r in rows if not r["official_domain"]]
            conflicting = [r for r in rows if r["official_domain"] and normalize_domain(r["official_domain"]) != domain]
            same_domain = [r for r in rows if r["official_domain"] and normalize_domain(r["official_domain"]) == domain]
            if same_domain:
                return CompanyMatch(same_domain[0]["company_id"], SourceConfidence.CONFIRMED, False, "domain match")
            if non_conflicting:
                return CompanyMatch(non_conflicting[0]["company_id"], SourceConfidence.STRONG, False,
                                    "name match; existing had no domain")
            if conflicting:
                return CompanyMatch(None, SourceConfidence.TENTATIVE, True,
                                    "same name but a different official domain — not merged")
            return CompanyMatch(None, SourceConfidence.UNKNOWN, True, "ambiguous name/domain")

        # No domain supplied.
        if len(rows) == 1:
            return CompanyMatch(rows[0]["company_id"], SourceConfidence.STRONG, False, "unique name match")
        return CompanyMatch(None, SourceConfidence.TENTATIVE, True,
                            "multiple companies share this name — supply a domain to disambiguate")

    # -- registration ------------------------------------------------------
    def register_company(self, obs: CompanyObservation) -> Company:
        key = company_identity_key(obs.name)
        canonical = canonical_company_name(obs.name)
        domain = normalize_domain(obs.official_domain)
        match = self.resolve(obs.name, obs.official_domain)

        provenance = {"method": obs.method.value, "reason": obs.reason}
        if match.matched:
            target = match.company_id
            existing = self.store.get_company(target)
            # Update in place; never overwrite a known domain with None.
            self.store.upsert_company(
                target,
                canonical_name=existing["canonical_name"],
                identity_key=existing["identity_key"],
                display_name=existing["display_name"] or canonical,
                official_domain=domain,
                careers_url=obs.careers_url,
                country=obs.country,
                status=existing["status"],
            )
            # If the incoming name is a genuine variant, keep it as an alias.
            if key and key != existing["identity_key"]:
                self.add_alias(target, obs.name, source="discovery")
            company_id = target
        else:
            company_id = derive_company_id(key, obs.official_domain)
            if match.ambiguous:
                provenance["ambiguous"] = True
                provenance["ambiguity_reason"] = match.reason
            self.store.upsert_company(
                company_id,
                canonical_name=canonical,
                identity_key=key,
                display_name=canonical,
                official_domain=domain,
                careers_url=obs.careers_url,
                country=obs.country,
                provenance=provenance,
            )

        for alias in obs.aliases:
            self.add_alias(company_id, alias, source="explicit")

        return self.get_company(company_id)

    def add_alias(self, company_id: str, alias: str, *, source: str = "explicit") -> bool:
        from atlas.company.identity import derive_alias_id

        alias_key = company_identity_key(alias)
        if not alias_key:
            return False
        company = self.store.get_company(company_id)
        if company is not None and alias_key == company["identity_key"]:
            return False  # redundant with the canonical identity
        alias_id = derive_alias_id(company_id, alias_key)
        return self.store.add_company_alias(alias_id, company_id, alias, alias_key, source=source)

    def set_official_domain(self, company_id: str, domain: str) -> None:
        normalized = normalize_domain(domain)
        if normalized:
            self.store.set_company_domain(company_id, normalized)

    def update_verification(self, company_id: str, *, status: Optional[str] = None) -> None:
        self.store.update_company_verification(company_id, status=status)

    # -- relationships -----------------------------------------------------
    def attach_source_instance(
        self,
        company_id: str,
        instance_id: str,
        source_type_value: str,
        *,
        base_url: Optional[str] = None,
        tenant: Optional[str] = None,
        state: RelationshipState = RelationshipState.DISCOVERED,
        confidence: SourceConfidence = SourceConfidence.UNKNOWN,
        is_current: bool = True,
        provenance: Optional[dict] = None,
    ) -> CompanySourceRelationship:
        relationship_id = derive_relationship_id(company_id, instance_id)
        self.store.upsert_source_relationship(
            relationship_id, company_id, instance_id, source_type_value,
            base_url=base_url, tenant=tenant, state=state.value, confidence=confidence.value,
            is_current=is_current, provenance=provenance,
        )
        return self._row_to_relationship(self.store.get_source_relationship(relationship_id))

    def mark_relationship_state(
        self, relationship_id: str, state: RelationshipState, *, is_current: Optional[bool] = None
    ) -> None:
        self.store.set_relationship_state(relationship_id, state.value, is_current=is_current)

    def record_observation(self, obs: SourceDiscoveryObservation) -> bool:
        return self.store.add_source_discovery_observation(
            obs.observation_id, obs.company_id, obs.method.value,
            instance_id=obs.instance_id, input_url=obs.input_url, resolved_url=obs.resolved_url,
            detected_ats=obs.detected_ats, tenant=obs.tenant, confidence=obs.confidence.value,
            evidence_ref=obs.evidence_ref, verification_state=obs.verification_state.value,
            observed_at=obs.observed_at, detail=dict(obs.detail),
        )

    # -- reads -------------------------------------------------------------
    def get_company(self, company_id: str) -> Optional[Company]:
        row = self.store.get_company(company_id)
        return self._row_to_company(row) if row is not None else None

    def find_company(self, name: str, official_domain: Optional[str] = None) -> Optional[Company]:
        match = self.resolve(name, official_domain)
        return self.get_company(match.company_id) if match.matched else None

    def list_companies(self, limit: int = 1000) -> list[Company]:
        return [self._row_to_company(r) for r in self.store.list_companies(limit=limit)]

    def list_relationships(self, company_id: str) -> list[CompanySourceRelationship]:
        return [self._row_to_relationship(r) for r in self.store.list_relationships_for_company(company_id)]

    # -- row builders ------------------------------------------------------
    def _row_to_company(self, row) -> Company:
        aliases = tuple(a["alias"] for a in self.store.list_company_aliases(row["company_id"]))
        return Company(
            company_id=row["company_id"],
            canonical_name=row["canonical_name"],
            identity_key=row["identity_key"],
            display_name=row["display_name"],
            official_domain=row["official_domain"],
            careers_url=row["careers_url"],
            country=row["country"],
            status=CompanyStatus(row["status"]),
            discovered_at=row["discovered_at"],
            last_verified_at=row["last_verified_at"],
            provenance=json.loads(row["provenance_json"]),
            aliases=aliases,
        )

    @staticmethod
    def _row_to_relationship(row) -> CompanySourceRelationship:
        return CompanySourceRelationship(
            relationship_id=row["relationship_id"],
            company_id=row["company_id"],
            instance_id=row["instance_id"],
            source_type=row["source_type"],
            base_url=row["base_url"],
            tenant=row["tenant"],
            state=RelationshipState(row["state"]),
            confidence=SourceConfidence(row["confidence"]),
            is_current=bool(row["is_current"]),
            discovered_at=row["discovered_at"],
            updated_at=row["updated_at"],
            provenance=json.loads(row["provenance_json"]),
        )


__all__ = ["CompanyMatch", "CompanyRegistry"]
