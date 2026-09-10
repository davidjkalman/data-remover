"""Which statute to cite, and how long they have.

Citing the right law matters: it converts a courtesy request the broker may
ignore into one with a deadline and a regulator behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class Law:
    key: str
    name: str
    citation: str
    deadline_days: int
    extension_days: int
    ack_days: Optional[int]      # deadline to confirm receipt, if the statute has one
    regulator: str
    complaint_url: str

    def deadline_text(self) -> str:
        base = f"{self.deadline_days} days"
        if self.extension_days:
            base += f" (extendable once by {self.extension_days} days with notice)"
        return base


CCPA = Law(
    key="ccpa",
    name="CCPA/CPRA",
    citation="Cal. Civ. Code §1798.105 (deletion) and §1798.120 (opt-out of sale/sharing)",
    deadline_days=45,
    extension_days=45,
    ack_days=10,
    regulator="California Privacy Protection Agency",
    complaint_url="https://cppa.ca.gov/webapplications/complaint",
)

GDPR = Law(
    key="gdpr",
    name="GDPR",
    citation="Regulation (EU) 2016/679, Article 17 (erasure) and Article 21 (objection)",
    deadline_days=30,
    extension_days=60,
    ack_days=None,
    regulator="your national supervisory authority",
    complaint_url="https://edpb.europa.eu/about-edpb/board/members_en",
)

# The post-CCPA state laws are close enough to share a template; the citation
# and the regulator are what actually differ.
_STATE = {
    "VA": ("Virginia CDPA", "Va. Code §59.1-577", "Virginia Attorney General",
           "https://www.oag.state.va.us/consumer-protection/index.php/file-a-complaint"),
    "CO": ("Colorado Privacy Act", "Colo. Rev. Stat. §6-1-1306", "Colorado Attorney General",
           "https://coag.gov/file-complaint/"),
    "CT": ("Connecticut CTDPA", "Conn. Gen. Stat. §42-518", "Connecticut Attorney General",
           "https://portal.ct.gov/AG/Common/Complaint-Form-Landing-Page"),
    "UT": ("Utah UCPA", "Utah Code §13-61-201", "Utah Division of Consumer Protection",
           "https://dcp.utah.gov/complaints/"),
    "TX": ("Texas TDPSA", "Tex. Bus. & Com. Code §541.051", "Texas Attorney General",
           "https://www.texasattorneygeneral.gov/consumer-protection/file-consumer-complaint"),
    "OR": ("Oregon OCPA", "Or. Rev. Stat. §646A.578", "Oregon Department of Justice",
           "https://justice.oregon.gov/consumercomplaints/"),
    "MT": ("Montana MCDPA", "Mont. Code §30-14-2812", "Montana Department of Justice",
           "https://dojmt.gov/consumer/"),
    "DE": ("Delaware DPDPA", "Del. Code tit. 6 §12D-104", "Delaware Department of Justice",
           "https://attorneygeneral.delaware.gov/fraud/cpu/complaint/"),
    "NH": ("New Hampshire NHPA", "N.H. Rev. Stat. §507-H:4", "New Hampshire Attorney General",
           "https://www.doj.nh.gov/consumer/complaints/"),
    "NJ": ("New Jersey NJDPA", "N.J. Stat. §56:8-166.9", "New Jersey Division of Consumer Affairs",
           "https://www.njconsumeraffairs.gov/Pages/Consumer-Complaint.aspx"),
    "NE": ("Nebraska NDPA", "Neb. Rev. Stat. §87-1104", "Nebraska Attorney General",
           "https://protectthegoodlife.nebraska.gov/file-complaint"),
    "MN": ("Minnesota MCDPA", "Minn. Stat. §325O.05", "Minnesota Attorney General",
           "https://www.ag.state.mn.us/office/complaint.asp"),
    "MD": ("Maryland MODPA", "Md. Code, Com. Law §14-4705", "Maryland Attorney General",
           "https://www.marylandattorneygeneral.gov/Pages/CPD/Complaint.aspx"),
    "IA": ("Iowa ICDPA", "Iowa Code §715D.3", "Iowa Attorney General",
           "https://www.iowaattorneygeneral.gov/for-consumers/file-a-consumer-complaint"),
    "TN": ("Tennessee TIPA", "Tenn. Code §47-18-3303", "Tennessee Attorney General",
           "https://www.tn.gov/attorneygeneral/working-for-tennessee/consumer/file-a-complaint.html"),
    "IN": ("Indiana CDPA", "Ind. Code §24-15-3-1", "Indiana Attorney General",
           "https://www.in.gov/attorneygeneral/consumer-protection-division/file-a-complaint/"),
    "KY": ("Kentucky KCDPA", "Ky. Rev. Stat. §367.3613", "Kentucky Attorney General",
           "https://www.ag.ky.gov/Priorities/Protecting-Consumers/Pages/default.aspx"),
    "RI": ("Rhode Island RIDTPPA", "R.I. Gen. Laws §6-48.1-4", "Rhode Island Attorney General",
           "https://riag.ri.gov/consumer-protection/file-consumer-complaint"),
}

# No comprehensive statute: the request still works, it just has no deadline.
NONE = Law(
    key="none",
    name="no comprehensive state privacy statute",
    citation="the site's own published privacy policy and removal procedure",
    deadline_days=45,
    extension_days=0,
    ack_days=None,
    regulator="the Federal Trade Commission",
    complaint_url="https://reportfraud.ftc.gov/",
)


def for_state(state: str) -> Law:
    st = (state or "").strip().upper()
    if st == "CA":
        return CCPA
    if st in _STATE:
        name, citation, regulator, url = _STATE[st]
        return Law(
            key="state",
            name=name,
            citation=citation,
            deadline_days=45,
            extension_days=45,
            ack_days=None,
            regulator=regulator,
            complaint_url=url,
        )
    return NONE


def choose(residency_state: str, is_eu_resident: bool) -> Law:
    """EU wins on strength when someone is covered by both."""
    if is_eu_resident:
        return GDPR
    return for_state(residency_state)


ALL: Dict[str, Law] = {"ccpa": CCPA, "gdpr": GDPR, "none": NONE}
