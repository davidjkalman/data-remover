"""Check that opt-out URLs still go where the catalog says they do.

Read-only: this fetches pages a consumer is invited to visit and submits
nothing. It exists because opt-out URLs rot constantly - a 404 you discover
three months into a removal campaign has cost you three months.

Deliberately stdlib-only. Against a site running bot protection no HTTP client
wins, so there is nothing to buy by adding a dependency.
"""

from __future__ import annotations

import gzip
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

# Identify honestly. A tool pretending to be Chrome is a tool whose author knew
# it was unwelcome; we are fetching public consumer pages and can say so.
USER_AGENT = (
    "dataremoval/0.1 (personal data-broker opt-out link checker; read-only; "
    "one request per opt-out page)"
)

MAX_REDIRECTS = 8
MAX_BODY = 200_000          # enough to classify, small enough to stay cheap

# Wording that means "this really is the opt-out page".
OPTOUT_HINTS = re.compile(
    r"opt[\s\-]?out|removal|remove (my|your) (info|record|listing|name)|suppress|"
    r"delete (my|your) (info|record|data)|privacy request|do not sell|"
    r"right to (delete|erasure)|data subject",
    re.I,
)

# Wording that means the server said 200 but meant 404.
NOTFOUND_HINTS = re.compile(
    r"page (not|cannot be) found|404 error|no longer available|"
    r"page you (are looking for|requested) (does not|doesn't) exist|"
    r"we can'?t find that page",
    re.I,
)

# Bot walls answer with a challenge instead of the page. Note what is NOT here:
# "captcha". Half the real opt-out pages carry a captcha widget - it is a hurdle
# on the correct page, not evidence of the wrong one. It gets detected
# separately and reported as a hurdle.
BOTWALL_HINTS = re.compile(
    r"cf-browser-verification|cf_chl|just a moment|checking your browser|"
    r"attention required|enable javascript and cookies|access denied|"
    r"request unsuccessful|incapsula|perimeterx|are you a robot|"
    r"unusual traffic|verify you are (a )?human",
    re.I,
)

# Worth telling the user about, but only ever an annotation.
CAPTCHA_HINTS = re.compile(
    r"recaptcha|hcaptcha|turnstile|g-recaptcha|captcha", re.I
)

STATUS_ORDER = ["ok", "moved", "soft404", "notfound", "botwall", "error", "skipped"]

# Failures that mean "ask again", not "this URL is dead". DNS in particular
# falls over under concurrency, and a resolver hiccup reported as a dead broker
# is how you delete a working entry from your own catalog.
TRANSIENT = re.compile(
    r"nodename nor servname|name or service not known|temporary failure in name"
    r"|timed out|timeout|connection reset|connection refused|broken pipe"
    r"|EOF occurred|handshake|try again",
    re.I,
)

EXPLAIN = {
    "ok":       "reachable and looks like an opt-out page",
    "moved":    "redirects elsewhere - catalog URL is stale",
    "soft404":  "returns 200 but the page reads as missing or unrelated",
    "notfound": "404/410 - the opt-out page is gone",
    "botwall":  "bot protection answered instead of the site; URL unproven",
    "error":    "network, TLS or server failure",
    "skipped":  "no URL in the catalog for this broker",
}


# Defaults sized for a 549-broker sweep that nobody has to apologise for:
# ~2 requests/second overall, never two at once to the same host.
DEFAULT_RATE = 2.0          # requests per second, globally
DEFAULT_HOST_GAP = 1.0      # seconds between requests to one host
MAX_RETRY_AFTER = 30.0      # obey Retry-After up to here, then just report it


class HostGate:
    """One in-flight request per host, with a minimum gap between them.

    Concurrency is fine across 549 different companies and rude against any one
    of them. This keeps the parallelism where it belongs.
    """

    def __init__(self, min_gap: float = DEFAULT_HOST_GAP) -> None:
        self.min_gap = min_gap
        self._guard = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}
        self._next_ok: Dict[str, float] = {}

    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    @contextmanager
    def hold(self, host: str):
        if not host:
            yield
            return
        with self._lock_for(host):
            wait = self._next_ok.get(host, 0.0) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                yield
            finally:
                self._next_ok[host] = time.monotonic() + self.min_gap

    def penalise(self, host: str, seconds: float) -> None:
        """Back off a host that asked us to (Retry-After)."""
        if host:
            self._next_ok[host] = max(
                self._next_ok.get(host, 0.0), time.monotonic() + seconds
            )


class RateLimit:
    """Global ceiling on requests per second, shared by every worker."""

    def __init__(self, per_second: float = DEFAULT_RATE) -> None:
        self.interval = 1.0 / per_second if per_second and per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if not self.interval:
            return
        with self._lock:
            start = max(time.monotonic(), self._next)
            self._next = start + self.interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def host_of(url: Optional[str]) -> str:
    if not url:
        return ""
    return (urlparse(url).netloc or "").lower()


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Retry-After is either seconds or an HTTP date. Only seconds is worth
    honouring here; a date far in the future is a refusal, not a delay."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone

        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


@dataclass
class Result:
    key: str
    name: str
    url: Optional[str]
    status: str
    http: Optional[int] = None
    final_url: Optional[str] = None
    note: str = ""
    retry_after: Optional[float] = None

    @property
    def moved(self) -> bool:
        return self.status == "moved"

    @property
    def verified(self) -> bool:
        return self.status == "ok"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects by hand so we can see the whole chain."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=ctx)
    )


def _read_body(resp) -> str:
    raw = resp.read(MAX_BODY)
    if resp.headers.get("Content-Encoding", "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass  # truncated gzip - classify on whatever decoded
    charset = "utf-8"
    ctype = resp.headers.get("Content-Type", "")
    if "charset=" in ctype:
        charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    return raw.decode(charset, errors="replace")


def _normalize(url: str) -> str:
    """Compare URLs without tripping over trailing slashes, case or http->https."""
    p = urlparse(url)
    path = (p.path or "/").rstrip("/") or "/"
    return f"{p.netloc.lower().lstrip('www.')}{path.lower()}"


def _same_page(a: str, b: str) -> bool:
    return _normalize(a) == _normalize(b)


def _is_bare_root(url: str) -> bool:
    return (urlparse(url).path or "/").rstrip("/") == ""


def check_url(
    url: str, timeout: float = 15.0
) -> Tuple[str, Optional[int], str, str]:
    """Return (status, http_code, final_url, note).

    Any Retry-After the server sends is appended to the note as
    `retry-after=<seconds>` for the caller to parse; see `_retry_after_of`.
    """
    opener = _opener()
    current = url
    chain = 0

    while True:
        req = urllib.request.Request(
            current,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                # No Accept-Encoding: let the server send plain text.
            },
            method="GET",
        )
        try:
            resp = opener.open(req, timeout=timeout)
            code = resp.getcode()
        except urllib.error.HTTPError as e:
            resp, code = e, e.code
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, socket.timeout):
                return "error", None, current, f"timeout after {timeout:g}s"
            if isinstance(reason, ssl.SSLError):
                return "error", None, current, f"TLS failure: {reason}"
            return "error", None, current, str(reason)
        except socket.timeout:
            return "error", None, current, f"timeout after {timeout:g}s"
        except Exception as e:  # malformed URL, weird scheme, decoding blowup
            return "error", None, current, f"{type(e).__name__}: {e}"

        if code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                return "error", code, current, f"{code} with no Location header"
            chain += 1
            if chain > MAX_REDIRECTS:
                return "error", code, current, f"redirect loop (>{MAX_REDIRECTS} hops)"
            current = urljoin(current, location)
            continue

        resp_headers = dict(getattr(resp, "headers", {}) or {})
        try:
            body = _read_body(resp)
        except Exception as e:
            body = ""
            note_read = f" (body unreadable: {type(e).__name__})"
        else:
            note_read = ""
        finally:
            try:
                resp.close()
            except Exception:
                pass

        # A refusal code is a refusal regardless of what the body says.
        if code in (401, 403, 405, 406, 429, 503):
            ra = parse_retry_after(resp_headers.get("Retry-After"))
            suffix = f" retry-after={ra:.0f}" if ra is not None else ""
            if code in (429, 503):
                return ("botwall", code, current,
                        f"HTTP {code}; rate limited or unavailable{suffix}")
            return "botwall", code, current, f"HTTP {code}; needs a real browser{note_read}"

        # 404 before body sniffing: a bot-flavoured 404 page is still a 404.
        if code in (404, 410):
            return "notfound", code, current, f"HTTP {code}{note_read}"

        if code >= 500:
            return "error", code, current, f"HTTP {code} server error{note_read}"

        if code != 200:
            return "error", code, current, f"unexpected HTTP {code}{note_read}"

        # 200. Decide whether it is really the page we asked for.
        found_optout = bool(OPTOUT_HINTS.search(body))

        # Opt-out wording beats a challenge match: a page that both explains the
        # opt-out and mentions a robot check is the page, with a hurdle on it.
        if not found_optout and BOTWALL_HINTS.search(body):
            return "botwall", code, current, f"HTTP 200 challenge page{note_read}"

        if NOTFOUND_HINTS.search(body) and not found_optout:
            return "soft404", code, current, "200 but the page says it does not exist"

        redirected = not _same_page(url, current)
        if redirected and _is_bare_root(current) and not _is_bare_root(url):
            hint = " (destination may still be the right portal - check by hand)" if found_optout else ""
            return "soft404", code, current, f"bounced to the site root - opt-out path is gone{hint}"

        if not found_optout:
            if not body.strip():
                return "soft404", code, current, "empty body - likely JS-rendered, check by hand"
            return "soft404", code, current, "no opt-out wording found on the page"

        extra = " (has captcha)" if CAPTCHA_HINTS.search(body) else ""
        if redirected:
            return "moved", code, current, f"redirects, but the destination is an opt-out page{extra}"

        return "ok", code, current, f"HTTP {code}{extra}{note_read}"


def check_broker(
    key: str,
    name: str,
    url: Optional[str],
    timeout: float = 15.0,
    retries: int = 1,
    backoff: float = 1.5,
) -> Result:
    if not url:
        return Result(key, name, None, "skipped", note="no opt-out URL in catalog")

    status, code, final, note = check_url(url, timeout=timeout)

    # Retry only network-level failures, and only serially - the whole point is
    # to step out of the contention that caused them.
    attempt = 0
    while status == "error" and attempt < retries and TRANSIENT.search(note or ""):
        attempt += 1
        time.sleep(backoff * attempt)
        status, code, final, note = check_url(url, timeout=timeout)
        if status != "error":
            note = f"{note} (recovered on retry {attempt})"

    if status == "error" and TRANSIENT.search(note or ""):
        note = f"{note} - transient; re-run `dr verify {key}` before believing it"

    return Result(
        key, name, url, status, http=code, final_url=final, note=note,
        retry_after=_retry_after_of(note),
    )


def _retry_after_of(note: str) -> Optional[float]:
    m = re.search(r"retry-after=(\d+(?:\.\d+)?)", note or "")
    return float(m.group(1)) if m else None


def check_many(
    brokers: List[Tuple[str, str, Optional[str]]],
    workers: int = 4,
    timeout: float = 15.0,
    retries: int = 1,
    rate: float = DEFAULT_RATE,
    host_gap: float = DEFAULT_HOST_GAP,
    on_result=None,
) -> List[Result]:
    """Check brokers concurrently but gently.

    Parallelism is spread across hosts, never stacked on one. Results are
    handed to `on_result` as they land so a caller can persist incrementally -
    a sweep of several hundred brokers will be interrupted, and work already
    done should survive it.

    On KeyboardInterrupt the outstanding checks are cancelled and whatever
    finished is returned, in catalog order.
    """
    gate = HostGate(host_gap)
    limiter = RateLimit(rate)
    results: List[Optional[Result]] = [None] * len(brokers)

    # Cooperative stop. future.cancel() cannot touch work a worker has already
    # picked up, and a queued item would otherwise still sit through its
    # rate-limit sleep before discovering the sweep was abandoned. Checking a
    # flag makes cancellation immediate and, more usefully, testable.
    stop = threading.Event()

    def one(index: int, item) -> Tuple[int, Optional[Result]]:
        if stop.is_set():
            return index, None
        key, name, url = item
        host = host_of(url)
        limiter.acquire()
        if stop.is_set():
            return index, None
        with gate.hold(host):
            if stop.is_set():
                return index, None
            res = check_broker(key, name, url, timeout=timeout, retries=retries)
        # A server that named a back-off gets it applied to the whole host.
        if res.retry_after:
            gate.penalise(host, min(res.retry_after, MAX_RETRY_AFTER))
        return index, res

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = {pool.submit(one, i, b): i for i, b in enumerate(brokers)}
    try:
        for fut in as_completed(futures):
            index, res = fut.result()
            if res is None:
                continue
            results[index] = res
            if on_result:
                on_result(res)
    except KeyboardInterrupt:
        stop.set()
        for f in futures:
            f.cancel()
        pool.shutdown(wait=False)
        return [r for r in results if r is not None]
    finally:
        stop.set()
        pool.shutdown(wait=False)

    return [r for r in results if r is not None]
