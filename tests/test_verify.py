"""Classification tests against a local server, so no real broker is touched."""

import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataremoval import verify

OPTOUT_PAGE = b"<html><body><h1>Opt Out</h1><p>Remove your listing here.</p></body></html>"
SOFT_404 = b"<html><body><h1>Page not found</h1></body></html>"
UNRELATED = b"<html><body><h1>Buy a background report today</h1></body></html>"
CHALLENGE = b"<html><body>Just a moment... checking your browser</body></html>"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = self.path
        if path == "/optout":
            self._send(200, OPTOUT_PAGE)
        elif path == "/gone":
            self._send(404, b"not here")
        elif path == "/soft":
            self._send(200, SOFT_404)
        elif path == "/unrelated":
            self._send(200, UNRELATED)
        elif path == "/empty":
            self._send(200, b"")
        elif path == "/moved":
            self._send(301, headers={"Location": "/optout"})
        elif path == "/to-root":
            self._send(302, headers={"Location": "/"})
        elif path == "/":
            self._send(200, UNRELATED)
        elif path == "/wall":
            self._send(403, CHALLENGE)
        elif path == "/wall200":
            self._send(200, CHALLENGE)
        elif path == "/boom":
            self._send(500, b"kaboom")
        elif path.startswith("/loop"):
            n = int(path[5:] or 0)
            self._send(302, headers={"Location": f"/loop{n + 1}"})
        else:
            self._send(404, b"nope")


@pytest.fixture(scope="module")
def base():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def status(base, path):
    return verify.check_url(base + path, timeout=5)[0]


def test_live_optout_page_is_ok(base):
    assert status(base, "/optout") == "ok"


def test_404_is_notfound(base):
    assert status(base, "/gone") == "notfound"


def test_200_saying_not_found_is_soft404(base):
    assert status(base, "/soft") == "soft404"


def test_page_without_optout_wording_is_soft404(base):
    """A broker that quietly replaced its opt-out page with a sales page should
    not read as a working opt-out."""
    assert status(base, "/unrelated") == "soft404"


def test_empty_body_is_soft404(base):
    assert status(base, "/empty") == "soft404"


def test_redirect_to_real_optout_is_moved_with_final_url(base):
    st, code, final, _ = verify.check_url(base + "/moved", timeout=5)
    assert st == "moved"
    assert final.endswith("/optout")


def test_bounce_to_site_root_is_soft404(base):
    """The classic dead opt-out: a 302 to the homepage."""
    assert status(base, "/to-root") == "soft404"


def test_403_challenge_is_botwall_not_broken(base):
    assert status(base, "/wall") == "botwall"


def test_200_challenge_body_is_botwall(base):
    assert status(base, "/wall200") == "botwall"


def test_5xx_is_error(base):
    assert status(base, "/boom") == "error"


def test_redirect_loop_terminates(base):
    st, _, _, note = verify.check_url(base + "/loop0", timeout=5)
    assert st == "error" and "redirect" in note


def test_unreachable_host_is_error():
    st, _, _, _ = verify.check_url("http://127.0.0.1:9/nothing", timeout=2)
    assert st == "error"


def test_missing_url_is_skipped():
    res = verify.check_broker("k", "Nameless", None)
    assert res.status == "skipped" and not res.verified


def test_only_ok_counts_as_verified(base):
    assert verify.check_broker("a", "A", base + "/optout").verified
    for path in ("/gone", "/soft", "/wall", "/moved"):
        assert not verify.check_broker("a", "A", base + path).verified


def test_check_many_preserves_order(base):
    items = [("a", "A", base + "/optout"), ("b", "B", base + "/gone"),
             ("c", "C", base + "/soft")]
    got = verify.check_many(items, workers=3, timeout=5, rate=0, host_gap=0)
    assert [r.key for r in got] == ["a", "b", "c"]
    assert [r.status for r in got] == ["ok", "notfound", "soft404"]


def test_url_normalization_ignores_cosmetic_differences():
    assert verify._same_page("https://x.com/optout", "https://www.x.com/optout/")
    assert verify._same_page("http://x.com/OptOut", "https://x.com/optout")
    assert not verify._same_page("https://x.com/optout", "https://x.com/privacy")


# --- regressions from the first real sweep -------------------------------

CAPTCHA_OPTOUT = (b"<html><body><h1>Opt out of our directory</h1>"
                  b"<div class='g-recaptcha'></div>Remove your listing.</body></html>")
BOT_404 = b"<html><body>404 error. Please enable JavaScript and cookies.</body></html>"


def test_real_optout_page_with_a_captcha_is_ok_not_botwall(base, monkeypatch):
    """Spokeo, Whitepages and OptOutPrescreen all serve a working opt-out page
    with a captcha on it. Calling those bot-walled hides the working URLs."""
    import dataremoval.verify as v

    class FakeResp:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        def read(self, n): return CAPTCHA_OPTOUT
        def getcode(self): return 200
        def close(self): pass

    monkeypatch.setattr(v, "_opener", lambda: type("O", (), {"open": lambda s, r, timeout: FakeResp()})())
    st, code, _, note = v.check_url("https://example.com/optout", timeout=5)
    assert st == "ok", f"got {st}: {note}"
    assert "captcha" in note, "the captcha should still be reported as a hurdle"


def test_404_with_a_bot_flavoured_body_is_notfound(base, monkeypatch):
    """Epsilon returned exactly this and got labelled 'HTTP 404; needs a real
    browser', which is nonsense - the status is authoritative."""
    import dataremoval.verify as v

    class FakeErr(Exception):
        code = 404
        headers = {"Content-Type": "text/html"}
        def read(self, n): return BOT_404
        def close(self): pass

    import urllib.error
    err = urllib.error.HTTPError("u", 404, "nf", {"Content-Type": "text/html"}, None)
    err.read = lambda n=None: BOT_404
    err.close = lambda: None
    monkeypatch.setattr(v, "_opener",
                        lambda: type("O", (), {"open": lambda s, r, timeout: (_ for _ in ()).throw(err)})())
    st, code, _, note = v.check_url("https://example.com/x", timeout=5)
    assert st == "notfound" and code == 404
    assert "browser" not in note


def test_transient_network_failure_is_retried_and_can_recover(monkeypatch):
    """A DNS blip under concurrency must not be reported as a dead broker."""
    import dataremoval.verify as v

    calls = []

    def flaky(url, timeout=15.0):
        calls.append(url)
        if len(calls) == 1:
            return "error", None, url, "[Errno 8] nodename nor servname provided"
        return "ok", 200, url, "HTTP 200"

    monkeypatch.setattr(v, "check_url", flaky)
    monkeypatch.setattr(v.time, "sleep", lambda s: None)
    res = v.check_broker("peekyou", "PeekYou", "https://peekyou.com/optout")
    assert res.status == "ok" and len(calls) == 2
    assert "recovered on retry" in res.note


def test_persistent_transient_error_is_labelled_as_unproven(monkeypatch):
    import dataremoval.verify as v

    monkeypatch.setattr(v, "check_url",
                        lambda url, timeout=15.0: ("error", None, url, "timed out"))
    monkeypatch.setattr(v.time, "sleep", lambda s: None)
    res = v.check_broker("x", "X", "https://x.example/optout")
    assert res.status == "error"
    assert "re-run" in res.note, "a transient failure must not read as a verdict"


def test_http_errors_are_not_retried(monkeypatch):
    """A 404 is an answer. Retrying it just doubles the load for no information."""
    import dataremoval.verify as v

    calls = []

    def once(url, timeout=15.0):
        calls.append(url)
        return "notfound", 404, url, "HTTP 404"

    monkeypatch.setattr(v, "check_url", once)
    res = v.check_broker("x", "X", "https://x.example/gone")
    assert res.status == "notfound" and len(calls) == 1
