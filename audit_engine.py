"""
audit_engine.py
Real, live SEO audit logic. No mock data — every check hits the actual site.
Coverage modeled on JeffLi1993/seo-audit-skill (script layer: deterministic checks).
"""
import re
import time
import socket
import ipaddress
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; SEOAuditBot/1.0; +https://example.com/bot)"
TIMEOUT = 10


class Check:
    def __init__(self, id, title, category, status, message, evidence=None):
        self.id = id
        self.title = title
        self.category = category  # "site" or "page"
        self.status = status      # "pass" | "warn" | "fail"
        self.message = message
        self.evidence = evidence or ""

    def to_dict(self):
        return {
            "id": self.id, "title": self.title, "category": self.category,
            "status": self.status, "message": self.message, "evidence": self.evidence,
        }


def _is_public_host(hostname: str) -> bool:
    """Basic SSRF guard: block private/loopback/link-local targets."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            return False
    return True


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


def safe_get(url, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    return requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True, **kwargs)


def run_audit(raw_url: str) -> dict:
    url = normalize_url(raw_url)
    parsed = urlparse(url)
    hostname = parsed.hostname

    if not hostname:
        raise ValueError("Could not parse a hostname from that URL.")
    if not _is_public_host(hostname):
        raise ValueError("That host can't be audited (private/internal address blocked).")

    checks = []
    t0 = time.time()

    # ---- Fetch the page ----
    try:
        resp = safe_get(url)
    except requests.RequestException as e:
        raise ValueError(f"Could not fetch {url}: {e}")

    load_time = time.time() - t0
    final_url = resp.url
    final_parsed = urlparse(final_url)
    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    checks.append(Check(
        "http_status", "HTTP Status", "site",
        "pass" if resp.status_code == 200 else ("warn" if resp.status_code < 400 else "fail"),
        f"Page responded with HTTP {resp.status_code}.",
        f"Final URL after redirects: {final_url}"
    ))

    checks.append(Check(
        "https", "HTTPS", "site",
        "pass" if final_parsed.scheme == "https" else "fail",
        "Page is served over HTTPS." if final_parsed.scheme == "https" else "Page is not served over HTTPS.",
    ))

    if parsed.scheme == "http" and final_parsed.scheme == "https":
        checks.append(Check("http_redirect", "HTTP → HTTPS Redirect", "site", "pass",
                             "HTTP requests correctly redirect to HTTPS."))
    elif parsed.scheme == "http" and final_parsed.scheme != "https":
        checks.append(Check("http_redirect", "HTTP → HTTPS Redirect", "site", "fail",
                             "HTTP does not redirect to HTTPS."))

    checks.append(Check(
        "load_time", "Response Time", "site",
        "pass" if load_time < 1.0 else ("warn" if load_time < 2.5 else "fail"),
        f"Initial HTML response took {load_time:.2f}s.",
    ))

    # ---- robots.txt ----
    robots_url = f"{final_parsed.scheme}://{final_parsed.netloc}/robots.txt"
    sitemap_paths = []
    try:
        r = safe_get(robots_url)
        if r.status_code == 200 and r.text.strip():
            sitemap_paths = re.findall(r"(?im)^Sitemap:\s*(\S+)", r.text)
            disallow_all = re.search(r"(?im)^User-agent:\s*\*\s*\n(Disallow:\s*/\s*$)", r.text)
            checks.append(Check(
                "robots_txt", "robots.txt", "site", "pass" if not disallow_all else "warn",
                "robots.txt found and readable." + (" Warning: blocks all crawlers (Disallow: /)." if disallow_all else ""),
                f"{robots_url} — {len(r.text.splitlines())} lines, {len(sitemap_paths)} Sitemap directive(s)."
            ))
        else:
            checks.append(Check("robots_txt", "robots.txt", "site", "warn",
                                 f"robots.txt returned HTTP {r.status_code} or was empty.", robots_url))
    except requests.RequestException:
        checks.append(Check("robots_txt", "robots.txt", "site", "warn",
                             "robots.txt could not be fetched.", robots_url))

    # ---- sitemap.xml ----
    candidates = sitemap_paths or [f"{final_parsed.scheme}://{final_parsed.netloc}/sitemap.xml"]
    sitemap_ok = False
    for sm_url in candidates:
        try:
            r = safe_get(sm_url)
            if r.status_code == 200 and ("<urlset" in r.text or "<sitemapindex" in r.text):
                url_count = len(re.findall(r"<loc>", r.text))
                checks.append(Check(
                    "sitemap", "XML Sitemap", "site", "pass",
                    f"Sitemap found with {url_count} URL(s) referenced.", sm_url
                ))
                sitemap_ok = True
                break
        except requests.RequestException:
            continue
    if not sitemap_ok:
        checks.append(Check("sitemap", "XML Sitemap", "site", "fail",
                             "No valid sitemap.xml found via robots.txt or the default path.",
                             ", ".join(candidates)))

    # ---- 404 handling ----
    try:
        probe = f"{final_parsed.scheme}://{final_parsed.netloc}/this-page-should-not-exist-{int(time.time())}"
        r = safe_get(probe)
        if r.status_code == 404:
            status, msg = "pass", "Non-existent URLs correctly return HTTP 404."
        elif r.status_code == 200:
            status, msg = "warn", "Non-existent URLs return HTTP 200 (soft 404) instead of a real 404."
        else:
            status, msg = "warn", f"Non-existent URLs return HTTP {r.status_code}."
        checks.append(Check("404_handling", "404 Handling", "site", status, msg))
    except requests.RequestException:
        pass

    # ---- Title tag ----
    title_tag = soup.find("title")
    title_text = title_tag.get_text(strip=True) if title_tag else ""
    if not title_text:
        checks.append(Check("title", "Title Tag", "page", "fail", "No <title> tag found."))
    else:
        length = len(title_text)
        status = "pass" if 30 <= length <= 60 else "warn"
        checks.append(Check("title", "Title Tag", "page", status,
                             f'Title is {length} characters (ideal: 50–60). Content: "{title_text}"'))

    # ---- Meta description ----
    meta_desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    desc_text = meta_desc.get("content", "").strip() if meta_desc else ""
    if not desc_text:
        checks.append(Check("meta_description", "Meta Description", "page", "fail",
                             "No meta description found."))
    else:
        length = len(desc_text)
        status = "pass" if 120 <= length <= 160 else "warn"
        checks.append(Check("meta_description", "Meta Description", "page", status,
                             f'Meta description is {length} characters (ideal: 120–160). Content: "{desc_text}"'))

    # ---- H1 ----
    h1s = soup.find_all("h1")
    if len(h1s) == 0:
        checks.append(Check("h1", "H1 Tag", "page", "fail", "No H1 tag found on the page."))
    elif len(h1s) == 1:
        checks.append(Check("h1", "H1 Tag", "page", "pass",
                             f'Exactly one H1 found: "{h1s[0].get_text(strip=True)[:120]}"'))
    else:
        checks.append(Check("h1", "H1 Tag", "page", "warn",
                             f"{len(h1s)} H1 tags found — should be exactly one."))

    # ---- Heading structure ----
    h2s = soup.find_all("h2")
    h3s = soup.find_all("h3")
    h2_status = "pass" if 2 <= len(h2s) <= 12 else "warn"
    checks.append(Check("headings", "Heading Structure", "page", h2_status,
                         f"{len(h2s)} H2s, {len(h3s)} H3s found."))

    # ---- Canonical ----
    canon = soup.find("link", rel=lambda v: v and "canonical" in v)
    if canon and canon.get("href"):
        canon_href = urljoin(final_url, canon["href"])
        status = "pass" if canon_href.rstrip("/") == final_url.rstrip("/") else "warn"
        checks.append(Check("canonical", "Canonical Tag", "page", status,
                             "Self-referencing canonical." if status == "pass" else "Canonical points elsewhere.",
                             canon_href))
    else:
        checks.append(Check("canonical", "Canonical Tag", "page", "warn", "No canonical tag found."))

    # ---- Robots meta ----
    robots_meta = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    if robots_meta and "noindex" in robots_meta.get("content", "").lower():
        checks.append(Check("robots_meta", "Robots Meta Tag", "page", "fail",
                             "Page has a noindex directive — it will not appear in search results."))
    else:
        checks.append(Check("robots_meta", "Robots Meta Tag", "page", "pass",
                             "No blocking noindex/nofollow directive found."))

    # ---- Word count ----
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    body_text = soup.get_text(separator=" ", strip=True)
    word_count = len(body_text.split())
    wc_status = "pass" if word_count >= 500 else ("warn" if word_count >= 250 else "fail")
    checks.append(Check("word_count", "Word Count", "page", wc_status,
                         f"{word_count} words of visible body text (target: 500+)."))

    # ---- Image alt text ----
    imgs = soup.find_all("img")
    missing_alt = [i for i in imgs if not i.get("alt", "").strip()]
    if not imgs:
        checks.append(Check("alt_text", "Image Alt Text", "page", "warn", "No <img> tags found on the page."))
    else:
        pct_missing = len(missing_alt) / len(imgs) * 100
        status = "pass" if pct_missing == 0 else ("warn" if pct_missing < 30 else "fail")
        checks.append(Check("alt_text", "Image Alt Text", "page", status,
                             f"{len(missing_alt)} of {len(imgs)} images are missing alt text."))

    # ---- Internal links ----
    links = soup.find_all("a", href=True)
    internal, external = 0, 0
    for a in links:
        href = a["href"]
        if href.startswith("#") or href.lower().startswith("mailto:") or href.lower().startswith("tel:"):
            continue
        full = urljoin(final_url, href)
        if urlparse(full).netloc == final_parsed.netloc:
            internal += 1
        else:
            external += 1
    link_status = "pass" if internal >= 3 else "warn"
    checks.append(Check("internal_links", "Internal Links", "page", link_status,
                         f"{internal} internal link(s), {external} external link(s) found."))

    # ---- Open Graph / Twitter cards ----
    og_title = soup.find("meta", property="og:title")
    og_desc = soup.find("meta", property="og:description")
    og_image = soup.find("meta", property="og:image")
    twitter_card = soup.find("meta", attrs={"name": "twitter:card"})
    og_present = sum(bool(x) for x in [og_title, og_desc, og_image])
    checks.append(Check(
        "og_tags", "Open Graph / Social Tags", "page",
        "pass" if og_present == 3 else ("warn" if og_present > 0 else "fail"),
        f"{og_present}/3 core OG tags present (title/description/image)." +
        (" Twitter card tag present." if twitter_card else " No twitter:card tag.")
    ))

    # ---- JSON-LD schema ----
    ld_scripts = soup.find_all("script", type="application/ld+json")
    if ld_scripts:
        types_found = []
        for s in ld_scripts:
            types_found += re.findall(r'"@type"\s*:\s*"([^"]+)"', s.get_text())
        checks.append(Check("schema", "Structured Data (JSON-LD)", "page", "pass",
                             f"{len(ld_scripts)} JSON-LD block(s) found. Types: {', '.join(sorted(set(types_found))) or 'unspecified'}."))
    else:
        checks.append(Check("schema", "Structured Data (JSON-LD)", "page", "warn",
                             "No JSON-LD structured data found on the page."))

    # ---- E-E-A-T trust pages ----
    trust_patterns = {
        "About": re.compile(r"about", re.I),
        "Contact": re.compile(r"contact", re.I),
        "Privacy": re.compile(r"privacy", re.I),
        "Terms": re.compile(r"terms", re.I),
    }
    found_trust = []
    for a in links:
        text = (a.get_text(strip=True) + " " + a["href"]).lower()
        for label, pat in trust_patterns.items():
            if pat.search(text) and label not in found_trust:
                found_trust.append(label)
    trust_status = "pass" if len(found_trust) >= 3 else ("warn" if found_trust else "fail")
    checks.append(Check("trust_pages", "E-E-A-T Trust Pages", "site", trust_status,
                         f"Found links to: {', '.join(found_trust) or 'none'} (of About/Contact/Privacy/Terms)."))

    # ---- URL slug ----
    path = final_parsed.path
    slug_issues = []
    if re.search(r"[A-Z]", path):
        slug_issues.append("contains uppercase characters")
    if "_" in path:
        slug_issues.append("uses underscores instead of hyphens")
    if re.search(r"\s", path):
        slug_issues.append("contains spaces")
    slug_status = "pass" if not slug_issues else "warn"
    checks.append(Check("url_slug", "URL Slug", "page", slug_status,
                         "Slug looks clean." if not slug_issues else f"Slug issues: {', '.join(slug_issues)}.",
                         path or "/"))

    # ---- Scoring ----
    total = len(checks)
    passed = sum(1 for c in checks if c.status == "pass")
    warned = sum(1 for c in checks if c.status == "warn")
    failed = sum(1 for c in checks if c.status == "fail")
    score = round((passed * 1.0 + warned * 0.5) / total * 100) if total else 0

    if score >= 85:
        verdict = "Strong SEO foundation with minor polish items."
    elif score >= 65:
        verdict = "Solid base, but several fixable issues are holding this page back."
    elif score >= 40:
        verdict = "Meaningful SEO gaps — prioritize the failing checks below."
    else:
        verdict = "Significant SEO issues found across the site and page level."

    # Priority actions: fails first, then warns, in the checks' natural order
    priority = [c for c in checks if c.status == "fail"] + [c for c in checks if c.status == "warn"]
    priority_actions = [c.to_dict() for c in priority[:3]]

    return {
        "url": url,
        "final_url": final_url,
        "hostname": hostname,
        "audited_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "score": score,
        "verdict": verdict,
        "counts": {"pass": passed, "warn": warned, "fail": failed, "total": total},
        "priority_actions": priority_actions,
        "site_checks": [c.to_dict() for c in checks if c.category == "site"],
        "page_checks": [c.to_dict() for c in checks if c.category == "page"],
    }
