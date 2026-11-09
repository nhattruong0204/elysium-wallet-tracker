#!/usr/bin/env python3
"""Verify the reverse proxy in front of the private trading ingress (#95 F-17).

Remote checks (from any machine, against the public origin; nothing is changed):
    python scripts/verify_ingress_proxy.py --origin https://app.example.com
Local checks (on the VPS, read-only; needs permission to read the nginx config):
    sudo python scripts/verify_ingress_proxy.py --origin https://app.example.com --local \\
        --secret-include /etc/elysium/ingress-secret.conf --env-file /etc/elysium/trading.env

Each check prints PASS / FAIL / WARN / SKIP with a reason; exit code 1 if anything FAILs.
The shared ingress secret is never read into memory beyond a stat() of its file; no secret, cookie
or header value is printed. Rate-limit probing is opt-in (`--burst N`) and bounded.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

SECRET_HEADER = "X-Elysium-Ingress"


@dataclass
class Result:
    name: str
    status: str
    detail: str


def _status(resp: Any) -> int | None:
    return None if resp is None else int(getattr(resp, "status_code", 0))


def remote_checks(origin: str, session: Any, *, burst: int = 0, timeout: float = 10.0) -> list[Result]:
    u = urlsplit(origin)
    host = u.hostname or ""
    base = f"https://{host}" + (f":{u.port}" if u.port else "")
    out: list[Result] = []

    def req(method: str, path: str, **kw) -> Any:
        try:
            return session.request(method, base + path, timeout=timeout, allow_redirects=False, **kw)
        except Exception as exc:                                       # noqa: BLE001
            return exc

    r = req("GET", "/")
    if isinstance(r, Exception):
        out.append(Result("tls_and_reachability", "FAIL", f"{type(r).__name__} (certificate or network)"))
        return out
    out.append(Result("tls_and_reachability", "PASS" if _status(r) == 200 else "FAIL",
                      f"GET / -> {_status(r)} with a verified certificate"))
    hdr = {k.lower(): v for k, v in r.headers.items()}
    out.append(Result("hsts", "PASS" if "strict-transport-security" in hdr else "FAIL",
                      "Strict-Transport-Security present" if "strict-transport-security" in hdr
                      else "no HSTS header"))
    server = hdr.get("server", "")
    out.append(Result("server_tokens", "FAIL" if re.search(r"\d", server) else "PASS",
                      "Server header leaks a version" if re.search(r"\d", server)
                      else "no version in Server header"))
    sec = [h for h in ("content-security-policy", "x-content-type-options", "referrer-policy")
           if h not in hdr]
    out.append(Result("app_security_headers", "PASS" if not sec else "WARN",
                      "present" if not sec else f"missing {sec} (is the app behind it?)"))

    plain = None
    try:
        plain = session.request("GET", f"http://{host}/", timeout=timeout, allow_redirects=False)
    except Exception:                                                  # noqa: BLE001
        plain = None
    if plain is None:
        out.append(Result("http_redirect", "PASS", "plain HTTP not served"))
    else:
        loc = {k.lower(): v for k, v in plain.headers.items()}.get("location", "")
        ok = _status(plain) in (301, 308) and loc.startswith(f"https://{host}")
        out.append(Result("http_redirect", "PASS" if ok else "FAIL",
                          f"http:// -> {_status(plain)} {'to https' if ok else '(not redirected to https)'}"))

    r = req("GET", "/", headers={"Host": "evil.example"})
    st = None if isinstance(r, Exception) else _status(r)
    out.append(Result("foreign_host_refused", "PASS" if st is None or st >= 400 else "FAIL",
                      f"Host: evil.example -> {st if st is not None else 'connection refused/closed'}"))

    r = req("GET", "/", headers={"X-Forwarded-Proto": "http", "X-Forwarded-For": "6.6.6.6",
                                 "X-Forwarded-Host": "evil.example"})
    out.append(Result("forwarded_headers_overwritten",
                      "PASS" if _status(r) == 200 else "FAIL",
                      "client X-Forwarded-* ignored" if _status(r) == 200 else
                      f"spoofed X-Forwarded-Proto changed the answer ({_status(r)}): proxy passes "
                      "client headers through"))

    r = req("GET", "/", headers={SECRET_HEADER: "forged-" + "0" * 40})
    out.append(Result("secret_header_overwritten",
                      "PASS" if _status(r) == 200 else "FAIL",
                      "client-supplied secret header replaced" if _status(r) == 200 else
                      f"a client-supplied {SECRET_HEADER} broke the request ({_status(r)}): the proxy "
                      "APPENDS instead of overwriting, or forwards the client's value"))

    r = req("POST", "/api/quote", data=b"x" * 20_000, headers={"content-type": "application/json"})
    out.append(Result("body_limit", "PASS" if _status(r) == 413 else "FAIL",
                      f"20 KiB body -> {_status(r)} (expected 413 at the proxy)"))

    leaks = []
    for p in ("/admin", "/.env", "/metrics", "/health", "/docs", "/openapi.json", "/../etc/passwd"):
        r = req("GET", p)
        if _status(r) not in (400, 404, None):
            leaks.append(f"{p}={_status(r)}")
    out.append(Result("unknown_paths", "PASS" if not leaks else "FAIL",
                      "only application routes are proxied" if not leaks else ", ".join(leaks)))

    r = req("PUT", "/")
    out.append(Result("methods", "PASS" if _status(r) in (403, 405) else "FAIL",
                      f"PUT / -> {_status(r)}"))

    if burst > 0:
        burst = min(burst, 200)
        codes = []
        for _ in range(burst):
            r = req("GET", "/static/does-not-exist")
            codes.append(_status(r))
        limited = sum(1 for c in codes if c in (429, 503))
        out.append(Result("rate_limit", "PASS" if limited else "WARN",
                          f"{limited}/{burst} burst requests limited"))
    else:
        out.append(Result("rate_limit", "SKIP", "not probed (use --burst 60)"))
    return out


def local_checks(*, nginx_text: str, ingress_port: int, listeners_text: str,
                 secret_include: str | None, env_file: str | None,
                 stat_fn: Callable[[str], os.stat_result] = os.stat) -> list[Result]:
    out: list[Result] = []
    t = nginx_text
    must = {
        "x_forwarded_proto_set": r"proxy_set_header\s+X-Forwarded-Proto\s+\"?https\"?\s*;",
        "x_forwarded_for_set": r"proxy_set_header\s+X-Forwarded-For\s+\$remote_addr\s*;",
        "body_limit_set": r"client_max_body_size\s+16k\s*;",
        "secret_included": r"include\s+\S*ingress-secret\.conf\s*;",
        "proxy_to_loopback": rf"proxy_pass\s+http://127\.0\.0\.1:{ingress_port}\s*;",
    }
    for name, rx in must.items():
        out.append(Result(name, "PASS" if re.search(rx, t) else "FAIL",
                          "found in nginx -T" if re.search(rx, t) else f"missing: {rx}"))
    bad = {"appends_x_forwarded_for": r"\$proxy_add_x_forwarded_for",
           "passes_client_proto": r"X-Forwarded-Proto\s+\$http_x_forwarded_proto",
           "passes_client_host": r"proxy_set_header\s+Host\s+\$http_host"}
    for name, rx in bad.items():
        hit = re.search(rx, t)
        out.append(Result(name, "FAIL" if hit else "PASS", "present (unsafe)" if hit else "absent"))
    out.append(Result("secret_not_inline",
                      "FAIL" if re.search(r"X-Elysium-Ingress\s+\"?[0-9a-fA-F]{32,}", t) and
                      "ingress-secret.conf" not in t else "PASS",
                      "secret only via the include file"))
    lst = [ln for ln in listeners_text.splitlines() if re.search(rf":{ingress_port}\b", ln)]
    public = [ln for ln in lst if not re.search(rf"(127\.0\.0\.1|\[::1\]|::1):{ingress_port}\b", ln)]
    out.append(Result("ingress_loopback_only",
                      "PASS" if lst and not public else ("SKIP" if not lst else "FAIL"),
                      "listening on loopback only" if lst and not public else
                      "ingress not listening (service stopped?)" if not lst else
                      "ingress port reachable on a non-loopback address"))
    for label, path, maxmode in (("secret_include_mode", secret_include, 0o640),
                                 ("env_file_mode", env_file, 0o640)):
        if not path:
            out.append(Result(label, "SKIP", "path not given"))
            continue
        try:
            st = stat_fn(path)
        except OSError as exc:
            out.append(Result(label, "FAIL", f"cannot stat ({exc.strerror})"))
            continue
        mode = stat.S_IMODE(st.st_mode)
        ok = (mode & ~maxmode) == 0 and st.st_uid == 0 and not stat.S_ISLNK(st.st_mode)
        out.append(Result(label, "PASS" if ok else "FAIL",
                          f"mode {oct(mode)} uid {st.st_uid} (want <= {oct(maxmode)}, root-owned)"))
    return out


def main(argv: list[str] | None = None, *, session: Any = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--origin", required=True)
    ap.add_argument("--burst", type=int, default=0)
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--ingress-port", type=int, default=8765)
    ap.add_argument("--secret-include")
    ap.add_argument("--env-file")
    a = ap.parse_args(argv)
    if not a.origin.startswith("https://"):
        ap.error("--origin must be https://")
    if session is None:
        import requests
        session = requests.Session()
    results = remote_checks(a.origin, session, burst=a.burst)
    if a.local:
        try:
            nginx = subprocess.run(["nginx", "-T"], capture_output=True, text=True, timeout=20,
                                   check=False).stdout
            ss = subprocess.run(["ss", "-ltn"], capture_output=True, text=True, timeout=10,
                                check=False).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            results.append(Result("local", "FAIL", f"cannot run nginx -T / ss ({type(exc).__name__})"))
        else:
            # nginx -T prints the secret include's content: redact before any processing/printing
            nginx = re.sub(r"(X-Elysium-Ingress\s+\")[^\"]*", r"\1<redacted>", nginx)
            results += local_checks(nginx_text=nginx, ingress_port=a.ingress_port, listeners_text=ss,
                                    secret_include=a.secret_include, env_file=a.env_file)
    for r in results:
        print(f"{r.status:4}  {r.name:32} {r.detail}")
    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
