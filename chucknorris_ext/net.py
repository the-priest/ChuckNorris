"""net.py — one HTTP layer for the whole app: pooled, cached, proxy-aware.

Why this exists
---------------
Every outbound request used to build a fresh ``urllib`` opener, which means a
fresh TCP connection and a fresh TLS handshake. On a research turn that reads
three pages after probing four SearXNG instances, that is seven handshakes —
somewhere between 0.7s and 2s of pure round-trip latency spent on nothing but
saying hello. urllib has no connection reuse and no way to bolt it on.

So: a small keep-alive pool over ``http.client`` (stdlib, no new dependency),
plus a short-lived response cache so the same page fetched twice in one turn
costs nothing the second time.

Three hard rules:

  1. NEVER be less capable than what it replaces. Redirects are followed, the
     http/https scheme allowlist is enforced here too, and ANY failure inside
     the fast path falls back to plain urllib rather than surfacing an error the
     old code would not have produced.
  2. NEVER cache anything but a successful GET of a body we actually read, and
     never for long. This is a latency cache, not a store — 90 seconds by
     default, and it holds a bounded number of entries.
  3. Proxies are honoured exactly as before: if one is set, everything goes
     through it, and a pooled connection is keyed by (proxy, host) so traffic
     can never leak onto a direct connection because a socket happened to be
     warm.
"""

import gzip
import ssl
import time
import zlib
import threading
import http.client
import urllib.error
import urllib.parse
import urllib.request

ALLOWED_SCHEMES = ("http", "https")
MAX_REDIRECTS = 5
POOL_PER_HOST = 2               # sockets kept warm per (proxy, scheme, host)
IDLE_TIMEOUT = 55.0             # most servers close an idle keep-alive at 60s
CACHE_TTL = 90.0                # seconds a fetched body stays reusable
CACHE_MAX = 64                  # entries; small on purpose, this is not a store
CACHE_MAX_BYTES = 2_000_000     # never cache anything huge


class _Resp:
    """The subset of a urllib response the app actually uses.

    Callers do ``.read()`` and ``.headers.get(...)``; giving them exactly that
    keeps every existing call site working whether it was served from the pool,
    from the cache, or from the urllib fallback.
    """

    __slots__ = ("_body", "headers", "status", "url")

    def __init__(self, body, headers, status, url):
        self._body = body
        self.headers = headers
        self.status = status
        self.url = url

    def read(self, amt=None):
        if amt is None or amt >= len(self._body):
            out, self._body = self._body, b""
            return out
        out, self._body = self._body[:amt], self._body[amt:]
        return out

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def close(self):
        self._body = b""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _Headers(dict):
    """Case-insensitive header lookup, same shape as email.message.Message."""

    def __init__(self, pairs):
        super().__init__((k.lower(), v) for k, v in pairs)

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)

    def __getitem__(self, key):
        return super().__getitem__(str(key).lower())


# ── connection pool ─────────────────────────────────────────────────────────
_POOL = {}                      # key -> [(conn, last_used_monotonic), ...]
_POOL_LOCK = threading.Lock()
_SSL_CTX = None


def _ssl_context():
    """One shared, verified context. Building an SSLContext loads the whole CA
    bundle off disk — doing that per request was measurable on its own."""
    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def _key(proxy, scheme, host, port):
    return (proxy or "", scheme, host, port)


def _take(key):
    """A warm connection for this key, or None. Stale ones are dropped."""
    now = time.monotonic()
    with _POOL_LOCK:
        bucket = _POOL.get(key)
        while bucket:
            conn, last = bucket.pop()
            if now - last < IDLE_TIMEOUT:
                return conn
            try:
                conn.close()
            except Exception:
                pass
    return None


def _give(key, conn):
    with _POOL_LOCK:
        bucket = _POOL.setdefault(key, [])
        if len(bucket) >= POOL_PER_HOST:
            try:
                bucket.pop(0)[0].close()
            except Exception:
                pass
        bucket.append((conn, time.monotonic()))


def close_all():
    """Drop every pooled socket. Called when the proxy setting changes, so a
    connection opened before the change can never be reused after it."""
    with _POOL_LOCK:
        for bucket in _POOL.values():
            for conn, _ in bucket:
                try:
                    conn.close()
                except Exception:
                    pass
        _POOL.clear()


def _connect(proxy, scheme, host, port, timeout):
    """A live connection to host, through proxy if one is configured."""
    if proxy:
        p = urllib.parse.urlsplit(proxy if "//" in proxy else "//" + proxy)
        phost, pport = p.hostname, (p.port or 8080)
        if not phost:
            raise ValueError(f"unusable proxy {proxy!r}")
        # A plain HTTP proxy is spoken to in cleartext even for an https target:
        # CONNECT first, THEN TLS inside the tunnel. Wrapping the hop to the
        # proxy itself in TLS is the classic way to get an unexplained
        # handshake failure here.
        conn = http.client.HTTPConnection(phost, pport, timeout=timeout)
        if scheme == "https":
            conn.set_tunnel(host, port)
            conn = _TunnelledTLS(conn, host, timeout)
        return conn
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout,
                                           context=_ssl_context())
    return http.client.HTTPConnection(host, port, timeout=timeout)


class _TunnelledTLS:
    """https through an http proxy: CONNECT, then TLS over the tunnel.

    http.client already does exactly this when set_tunnel() is used on an
    HTTPSConnection, but only if the connection to the proxy is itself https.
    This wrapper drives an HTTPConnection through CONNECT and then hands the
    upgraded socket back, which is what a plain proxy expects.
    """

    def __init__(self, conn, host, timeout):
        self._raw = conn
        self._host = host
        self._timeout = timeout
        self._tls = None

    def request(self, method, selector, body=None, headers=None):
        if self._tls is None:
            self._raw.connect()
            sock = _ssl_context().wrap_socket(self._raw.sock,
                                              server_hostname=self._host)
            self._tls = http.client.HTTPConnection(self._host, timeout=self._timeout)
            self._tls.sock = sock
        return self._tls.request(method, selector, body=body, headers=headers or {})

    def getresponse(self):
        return self._tls.getresponse()

    def close(self):
        for c in (self._tls, self._raw):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        self._tls = None


# ── response cache ──────────────────────────────────────────────────────────
_CACHE = {}                     # url -> (expires_at, body, headers, status)
_CACHE_LOCK = threading.Lock()


def cache_clear():
    with _CACHE_LOCK:
        _CACHE.clear()


def _cache_get(url, ttl):
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(url)
        if not hit:
            return None
        if hit[0] < now:
            _CACHE.pop(url, None)
            return None
        return _Resp(hit[1], hit[2], hit[3], url)


def _cache_put(url, body, headers, status, ttl):
    if ttl <= 0 or status != 200 or len(body) > CACHE_MAX_BYTES:
        return
    with _CACHE_LOCK:
        if len(_CACHE) >= CACHE_MAX:
            # cheapest sane eviction: drop whatever expires soonest
            oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
            _CACHE.pop(oldest, None)
        _CACHE[url] = (time.monotonic() + ttl, body, headers, status)


# ── the one entry point ─────────────────────────────────────────────────────
def _decode(body, encoding):
    enc = (encoding or "").lower()
    try:
        if enc == "gzip":
            return gzip.decompress(body)
        if enc == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception:
        return body            # a lying Content-Encoding is not worth failing over
    return body


def _urllib_fallback(url, data, timeout, headers, proxy):
    """The old path, kept intact. Anything the fast path can't do lands here."""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def request(url, data=None, timeout=20, headers=None, proxy=None,
            cache_ttl=0.0, max_bytes=None):
    """Fetch a URL. Returns a response with .read() and .headers.

    ``cache_ttl`` > 0 makes a successful GET reusable for that many seconds —
    only ever set by callers that are fetching a page for its text, never for
    the model API and never for anything with a body.
    """
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"blocked URL scheme {scheme!r} (only http/https allowed)")

    method = "POST" if data is not None else "GET"
    if method == "GET":
        cached = _cache_get(url, cache_ttl)
        if cached is not None:
            return cached

    hdrs = dict(headers or {})
    hdrs.setdefault("Accept-Encoding", "gzip, deflate")
    hdrs["Connection"] = "keep-alive"
    if data is not None:
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")

    try:
        resp = _pooled(url, method, data, timeout, hdrs, proxy, max_bytes)
    except (ValueError, urllib.error.HTTPError):
        raise
    except Exception:
        # Pool trouble is never the user's problem: retire the sockets for this
        # host and take the boring path.
        close_all()
        r = _urllib_fallback(url, data, timeout, headers, proxy)
        body = r.read(max_bytes + 1) if max_bytes else r.read()
        out = _Resp(body, r.headers, getattr(r, "status", 200), url)
        if method == "GET":
            _cache_put(url, body, out.headers, out.status, cache_ttl)
        return out

    if method == "GET":
        _cache_put(url, resp._body, resp.headers, resp.status, cache_ttl)
    return resp


def _pooled(url, method, data, timeout, hdrs, proxy, max_bytes):
    seen = 0
    current = url
    while True:
        parts = urllib.parse.urlsplit(current)
        scheme = parts.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise ValueError(f"blocked URL scheme {scheme!r} (only http/https allowed)")
        host = parts.hostname
        if not host:
            raise ValueError(f"no host in {current!r}")
        port = parts.port or (443 if scheme == "https" else 80)
        selector = urllib.parse.urlunsplit(("", "", parts.path or "/",
                                            parts.query, ""))
        key = _key(proxy, scheme, host, port)

        send = dict(hdrs)
        send.setdefault("Host", parts.netloc.split("@")[-1])

        conn = _take(key)
        reused = conn is not None
        for attempt in (0, 1):
            if conn is None:
                conn = _connect(proxy, scheme, host, port, timeout)
                reused = False
            try:
                conn.request(method, selector or "/", body=data, headers=send)
                r = conn.getresponse()
                break
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
                # A reused socket the server closed while it was idle fails on
                # the first write and is not an error — reconnect once, silently.
                # A fresh connection failing is real; let it out.
                if attempt or not reused:
                    raise

        body = r.read(max_bytes + 1) if max_bytes else r.read()
        status = r.status
        raw_headers = _Headers(r.getheaders())
        keep = (r.version == 11
                and "close" not in (raw_headers.get("connection", "") or "").lower())
        if keep:
            _give(key, conn)
        else:
            try:
                conn.close()
            except Exception:
                pass

        if status in (301, 302, 303, 307, 308):
            loc = raw_headers.get("location")
            seen += 1
            if loc and seen <= MAX_REDIRECTS:
                current = urllib.parse.urljoin(current, loc)
                if status in (301, 302, 303) and method == "POST":
                    method, data = "GET", None
                    send.pop("Content-Type", None)
                continue
        if status >= 400:
            # Match urllib: a 4xx/5xx is an exception, so every existing
            # try/except around a fetch keeps behaving the way it always has.
            raise urllib.error.HTTPError(current, status, r.reason,
                                         raw_headers, None)

        body = _decode(body, raw_headers.get("content-encoding"))
        return _Resp(body, raw_headers, status, current)
