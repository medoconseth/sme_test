"""
Aplikácia: údaje z Registra účtovných závierok podľa IČO
Spustenie:  python -m streamlit run app.py
"""
import datetime as dt
import io

import pandas as pd
import streamlit as st

from export_pdf import build_pdf
from ruz_core import RuzClient, clean_ico, fetch_company

st.set_page_config(page_title="Údaje z RÚZ podľa IČO", layout="wide")
st.title("Údaje z Registra účtovných závierok")
st.caption(
    "Počet zamestnancov, ročný obrat a bilančná suma za posledných 5 rokov "
    "a súhrn prepočítaný zadaným percentom (podielom) za všetky IČO."
)

# ---------------------------------------------------------------- vstupy
st.subheader("1. Zadanie IČO a percent")
if "vstup" not in st.session_state:
    st.session_state.vstup = pd.DataFrame({"IČO": [""], "Podiel %": [100.0]})

vstup = st.data_editor(
    st.session_state.vstup,
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "IČO": st.column_config.TextColumn("IČO", help="8-miestne IČO"),
        "Podiel %": st.column_config.NumberColumn("Podiel %", min_value=0.0, max_value=100.0,
                                                  step=0.01, format="%.2f"),
    },
    key="vstup_editor",
)

c1, c2, c3 = st.columns(3)
posledny_rok = c1.number_input("Posledný sledovaný rok", 2000, 2100, dt.date.today().year - 1)
pocet_rokov = c2.number_input("Počet rokov", 1, 15, 5)
citat_pdf = c3.checkbox("Hľadať počet zamestnancov v PDF poznámkach", value=True)
roky = list(range(int(posledny_rok) - int(pocet_rokov) + 1, int(posledny_rok) + 1))

# ---------------------------------------------------------------- načítanie
if st.button("Načítať údaje z registra", type="primary"):
    zoznam = []
    for _, r in vstup.iterrows():
        ico = clean_ico(r.get("IČO"))
        if ico:
            podiel = r.get("Podiel %")
            zoznam.append((ico, 100.0 if pd.isna(podiel) else float(podiel)))
    if not zoznam:
        st.warning("Zadajte aspoň jedno IČO.")
    else:
        client = RuzClient()
        riadky, stav = [], st.status("Načítavam údaje z registra…", expanded=True)
        bar = stav.progress(0.0)
        for i, (ico, podiel) in enumerate(zoznam):
            stav.write(f"IČO {ico}…")
            try:
                res = fetch_company(client, ico, roky, read_pdfs=citat_pdf)
            except Exception as e:
                res = {"ico": ico, "nazov": None, "chyba": str(e), "link": None, "roky": {}}
            if res["chyba"]:
                stav.write(f"⚠️ {ico}: {res['chyba']}")
            for rok in roky:
                d = res["roky"].get(rok, {})
                riadky.append({
                    "IČO": ico,
                    "Názov": res["nazov"],
                    "Podiel %": podiel,
                    "Rok": rok,
                    "Sústava": d.get("sustava"),
                    "Počet zamestnancov": d.get("zamestnanci"),
                    "Ročný obrat (EUR)": d.get("obrat"),
                    "Bilančná suma (EUR)": d.get("bilancna"),
                    "Zdroj počtu zamestnancov": d.get("zdroj_zamestnancov"),
                    "Poznámky (PDF)": d.get("odkaz_pdf"),
                    "Obdobie": d.get("obdobie"),
                    "Poznámka": "; ".join(d.get("poznamka", [])) or res["chyba"],
                    "Odkaz do RÚZ": res["link"],
                })
            bar.progress((i + 1) / len(zoznam))
        stav.update(label="Hotovo", state="complete", expanded=False)
        st.session_state.detail = pd.DataFrame(riadky)

# ---------------------------------------------------------------- výsledky
if "detail" in st.session_state:
    st.subheader("2. Detail podľa IČO a roka")
    st.caption("Chýbajúce alebo nesprávne hodnoty môžete priamo opraviť v tabuľke – súhrn sa prepočíta.")
    num = lambda f: st.column_config.NumberColumn(format=f)
    detail = st.data_editor(
        st.session_state.detail,
        use_container_width=True,
        hide_index=True,
        disabled=["IČO", "Názov", "Rok", "Obdobie", "Odkaz do RÚZ", "Poznámky (PDF)"],
        column_config={
            "Podiel %": num("%.2f"),
            "Počet zamestnancov": num("%.2f"),
            "Ročný obrat (EUR)": num("%.0f"),
            "Bilančná suma (EUR)": num("%.0f"),
            "Odkaz do RÚZ": st.column_config.LinkColumn("Odkaz do RÚZ", display_text="otvoriť"),
            "Poznámky (PDF)": st.column_config.LinkColumn(
                "Poznámky (PDF)", display_text="PDF",
                help="PDF, z ktorého sa prečítal počet zamestnancov – na overenie"),
        },
        key="detail_editor",
    )

    params = ["Počet zamestnancov", "Ročný obrat (EUR)", "Bilančná suma (EUR)"]
    prep = detail[["IČO", "Názov", "Rok", "Podiel %"]].copy()
    for p in params:
        prep[p] = pd.to_numeric(detail[p], errors="coerce") * detail["Podiel %"] / 100

    st.subheader("3. Súhrn za všetky IČO (prepočítané podielom)")
    suhrn = prep.groupby("Rok")[params].sum(min_count=1).reset_index()
    chyby = detail.groupby("Rok")[params].apply(lambda g: g.isna().sum())
    suhrn["Chýbajúce údaje"] = [
        ", ".join(f"{p.split(' (')[0]}: {int(n)}" for p, n in chyby.loc[r].items() if n) or "–"
        for r in suhrn["Rok"]
    ]
    st.dataframe(
        suhrn, use_container_width=True, hide_index=True,
        column_config={"Rok": st.column_config.NumberColumn(format="%d"),
                       "Počet zamestnancov": num("%.2f"),
                       "Ročný obrat (EUR)": num("%.0f"),
                       "Bilančná suma (EUR)": num("%.0f")},
    )
    if (suhrn["Chýbajúce údaje"] != "–").any():
        st.warning("Pri niektorých rokoch chýbajú údaje – súčet je neúplný. Doplňte ich v tabuľke detailu.")

    with st.expander("Prepočítané hodnoty podľa IČO"):
        st.dataframe(prep, use_container_width=True, hide_index=True)

    st.subheader("4. Export")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        suhrn.to_excel(xw, sheet_name="Súhrn", index=False)
        prep.to_excel(xw, sheet_name="Prepočet podľa IČO", index=False)
        detail.to_excel(xw, sheet_name="Detail z RÚZ", index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(
                    60, max(10, max(len(str(c.value or "")) for c in col) + 2))
    stamp = f"{dt.date.today():%Y%m%d}"
    e1, e2 = st.columns(2)
    e1.download_button("Stiahnuť Excel (.xlsx)", buf.getvalue(),
                       file_name=f"RUZ_udaje_{stamp}.xlsx", use_container_width=True,
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    try:
        pdf_bytes = build_pdf(suhrn, prep, detail, sorted(detail["Rok"].unique()))
        e2.download_button("Stiahnuť PDF", pdf_bytes, file_name=f"RUZ_udaje_{stamp}.pdf",
                           mime="application/pdf", use_container_width=True)
    except Exception as e:
        e2.error(f"PDF sa nepodarilo vytvoriť: {e}")
