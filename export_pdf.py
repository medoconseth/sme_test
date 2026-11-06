"""Export výsledkov do PDF (A4 na šírku, slovenská diakritika cez písmo DejaVu)."""
import datetime as dt
import io
import os

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_CANDIDATES = [
    (os.path.join(HERE, "fonts", "DejaVuSans.ttf"), os.path.join(HERE, "fonts", "DejaVuSans-Bold.ttf")),
    (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]


def _fonts():
    if "SK" in pdfmetrics.getRegisteredFontNames():
        return "SK", "SK-Bold"
    for reg, bold in FONT_CANDIDATES:
        if os.path.exists(reg) and os.path.exists(bold):
            pdfmetrics.registerFont(TTFont("SK", reg))
            pdfmetrics.registerFont(TTFont("SK-Bold", bold))
            return "SK", "SK-Bold"
    return "Helvetica", "Helvetica-Bold"  # bez diakritiky, len núdzovo


def fmt(v, dec=0):
    """Slovenský formát čísla: 1 234 567,89; prázdne = –."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "–"
    try:
        s = f"{float(v):,.{dec}f}"
    except (TypeError, ValueError):
        return str(v)
    return s.replace(",", "\u00a0").replace(".", ",")


def _txt(v):
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)


def build_pdf(suhrn, prep, detail, roky):
    reg, bold = _fonts()
    base = ParagraphStyle("b", fontName=reg, fontSize=8, leading=10)
    small = ParagraphStyle("s", parent=base, fontSize=7, leading=8.5)
    h1 = ParagraphStyle("h1", fontName=bold, fontSize=15, leading=19, spaceAfter=2)
    h2 = ParagraphStyle("h2", fontName=bold, fontSize=11, leading=14, spaceBefore=10, spaceAfter=4)
    muted = ParagraphStyle("m", parent=base, textColor=colors.HexColor("#555555"))

    def table(rows, widths, num_cols=(), head_bold=True):
        data = [[Paragraph(_txt(c), small if i else ParagraphStyle("hh", parent=small, fontName=bold))
                 for c in r] for i, r in enumerate(rows)]
        t = Table(data, colWidths=widths, repeatRows=1)
        st = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF6")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B8C2CC")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ]
        t.setStyle(TableStyle(st))
        for c in num_cols:
            for r in data[1:]:
                r[c].style = ParagraphStyle("n", parent=small, alignment=2)
        return t

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm,
                            title="Údaje z Registra účtovných závierok")
    W = doc.width
    el = [
        Paragraph("Údaje z Registra účtovných závierok", h1),
        Paragraph(f"Vygenerované {dt.date.today():%d.%m.%Y} · roky {min(roky)} – {max(roky)} · "
                  f"zdroj: www.registeruz.sk", muted),
        Spacer(1, 4),
    ]

    # Zadané IČO a podiely
    firmy = detail[["IČO", "Názov", "Podiel %"]].drop_duplicates("IČO")
    el.append(Paragraph("Zadané spoločnosti", h2))
    rows = [["IČO", "Názov", "Podiel %"]] + [[r["IČO"], r["Názov"] or "–", fmt(r["Podiel %"], 2)]
                                           for _, r in firmy.iterrows()]
    el.append(table(rows, [W * .12, W * .73, W * .15], num_cols=(2,)))

    # Súhrn
    el.append(Paragraph("Súhrn za všetky IČO (prepočítané podielom)", h2))
    rows = [["Rok", "Počet zamestnancov", "Ročný obrat (EUR)", "Bilančná suma (EUR)", "Chýbajúce údaje"]]
    for _, r in suhrn.iterrows():
        rows.append([int(r["Rok"]), fmt(r["Počet zamestnancov"], 2), fmt(r["Ročný obrat (EUR)"]),
                     fmt(r["Bilančná suma (EUR)"]), r["Chýbajúce údaje"]])
    el.append(table(rows, [W * .08, W * .17, W * .2, W * .2, W * .35], num_cols=(1, 2, 3)))

    # Prepočet podľa IČO
    el.append(Paragraph("Prepočítané hodnoty podľa IČO", h2))
    rows = [["IČO", "Názov", "Rok", "Podiel %", "Zamestnanci", "Obrat (EUR)", "Bilančná suma (EUR)"]]
    for _, r in prep.sort_values(["IČO", "Rok"]).iterrows():
        rows.append([r["IČO"], r["Názov"] or "–", int(r["Rok"]), fmt(r["Podiel %"], 2),
                     fmt(r["Počet zamestnancov"], 2), fmt(r["Ročný obrat (EUR)"]),
                     fmt(r["Bilančná suma (EUR)"])])
    el.append(table(rows, [W * .09, W * .3, W * .06, W * .09, W * .12, W * .17, W * .17],
                    num_cols=(3, 4, 5, 6)))

    # Detail z registra
    el.append(Paragraph("Detail z registra (hodnoty pred prepočtom)", h2))
    rows = [["IČO", "Rok", "Sústava", "Zamestnanci", "Obrat (EUR)", "Bilančná suma (EUR)",
             "Zdroj počtu zamestnancov", "Poznámka"]]
    for _, r in detail.sort_values(["IČO", "Rok"]).iterrows():
        rows.append([r["IČO"], int(r["Rok"]), _txt(r.get("Sústava")) or "–",
                     fmt(r["Počet zamestnancov"], 2), fmt(r["Ročný obrat (EUR)"]),
                     fmt(r["Bilančná suma (EUR)"]), _txt(r.get("Zdroj počtu zamestnancov")),
                     _txt(r.get("Poznámka"))])
    el.append(table(rows, [W * .07, W * .045, W * .12, W * .075, W * .1, W * .1, W * .26, W * .23],
                    num_cols=(3, 4, 5)))

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont(reg, 7)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawRightString(d.pagesize[0] - 12 * mm, 7 * mm, f"Strana {d.page}")
        canvas.restoreState()

    doc.build(el, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
