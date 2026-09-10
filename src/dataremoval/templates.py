"""Request, follow-up and complaint text.

Everything here is deliberately short and procedural. Long, angry letters get
routed to a human who does nothing; a short letter that names a statute, a
deadline and a regulator gets routed to the compliance queue.
"""

from __future__ import annotations

import textwrap
from datetime import date
from typing import List, Optional

from .laws import Law
from .profile import Profile


WIDTH = 78


def _wrap(text: str) -> str:
    """Hard-wrap a paragraph. Plain-text email that relies on the client to wrap
    renders as one endless line in half of them."""
    return textwrap.fill(" ".join(text.split()), width=WIDTH)


def _urls_block(profile_urls: List[str]) -> str:
    if not profile_urls:
        return ""
    listed = "\n".join(f"  {u}" for u in profile_urls)
    return f"\nThe listings I am referring to:\n{listed}\n"


def subject(law: Law, broker_name: str) -> str:
    if law.key == "gdpr":
        return f"Article 17 erasure request - {broker_name}"
    if law.key == "ccpa":
        return "CCPA/CPRA request to delete and opt out of sale/sharing"
    if law.key == "none":
        return "Request to remove my personal information"
    return f"{law.name} request to delete personal data"


def deletion_request(
    profile: Profile,
    law: Law,
    broker_name: str,
    profile_urls: Optional[List[str]] = None,
    today: Optional[date] = None,
) -> str:
    today = today or date.today()
    urls = _urls_block(profile_urls or [])

    if law.key == "gdpr":
        ask = (
            "Under Article 17 I request erasure of all personal data you hold about me. "
            "Under Article 21 I object to any processing for direct marketing, which is "
            "unconditional and takes effect immediately. Under Article 15 I also request "
            "a copy of the data you hold and the recipients you have disclosed it to."
        )
        onward = (
            "Under Article 19 you must communicate this erasure to each recipient to whom "
            "the data was disclosed, and tell me who they are."
        )
    elif law.key == "ccpa":
        ask = (
            "Under Cal. Civ. Code §1798.105 I request deletion of all personal information "
            "you have collected about me. Under §1798.120 I direct you not to sell or share "
            "my personal information. Under §1798.110 I request disclosure of the categories "
            "of personal information collected and the categories of third parties to whom it "
            "was sold or disclosed."
        )
        onward = (
            "Under §1798.105(c)(1) you must direct your service providers and contractors to "
            "delete this information as well. If you are a registered data broker, this "
            "request also applies to your obligations under the DELETE Act (Cal. Civ. Code "
            "§1798.99.80 et seq.)."
        )
    elif law.key == "none":
        ask = (
            "I request that you delete all personal information you hold about me and remove "
            "my listings from your site and any site you syndicate to, in accordance with the "
            "removal process described in your own privacy policy."
        )
        onward = (
            "Please also direct any partner or downstream site that received this data from "
            "you to remove it."
        )
    else:
        ask = (
            f"Under {law.citation} I request deletion of all personal data you hold about me, "
            "and I opt out of the sale of my personal data and of any processing for targeted "
            "advertising or profiling."
        )
        onward = (
            "Please also direct your processors to delete this data and confirm that you have "
            "done so."
        )

    ack = ""
    if law.ack_days:
        ack = f"Please confirm receipt within {law.ack_days} days as the statute requires.\n"

    ask, onward = _wrap(ask), _wrap(onward)

    return f"""\
Date: {today.isoformat()}
To: {broker_name}
Subject: {subject(law, broker_name)}

I am a data subject writing on my own behalf. This is a verifiable request under
{law.name}.

{ask}
{urls}
Identifying information, provided solely so you can locate my records and for no
other purpose:

{profile.identity_block()}

{onward}

{ack}{_wrap(f'You have {law.deadline_text()} to respond. Please reply to this address with written confirmation of what was deleted and a reference number for this request.')}

Do not add the contact address used for this request to any marketing or
identity-verification database, and do not require me to create an account in
order to exercise a statutory right.

{profile.full_name}
{profile.contact_email}
"""


def followup(
    profile: Profile,
    law: Law,
    broker_name: str,
    sent_on: date,
    days_overdue: int,
    confirmation: Optional[str] = None,
    today: Optional[date] = None,
) -> str:
    today = today or date.today()
    ref = f"\nYour reference: {confirmation}\n" if confirmation else ""
    return f"""\
Date: {today.isoformat()}
To: {broker_name}
Subject: OVERDUE - {subject(law, broker_name)} (sent {sent_on.isoformat()})

{_wrap(f'I sent the request below on {sent_on.isoformat()}. The {law.deadline_days}-day period under {law.citation} expired {days_overdue} day(s) ago and I have had no adequate response.')}
{ref}
This is a final request before I file a complaint with the {law.regulator}.

{_wrap('Confirm in writing within 10 days: what personal data you held, what you deleted, what you retained and the specific exemption you rely on for it, and which third parties you disclosed it to.')}

Identifying information as previously provided:

{profile.identity_block()}

{profile.full_name}
{profile.contact_email}
"""


def complaint(
    profile: Profile,
    law: Law,
    broker_name: str,
    sent_on: date,
    days_overdue: int,
    history: Optional[List[str]] = None,
    today: Optional[date] = None,
) -> str:
    today = today or date.today()
    hist = "\n".join(f"  {h}" for h in (history or [])) or "  (see attached correspondence)"
    return f"""\
Complaint to: {law.regulator}
File at: {law.complaint_url}
Date: {today.isoformat()}

Respondent: {broker_name}
Complainant: {profile.full_name}, {profile.residency_state or 'n/a'}

Summary
-------
{_wrap(f'{broker_name} has failed to comply with a verifiable deletion request made under {law.citation}. The request was submitted on {sent_on.isoformat()}. The statutory response period of {law.deadline_days} days expired {days_overdue} day(s) ago.')}

Timeline
--------
{hist}

Requested relief
----------------
{_wrap(f'An order compelling deletion of my personal data, written confirmation of the categories deleted and the third parties to whom the data was disclosed, and whatever penalty the {law.regulator} deems appropriate for the failure to respond within the statutory period.')}

Contact: {profile.contact_email}
"""


def form_crib(profile: Profile, broker_name: str, law: Law) -> str:
    """What to paste into a web form. Same content, no letterhead."""
    reason = {
        "ccpa": "CCPA/CPRA deletion and do-not-sell request (Cal. Civ. Code 1798.105, 1798.120)",
        "gdpr": "GDPR Article 17 erasure request",
        "none": "Removal request per your published privacy policy",
    }.get(law.key, f"{law.name} deletion and opt-out request")

    return f"""\
--- paste into {broker_name}'s form ---

Name:            {profile.full_name}
Also known as:   {', '.join(profile.all_names()[1:]) or '(none)'}
Year of birth:   {profile.birth_year or '(withhold unless required)'}
Email:           {profile.contact_email}
Reason / notes:  {reason}

Addresses to match:
{chr(10).join('  ' + a.one_line() + (f'  [{a.span()}]' if a.span() else '') for a in profile.addresses) or '  (none on file)'}

--- before you submit ---
* Search the site for yourself first and collect EVERY listing URL. Most forms
  remove one listing, not one person.
* Give the minimum the form actually requires. If it demands a government ID or
  a full SSN, stop and use the email channel instead - mark this one `blocked`.
* Screenshot the confirmation screen. It is your evidence if you have to escalate.
"""
