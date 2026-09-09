"""Budgeted, cached, snapshotting HTTP session. The only way the agent talks to the network.

Every attempt (including redirect hops and retries) counts against the per-company and global
request budgets. Cache hits within a run are free. Raw bytes of every response are written to
``<run_dir>/snapshots/<sha256>.<ext>`` so every claim can point at an immutable snapshot.
"""
from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_UA = "signalpost-norway-agent/1.0 (+https://github.com/AnSa30-06/signalpost-norway; research crawler; contact via repo)"
EXT_FOR_KIND = {"html": "html", "json": "json", "xml": "xml", "text": "txt", "pdf": "pdf"}
PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home")
HOST_CONCURRENCY = {"data.brreg.no": 8, "arbeidsplassen.nav.no": 1}
HOST_MIN_INTERVAL = {"arbeidsplassen.nav.no": 1.5}   # seconds between request starts on that host
RATE_LIMIT_TRIP = 3                                   # consecutive 429s before a host is put on cooldown
RATE_LIMIT_COOLDOWN = 90.0                            # seconds; requests during cooldown are not sent (and not charged)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UnsafeURL(ValueError):
    pass


def assert_public_url(url: str) -> str:
    """Reject anything that is not a public http(s) URL. Resolves the host and rejects non-global IPs."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL(f"scheme not allowed: {parts.scheme!r}")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(PRIVATE_HOST_SUFFIXES):
        raise UnsafeURL(f"host not allowed: {host!r}")
    if parts.username or parts.password:
        raise UnsafeURL("credentials in URL are not allowed")
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise UnsafeURL(f"non-global IP literal: {host}")
        return url
    except ValueError:
        pass
    if not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host:
        raise UnsafeURL(f"malformed host: {host!r}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURL(f"dns failure for {host}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise UnsafeURL(f"{host} resolves to non-global address {ip}")
    return url


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: Optional[int] = None
    redirects: list = field(default_factory=list)
    body: bytes = b""
    text: str = ""
    sha256: str = ""
    retrieved_at: str = ""
    snapshot_path: str = ""
    error: Optional[str] = None
    blocked: bool = False
    budget_exhausted: bool = False
    cached: bool = False
    content_type: str = ""
    elapsed_ms: int = 0
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300 and self.error is None

    def json(self):
        if not self.ok:
            return None
        try:
            return json.loads(self.text or self.body.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    enc = m.group(1) if m else None
    if not enc:
        head = body[:4096].decode("ascii", "ignore")
        m = re.search(r"charset=[\"']?([\w\-]+)", head, re.I)
        enc = m.group(1) if m else "utf-8"
    try:
        return body.decode(enc, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


class Session:
    """Thread-safe budgeted HTTP session."""

    def __init__(self, run_dir: str | Path, max_total_requests: int = 1950, per_company_cap: int = 22,
                 timeout: float = 12.0, user_agent: str = DEFAULT_UA, host_concurrency: int = 4):
        self.run_dir = Path(run_dir)
        self.snap_dir = self.run_dir / "snapshots"
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        self.max_total = max_total_requests
        self.per_company_cap = per_company_cap
        self.timeout = timeout
        self.ua = user_agent
        self.host_concurrency = host_concurrency
        self._lock = threading.Lock()
        self._cache: dict[str, FetchResult] = {}
        self._robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._per_company: dict[str, int] = {}
        self._host_sem: dict[str, threading.Semaphore] = {}
        self._host_last: dict[str, float] = {}
        self._host_429: dict[str, int] = {}
        self._host_cool_until: dict[str, float] = {}
        self.total_requests = 0
        self.total_bytes = 0
        self.log: list[dict] = []

    # ---- budget -----------------------------------------------------------------------------------------
    def requests_used(self, company: str) -> int:
        return self._per_company.get(company, 0)

    def remaining(self, company: str) -> int:
        with self._lock:
            return min(self.per_company_cap - self._per_company.get(company, 0), self.max_total - self.total_requests)

    def _charge(self, company: str) -> bool:
        with self._lock:
            if self.total_requests >= self.max_total or self._per_company.get(company, 0) >= self.per_company_cap:
                return False
            self.total_requests += 1
            self._per_company[company] = self._per_company.get(company, 0) + 1
            return True

    def _pace(self, host: str) -> None:
        """Enforce the per-host minimum interval between request starts."""
        gap = HOST_MIN_INTERVAL.get(host)
        if not gap:
            return
        while True:
            with self._lock:
                last = self._host_last.get(host, 0.0)
                wait = last + gap - time.monotonic()
                if wait <= 0:
                    self._host_last[host] = time.monotonic()
                    return
            time.sleep(min(wait, gap))

    def _sem(self, host: str) -> threading.Semaphore:
        with self._lock:
            if host not in self._host_sem:
                self._host_sem[host] = threading.Semaphore(HOST_CONCURRENCY.get(host, self.host_concurrency))
            return self._host_sem[host]

    # ---- robots -----------------------------------------------------------------------------------------
    def robots_allowed(self, url: str, company: str) -> Optional[bool]:
        """True/False per robots.txt; None when robots could not be fetched (treated as allowed, recorded)."""
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            have = origin in self._robots
        if not have:
            r = self.get(origin + "/robots.txt", company=company, kind="text", robots=False, max_bytes=200_000)
            rp: Optional[urllib.robotparser.RobotFileParser] = None
            if r.ok:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
            elif r.status in (401, 403):
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(["User-agent: *", "Disallow: /"])
            with self._lock:
                self._robots[origin] = rp
        rp = self._robots.get(origin)
        if rp is None:
            return None
        try:
            return rp.can_fetch(self.ua, url) and rp.can_fetch("*", url)
        except Exception:
            return None

    def sitemaps_from_robots(self, origin: str) -> list[str]:
        rp = self._robots.get(origin)
        if rp is None:
            return []
        try:
            return list(rp.site_maps() or [])
        except Exception:
            return []

    # ---- fetch ------------------------------------------------------------------------------------------
    def get(self, url: str, company: str, kind: str = "html", max_bytes: int = 2_000_000,
            robots: bool = True, headers: Optional[dict] = None, max_redirects: int = 5) -> FetchResult:
        url = url.strip()
        with self._lock:
            cached = self._cache.get(url)
        if cached is not None:
            c = FetchResult(**{**cached.__dict__, "cached": True})
            return c
        res = FetchResult(url=url, final_url=url, retrieved_at=utc_now())
        try:
            assert_public_url(url)
        except UnsafeURL as exc:
            res.error = f"unsafe_url: {exc}"
            return self._finish(res, company)
        if robots:
            allowed = self.robots_allowed(url, company)
            if allowed is False:
                res.blocked = True
                res.error = "blocked_by_robots"
                return self._finish(res, company)
        current = url
        hops = 0
        started = time.monotonic()
        while True:
            host = urllib.parse.urlsplit(current).hostname or ""
            with self._lock:
                cool = self._host_cool_until.get(host, 0.0)
            if cool > time.monotonic():
                res.status = 429
                res.error = f"rate_limited_cooldown: {host} returned {RATE_LIMIT_TRIP} consecutive HTTP 429; not contacted for {int(cool - time.monotonic())}s"
                break
            if not self._charge(company):
                res.budget_exhausted = True
                res.error = "request_budget_exhausted"
                return self._finish(res, company)
            res.attempts += 1
            self._pace(host)
            req = urllib.request.Request(current, headers={
                "User-Agent": self.ua,
                "Accept": {"json": "application/json", "xml": "application/xml,text/xml;q=0.9,*/*;q=0.5",
                           "pdf": "application/pdf"}.get(kind, "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
                "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en;q=0.6",
                "Accept-Encoding": "gzip, deflate, identity",
                **(headers or {}),
            })
            opener = urllib.request.build_opener(_NoRedirect)
            status = None
            try:
                with self._sem(host):
                    with opener.open(req, timeout=self.timeout) as resp:
                        status = resp.status
                        res.content_type = resp.headers.get("Content-Type", "") or ""
                        body = resp.read(max_bytes + 1)
                        enc = (resp.headers.get("Content-Encoding") or "").lower()
                        if enc == "gzip":
                            body = gzip.decompress(body)
                        elif enc == "deflate":
                            body = zlib.decompress(body, -zlib.MAX_WBITS) if body[:2] != b"\x78\x9c" else zlib.decompress(body)
                res.status = status
                res.body = body[:max_bytes]
                res.final_url = current
                with self._lock:
                    self._host_429[host] = 0
                break
            except urllib.error.HTTPError as exc:
                status = exc.code
                res.status = status
                res.content_type = exc.headers.get("Content-Type", "") or ""
                if status in (301, 302, 303, 307, 308):
                    loc = exc.headers.get("Location")
                    if not loc or hops >= max_redirects:
                        res.error = "too_many_redirects" if loc else "redirect_without_location"
                        break
                    nxt = urllib.parse.urljoin(current, loc)
                    try:
                        assert_public_url(nxt)
                    except UnsafeURL as u:
                        res.error = f"unsafe_redirect: {u}"
                        break
                    res.redirects.append(nxt)
                    current = nxt
                    hops += 1
                    res.final_url = current
                    continue
                if status in (401, 403, 451):
                    res.blocked = True
                    res.error = f"http_{status}"
                    try:
                        res.body = exc.read(max_bytes)
                    except Exception:
                        pass
                    break
                if status == 429:
                    with self._lock:
                        self._host_429[host] = self._host_429.get(host, 0) + 1
                        tripped = self._host_429[host] >= RATE_LIMIT_TRIP
                        if tripped:
                            self._host_cool_until[host] = time.monotonic() + RATE_LIMIT_COOLDOWN
                    if not tripped and res.attempts < 3 and self.remaining(company) > 0:
                        time.sleep(3.0 * res.attempts)
                        continue
                    res.error = "http_429"
                    break
                if status in (500, 502, 503, 504) and res.attempts < 2 and self.remaining(company) > 0:
                    time.sleep(0.8)
                    continue
                try:
                    res.body = exc.read(max_bytes)
                except Exception:
                    pass
                res.error = f"http_{status}"
                break
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError, ValueError) as exc:
                msg = str(getattr(exc, "reason", exc))[:200]
                if res.attempts < 2 and self.remaining(company) > 0 and ("timed out" in msg.lower() or "reset" in msg.lower()):
                    time.sleep(0.5)
                    continue
                res.error = f"network: {msg}"
                break
            except Exception as exc:  # pragma: no cover - defensive
                res.error = f"unexpected: {type(exc).__name__}: {str(exc)[:150]}"
                break
        res.elapsed_ms = int((time.monotonic() - started) * 1000)
        if res.body:
            res.sha256 = hashlib.sha256(res.body).hexdigest()
            ext = EXT_FOR_KIND.get(kind, "bin")
            if kind == "html" and "json" in res.content_type:
                ext = "json"
            path = self.snap_dir / f"{res.sha256}.{ext}"
            if not path.exists():
                try:
                    path.write_bytes(res.body)
                except OSError:
                    pass
            res.snapshot_path = f"snapshots/{res.sha256}.{ext}"
            if kind != "pdf":
                res.text = _decode(res.body, res.content_type)
            with self._lock:
                self.total_bytes += len(res.body)
        return self._finish(res, company)

    def _finish(self, res: FetchResult, company: str) -> FetchResult:
        with self._lock:
            self._cache[res.url] = res
            self.log.append({"company": company, "url": res.url, "final_url": res.final_url, "status": res.status,
                             "attempts": res.attempts, "redirects": len(res.redirects), "error": res.error,
                             "sha256": res.sha256, "retrieved_at": res.retrieved_at, "elapsed_ms": res.elapsed_ms,
                             "bytes": len(res.body)})
        return res

    def dump_log(self) -> None:
        with open(self.run_dir / "requests.jsonl", "w", encoding="utf-8") as fh:
            for row in self.log:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
