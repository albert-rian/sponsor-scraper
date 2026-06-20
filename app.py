import csv
import io
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)
requests.packages.urllib3.disable_warnings()

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

# In-memory job store: job_id -> {status, progress, total, company, csv, error}
JOBS = {}


# ---------------------------------------------------------------------------
# SCRAPER — find and extract sponsor names from a conference site
# ---------------------------------------------------------------------------

def find_sponsor_page(homepage_url: str) -> list[str]:
    """
    Given a homepage URL, return candidate sponsor page URLs.
    Tries three layers: direct crawl, DuckDuckGo search, common URL patterns.
    """
    from urllib.parse import urlparse
    parsed = urlparse(homepage_url)
    domain = parsed.netloc
    base = f"{parsed.scheme}://{domain}"
    keywords = ["sponsor", "partner", "exhibitor", "supporter"]
    candidates = []
    seen = set()

    # Layer 1: direct crawl
    try:
        resp = requests.get(homepage_url, headers=HEADERS, timeout=12, verify=False)
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True).lower()
            full = urljoin(homepage_url, href)
            if any(k in href.lower() or k in text for k in keywords) and full not in seen:
                seen.add(full)
                candidates.append(full)
    except Exception:
        pass

    # Layer 2: DuckDuckGo search fallback (handles Cloudflare-protected homepages)
    if not candidates:
        try:
            with DDGS() as d:
                results = list(d.text(f"sponsors exhibitors site:{domain}", max_results=5))
            for r in results:
                href = r.get("href", "")
                if domain in href and any(k in href.lower() for k in keywords):
                    if href not in seen:
                        seen.add(href)
                        candidates.append(href)
        except Exception:
            pass

    # Layer 3: probe common URL patterns
    if not candidates:
        for path in ["/sponsors", "/sponsors-exhibitors", "/partners", "/exhibitors", "/sponsorship"]:
            url = base + path
            try:
                r = requests.get(url, headers=HEADERS, timeout=8, verify=False)
                if r.status_code == 200 and url not in seen:
                    seen.add(url)
                    candidates.append(url)
            except Exception:
                continue

    return candidates


def try_js_api(page_url: str, page_html: str) -> list[str] | None:
    """
    Look for an embedded JS API endpoint (like ai4.io's Cloudflare worker).
    Returns list of sponsor names if found, else None.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    for script in soup.find_all("script"):
        content = script.string or ""
        # Look for worker/API URL patterns
        m = re.search(r'["\']([https]+://[^\s"\']+workers\.dev[^\s"\']*)["\']', content)
        if not m:
            m = re.search(r'WORKER_URL\s*=\s*["\']([^"\']+)["\']', content)
        if not m:
            m = re.search(r'getAttribute\("data-worker-url"\)[^;]+\|\|\s*["\']([^"\']+)["\']', content)
        if m:
            api_base = m.group(1).rstrip("/")
            for path in ["/sponsors", "/exhibitors", "/api/sponsors"]:
                try:
                    r = requests.get(api_base + path, headers=HEADERS, timeout=10)
                    if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                        data = r.json()
                        sponsors = data.get("sponsors", data.get("exhibitors", []))
                        if sponsors:
                            names = []
                            for s in sponsors:
                                name = s.get("name", "").strip()
                                cf = (s.get("customFields") or {}).get("Include in public list", {})
                                if name and cf.get("value") != "no":
                                    names.append(name)
                            return sorted(set(names))
                except Exception:
                    continue
    return None


def clean_logo_alt(alt: str) -> str:
    """Turn messy logo alt text into a clean company name."""
    # Strip common logo filename suffixes
    alt = re.sub(r'(?i)[_\-\s]*(logo|icon|img|image|badge|sponsor|lockup|white|black|color|logotype|LogoName|_logo|horizontal|vertical|stacked|full|primary|secondary)[\s_\-]*', ' ', alt)
    alt = re.sub(r'\.(png|jpg|jpeg|svg|webp)$', '', alt, flags=re.IGNORECASE)
    alt = re.sub(r'[_\-]+', ' ', alt).strip()
    alt = re.sub(r'\s{2,}', ' ', alt).strip()
    return alt


def extract_names_from_json(obj, depth=0) -> list[str]:
    """Recursively walk parsed JSON looking for sponsor/company name fields."""
    if depth > 8:
        return []
    names = []
    if isinstance(obj, dict):
        for key in ("name", "title", "companyName", "company_name", "sponsor_name",
                    "exhibitorName", "label", "organizationName"):
            val = obj.get(key)
            if isinstance(val, str) and 2 < len(val) < 100:
                names.append(val.strip())
        for v in obj.values():
            names.extend(extract_names_from_json(v, depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            names.extend(extract_names_from_json(item, depth + 1))
    return names


def scrape_sponsors_from_html(page_html: str) -> list[str]:
    """
    Extract sponsor names from HTML: embedded JSON first, then DOM heuristics.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    names = set()
    keywords = ["sponsor", "partner", "exhibitor", "supporter"]

    # Strategy 0: embedded JSON in <script> tags (Next.js, SPAs, event platforms)
    for script in soup.find_all("script"):
        stype = script.get("type", "")
        sid = script.get("id", "")
        content = script.string or ""
        if not content:
            continue
        # Next.js __NEXT_DATA__ or any JSON script block
        if "application/json" in stype or sid == "__NEXT_DATA__" or any(
            k in content[:200] for k in ('"sponsors"', '"exhibitors"', '"partners"', '"companies"')
        ):
            try:
                # Find the outermost JSON object/array
                for m in re.finditer(r'(\{|\[)', content):
                    try:
                        obj = json.loads(content[m.start():])
                        found = extract_names_from_json(obj)
                        # Only use if it looks like a sponsor list (multiple entries)
                        if len(found) >= 3:
                            names.update(found)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        # window.__data__ / window.sponsors = [...] style assignments
        m = re.search(r'(?:sponsors|exhibitors|partners)\s*[=:]\s*(\[.*?\])', content, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(1))
                names.update(extract_names_from_json(obj))
            except Exception:
                pass

    if len(names) >= 3:
        return sorted(names)

    # Strategy 1: elements near sponsor headings
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        if any(k in tag.get_text(strip=True).lower() for k in keywords):
            for sibling in tag.find_next_siblings():
                if sibling.name in ["h1", "h2", "h3"] and not any(k in sibling.get_text(strip=True).lower() for k in keywords):
                    break
                for img in sibling.find_all("img"):
                    alt = clean_logo_alt(img.get("alt", "").strip())
                    if alt and len(alt) > 2 and alt.lower() not in keywords:
                        names.add(alt)
                for a in sibling.find_all("a"):
                    text = a.get_text(strip=True)
                    if text and len(text) > 2 and not any(k in text.lower() for k in ["click", "view", "more", "apply", "become"]):
                        names.add(text)

    # Strategy 2: elements with sponsor-related class/id
    if not names:
        for el in soup.find_all(True):
            cls = " ".join(el.get("class", []))
            eid = el.get("id", "")
            if any(k in (cls + eid).lower() for k in keywords):
                for img in el.find_all("img"):
                    alt = clean_logo_alt(img.get("alt", "").strip())
                    if alt and len(alt) > 2:
                        names.add(alt)
                for a in el.find_all("a"):
                    text = a.get_text(strip=True)
                    if text and 2 < len(text) < 80:
                        names.add(text)

    # Strategy 3: dedicated sponsors page — grab all img alts on the whole page
    if not names:
        page_text = soup.get_text(" ", strip=True).lower()
        if any(k in page_text[:2000] for k in keywords):
            for img in soup.find_all("img"):
                alt = clean_logo_alt(img.get("alt", "").strip())
                if alt and 3 < len(alt) < 80 and alt.lower() not in keywords:
                    names.add(alt)

    return sorted(names)


def try_worker_api_via_search(domain: str) -> list[str] | None:
    """
    Search DuckDuckGo for a Cloudflare Worker API URL associated with this domain,
    then hit it directly. Works even when the conference site itself blocks scrapers.
    """
    try:
        with DDGS() as d:
            results = list(d.text(f'"{domain}" "workers.dev" sponsors', max_results=5))
        for r in results:
            body = r.get("body", "") + " " + r.get("href", "")
            m = re.search(r'https://[\w-]+\.[\w-]+\.workers\.dev', body)
            if m:
                api_base = m.group(0).rstrip("/")
                for path in ["/sponsors", "/exhibitors", "/api/sponsors"]:
                    try:
                        resp = requests.get(api_base + path, headers=HEADERS, timeout=10)
                        if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
                            data = resp.json()
                            sponsors = data.get("sponsors", data.get("exhibitors", []))
                            if sponsors:
                                names = []
                                for s in sponsors:
                                    name = s.get("name", "").strip()
                                    cf = (s.get("customFields") or {}).get("Include in public list", {})
                                    if name and cf.get("value") != "no":
                                        names.append(name)
                                if names:
                                    return sorted(set(names))
                    except Exception:
                        continue
    except Exception:
        pass
    return None


def get_sponsors(homepage_url: str) -> list[str]:
    """Main entry point: return deduplicated sponsor names from a conference site."""
    from urllib.parse import urlparse
    domain = urlparse(homepage_url).netloc

    candidate_pages = find_sponsor_page(homepage_url)
    if not candidate_pages:
        candidate_pages = [homepage_url]

    for page_url in candidate_pages[:5]:
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=12, verify=False)
            html = resp.text

            # Try JS API first (highest quality)
            names = try_js_api(page_url, html)
            if names and len(names) > 3:
                return names

            # Fallback to HTML parsing
            names = scrape_sponsors_from_html(html)
            if names and len(names) > 3:
                return names
        except Exception:
            continue

    # Last resort: search for Cloudflare Worker API URL via DuckDuckGo
    names = try_worker_api_via_search(domain)
    if names:
        return names

    return []


# ---------------------------------------------------------------------------
# ENRICHER — look up company info via DuckDuckGo + website meta tags
# ---------------------------------------------------------------------------

def ddg(query: str, n: int = 5) -> list[dict]:
    try:
        with DDGS() as d:
            return list(d.text(query, max_results=n))
    except Exception:
        time.sleep(2)
        return []


def fetch_meta_description(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=4, verify=False, allow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        desc = (
            (soup.find("meta", property="og:description") or {}).get("content")
            or (soup.find("meta", attrs={"name": "description"}) or {}).get("content")
            or ""
        )
        return desc.strip()[:400]
    except Exception:
        return ""


US_STATES = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
}

# Cache for Nominatim lookups (city name → country) to avoid duplicate requests
_CITY_CACHE: dict[str, str] = {}


def city_to_country(city: str) -> str:
    """Look up any city in the world via OpenStreetMap Nominatim. Free, no API key."""
    key = city.lower().strip()
    if key in _CITY_CACHE:
        return _CITY_CACHE[key]
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": city, "format": "json", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": "SponsorScraper/1.0 contact@example.com"},
            timeout=3,
        )
        data = resp.json()
        country = data[0].get("address", {}).get("country", "") if data else ""
    except Exception:
        country = ""
    _CITY_CACHE[key] = country
    return country


# Known country names — if a region already IS a country, skip Nominatim
KNOWN_COUNTRIES = {
    "United States", "United Kingdom", "Germany", "France", "India", "Canada",
    "Australia", "Japan", "China", "Singapore", "Israel", "Netherlands", "Sweden",
    "Finland", "Norway", "Denmark", "Switzerland", "Spain", "Italy", "Poland",
    "Brazil", "Mexico", "South Korea", "Russia", "United Arab Emirates",
    "Ireland", "Belgium", "Austria", "Portugal", "New Zealand", "South Africa",
    "Argentina", "Colombia", "Chile", "Indonesia", "Malaysia", "Thailand",
    "Vietnam", "Philippines", "Pakistan", "Bangladesh", "Egypt", "Nigeria",
    "Kenya", "Ghana", "Turkey", "Saudi Arabia", "Qatar", "Kuwait",
}

JUNK_PREFIXES = (
    "find the latest", "check out", "welcome to", "sign in", "log in",
    "login", "subscribe", "shop", "buy", "get started", "learn more",
    "click here", "read more", "home -", "official site", "explore",
    "discover", "join us", "contact", "about us",
)

QUALITY_VERBS = (
    "is a", "is an", "provides", "offers", "develops", "builds",
    "helps", "enables", "delivers", "creates", "powers", "connects",
    "specializes", "focuses", "designs", "manufactures", "operates",
)


INDIA_STATES = {
    "Maharashtra", "Karnataka", "Tamil Nadu", "Telangana", "Andhra Pradesh",
    "Uttar Pradesh", "Gujarat", "Rajasthan", "West Bengal", "Punjab",
    "Haryana", "Madhya Pradesh", "Bihar", "Odisha", "Kerala",
    "Jharkhand", "Assam", "Uttarakhand", "Himachal Pradesh", "Goa",
    "Delhi", "Chandigarh",
}

CANADA_PROVINCES = {
    "Ontario", "Quebec", "British Columbia", "Alberta", "Manitoba",
    "Saskatchewan", "Nova Scotia", "New Brunswick", "Newfoundland",
    "Prince Edward Island",
}

AUSTRALIA_STATES = {
    "New South Wales", "Victoria", "Queensland", "Western Australia",
    "South Australia", "Tasmania", "Northern Territory",
}


def resolve_hq(city: str, region: str) -> tuple[str, str]:
    """Map a city + state/country to (hq_city, hq_country)."""
    region = region.strip()
    if region in US_STATES:
        return region, "United States"
    if region in INDIA_STATES:
        return city.strip(), "India"
    if region in CANADA_PROVINCES:
        return city.strip(), "Canada"
    if region in AUSTRALIA_STATES:
        return city.strip(), "Australia"
    abbrev = {"USA": "United States", "US": "United States",
              "UK": "United Kingdom", "UAE": "United Arab Emirates"}
    if region in abbrev:
        return city.strip(), abbrev[region]
    # If region is already a known country name, use it directly
    if region in KNOWN_COUNTRIES:
        return city.strip(), region
    # Unknown region — ask Nominatim (only for truly ambiguous cases)
    country = city_to_country(city)
    return city.strip(), country if country else region


def clean_wikipedia_summary(extract: str) -> str:
    """Take Wikipedia first sentence as-is — strip pronunciation, minimal quality check."""
    if not extract:
        return ""
    text = re.sub(r'\s+', ' ', extract).strip()
    first = re.split(r'(?<=[.!?])\s', text)[0]
    # Strip IPA pronunciation blocks: (/ ... /) or ([...]) with phonetic chars
    first = re.sub(r'\s*\(/[^)]+/\)', '', first)
    first = re.sub(r'\s*\([^)]*[ˈˌɛɪəʊæɑɒʌʃʒθðŋ][^)]*\)', '', first)
    # Strip Unicode garbage / encoding artifacts
    first = re.sub(r'[^\x00-\x7FÀ-ɏḀ-ỿ]', '', first)
    first = re.sub(r'\s{2,}', ' ', first).strip()
    if len(first) < 30 or first.lower().startswith("this article"):
        return ""
    return first[:300]


def clean_fallback_summary(text: str) -> str:
    """Quality check for non-Wikipedia summaries (meta desc / DDG snippet)."""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text).strip()
    first = re.split(r'(?<=[.!?])\s', text)[0]
    if len(first) < 30:
        return ""
    if any(first.lower().startswith(j) for j in JUNK_PREFIXES):
        return ""
    if "!" in first:
        return ""
    if not any(re.search(r'\b' + re.escape(v) + r'\b', first, re.IGNORECASE) for v in QUALITY_VERBS):
        return ""
    return first[:300]



def wikipedia_lookup(name: str) -> dict:
    """
    Query the Wikipedia API for a company. Returns partial result dict.
    Uses DDG to find the right Wikipedia page (avoids company-stub vs product-page mismatch).
    """
    out = {}
    try:
        # Use DDG to find the best Wikipedia article for this company
        wiki_results = ddg(f"{name} wikipedia", 5)
        title = None
        for r in wiki_results:
            href = r.get("href", "")
            if "en.wikipedia.org/wiki/" in href:
                # Extract page title from URL
                title = href.split("/wiki/")[-1].split("?")[0].replace("_", " ")
                break
        if not title:
            return out

        # Fetch page extract
        page_resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "titles": title, "prop": "extracts",
                    "exintro": True, "explaintext": True, "format": "json"},
            timeout=6
        ).json()
        pages = page_resp.get("query", {}).get("pages", {})
        page = next(iter(pages.values()))
        extract = page.get("extract", "")
        if not extract:
            return out

        # Summary — use Wikipedia first sentence as-is
        summary = clean_wikipedia_summary(extract)
        if summary:
            out["summary"] = summary

        # HQ — handles both state names (Texas) and country names (Germany)
        for pat in [
            r'headquartered\s+in\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)',
            r'based\s+in\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)',
            r'its\s+headquarters\s+(?:is|are)\s+(?:in|located\s+in)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)',
        ]:
            m = re.search(pat, extract)
            if m:
                city, country = resolve_hq(m.group(1), m.group(2))
                out["hq_city"] = city
                out["hq_country"] = country
                break

        # Employees
        for pat in [
            r'(\d[\d,]+)\s*[-–]\s*(\d[\d,]+)\s*employees',
            r'(\d[\d,]+)\+?\s*employees',
            r'(?:approximately|about|over|more than)\s+(\d[\d,]+)\s*employees',
        ]:
            m = re.search(pat, extract, re.IGNORECASE)
            if m:
                if m.lastindex and m.lastindex >= 2:
                    out["employee_size"] = f"{m.group(1)}-{m.group(2)} employees"
                else:
                    out["employee_size"] = m.group(1) + " employees"
                break

    except Exception:
        pass
    return out


def best_website(name: str, search_results: list[dict]) -> str:
    """Pick the official company website from search results."""
    # Sites that are never the official company website
    skip = (
        "linkedin", "facebook", "twitter", "x.com", "crunchbase", "bloomberg",
        "wikipedia", "glassdoor", "indeed", "youtube", "instagram", "yelp",
        "yahoo", "reuters", "forbes", "techcrunch", "wsj", "fortune",
        "businesswire", "prnewswire", "businessinsider", "cnbc", "cnn",
        "perplexity", "google", "bing", "reddit", "quora", "medium",
        "pitchbook", "owler", "zoominfo", "dnb.com", "craft.co",
        "apollo.io", "rocketreach", "clearbit", "clutch.co", "g2.com",
        "capterra", "trustpilot", "mapquest", "yellowpages", "manta.com",
        "finance.", "/finance/", "stock", "markets", "investing",
    )
    # Build slugs from each word in the company name (ignore common words)
    ignore = {"the", "a", "an", "and", "of", "inc", "llc", "ltd", "corp",
              "corporation", "technologies", "technology", "solutions", "group",
              "systems", "services", "software", "global", "international"}
    name_words = [w for w in re.sub(r'[^a-z0-9 ]', '', name.lower()).split() if w not in ignore]

    for r in search_results:
        href = r.get("href", "")
        if not href or any(s in href.lower() for s in skip):
            continue
        domain = re.sub(r'https?://(www\.)?', '', href).split("/")[0].lower()
        domain_slug = re.sub(r'[^a-z0-9]', '', domain)
        # Accept only if a meaningful name word appears in the domain
        if any(len(w) >= 3 and w in domain_slug for w in name_words):
            return "https://www." + domain if not domain.startswith("www") else "https://" + domain
    return ""


def enrich(name: str) -> dict:
    result = {
        "company_name": name,
        "website": "",
        "summary": "",
        "hq_city": "",
        "hq_country": "",
    }

    # Step 1: Wikipedia lookup (fast, high quality for known companies)
    wiki = wikipedia_lookup(name)
    result.update({k: v for k, v in wiki.items() if v})

    # Step 2: Dedicated website search — try multiple query variations
    if not result["website"]:
        site_results = ddg(f"{name} official website", 6)
        result["website"] = best_website(name, site_results)
    if not result["website"]:
        site_results2 = ddg(f"{name} company site", 6)
        result["website"] = best_website(name, site_results2)
    if not result["website"]:
        site_results3 = ddg(name, 8)
        result["website"] = best_website(name, site_results3)
    if not result["website"]:
        # Last resort: probe common TLDs directly for the slugified name
        slug = re.sub(r'[^a-z0-9]', '', name.lower())
        for tld in [".com", ".io", ".ai", ".co"]:
            url = f"https://www.{slug}{tld}"
            try:
                r = requests.get(url, headers=HEADERS, timeout=3, allow_redirects=True, verify=False)
                if r.status_code == 200:
                    result["website"] = url
                    break
            except Exception:
                continue

    # Step 3: General search for remaining fields
    search_results = ddg(f"{name} company headquarters", 6)
    blob = " | ".join(r.get("title", "") + " " + r.get("body", "") for r in search_results)

    # Summary fallback: meta description from official site, then search snippet
    if not result["summary"] and result["website"]:
        meta = fetch_meta_description(result["website"])
        if meta:
            result["summary"] = clean_fallback_summary(meta)
    if not result["summary"]:
        for r in search_results:
            body = r.get("body", "").strip()
            s = clean_fallback_summary(body)
            if s:
                result["summary"] = s
                break

    # HQ fallback with state/region mapping
    if not result["hq_city"]:
        for pat in [
            r'headquartered?\s+in\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)',
            r'based\s+in\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)',
            r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*(United States|USA|UK|United Kingdom|Canada|Germany|France|India|Israel|Australia|Singapore|Japan|China|Netherlands|Sweden|Finland)',
        ]:
            m = re.search(pat, blob)
            if m:
                city, country = resolve_hq(m.group(1), m.group(2))
                result["hq_city"] = city
                result["hq_country"] = country
                break

    # HQ last resort: extract city from blob then resolve via Nominatim
    if not result["hq_city"]:
        m = re.search(
            r'\b(headquartered?|based|located|offices?)\s+in\s+([A-Z][a-z]+(?:[\s-][A-Z][a-z]+)*)',
            blob
        )
        if m:
            city = m.group(2).strip()
            country = city_to_country(city)
            if country:
                result["hq_city"] = city
                result["hq_country"] = country

    return result


# ---------------------------------------------------------------------------
# PIPELINE — runs in background thread, pushes progress via queue
# ---------------------------------------------------------------------------

def run_pipeline(job_id: str, homepage_url: str):
    job = JOBS[job_id]

    try:
        job["message"] = f"Finding sponsors page on {homepage_url}..."
        names = get_sponsors(homepage_url)

        if not names:
            job["status"] = "error"
            job["error"] = "Could not find any sponsor names on this site. Try pasting the direct sponsors page URL."
            return

        job["total"] = len(names)
        job["message"] = f"Found {len(names)} sponsors. Starting enrichment..."

        rows = [None] * len(names)
        completed = [0]

        def enrich_one(idx_name):
            idx, name = idx_name
            try:
                return idx, enrich(name)
            except Exception:
                return idx, {
                    "company_name": name, "website": "",
                    "summary": "", "hq_city": "", "hq_country": "",
                }

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(enrich_one, (i, name)): name for i, name in enumerate(names)}
            for future in as_completed(futures):
                idx, data = future.result()
                rows[idx] = data
                completed[0] += 1
                job["progress"] = completed[0]
                job["company"] = data["company_name"]

        # Sort alphabetically by company name
        rows.sort(key=lambda r: r["company_name"].lower())

        # Build CSV
        fieldnames = ["company_name", "website", "summary", "hq_city", "hq_country"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        job["csv"] = buf.getvalue()
        job["status"] = "done"

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


# ---------------------------------------------------------------------------
# FLASK ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not url.startswith("http"):
        url = "https://" + url

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "status": "running", "progress": 0, "total": 0,
        "company": "", "message": "Starting...", "csv": None, "error": None
    }
    threading.Thread(target=run_pipeline, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "total": job["total"],
        "company": job["company"],
        "message": job["message"],
        "error": job["error"],
    })


@app.route("/download/<job_id>")
def download(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("csv"):
        return jsonify({"error": "Not ready"}), 404

    buf = io.BytesIO(job["csv"].encode("utf-8"))
    buf.seek(0)
    return send_file(buf, mimetype="text/csv", as_attachment=True,
                     download_name="sponsors_enriched.csv")


if __name__ == "__main__":
    app.run(debug=True)
