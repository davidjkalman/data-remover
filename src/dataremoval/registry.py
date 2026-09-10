"""Import the California data broker registry.

Every data broker doing business in California must register with the CPPA
(AB 1202, extended by the DELETE Act). The state publishes the whole list as a
CSV, which makes catalog breadth a download rather than years of curation.

The data is authoritative about *who* the brokers are and rough about
everything else: emails are obfuscated, and the opt-out column is free text
that ranges from a clean URL to an entire privacy policy pasted into a cell.
Parsing is therefore best-effort and says so - a row we cannot get a URL from
becomes an email-channel broker rather than a guess.
"""

from __future__ import annotations

import csv
import io
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

REGISTRY_URL = (
    "https://cppa.ca.gov/data_broker_registry/complete-reg-data-brokers.csv"
)

# Column headings as the state publishes them.
COL_NAME = "Data Broker Name"
COL_EMAIL = "Email Address"
COL_SITE = "Website URL"
COL_ADDRESS = "Physical Address"
COL_OPTOUT = "How a consumer may opt out of sale or submit requests under the CCPA"
COL_MINOR = (
    "How a protected individual can demand deletion of information posted "
    "online under Gov. Code sections 6208.1(b) or 6254.21(c)(1)"
)
COL_DATE = "Date Added"

URL_RE = re.compile(r'https?://[^\s,;"\'<>)\]]+', re.I)

# A URL whose path talks about opting out beats the first URL in the cell,
# which is usually a link to the privacy policy the text was copied from.
OPTOUT_URL_HINT = re.compile(
    r"opt[-_]?out|donotsell|do-not-sell|privacy[-_]?(request|choice|center|portal)"
    r"|removal|remove|dsar|subject[-_]?request|preferences|unsubscribe",
    re.I,
)

MAX_NOTE = 400


def deobfuscate_email(raw: Optional[str]) -> Optional[str]:
    """`privacy [at] example.com` -> `privacy@example.com`.

    The registry obfuscates every address to deter scraping; we are a consumer
    exercising a statutory right, which is what the column is published for.
    """
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"\s*[\[\(]\s*at\s*[\]\)]\s*", "@", s, flags=re.I)
    s = re.sub(r"\s*[\[\(]\s*dot\s*[\]\)]\s*", ".", s, flags=re.I)
    s = re.sub(r"\s+at\s+", "@", s, flags=re.I) if "@" not in s else s
    s = s.strip().strip(".,;")
    # Multiple addresses in one cell: take the first usable one.
    for part in re.split(r"[;,\s]+", s):
        if "@" in part and "." in part.split("@")[-1]:
            return part.lower()
    return None


def pick_optout_url(text: Optional[str]) -> Tuple[Optional[str], str]:
    """Choose the best opt-out URL from a free-text cell.

    Returns (url, how) where `how` records the basis for the choice so the
    import can report its own confidence instead of implying certainty.
    """
    if not text:
        return None, "no text"
    urls = [u.rstrip(".,;)]'\"") for u in URL_RE.findall(text)]
    if not urls:
        return None, "no url in text"
    preferred = [u for u in urls if OPTOUT_URL_HINT.search(u)]
    if preferred:
        return preferred[0], "matched opt-out keyword"
    if len(urls) == 1:
        return urls[0], "only url present"
    return urls[0], f"first of {len(urls)} urls - unverified"


def registrable_domain(url: Optional[str]) -> str:
    """Coarse eTLD+1. Good enough to dedupe brokers; not a public-suffix list,
    so a few multi-part TLDs (`co.uk`) collapse to the wrong level. That costs
    us a duplicate, never a wrong removal."""
    if not url:
        return ""
    host = urlparse(url if "//" in url else "//" + url).netloc.lower()
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    # Handle the common two-part suffixes without pulling in a dependency.
    if parts[-2] in {"co", "com", "org", "net", "gov", "ac"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def slug_for(name: str, domain: str) -> str:
    """Prefer the domain: it is shorter, stabler and dedupes naturally.
    Company names in this registry run to 'X, Inc. doing business as Y'."""
    base = domain or name
    base = base.lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return base[:48] or "unknown"


@dataclass
class Entry:
    key: str
    name: str
    domain: str
    email: Optional[str]
    optout_url: Optional[str]
    url_basis: str
    address: str
    registered: str
    optout_text: str
    minor_text: str = ""
    notes: str = field(default="", init=False)

    def as_broker(self, tier: int = 3, recheck_days: int = 365) -> Dict[str, object]:
        bits = [f"CA data broker registry entry (registered {self.registered or 'unknown'})."]
        if self.url_basis and self.optout_url:
            bits.append(f"Opt-out URL {self.url_basis}.")
        elif not self.optout_url:
            bits.append("Registry gave no usable opt-out URL; use the email channel.")
        if self.address:
            bits.append(f"Registered address: {self.address}")
        if self.optout_text:
            txt = " ".join(self.optout_text.split())
            if len(txt) > MAX_NOTE:
                txt = txt[:MAX_NOTE].rstrip() + "..."
            bits.append(f'Registry says: "{txt}"')
        return {
            "key": self.key,
            "name": self.name,
            "tier": tier,
            "method": "form" if self.optout_url else "email",
            "optout_url": self.optout_url,
            "email": self.email,
            "requires": "",
            "recheck_days": recheck_days,
            "feeds": "",
            "notes": " ".join(bits),
        }


def parse(text: str) -> List[Entry]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or COL_NAME not in reader.fieldnames:
        raise ValueError(
            "This does not look like the CA registry CSV: expected a "
            f"'{COL_NAME}' column, got {reader.fieldnames}"
        )

    entries: List[Entry] = []
    used: Dict[str, int] = {}
    for row in reader:
        name = (row.get(COL_NAME) or "").strip()
        if not name:
            continue
        site = (row.get(COL_SITE) or "").strip()
        domain = registrable_domain(site)
        optout_text = (row.get(COL_OPTOUT) or "").strip()
        url, basis = pick_optout_url(optout_text)

        key = slug_for(name, domain)
        # Distinct registrants can share a domain; keep both rather than lose one.
        if key in used:
            used[key] += 1
            key = f"{key}-{used[key]}"
        else:
            used[key] = 1

        entries.append(
            Entry(
                key=key,
                name=name,
                domain=domain,
                email=deobfuscate_email(row.get(COL_EMAIL)),
                optout_url=url,
                url_basis=basis,
                address=" ".join((row.get(COL_ADDRESS) or "").split()),
                registered=(row.get(COL_DATE) or "").strip(),
                optout_text=optout_text,
                minor_text=(row.get(COL_MINOR) or "").strip(),
            )
        )
    return entries


def fetch(url: str = REGISTRY_URL, timeout: float = 60.0) -> str:
    from .verify import USER_AGENT

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    raw = urllib.request.urlopen(req, timeout=timeout).read()
    return raw.decode("utf-8-sig", errors="replace")


def load(path: Optional[Path] = None, url: str = REGISTRY_URL) -> List[Entry]:
    text = path.read_text(encoding="utf-8-sig", errors="replace") if path else fetch(url)
    return parse(text)
