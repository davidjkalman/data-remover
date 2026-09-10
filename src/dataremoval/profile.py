"""The identity you are trying to suppress.

Brokers match on name + address history + relatives, so incomplete history is the
single biggest reason a removal 'fails' - the listing keyed to an old address
simply is not the one you asked about.

This file holds PII. It lives outside the DB so it is easy to keep out of git and
easy to shred.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml

TEMPLATE = """\
# Your identity as brokers see it. Be exhaustive: every variant is a separate
# listing, and a listing you don't name is a listing that survives.

full_name: Jane Q. Public
name_variants:          # maiden names, middle-initial forms, misspellings brokers use
  - Jane Public
  - J. Q. Public
birth_year: 1980        # brokers publish age; year alone is enough to disambiguate

emails:
  - you@example.com
phones:
  - "+1 555 555 0100"

# Most recent first. Include anything from the last ~15 years - brokers index
# deep, and old addresses are how they re-link you.
addresses:
  - line1: 123 Main St
    line2: Apt 4
    city: Springfield
    state: CA
    postal: "90210"
    from_year: 2019
    to_year: null       # null = current

# Named relatives make listings findable and are often the field that identifies
# the right profile. You are not requesting anything on their behalf.
relatives:
  - Jane Public Sr.

# Drives which statute you cite. CA gets the strongest tools.
residency_state: CA
is_eu_resident: false

# Use a dedicated address for these requests. Brokers will add whatever you give
# them to your record, and you do not want that to be your primary mailbox.
contact_email: privacy-requests@example.com
"""


@dataclass
class Address:
    line1: str = ""
    line2: str = ""
    city: str = ""
    state: str = ""
    postal: str = ""
    from_year: Any = None
    to_year: Any = None

    def one_line(self) -> str:
        parts = [self.line1, self.line2, f"{self.city}, {self.state} {self.postal}".strip()]
        return ", ".join(p for p in parts if p and p.strip())

    def span(self) -> str:
        if self.from_year and not self.to_year:
            return f"{self.from_year}-present"
        if self.from_year and self.to_year:
            return f"{self.from_year}-{self.to_year}"
        return ""


@dataclass
class Profile:
    full_name: str = ""
    name_variants: List[str] = field(default_factory=list)
    birth_year: Any = None
    emails: List[str] = field(default_factory=list)
    phones: List[str] = field(default_factory=list)
    addresses: List[Address] = field(default_factory=list)
    relatives: List[str] = field(default_factory=list)
    residency_state: str = ""
    is_eu_resident: bool = False
    contact_email: str = ""

    @classmethod
    def load(cls, path: Path) -> "Profile":
        if not path.exists():
            raise FileNotFoundError(
                f"No profile at {path}. Run `dr init` to write a starter template."
            )
        raw: Dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        addrs = [Address(**a) for a in (raw.pop("addresses", None) or [])]
        known = {f for f in cls.__dataclass_fields__ if f != "addresses"}
        return cls(addresses=addrs, **{k: v for k, v in raw.items() if k in known})

    def is_placeholder(self) -> bool:
        return "example.com" in (self.contact_email or "") or self.full_name == "Jane Q. Public"

    def all_names(self) -> List[str]:
        out = [self.full_name] + list(self.name_variants)
        seen, uniq = set(), []
        for n in out:
            if n and n not in seen:
                seen.add(n)
                uniq.append(n)
        return uniq

    def identity_block(self) -> str:
        """The block every broker asks for, formatted once."""
        lines = [f"Full name: {self.full_name}"]
        if len(self.all_names()) > 1:
            lines.append("Also listed as: " + "; ".join(self.all_names()[1:]))
        if self.birth_year:
            lines.append(f"Year of birth: {self.birth_year}")
        if self.addresses:
            lines.append("Addresses (current and former):")
            for a in self.addresses:
                span = a.span()
                lines.append(f"  - {a.one_line()}" + (f"  [{span}]" if span else ""))
        if self.emails:
            lines.append("Email addresses: " + ", ".join(self.emails))
        if self.phones:
            lines.append("Phone numbers: " + ", ".join(self.phones))
        if self.relatives:
            lines.append("Associated names appearing in listings: " + ", ".join(self.relatives))
        return "\n".join(lines)

    def warnings(self) -> List[str]:
        w = []
        if not self.full_name:
            w.append("full_name is empty - nothing to match on.")
        if not self.addresses:
            w.append("No addresses. Brokers key on address history; expect partial removals.")
        elif len(self.addresses) < 2:
            w.append("Only one address. Add prior addresses or old listings will survive.")
        if not self.contact_email:
            w.append("No contact_email - you need one to receive verification links.")
        elif self.contact_email in self.emails:
            w.append(
                "contact_email is also listed in emails: you will be handing brokers an "
                "address they can index. Use a dedicated one."
            )
        if not self.residency_state and not self.is_eu_resident:
            w.append("No residency set - requests will fall back to a no-statute template.")
        return w


def write_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE)


def summary(p: Profile) -> str:
    return textwrap.dedent(
        f"""\
        Name        {p.full_name}  ({len(p.all_names())} variant(s))
        Residency   {p.residency_state or '-'}{'  + EU' if p.is_eu_resident else ''}
        Addresses   {len(p.addresses)}
        Emails      {len(p.emails)}   Phones {len(p.phones)}
        Contact     {p.contact_email or '-'}"""
    )
