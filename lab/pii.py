"""PII column detection, veto overrides, and Faker provider mapping (§1.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass


DEFAULT_PII_TOKENS: tuple[str, ...] = (
    "email",
    "phone",
    "mobile",
    "name",
    "address",
    "street",
    "dob",
    "birth",
    "ssn",
    "tax",
    "passport",
    "iban",
    "card",
)


@dataclass(frozen=True)
class PIIVetoOverride:
    """Audit record when a human operator overrides a PII column name veto (§1.1)."""

    column: str
    matched_pattern: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "column": self.column,
            "matched_pattern": self.matched_pattern,
            "reason": self.reason,
        }


class PIIMatcher:
    """Matches column names against PII patterns and manages veto overrides."""

    def __init__(
        self,
        extra_patterns: list[str] | None = None,
        raw_overrides: list[str] | None = None,
    ) -> None:
        tokens = list(DEFAULT_PII_TOKENS)
        if extra_patterns:
            for pat in extra_patterns:
                cleaned = pat.strip()
                if cleaned:
                    tokens.append(cleaned)
        self._tokens = tokens
        self._compiled = [(tok, re.compile(tok, re.IGNORECASE)) for tok in tokens]
        self._overrides_input = self._parse_overrides(raw_overrides or [])

    @staticmethod
    def _parse_overrides(raw_overrides: list[str]) -> dict[str, str]:
        overrides: dict[str, str] = {}
        for item in raw_overrides:
            if "=" not in item:
                raise ValueError(
                    f"Invalid --override-pii-veto {item!r}: expected format 'schema.table.col=<reason>'."
                )
            col_fqn, _, reason = item.partition("=")
            col_fqn = col_fqn.strip()
            reason = reason.strip()
            if not col_fqn or col_fqn.count(".") != 2:
                raise ValueError(
                    f"Invalid column reference in --override-pii-veto {item!r}: expected 'schema.table.column'."
                )
            if not reason:
                raise ValueError(
                    f"Refusing empty reason in --override-pii-veto for {col_fqn!r} (§1.1)."
                )
            overrides[col_fqn] = reason
        return overrides

    def match_column(self, col_name: str) -> str | None:
        """Return the first matching PII pattern token for col_name, or None."""
        for tok, regex in self._compiled:
            if regex.search(col_name):
                return tok
        return None

    def classify_pii_category(self, col_name: str) -> str | None:
        """Map a PII-matched column name to a semantic Faker provider category."""
        matched = self.match_column(col_name)
        if matched is None:
            return None
        lower = col_name.lower()
        if "email" in lower:
            return "email"
        if "phone" in lower or "mobile" in lower:
            return "phone"
        if "dob" in lower or "birth" in lower:
            return "dob"
        if "address" in lower or "street" in lower:
            return "address"
        if "ssn" in lower or "tax" in lower:
            return "ssn"
        if "passport" in lower:
            return "passport"
        if "iban" in lower:
            return "iban"
        if "card" in lower:
            return "card"
        if "name" in lower:
            if "slug" in lower or "code" in lower:
                return "slug"
            if "org" in lower or "company" in lower or "branch" in lower or "campus" in lower:
                return "company"
            return "name"
        return "generic"

    def is_veto_overridden(self, col_fqn: str) -> bool:
        """Return True if the operator provided a valid override for schema.table.col."""
        return col_fqn in self._overrides_input

    def get_override_reason(self, col_fqn: str) -> str | None:
        return self._overrides_input.get(col_fqn)

    def build_audit_overrides(self, all_columns_fqn: dict[str, str]) -> list[dict[str, str]]:
        """Validate overrides against actual columns and return audit records for profile.json.

        all_columns_fqn maps 'schema.table.col' -> 'col'.
        """
        records: list[dict[str, str]] = []
        for col_fqn, reason in sorted(self._overrides_input.items()):
            if col_fqn not in all_columns_fqn:
                raise ValueError(
                    f"--override-pii-veto references unknown column {col_fqn!r}."
                )
            col_name = all_columns_fqn[col_fqn]
            matched = self.match_column(col_name) or "custom"
            records.append(
                PIIVetoOverride(
                    column=col_fqn,
                    matched_pattern=matched,
                    reason=reason,
                ).to_dict()
            )
        return records
