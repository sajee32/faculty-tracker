"""Daily scraper: fetch pages -> extract faculty ads -> read advert PDFs -> diff -> write docs/data/*.json"""
import hashlib, io, json, re, time, datetime as dt
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"; DATA.mkdir(parents=True, exist_ok=True)
ADS, CHG, META = DATA / "ads.json", DATA / "changes.json", DATA / "meta.json"
HDR = {"User-Agent": "Mozilla/5.0 (compatible; FacultyTracker/1.0)"}
TODAY = dt.date.today()
MAX_PDFS = 80  # PDFs read per run; a backlog is worked through over a few daily runs

FACULTY = re.compile(r"faculty|professor|lecturer|teaching (?:position|post)", re.I)
EXCLUDE = re.compile(r"project|post-?\s?doc|postdoctoral|\bJRF\b|\bSRF\b|research (?:associate|scientist|fellow|assistant)|"
                     r"non[\s-]?teaching|technical (?:support|assistant|officer)|\bstaff\b|question paper|walk-in|"
                     r"intern(?:ship)?\b|tender|engineer|librarian|registrar|scientific|medical officer|consultant|"
                     r"previous year|syllabus|scheme of exam", re.I)
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
    return bool(FACULTY.search(text)) and not EXCLUDE.search(text)

def to_iso(m):
    d, mo, y = m.groups()
    try:
        mo = int(mo) if mo.isdigit() else MON.index(mo[:3].lower()) + 1
        return dt.date(int(y), mo, int(d)).isoformat()
    except (ValueError, IndexError):
        return ""

def positions(t):
    found = [p for p in ("Assistant Professor", "Associate Professor", "Professor of Practice") if re.search(p, t, re.I)]
    rest = re.sub(r"(Assistant|Associate) Professor|Professor of Practice", "", t, flags=re.I)
    if re.search(r"\bProfessor\b", rest, re.I): found.append("Professor")
    return ", ".join(found)

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
    if len(text) < 50: return  # scanned image PDF: nothing to read
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

# ---------- page scraping ----------
def extract(inst, page_url, soup):
    ads = []
    for a in soup.find_all("a", href=True):
        title = " ".join(a.get_text(" ", strip=True).split())
        ctx = " ".join((a.parent.get_text(" ", strip=True) if a.parent else title).split())[:500]
        href = urljoin(page_url, a["href"])
        is_pdf = href.lower().split("?")[0].endswith(".pdf")
        if not title: continue
        generic = len(title) < 25 or re.match(r"(click|download|view|read|details|pdf|here)", title, re.I)
        basis = ctx if generic else title
        if SKIP.search(basis) or not is_faculty(basis): continue
        text = f"{title} {ctx}"
        dates = [(m.start(), to_iso(m)) for m in DATE.finditer(text)]
        dates = [(p, d) for p, d in dates if d]
        deadline = ""
        for p, d in dates:
            if DEADLINE_CTX.search(text[max(0, p - 40):p]): deadline = d
        pub = next((d for _, d in dates if d != deadline), "")
        m = ADNO.search(text)
        ads.append({"institution": inst["name"], "position": positions(text),
                    "advertisement_number": m.group(1).rstrip(".,") if m else "",
                    "advertisement_title": title[:250], "publication_date": pub,
                    "application_deadline": deadline, "status": "Unknown",
                    "special_drive": ", ".join(n for n, rx in DRIVES if re.search(rx, text, re.I)),
                    "recruitment_url": page_url if is_pdf else href, "pdf_url": href if is_pdf else ""})
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
    for i, a in old.items():  # keep history; failed institutes stay untouched
        if i not in seen:
            set_status(a); seen[i] = a
    ads = sorted(seen.values(), key=lambda a: (a["first_seen"], a["institution"]), reverse=True)
    ADS.write_text(json.dumps(ads, indent=1, ensure_ascii=False), encoding="utf-8")
    CHG.write_text(json.dumps({"date": now, "bootstrap": first_run, "changes": [] if first_run else changes}, indent=1, ensure_ascii=False), encoding="utf-8")
    META.write_text(json.dumps({"updated": dt.datetime.now(dt.timezone.utc).isoformat(), "institutes": len(institutes), "errors": errors}, indent=1))
    print(f"Done: {len(ads)} ads, {len(changes)} changes, {len(errors)} failures")

if __name__ == "__main__":
    main()
