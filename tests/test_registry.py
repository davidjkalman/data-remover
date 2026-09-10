"""Registry parsing. The data is authoritative about who brokers are and
rough about everything else, so the parser must be honest about which."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataremoval import catalog, db, registry as R

CSV = '''"Data Broker Name","Email Address","Website URL","Physical Address","How a consumer may opt out of sale or submit requests under the CCPA","How a protected individual can demand deletion of information posted online under Gov. Code sections 6208.1(b) or 6254.21(c)(1)","Additional information about data collecting practices","Date Added"
"Exponential Interactive, Inc. doing business as VDX.tv","privacy [at] VDX.tv","http://vdx.tv","2340 Powell Street, Emeryville, CA","Please contact us with any questions.","same","N/A","1/29/2020"
"Clean Co","dpo [at] cleanco.com","https://www.cleanco.com","1 Main St","Opt out at https://cleanco.com/opt-out","same","","2/1/2021"
"Policy First Co","p [at] pf.com","https://pf.com","2 Main St","See https://pf.com/privacy-policy for details, or use https://pf.com/do-not-sell to opt out.","same","","3/1/2022"
"Single Link Co","x [at] single.com","https://single.com","3 Main St","Visit https://single.com/privacy","same","","4/1/2023"
"No Site Co","y [at] nosite.com","","4 Main St","Email us.","same","","5/1/2024"
'''


def parse():
    return R.parse(CSV)


def test_emails_are_deobfuscated():
    assert R.deobfuscate_email("privacy [at] VDX.tv") == "privacy@vdx.tv"
    assert R.deobfuscate_email("a [at] b [dot] com") == "a@b.com"
    assert R.deobfuscate_email("A [AT] B.COM") == "a@b.com"
    assert R.deobfuscate_email("") is None
    assert R.deobfuscate_email("not an address") is None


def test_first_of_several_emails_is_taken():
    assert R.deobfuscate_email("one [at] x.com, two [at] y.com") == "one@x.com"


def test_optout_keyword_beats_the_first_url():
    """The first URL in these cells is usually the privacy policy the text was
    copied out of, not the opt-out."""
    url, basis = R.pick_optout_url(
        "See https://pf.com/privacy-policy or use https://pf.com/do-not-sell"
    )
    assert url == "https://pf.com/do-not-sell"
    assert "keyword" in basis


def test_single_url_is_reported_as_such():
    url, basis = R.pick_optout_url("Visit https://single.com/privacy")
    assert url == "https://single.com/privacy" and basis == "only url present"


def test_ambiguous_multi_url_is_flagged_unverified():
    url, basis = R.pick_optout_url("https://a.com/one and https://a.com/two")
    assert url == "https://a.com/one" and "unverified" in basis


def test_no_url_yields_none_not_a_guess():
    """A row with no URL must become an email-channel broker, not a fabricated
    link built from the website column."""
    assert R.pick_optout_url("Please contact us.") == (None, "no url in text")
    assert R.pick_optout_url("") == (None, "no text")


def test_trailing_punctuation_is_stripped():
    url, _ = R.pick_optout_url("go to https://x.com/opt-out.")
    assert url == "https://x.com/opt-out"


def test_registrable_domain():
    assert R.registrable_domain("http://vdx.tv") == "vdx.tv"
    assert R.registrable_domain("https://www.foo.com/x") == "foo.com"
    assert R.registrable_domain("https://sub.a.example.com") == "example.com"
    assert R.registrable_domain("https://foo.co.uk/x") == "foo.co.uk"
    assert R.registrable_domain("") == ""


def test_keys_come_from_domain_not_trading_name():
    entries = parse()
    vdx = entries[0]
    assert vdx.key == "vdx-tv", "a key built from 'X, Inc. doing business as Y' is unusable"
    assert vdx.email == "privacy@vdx.tv"


def test_duplicate_domains_get_distinct_keys():
    dup = CSV + '"Other Co","z [at] cleanco.com","https://cleanco.com","5 Main St","x","same","","6/1/2024"\n'
    keys = [e.key for e in R.parse(dup)]
    assert len(keys) == len(set(keys)), "distinct registrants collided into one key"


def test_method_follows_whether_a_url_was_found():
    by_key = {e.key: e for e in parse()}
    assert by_key["cleanco-com"].as_broker()["method"] == "form"
    assert by_key["vdx-tv"].as_broker()["method"] == "email"


def test_notes_record_provenance_and_confidence():
    b = {e.key: e for e in parse()}["pf-com"].as_broker()
    assert "CA data broker registry" in b["notes"]
    assert "keyword" in b["notes"]


def test_notes_say_so_when_there_is_no_url():
    b = {e.key: e for e in parse()}["vdx-tv"].as_broker()
    assert "email channel" in b["notes"]


def test_long_registry_text_is_truncated():
    long = CSV.replace("Email us.", "blah " * 300)
    b = [e for e in R.parse(long) if "No Site" in e.name][0].as_broker()
    assert len(b["notes"]) < R.MAX_NOTE + 400
    assert b["notes"].endswith('..."'), "long registry text should be elided"


def test_rows_without_a_domain_still_parse():
    """No website column means no domain to key on; fall back to the name
    rather than dropping a registered broker on the floor."""
    entries = parse()
    nosite = [e for e in entries if "No Site" in e.name][0]
    assert nosite.domain == ""
    assert nosite.key == "no-site-co"


def test_rejects_a_csv_that_is_not_the_registry():
    with pytest.raises(ValueError, match="does not look like"):
        R.parse("a,b,c\n1,2,3\n")


def test_imported_tier_is_configurable():
    e = parse()[1]
    assert e.as_broker(tier=1)["tier"] == 1
    assert e.as_broker()["tier"] == 3, "registry entries default low - they are mostly ad-tech"


def test_curated_catalog_gets_domains_for_dedupe(tmp_path):
    """Without this backfill the import cannot tell it already has Spokeo."""
    c = db.connect(tmp_path / "d.db")
    db.init(c)
    catalog.sync(c, catalog.load_yaml(catalog.bundled_path()))
    doms = {r["key"]: r["domain"] for r in c.execute("SELECT key, domain FROM broker")}
    assert doms["spokeo"] == "spokeo.com"
    assert doms["lexisnexis"] == "lexisnexis.com"
    assert all(v for v in doms.values()), "every curated broker needs a domain"
