"""Probe 10: Rabby / OKX wallet universal links for 'open this dapp in the wallet'."""
import re, requests
S = requests.Session(); S.headers["user-agent"] = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"
for u in ("https://go.rabby.io/.well-known/apple-app-site-association",
          "https://go.rabby.io/.well-known/assetlinks.json",
          "https://go.rabby.io/mobile/?_cmd=open-dapp&dapp=https%3A%2F%2Fwww.agoradex.app%2Ftoken%2F0x9a9815af521149e8d982791703a31bca2f95f534",
          "https://web3.okx.com/.well-known/apple-app-site-association",
          "https://web3.okx.com/download?deeplink=okx%3A%2F%2Fwallet%2Fdapp%2Furl%3FdappUrl%3Dhttps%253A%252F%252Fwww.agoradex.app"):
    try:
        r = S.get(u, timeout=20, allow_redirects=True)
        t = r.text
        print(f"\n### {r.status_code} {u}\nfinal: {r.url}\nctype: {r.headers.get('content-type')}")
        if "apple-app-site" in u or "assetlinks" in u:
            print(t[:2500])
        else:
            print("title:", (re.search(r"<title>([^<]*)", t) or [None, "-"])[1])
            for pat in (r"rabby[a-z]*://[^\"' ]{0,80}", r"okx://[^\"' ]{0,80}", r"apps\.apple\.com[^\"' ]{0,60}", r"location\.(?:href|replace)[^;]{0,120}"):
                print(pat, "->", sorted(set(re.findall(pat, t)))[:5])
            print(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))[:600])
    except Exception as e:
        print("ERR", u, e)
