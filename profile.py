"""The firm(s) the classifier scores tenders against.

Was a single hard-coded fake profile (one small UK cleaning company). Now each
firm lives in its own `profiles/<name>.yaml` file, so the same pipeline serves
multiple clients — the multi-tenant story promised in the README.

The default profile ("cleaning") is byte-equivalent to the old hard-coded
DEMO_CLEANING_FIRM, since data/golden-tenders.jsonl is labeled against it —
`eval.py` must keep passing unchanged.

The profile is rendered into the system prompt as the firm's capability sheet.
The classifier's job: does this firm qualify for the tender, given exactly these
capabilities and limits?
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

PROFILES_DIR = Path(__file__).parent / "profiles"
DEFAULT_PROFILE = "cleaning"


class FirmProfile(BaseModel):
    name: str
    sector: str
    region: str = Field(description="Where the firm can realistically deliver")
    annual_turnover: str = Field(description="Drives turnover-floor disqualification")
    staff_count: str
    certifications: list[str] = Field(description="Certs the firm HOLDS — anything else is a gap")
    security_clearance: bool = Field(
        description="SC/BPSS clearance — many gov contracts require it"
    )
    insurance: str = Field(description="Public liability cover the firm carries")
    notes: str = Field(default="", description="Any extra constraints worth telling the classifier")
    recipient_email: str = Field(description="Where the daily digest gets sent")

    def as_prompt_block(self) -> str:
        certs = ", ".join(self.certifications) if self.certifications else "none"
        clearance = "yes" if self.security_clearance else "no"
        return f"""FIRM PROFILE (score the tender against exactly this — do not assume capabilities not listed):
  Name: {self.name}
  Sector: {self.sector}
  Can deliver in: {self.region}
  Annual turnover: {self.annual_turnover}
  Staff: {self.staff_count}
  Certifications HELD: {certs}
  Security clearance: {clearance}
  Insurance carried: {self.insurance}
  Notes: {self.notes or "none"}"""


def list_profiles() -> list[str]:
    """Names of every profile in profiles/, sorted."""
    return sorted(p.stem for p in PROFILES_DIR.glob("*.yaml"))


def load_profile(name: str = DEFAULT_PROFILE) -> FirmProfile:
    path = PROFILES_DIR / f"{name}.yaml"
    if not path.exists():
        available = ", ".join(list_profiles()) or "(none found)"
        raise SystemExit(f"profile '{name}' not found at {path} — available: {available}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return FirmProfile(**data)
