"""Tender Fit Radar — classifier schema.

Same discipline as leadlens: forced reasoning BEFORE the verdict. The classifier
cannot output a bid/skip decision without first quoting evidence from the tender
text. This is the leadlens F-003 fix applied to tenders — it stops the model from
pattern-matching a verdict and back-filling a justification.

The schema IS the eval spec: every field exists because it's checkable against the
source tender, and `bid_recommendation` is the single label we score against the
golden set.
"""

from typing import Literal

from pydantic import BaseModel, Field


class TenderFit(BaseModel):
    # --- input echo (filled from the raw tender, not the model) ---
    id: str
    title: str
    buyer: str
    url: str

    # --- extracted facts (checkable against the tender doc) ---
    location: str | None = Field(
        default=None,
        description="Delivery location / region named in the tender, e.g. 'London Borough of Camden'. None if not stated.",
    )
    deadline: str | None = Field(
        default=None,
        description="Submission deadline as stated, ISO date if parseable else raw string. None if not found.",
    )
    estimated_value: str | None = Field(
        default=None,
        description="Contract value as stated, raw string e.g. '£120,000 over 3 years'. None if not stated.",
    )
    contract_length: str | None = Field(
        default=None,
        description="Contract duration as stated, e.g. '24 months + 12 optional'. None if not stated.",
    )
    required_certs: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Certifications/accreditations the tender requires: 'ISO 9001', 'ISO 14001', 'SafeContractor', 'CHAS', 'BS EN ...', etc. Empty list if none named.",
    )
    site_visit_required: bool | None = Field(
        default=None,
        description="True if a mandatory site visit / pre-bid meeting is stated, False if explicitly not, None if unclear.",
    )
    insurance_required: str | None = Field(
        default=None,
        description="Insurance requirement if stated, e.g. '£5m public liability'. None if not stated.",
    )

    # --- forced reasoning BEFORE the verdict (the F-003 fix) ---
    fit_reasoning: str = Field(
        min_length=100,
        description=(
            "Quote specific phrases from the tender as evidence. Weigh them against the "
            "firm profile: does the firm meet the certs, turnover, location, clearance? "
            "No bid_recommendation may be assigned without this reasoning."
        ),
    )

    # --- the single label scored against the golden set ---
    bid_recommendation: Literal["bid", "maybe", "skip"] = Field(
        description=(
            "bid = firm clearly qualifies and it's worth pursuing. "
            "skip = firm is disqualified (missing mandatory cert/clearance, out of region, "
            "turnover threshold too high, deadline already passed). "
            "maybe = qualifies but a gap needs checking (e.g. a cert the profile doesn't confirm)."
        ),
    )
    missing_requirements: list[str] = Field(
        default_factory=list,
        description="Specific requirements the firm does NOT currently meet, e.g. 'ISO 14001 not held', 'turnover below £500k floor'. Drives the 'maybe'/'skip' call.",
    )
    summary: str = Field(
        min_length=20,
        description="Plain 1-2 sentence summary an owner can read in 5 seconds: what it is, why bid or skip.",
    )
