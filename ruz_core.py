"""
Jadro aplikácie: získavanie údajov z Registra účtovných závierok (RÚZ)
cez verejné API https://www.registeruz.sk/cruz-public/api/...

Postup pre jedno IČO:
  uctovne-jednotky?ico=  -> ID účtovnej jednotky
  uctovna-jednotka?id=   -> názov + zoznam ID závierok
  uctovna-zavierka?id=   -> obdobie, typ, zoznam ID výkazov
  uctovny-vykaz?id=      -> dáta tabuliek (+ prílohy PDF)
  sablona?id=            -> popis riadkov a stĺpcov tabuliek
"""

import io
import re
import time
import unicodedata

import requests

BASE = "https://www.registeruz.sk/cruz-public"
API = BASE + "/api"
ATTACHMENT_URL = BASE + "/domain/financialreport/attachment/{id}"
UNIT_URL = BASE + "/domain/accountingentity/show/{id}"

try:
    from pypdf import PdfReader  # voliteľné – čítanie poznámok v PDF
except Exception:  # pragma: no cover
    PdfReader = None


# --------------------------------------------------------------------------
# Pomocné funkcie
# --------------------------------------------------------------------------
def norm(text):
    """Malé písmená, bez diakritiky, zjednotené medzery."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t.lower()).strip()


def sk_text(obj):
    """Text v šablóne je buď reťazec, alebo {'sk': '...', 'en': '...'}."""
    if isinstance(obj, dict):
        return obj.get("sk") or next(iter(obj.values()), "")
    return obj or ""


def to_num(value):
    """Prevod hodnoty z výkazu na číslo. Prázdna bunka = 0."""
    if value is None:
        return None
    s = str(value).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if s in ("", "-"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def clean_ico(ico):
    digits = re.sub(r"\D", "", str(ico or ""))
    return digits.zfill(8) if digits else ""


# --------------------------------------------------------------------------
# Klient API
# --------------------------------------------------------------------------
class RuzClient:
    def __init__(self, delay=0.1, timeout=40, retries=3):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "RUZ-zber-udajov/1.0"
        self.delay, self.timeout, self.retries = delay, timeout, retries
        self._templates = {}

    def get(self, endpoint, **params):
        last = None
        for attempt in range(self.retries):
            try:
                r = self.s.get(f"{API}/{endpoint}", params=params, timeout=self.timeout)
                r.raise_for_status()
                time.sleep(self.delay)
                return r.json()
            except Exception as e:  # sieť / preťaženie servera
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Chyba pri volaní API {endpoint} {params}: {last}")

    def unit_ids(self, ico):
        data = self.get("uctovne-jednotky", **{"zmenene-od": "2000-01-01", "ico": ico})
        return data.get("id", []) or []

    def unit(self, uid):
        return self.get("uctovna-jednotka", id=uid)

    def closure(self, cid):
        return self.get("uctovna-zavierka", id=cid)

    def report(self, rid):
        return self.get("uctovny-vykaz", id=rid)

    def template(self, tid):
        if tid not in self._templates:
            self._templates[tid] = self.get("sablona", id=tid)
        return self._templates[tid]

    def attachment(self, aid):
        r = self.s.get(ATTACHMENT_URL.format(id=aid), timeout=self.timeout * 2)
        r.raise_for_status()
        time.sleep(self.delay)
        return r.content


# --------------------------------------------------------------------------
# Práca so šablónou a dátami výkazu
# --------------------------------------------------------------------------
def data_column_labels(tbl):
    """Texty hlavičiek pre dátové stĺpce (spojené cez všetky riadky hlavičky)."""
    n = tbl.get("pocetStlpcov") or 0
    nd = tbl.get("pocetDatovychStlpcov") or 0
    first = n - nd + 1
    labels = {c: "" for c in range(first, n + 1)}
    for h in tbl.get("hlavicka", []) or []:
        st, w = h.get("stlpec", 0), h.get("sirkaStlpca", 1) or 1
        for c in range(st, st + w):
            if c in labels:
                labels[c] += " " + sk_text(h.get("text"))
    return [norm(labels[c]) for c in sorted(labels)]


def current_period_column(tbl):
    """Index dátového stĺpca 'bežné účtovné obdobie' (pri súvahe Netto)."""
    labels = data_column_labels(tbl)
    for i, t in enumerate(labels):
        if "bezn" in t and "netto" in t:
            return i
    for i, t in enumerate(labels):
        if "bezn" in t and "brutto" not in t and "korekc" not in t:
            return i
    return 2 if (tbl.get("pocetDatovychStlpcov") == 4) else 0


def find_row(template, cislo, row_kw, table_hint=None):
    """Nájde (index tabuľky, poradie riadku) podľa čísla riadku a textu."""
    tables = template.get("tabulky", []) or []
    for use_hint in ([True, False] if table_hint else [False]):
        for ti, tbl in enumerate(tables):
            if use_hint and not re.search(table_hint, norm(sk_text(tbl.get("nazov")))):
                continue
            for pos, row in enumerate(tbl.get("riadky", []) or []):
                if row.get("cisloRiadku") == cislo and re.search(row_kw, norm(sk_text(row.get("text")))):
                    return ti, pos
    return None


def cell_value(report, template, ti, pos, col=None):
    tbl = template["tabulky"][ti]
    nd = tbl.get("pocetDatovychStlpcov") or 1
    col = current_period_column(tbl) if col is None else col
    try:
        data = report["obsah"]["tabulky"][ti]["data"]
    except (KeyError, IndexError, TypeError):
        return None
    idx = pos * nd + col
    return to_num(data[idx]) if idx < len(data) else None


# Pravidlá podľa sústavy účtovníctva: (číslo riadku, kľúčové slovo v texte riadku, nápoveda názvu tabuľky)
SPECS = [
    ("Jednoduché účtovníctvo (UZFO)", {
        "obrat": [(1, r"predaj tovar", r"prijm"), (2, r"predaj vyrobkov", r"prijm")],
        "bilancna": [(15, r"majetok celkom", r"majet")],
    }),
    ("Mikroúčtovná jednotka (UZMUJ)", {
        "obrat": [(2, r"trzby z predaja tovar", r"ziskov|strat"),
                  (3, r"trzby z predaja vlastnych vyrobkov", r"ziskov|strat")],
        "bilancna": [(1, r"spolu majetok", r"aktiv")],
    }),
    ("Podvojné účtovníctvo (UZPOD)", {
        "obrat": [(1, r"cisty obrat", r"ziskov|strat")],
        "bilancna": [(1, r"spolu majetok", r"aktiv")],
    }),
]


def evaluate_report(report, template):
    """Ak výkaz zodpovedá niektorej sústave, vráti (sústava, obrat, bilančná suma)."""
    for name, spec in SPECS:
        found = {k: [find_row(template, *r) for r in rows] for k, rows in spec.items()}
        if all(loc is not None for locs in found.values() for loc in locs):
            vals = {}
            for key, locs in found.items():
                parts = [cell_value(report, template, ti, pos) for ti, pos in locs]
                vals[key] = None if any(p is None for p in parts) else sum(parts)
            return name, vals["obrat"], vals["bilancna"]
    return None


# --------------------------------------------------------------------------
# Počet zamestnancov
# --------------------------------------------------------------------------
_DATE = re.compile(r"\b\d{1,2}\s?\.\s?\d{1,2}\s?\.\s?\d{2,4}\b")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_NUM = re.compile(r"(?<![\d.,])(\d{1,6}(?:[.,]\d{1,2})?)(?![\d])")
_EMP = re.compile(r"(pocet\s+(?:[a-z]+\s+){0,3}?zamestnanc[a-z]*)")


def _rank(context):
    """0 = prepočítaný, 1 = priemerný, 2 = iný (napr. stav k dátumu)."""
    if "prepocitan" in context:
        return 0
    if "priemern" in context:
        return 1
    return 2


RANK_LABEL = {0: "prepočítaný počet", 1: "priemerný počet", 2: "počet zamestnancov"}


def employees_from_text(raw_text):
    """Hľadá v texte poznámok počet zamestnancov. Vracia (hodnota, druh, úryvok) alebo None."""
    text = norm(raw_text)
    best = None
    for m in _EMP.finditer(text):
        phrase = m.group(1)
        if "veduc" in phrase:
            continue
        before = text[max(0, m.start() - 40):m.start()]
        after = text[m.end():m.end() + 160]
        rank = _rank(before + " " + phrase)
        scan = _YEAR.sub(" ", _DATE.sub(" ", after))
        scan = re.split(r"pocet\s", scan)[0]  # nečítať čísla ďalšieho údaju
        num = _NUM.search(scan)
        if not num:
            continue
        val = float(num.group(1).replace(",", "."))
        snippet = (before[-25:] + phrase + after[:60]).strip()
        if best is None or rank < best[1]:
            best = (val, rank, snippet)
        if rank == 0:
            break
    if best is None:
        return None
    return best[0], RANK_LABEL[best[1]], best[2]


def employees_from_structured(report, template):
    """Ak sú poznámky podané ako štruktúrovaný výkaz s riadkom o zamestnancoch."""
    best = None
    for ti, tbl in enumerate(template.get("tabulky", []) or []):
        for pos, row in enumerate(tbl.get("riadky", []) or []):
            t = norm(sk_text(row.get("text")))
            if "zamestnan" in t and "pocet" in t and "veduc" not in t:
                v = cell_value(report, template, ti, pos)
                if v:
                    rank = _rank(t)
                    if best is None or rank < best[1]:
                        best = (v, rank, sk_text(row.get("text")))
    if best is None:
        return None
    return best[0], RANK_LABEL[best[1]], best[2]


def pdf_text(content):
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Hlavná funkcia pre jedno IČO
# --------------------------------------------------------------------------
TYPE_PRIORITY = {"riadna": 0, "mimoriadna": 1}


def pick_closures(closures):
    """Pre každý rok (podľa konca obdobia) vyberie jednu závierku, prednostne riadnu."""
    by_year = {}
    for c in closures:
        end = c.get("obdobieDo") or ""
        if len(end) < 4 or not end[:4].isdigit():
            continue
        year = int(end[:4])
        key = (TYPE_PRIORITY.get(norm(c.get("typ")), 2), -(c.get("id") or 0))
        if year not in by_year or key < by_year[year][0]:
            by_year[year] = (key, c)
    return {y: v[1] for y, v in by_year.items()}


def fetch_company(client, ico, years, read_pdfs=True, log=lambda m: None):
    ico = clean_ico(ico)
    result = {"ico": ico, "nazov": None, "chyba": None, "link": None, "roky": {}}
    if not ico:
        result["chyba"] = "Neplatné IČO"
        return result

    unit_ids = client.unit_ids(ico)
    if not unit_ids:
        result["chyba"] = "IČO sa v registri účtovných závierok nenašlo"
        return result

    units = [client.unit(u) for u in unit_ids]
    named = [u for u in units if u.get("nazovUJ")]
    main = max(named or units, key=lambda u: len(u.get("idUctovnychZavierok") or []))
    result["nazov"] = main.get("nazovUJ")
    result["link"] = UNIT_URL.format(id=main.get("id"))
    log(f"{ico}: {result['nazov']}")

    closure_ids = sorted({c for u in units for c in (u.get("idUctovnychZavierok") or [])})
    closures = [client.closure(c) for c in closure_ids]
    picked = pick_closures(closures)

    for year in years:
        row = {"obdobie": None, "sustava": None, "obrat": None, "bilancna": None,
               "zamestnanci": None, "zdroj_zamestnancov": None, "poznamka": []}
        result["roky"][year] = row
        c = picked.get(year)
        if not c:
            row["poznamka"].append("Za tento rok nie je v registri závierka")
            continue
        row["obdobie"] = f"{c.get('obdobieOd', '')} – {c.get('obdobieDo', '')}"
        if norm(c.get("typ")) != "riadna":
            row["poznamka"].append(f"Použitá {c.get('typ', '').lower()} závierka")

        reports = []
        for rid in c.get("idUctovnychVykazov") or []:
            try:
                reports.append(client.report(rid))
            except Exception as e:
                row["poznamka"].append(f"Výkaz {rid} sa nepodarilo načítať ({e})")

        attachments = []
        for rep in reports:
            attachments += rep.get("prilohy") or []
            if not rep.get("obsah"):
                continue
            tid = rep.get("idSablony")
            if not tid:
                continue
            tpl = client.template(tid)
            if row["sustava"] is None:
                ev = evaluate_report(rep, tpl)
                if ev:
                    row["sustava"], row["obrat"], row["bilancna"] = ev
            if row["zamestnanci"] is None:
                emp = employees_from_structured(rep, tpl)
                if emp:
                    row["zamestnanci"] = emp[0]
                    row["zdroj_zamestnancov"] = f"výkaz – {emp[1]}: {emp[2]}"

        if row["sustava"] is None:
            if any(norm(r.get("pristupnostDat")) == "neverejne" for r in reports):
                row["poznamka"].append("Výkazy nie sú verejné")
            else:
                row["poznamka"].append("Výkazy nie sú v štruktúrovanej podobe alebo ide o iný vzor – doplňte ručne")

        if row["zamestnanci"] is None and read_pdfs:
            if PdfReader is None:
                row["poznamka"].append("Chýba knižnica pypdf – poznámky v PDF sa nečítali")
            for a in attachments:
                if "pdf" not in (a.get("mimeType") or "pdf").lower():
                    continue
                try:
                    emp = employees_from_text(pdf_text(client.attachment(a["id"])))
                except Exception:
                    emp = None
                if emp:
                    row["zamestnanci"] = emp[0]
                    row["zdroj_zamestnancov"] = f"PDF {a.get('meno', '')} – {emp[1]}: „{emp[2]}“"
                    break

        if row["zamestnanci"] is None:
            row["poznamka"].append("Počet zamestnancov sa v poznámkach nenašiel (napr. skenované PDF) – doplňte ručne")

    return result
