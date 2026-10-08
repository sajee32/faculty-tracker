"""Daily scraper: official pages -> faculty ads -> read advert PDFs -> cross-check on other
job websites -> confidence score -> diff -> write docs/data/*.json"""
import difflib, hashlib, io, json, re, time, datetime as dt
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"; DATA.mkdir(parents=True, exist_ok=True)
ADS, CHG, META = DATA / "ads.json", DATA / "changes.json", DATA / "meta.json"
SOURCES = ROOT / "scraper" / "sources.json"
HDR = {"User-Agent": "Mozilla/5.0 (compatible; FacultyTracker/1.0)"}
TODAY = dt.date.today()
MAX_PDFS = 80

FACULTY = re.compile(r"faculty|professor|lecturer|teaching (?:position|post)", re.I)
EXCLUDE = re.compile(r"project|post-?\s?doc|postdoctoral|\bJRF\b|\bSRF\b|research (?:associate|scientist|fellow|assistant)|"
                     r"non[\s-]?teaching|technical (?:support|assistant|officer)|\bstaff\b|question paper|walk-in|"
                     r"intern(?:ship)?\b|tender|engineer|librarian|registrar|scientific|medical officer|consultant|"
                     r"previous year|syllabus|scheme of exam", re.I)
NEWS = re.compile(r"\bwins?\b|\bwon\b|award|honou?r|felicitat|prize|medal|selected as|elected|appointed|nominated|"
                  r"inducted|receives?\b|recipient|conferred|commendation|congratulat|distinguished|\blectures?\b|"
                  r"seminar|workshop|conference|webinar|\bnews\b|view more|read more", re.I)
INTENT = re.compile(r"recruit|advertis|applications?\s+(?:are\s+)?invited|invit(?:es|ed|ing)\s+applications?|vacanc|"
                    r"opening|position|post of|\bposts\b|hiring|\bapply\b|call for|rolling|empanel|"
                    r"assistant professor|associate professor|professor of practice", re.I)
SKIP = re.compile(r"result|shortlist|interview schedule|screening list|selected candidates", re.I)
NAV = re.compile(r"recruitment|faculty position|faculty opening|careers|jobs|vacanc", re.I)
ADNO = re.compile(r"(?:advt\.?|advertisement|ref\.?|notification)\s*(?:no\.?|number)?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9/_.\-]{3,40})", re.I)
MON = "jan feb mar apr may jun jul aug sep oct nov dec".split()
DATE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s./\-]+([A-Za-z]{3,9}|\d{1,2})[\s./\-,]+(\d{4})\b")
DEADLINE_CTX = re.compile(r"(last date|deadline|closing|closes|apply (?:on or )?before|till|upto|up to)", re.I)
DRIVES = [("OBC", r"\bOBC\b"), ("SC/ST", r"\bSC\s*/\s*ST\b|\bSC\b|\bST\b"),
          ("EWS", r"\bEWS\b"), ("PwD", r"\bPwD\b|\bPwBD\b|persons? with disabilit"),
          ("Special Drive", r"special (?:recruitment )?drive|backlog")]

def is_faculty(text):
    return (bool(FACULTY.search(text)) and bool(INTENT.search(text))
            and not EXCLUDE.search(text) and not NEWS.search(text))

def to_iso(m):
    d, mo, y = m.groups()
    try:
        mo = int(mo) if mo.isdigit() else MON.index(mo[:3].lower()) + 1
        return dt.date(int(y), mo, int(d)).isoformat()
    except (ValueError, IndexError):
        return ""

def parse_dates(text):
    dates = [(m.start(), to_iso(m)) for m in DATE.finditer(text)]
    dates = [(p, d) for p, d in dates if d]
    deadline = ""
    for p, d in dates:
        if DEADLINE_CTX.search(text[max(0, p - 40):p]): deadline = d
    pub = next((d for _, d in dates if d != deadline), "")
    return pub, deadline

def positions(t):
    found = [p for p in ("Assistant Professor", "Associate Professor", "Professor of Practice") if re.search(p, t, re.I)]
    rest = re.sub(r"(Assistant|Associate) Professor|Professor of Practice", "", t, flags=re.I)
    if re.search(r"\bProfessor\b", rest, re.I): found.append("Professor")
    return ", ".join(found)

def clean(s): return " ".join(s.split())
def norm(s): return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

def fetch(url):
    r = requests.get(url, headers=HDR, timeout=30); r.raise_for_status()
    return BeautifulSoup(r.content, "lxml")

# ---------- PDF reading ----------
def date_after(label_rx, text, window=90):
    m = re.search(label_rx, text, re.I)
    if not m: return ""
    d = DATE.search(text[m.end(): m.end() + window])
    return to_iso(d) if d else ""

def read_pdf(url):
    r = requests.get(url, headers=HDR, timeout=40); r.raise_for_status()
    reader = PdfReader(io.BytesIO(r.content))
    return " ".join((p.extract_text() or "") for p in reader.pages[:3])

def enrich(ad):
    """Open the advert PDF and overwrite guessed fields with what the document itself says."""
    ad["pdf_parsed"] = True
    try: text = " ".join(read_pdf(ad["pdf_url"]).split())
    except Exception as e:
        print("  PDF failed:", ad["pdf_url"], e); return
    if len(text) < 50: return
    m = re.search(r"Advertisement\s*(?:No\.?|Number)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9/_.\- ]{3,60}?)(?=\s+(?:Date|Dated|Last|Closing|Advertisement)\b)", text, re.I)
    if m: ad["advertisement_number"] = m.group(1).strip(" .,")
    pub = date_after(r"(?:Date of Advertisement|Advertisement Date|Dated)\s*[:\-]?", text, 40)
    last = date_after(r"(?:Last\s+Date|Closing\s+Date|Deadline)", text)
    if pub: ad["publication_date"] = pub
    if last: ad["application_deadline"] = last
    pos = positions(text[:2500])
    if pos: ad["position"] = pos
    if re.search(DRIVES[4][1], text[:3000], re.I) and "Special Drive" not in ad["special_drive"]:
        ad["special_drive"] = ", ".join(x for x in (ad["special_drive"], "Special Drive") if x)

def set_status(ad):
    d, p = ad["application_deadline"], ad["publication_date"]
    years = [int(y) for y in re.findall(r"\b(20\d\d)\b", ad["advertisement_title"] + " " + ad["pdf_url"])]
    if d: ad["status"] = "Open" if d >= TODAY.isoformat() else "Closed"
    elif p and p < (TODAY - dt.timedelta(days=365)).isoformat(): ad["status"] = "Closed"
    elif years and max(years) <= TODAY.year - 2: ad["status"] = "Closed"
    else: ad["status"] = "Unknown"

# ---------- official pages ----------
def extract(inst, page_url, soup):
    ads = []
    for a in soup.find_all("a", href=True):
        title = clean(a.get_text(" ", strip=True))
        ctx = clean(a.parent.get_text(" ", strip=True) if a.parent else title)[:500]
        href = urljoin(page_url, a["href"])
        is_pdf = href.lower().split("?")[0].endswith(".pdf")
        if not title: continue
        generic = len(title) < 25 or re.match(r"(click|download|view|read|details|pdf|here)", title, re.I)
        basis = ctx if generic else title
        if SKIP.search(basis) or not is_faculty(basis): continue
        text = f"{title} {ctx}"
        pub, deadline = parse_dates(text)
        m = ADNO.search(text)
        ads.append({"institution": inst["name"], "position": positions(text),
                    "advertisement_number": m.group(1).rstrip(".,") if m else "",
                    "advertisement_title": title[:250], "publication_date": pub,
                    "application_deadline": deadline, "status": "Unknown",
                    "special_drive": ", ".join(n for n, rx in DRIVES if re.search(rx, text, re.I)),
                    "recruitment_url": page_url if is_pdf else href, "pdf_url": href if is_pdf else "",
                    "source_type": "official", "also_listed_on": []})
    return ads

def scrape(inst):
    pages, ads = [inst["url"]], []
    soup = fetch(inst["url"])
    if inst.get("follow"):
        for a in soup.find_all("a", href=True):
            if NAV.search(a.get_text(" ", strip=True)) and not a["href"].lower().endswith(".pdf"):
                u = urljoin(inst["url"], a["href"])
                if u not in pages and len(pages) < 4: pages.append(u)
    for i, u in enumerate(pages):
        try: ads += extract(inst, u, soup if i == 0 else fetch(u)); time.sleep(1)
        except Exception as e: print(f"  warn {u}: {e}")
    return ads

# ---------- other job websites (cross-check) ----------
def inst_patterns(names):
    pats = {}
    for n in dict.fromkeys(names):
        short = re.sub(r"^IIT\s*(?:\(ISM\))?\s*", "", n).strip()
        if "ISM" in n: rx = r"IIT\s*\(?ISM\)?|Indian Institute of Technology\s*\(?ISM\)?|ISM Dhanbad"
        else: rx = rf"IIT[\s\-]*{re.escape(short)}\b|Indian Institute of Technology[,\s\-]*{re.escape(short)}\b"
        pats[n] = re.compile(rx, re.I)
    return pats

def scrape_sources(institute_names):
    hits, errors = [], {}
    if not SOURCES.exists(): return hits, errors
    pats = inst_patterns(institute_names)
    for src in json.loads(SOURCES.read_text()):
        print("Cross-checking", src["name"])
        try: soup = fetch(src["url"])
        except Exception as e: errors["source:" + src["name"]] = str(e); continue
        for a in soup.find_all("a", href=True):
            title = clean(a.get_text(" ", strip=True))
            parent = clean(a.parent.get_text(" ", strip=True)) if a.parent else ""
            ctx = parent if len(parent) < 300 else title
            if not title or SKIP.search(title) or not is_faculty(title): continue
            inst = next((n for n, rx in pats.items() if rx.search(title)), None)
            if not inst: continue
            pub, deadline = parse_dates(f"{title} {ctx}")
            m = ADNO.search(f"{title} {ctx}")
            hits.append({"institution": inst, "title": title[:250], "text": ctx, "url": urljoin(src["url"], a["href"]),
                         "source": src["name"], "publication_date": pub, "application_deadline": deadline,
                         "advertisement_number": m.group(1).rstrip(".,") if m else ""})
    return hits, errors

def years_of(*parts): return set(re.findall(r"\b(20\d\d)\b", " ".join(parts)))

def match_score(hit, ad):
    """0 = no match. Higher = better. Same institute is required."""
    if hit["institution"] != ad["institution"]: return 0
    an = ad.get("advertisement_number", "")
    if an and len(an) > 5 and norm(an) in norm(hit["title"] + " " + hit["text"]): return 3
    ratio = difflib.SequenceMatcher(None, norm(hit["title"]), norm(ad["advertisement_title"])).ratio()
    if ratio >= 0.65: return 2 + ratio
    hp, ap = set(positions(hit["title"]).split(", ")) - {""}, set(ad["position"].split(", ")) - {""}
    hy = years_of(hit["title"]); ay = years_of(ad["publication_date"], ad["application_deadline"], ad["advertisement_title"])
    if hp & ap and (not hy or hy & ay): return 1 + ratio
    return 0

def attach_hits(seen, hits, now):
    """Official ads gain corroborating sources; unmatched hits become low-confidence 'aggregator' ads."""
    groups = []
    for h in hits:
        cands = [(match_score(h, ad), ad) for ad in seen.values() if ad.get("source_type") != "aggregator"]
        best = max(cands, key=lambda c: c[0], default=(0, None))
        match = best[1] if best[0] > 0 else None
        if match:
            if h["source"] not in match["also_listed_on"]: match["also_listed_on"].append(h["source"])
            continue
        g = next((g for g in groups if g["institution"] == h["institution"] and
                  difflib.SequenceMatcher(None, norm(g["title"]), norm(h["title"])).ratio() >= 0.75), None)
        if g:
            if h["source"] not in g["sources"]: g["sources"].append(h["source"])
        else: groups.append({**h, "sources": [h["source"]]})
    out = []
    for g in groups:
        ad = {"institution": g["institution"], "position": positions(g["title"] + " " + g["text"]),
              "advertisement_number": g["advertisement_number"], "advertisement_title": g["title"],
              "publication_date": g["publication_date"], "application_deadline": g["application_deadline"],
              "status": "Unknown", "special_drive": ", ".join(n for n, rx in DRIVES if re.search(rx, g["title"] + " " + g["text"], re.I)),
              "recruitment_url": g["url"], "pdf_url": "", "source_type": "aggregator", "also_listed_on": g["sources"]}
        set_status(ad)
        if ad["status"] != "Closed": out.append(ad)
    return out

def confidence(ad):
    official = ad.get("source_type", "official") == "official"
    s = 40 if official else 15
    if ad.get("pdf_parsed") and ad["advertisement_number"]: s += 15
    if ad["status"] == "Open": s += 10
    if ad["publication_date"] and ad["publication_date"] >= (TODAY - dt.timedelta(days=90)).isoformat(): s += 10
    n = len(ad.get("also_listed_on", [])) - (0 if official else 1)
    s += 0 if n <= 0 else 15 if n == 1 else 20
    if ad["position"]: s += 5
    if ad["status"] == "Closed": s = min(s, 25)
    return min(s, 100)

def uid(ad):
    base = ad["pdf_url"] or (ad["advertisement_number"] or ad["advertisement_title"].lower())
    return hashlib.sha1(f"{ad['institution']}|{base}".encode()).hexdigest()[:12]

def main():
    institutes = json.loads((ROOT / "scraper" / "institutes.json").read_text())
    first_run = not ADS.exists()
    old = {} if first_run else {a["id"]: a for a in json.loads(ADS.read_text()) if is_faculty(a["advertisement_title"])}
    now, changes, errors, seen, budget = TODAY.isoformat(), [], {}, {}, MAX_PDFS
    for inst in institutes:
        print("Scraping", inst["name"])
        try: found = scrape(inst)
        except Exception as e: errors[inst["name"]] = str(e); print("  FAILED", e); continue
        for ad in found:
            ad["id"] = uid(ad); prev = old.get(ad["id"])
            if prev and prev.get("pdf_parsed"):
                for k in ("advertisement_number", "publication_date", "application_deadline", "position", "special_drive"): ad[k] = prev[k]
                ad["pdf_parsed"] = True
            elif ad["pdf_url"] and budget > 0:
                budget -= 1; enrich(ad); time.sleep(0.5)
            set_status(ad)
            if not prev:
                ad.update(first_seen=now, last_seen=now, pdf_history=[ad["pdf_url"]] if ad["pdf_url"] else [])
                changes.append({"new": True, "type": "new", **{k: ad[k] for k in ("institution", "position", "advertisement_number", "application_deadline", "recruitment_url", "pdf_url", "advertisement_title", "special_drive")}})
            else:
                ad["first_seen"], ad["last_seen"], ad["pdf_history"] = prev["first_seen"], now, prev.get("pdf_history", [])
                for field, typ in (("application_deadline", "deadline_changed"), ("pdf_url", "new_pdf"), ("advertisement_title", "updated")):
                    if ad[field] and ad[field] != prev[field] and (prev.get("pdf_parsed") or not ad.get("pdf_parsed")):
                        changes.append({"new": False, "type": typ, "institution": ad["institution"], "advertisement_number": ad["advertisement_number"], "old": prev[field], "current": ad[field], "recruitment_url": ad["recruitment_url"]})
                if ad["pdf_url"] and ad["pdf_url"] not in ad["pdf_history"]: ad["pdf_history"].append(ad["pdf_url"])
            seen[ad["id"]] = ad
    for i, a in old.items():  # keep history of official ads; failed institutes stay untouched
        if i not in seen and a.get("source_type", "official") != "aggregator":
            a.setdefault("also_listed_on", []); set_status(a); seen[i] = a
    # cross-check on other job websites
    hits, src_errors = scrape_sources([i["name"] for i in institutes]); errors.update(src_errors)
    for ad in attach_hits(seen, hits, now):
        ad["id"] = uid(ad)
        if ad["id"] in seen: continue
        prev = old.get(ad["id"])
        ad.update(first_seen=prev["first_seen"] if prev else now, last_seen=now, pdf_history=[])
        seen[ad["id"]] = ad
    for ad in seen.values(): ad["confidence"] = confidence(ad)
    ads = sorted(seen.values(), key=lambda a: (a["confidence"], a["first_seen"]), reverse=True)
    ADS.write_text(json.dumps(ads, indent=1, ensure_ascii=False), encoding="utf-8")
    CHG.write_text(json.dumps({"date": now, "bootstrap": first_run, "changes": [] if first_run else changes}, indent=1, ensure_ascii=False), encoding="utf-8")
    META.write_text(json.dumps({"updated": dt.datetime.now(dt.timezone.utc).isoformat(), "institutes": len({i["name"] for i in institutes}), "errors": errors}, indent=1))
    print(f"Done: {len(ads)} ads, {len(changes)} changes, {len(errors)} failures")

if __name__ == "__main__":
    main()
