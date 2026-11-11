"""The F-17 proxy verifier against SIMULATED proxies (no network): a correctly configured one
passes every check; each classic misconfiguration is caught by the check meant for it."""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

spec = importlib.util.spec_from_file_location(
    "verify_ingress_proxy", Path(__file__).parent.parent / "scripts" / "verify_ingress_proxy.py")
V = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = V
spec.loader.exec_module(V)

HOST = "app.example.test"
SECRET = "s" * 64
APP_PATHS = ("/", "/wallet")
APP_PREFIXES = ("/static/", "/api/", "/wallet/static/", "/wallet/api/")
SEC_HEADERS = {"Content-Security-Policy": "default-src 'self'", "X-Content-Type-Options": "nosniff",
               "Referrer-Policy": "no-referrer"}


class Proxy:
    """nginx + the app's IngressGuard semantics, with switchable misconfigurations."""

    def __init__(self, **bad):
        self.bad = bad
        self.sent = []

    def app(self, headers):
        secrets = headers.get("x-elysium-ingress", [])
        if secrets != [SECRET] or headers.get("x-forwarded-proto") != ["https"] \
                or headers.get("host") != [HOST]:
            return 404, {}
        return 200, dict(SEC_HEADERS)

    def request(self, method, url, headers=None, data=None, timeout=None, allow_redirects=False):
        self.sent.append((method, url))
        u = urlsplit(url)
        client = {k.lower(): v for k, v in (headers or {}).items()}
        base = {"Server": "nginx/1.24.0" if self.bad.get("tokens") else "nginx"}
        if not self.bad.get("no_hsts"):
            base["Strict-Transport-Security"] = "max-age=31536000"
        if u.scheme == "http":
            if self.bad.get("no_redirect"):
                return SimpleNamespace(status_code=200, headers=base)
            return SimpleNamespace(status_code=301, headers={**base, "Location": f"https://{HOST}/"})
        if client.get("host", HOST) != HOST and not self.bad.get("any_host"):
            return SimpleNamespace(status_code=421, headers=base)
        if data is not None and len(data) > 16384 and not self.bad.get("no_body_limit"):
            return SimpleNamespace(status_code=413, headers=base)
        path = u.path
        if not (path in APP_PATHS or path.startswith(APP_PREFIXES)) and not self.bad.get("all_paths"):
            return SimpleNamespace(status_code=404, headers=base)
        if method not in ("GET", "HEAD", "POST") and not self.bad.get("all_methods"):
            return SimpleNamespace(status_code=403, headers=base)
        fwd: dict[str, list[str]] = {"host": [HOST], "x-forwarded-proto": ["https"],
                                     "x-elysium-ingress": [SECRET]}
        if self.bad.get("append_secret") and "x-elysium-ingress" in client:
            fwd["x-elysium-ingress"] = [client["x-elysium-ingress"], SECRET]
        if self.bad.get("pass_proto") and "x-forwarded-proto" in client:
            fwd["x-forwarded-proto"] = [client["x-forwarded-proto"]]
        if self.bad.get("all_paths") and path not in APP_PATHS and not path.startswith(APP_PREFIXES):
            return SimpleNamespace(status_code=200, headers=base)      # e.g. an admin page
        st, h = self.app(fwd)
        if path == "/api/quote" and st == 200:
            st = 400
        return SimpleNamespace(status_code=st, headers={**base, **h})


def results(proxy, **kw):
    return {r.name: r for r in V.remote_checks(f"https://{HOST}", proxy, **kw)}


def test_a_correct_proxy_passes_every_remote_check():
    rs = results(Proxy())
    assert {n: r.status for n, r in rs.items() if r.status != "PASS"} == {"rate_limit": "SKIP"}


@pytest.mark.parametrize("bad,check", [
    ("append_secret", "secret_header_overwritten"),
    ("pass_proto", "forwarded_headers_overwritten"),
    ("no_body_limit", "body_limit"),
    ("all_paths", "unknown_paths"),
    ("all_methods", "methods"),
    ("tokens", "server_tokens"),
    ("no_hsts", "hsts"),
    ("no_redirect", "http_redirect"),
    ("any_host", "foreign_host_refused"),
])
def test_each_misconfiguration_is_caught(bad, check):
    rs = results(Proxy(**{bad: True}))
    assert rs[check].status == "FAIL", rs[check]
    assert SECRET not in rs[check].detail


def test_unreachable_origin_fails_once_without_further_probing():
    class Down:
        def request(self, *a, **k):
            raise ConnectionError("tls")
    rs = V.remote_checks(f"https://{HOST}", Down())
    assert [r.status for r in rs] == ["FAIL"]


def test_rate_limit_probe_is_opt_in_and_bounded():
    p = Proxy()
    rs = results(p, burst=1000)
    assert rs["rate_limit"].status == "WARN"           # this simulated proxy never limits
    assert sum(1 for m, u in p.sent if u.endswith("/static/does-not-exist")) == 200


GOOD_NGINX = """
proxy_pass http://127.0.0.1:8765;
proxy_set_header Host "app.example.test";
proxy_set_header X-Forwarded-Proto https;
proxy_set_header X-Forwarded-For $remote_addr;
include /etc/elysium/ingress-secret.conf;
client_max_body_size 16k;
"""


def fake_stat(mode, uid=0):
    return lambda p: os.stat_result((stat.S_IFREG | mode, 0, 0, 1, uid, 0, 10, 0, 0, 0))


def local(nginx=GOOD_NGINX, listeners="LISTEN 0 64 127.0.0.1:8765 0.0.0.0:*", mode=0o640, uid=0):
    return {r.name: r.status for r in V.local_checks(
        nginx_text=nginx, ingress_port=8765, listeners_text=listeners,
        secret_include="/etc/elysium/ingress-secret.conf", env_file="/etc/elysium/trading.env",
        stat_fn=fake_stat(mode, uid))}


def test_a_correct_local_setup_passes():
    assert set(local().values()) == {"PASS"}


@pytest.mark.parametrize("kw,check", [
    (dict(nginx=GOOD_NGINX.replace("$remote_addr", "$proxy_add_x_forwarded_for")), "appends_x_forwarded_for"),
    (dict(nginx=GOOD_NGINX.replace("X-Forwarded-Proto https", "X-Forwarded-Proto $http_x_forwarded_proto")),
     "passes_client_proto"),
    (dict(nginx=GOOD_NGINX.replace("16k", "10m")), "body_limit_set"),
    (dict(nginx=GOOD_NGINX.replace("include /etc/elysium/ingress-secret.conf;", "")), "secret_included"),
    (dict(listeners="LISTEN 0 64 0.0.0.0:8765 0.0.0.0:*"), "ingress_loopback_only"),
    (dict(mode=0o644), "secret_include_mode"),
    (dict(uid=1000), "env_file_mode"),
])
def test_each_local_misconfiguration_is_caught(kw, check):
    assert local(**kw)[check] == "FAIL"


def test_cli_requires_https_and_reports_failures(capsys):
    with pytest.raises(SystemExit):
        V.main(["--origin", "http://app.example.test"], session=Proxy())
    assert V.main(["--origin", f"https://{HOST}"], session=Proxy()) == 0
    assert V.main(["--origin", f"https://{HOST}"], session=Proxy(append_secret=True)) == 1
    assert SECRET not in capsys.readouterr().out
