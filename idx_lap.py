#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IDX REPORT BOT - Laporan keuangan + aksi korporasi emiten saham Indonesia -> PDF
================================================================================
Isi laporan:
  1. Harga & ringkasan cepat (bahasa trader)
  2. Laporan keuangan (Aset, Liabilitas, Ekuitas, Pendapatan, Laba Kotor,
     Laba Operasi, Laba Bersih, EPS, Kas, Utang, Arus Kas Operasi, FCF)
  3. Rasio: GPM, OPM, NPM, ROE, ROA, DER, PBV, PER, Current Ratio, growth QoQ/YoY
  4. Aksi korporasi: Akuisisi, Private Placement (PMTHMETD), Right Issue (PMHMETD),
     RUPS/RUPSLB (+ agenda/tambahan agenda kalau PDF-nya bisa dibaca), Dividen,
     Buyback, Stock Split, Waran/MESOP, Suspensi/UMA, dll
  5. Berita terkait (Google News RSS)

Install (Termux / Linux / Windows):
    pip install requests reportlab
    pip install pypdf          # opsional: baca agenda RUPS dari lampiran PDF IDX
    pip install curl_cffi      # opsional: bantu tembus proteksi Cloudflare situs IDX

Contoh pakai:
    python idx_report.py BBCA
    python idx_report.py BBCA BBRI TLKM --days 365
    python idx_report.py KOKA --watch 300          # realtime: cek ulang tiap 5 menit
    python idx_report.py BBCA --out laporan --all  # --all = tampilkan juga pengumuman 'Lainnya'

Catatan: ini alat bantu riset, BUKAN rekomendasi beli/jual. Selalu cek ulang ke
dokumen resmi di idx.co.id sebelum ambil keputusan.
"""
import argparse
import email.utils
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import quote
from xml.sax.saxutils import escape

try:
    import requests
except ImportError:
    sys.exit("Install dulu: pip install requests reportlab")

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)
except ImportError:
    sys.exit("Install dulu: pip install requests reportlab")

# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
})


def http_get(url, params=None, headers=None, timeout=20, retries=2, kind="json"):
    """GET dengan retry. kind: json | text | bytes"""
    last = "unknown"
    for i in range(retries + 1):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                if kind == "json":
                    return r.json()
                return r.text if kind == "text" else r.content
            last = "HTTP %s" % r.status_code
            if r.status_code in (403, 404):
                break
        except Exception as e:  # noqa
            last = str(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(last)


def idx_get(url, params):
    """Situs IDX sering diproteksi Cloudflare. Coba curl_cffi dulu (kalau ada)."""
    ref = {"Referer": "https://www.idx.co.id/id/perusahaan-tercatat/pengumuman/"}
    try:
        from curl_cffi import requests as cr  # type: ignore
    except ImportError:
        cr = None
    if cr is not None:
        r = cr.get(url, params=params, impersonate="chrome", timeout=25, headers=ref)
        if r.status_code == 200:
            return r.json()
        raise RuntimeError("IDX HTTP %s" % r.status_code)
    return http_get(url, params=params, headers=ref)


# ----------------------------------------------------------------------------
# Format angka (gaya Indonesia)
# ----------------------------------------------------------------------------
def num(x, d=2):
    if x is None:
        return "-"
    s = "{:,.{d}f}".format(x, d=d)
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def rp(x):
    """Rupiah ringkas: Rp 1,23 T / 456,7 M / 12 Jt"""
    if x is None:
        return "-"
    a = abs(x)
    sign = "-" if x < 0 else ""
    if a >= 1e12:
        return "%sRp %s T" % (sign, num(a / 1e12, 2))
    if a >= 1e9:
        return "%sRp %s M" % (sign, num(a / 1e9, 1))
    if a >= 1e6:
        return "%sRp %s Jt" % (sign, num(a / 1e6, 1))
    return "%sRp %s" % (sign, num(a, 0))


def pct(x, d=1):
    return "-" if x is None else "%s%%" % num(x, d)


def growth(new, old):
    if new is None or old in (None, 0):
        return None
    return (new - old) / abs(old) * 100


def safe(s):
    """Font bawaan PDF hanya latin-1/cp1252; buang karakter aneh."""
    return str(s).encode("cp1252", "replace").decode("cp1252")


# ----------------------------------------------------------------------------
# Data harga (realtime) + fundamental
# ----------------------------------------------------------------------------
def yf_symbol(code):
    code = code.upper().strip()
    return code if code.endswith(".JK") else code + ".JK"


def get_price(code):
    d = http_get("https://query1.finance.yahoo.com/v8/finance/chart/%s" % yf_symbol(code),
                 params={"interval": "1d", "range": "1y"})
    res = d["chart"]["result"][0]
    m = res["meta"]
    closes = [c for c in res["indicators"]["quote"][0].get("close", []) if c is not None]
    price = m.get("regularMarketPrice")
    prev = closes[-2] if len(closes) >= 2 else None
    ts = m.get("regularMarketTime")
    return {
        "name": m.get("longName") or m.get("shortName") or code.upper(),
        "price": price,
        "prev": prev,
        "chg_pct": growth(price, prev),
        "day_high": m.get("regularMarketDayHigh"),
        "day_low": m.get("regularMarketDayLow"),
        "volume": m.get("regularMarketVolume"),
        "w52_high": m.get("fiftyTwoWeekHigh"),
        "w52_low": m.get("fiftyTwoWeekLow"),
        "time": datetime.fromtimestamp(ts).strftime("%d-%m-%Y %H:%M") if ts else "-",
    }


FIELDS = ["TotalAssets", "TotalLiabilitiesNetMinorityInterest", "StockholdersEquity",
          "TotalRevenue", "GrossProfit", "OperatingIncome",
          "NetIncomeCommonStockholders", "DilutedEPS", "OrdinarySharesNumber",
          "CashAndCashEquivalents", "TotalDebt", "CurrentAssets",
          "CurrentLiabilities", "OperatingCashFlow", "FreeCashFlow"]


def get_fundamentals(code):
    types = []
    for k in FIELDS:
        types += ["quarterly" + k, "annual" + k]
    now = int(time.time())
    d = http_get(
        "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/%s"
        % yf_symbol(code),
        params={"type": ",".join(types), "period1": now - 6 * 365 * 86400,
                "period2": now + 86400, "merge": "false", "lang": "id-ID", "region": "ID"})
    out = {}
    for r in d["timeseries"]["result"]:
        t = r["meta"]["type"][0]
        pts = []
        for it in r.get(t) or []:
            rv = (it or {}).get("reportedValue") or {}
            if rv.get("raw") is not None:
                pts.append((it["asOfDate"], rv["raw"]))
        if pts:
            out[t] = sorted(pts)
    return out


def latest_of(f, k):
    """Nilai terbaru: bandingkan kuartalan vs tahunan, ambil yang tanggalnya paling baru."""
    cands = [s[-1] for s in (f.get("quarterly" + k), f.get("annual" + k)) if s]
    return max(cands, key=lambda x: x[0]) if cands else (None, None)


def ttm_or_fy(f, k):
    q = f.get("quarterly" + k) or []
    if len(q) >= 4:
        return sum(v for _, v in q[-4:]), "TTM"
    a = f.get("annual" + k) or []
    if a:
        return a[-1][1], "FY" + a[-1][0][:4]
    return None, None


def compute(f, price):
    m = {}
    g = lambda k: latest_of(f, k)[1]  # noqa
    m["period"] = max([latest_of(f, "TotalAssets")[0] or "", latest_of(f, "StockholdersEquity")[0] or ""]) or "-"
    assets, liab, equity = g("TotalAssets"), g("TotalLiabilitiesNetMinorityInterest"), g("StockholdersEquity")
    if liab is None and assets is not None and equity is not None:
        liab = assets - equity
    shares = g("OrdinarySharesNumber")
    ni, basis = ttm_or_fy(f, "NetIncomeCommonStockholders")
    rev, _ = ttm_or_fy(f, "TotalRevenue")
    gp, _ = ttm_or_fy(f, "GrossProfit")
    op, _ = ttm_or_fy(f, "OperatingIncome")
    eps, _ = ttm_or_fy(f, "DilutedEPS")
    if eps is None and ni is not None and shares:
        eps = ni / shares
    bvps = equity / shares if equity is not None and shares else None
    m.update(basis=basis, assets=assets, liab=liab, equity=equity, shares=shares, ni=ni,
             rev=rev, gp=gp, op=op, eps=eps, bvps=bvps)
    m["gpm"] = gp / rev * 100 if gp is not None and rev else None
    m["opm"] = op / rev * 100 if op is not None and rev else None
    m["npm"] = ni / rev * 100 if ni is not None and rev else None
    m["roe"] = ni / equity * 100 if ni is not None and equity else None
    m["roa"] = ni / assets * 100 if ni is not None and assets else None
    m["der"] = liab / equity if liab is not None and equity else None
    ca, cl = g("CurrentAssets"), g("CurrentLiabilities")
    m["cr"] = ca / cl if ca is not None and cl else None
    m["pbv"] = price / bvps if price and bvps and bvps > 0 else None
    m["per"] = price / eps if price and eps and eps > 0 else None
    m["eps_yield"] = eps / price * 100 if price and eps else None
    # pertumbuhan
    for key, fk in (("rev", "TotalRevenue"), ("ni", "NetIncomeCommonStockholders")):
        q = f.get("quarterly" + fk) or []
        m[key + "_qoq"] = growth(q[-1][1], q[-2][1]) if len(q) >= 2 else None
        m[key + "_yoy"] = growth(q[-1][1], q[-5][1]) if len(q) >= 5 else None
        if m[key + "_yoy"] is None:
            a = f.get("annual" + fk) or []
            m[key + "_yoy"] = growth(a[-1][1], a[-2][1]) if len(a) >= 2 else None
            m[key + "_yoy_note"] = "tahunan" if m[key + "_yoy"] is not None else ""
    m["is_bank_like"] = gp is None  # bank/asuransi biasanya tidak punya laba kotor
    return m


def quick_notes(m, p):
    """Catatan singkat gaya trader. Bukan rekomendasi."""
    n = []
    if m["roe"] is not None:
        if m["roe"] >= 15:
            n.append("ROE %s: efisien banget bikin laba dari modal sendiri (di atas 15%% itu mantap)." % pct(m["roe"]))
        elif m["roe"] >= 8:
            n.append("ROE %s: lumayan/standar, belum istimewa." % pct(m["roe"]))
        else:
            n.append("ROE %s: rendah, modal belum kerja maksimal." % pct(m["roe"]))
    if m["pbv"] is not None:
        if m["pbv"] < 1:
            n.append("PBV %sx: harga di bawah nilai buku (kelihatan murah, tapi cek kenapa pasar cuek: laba turun? utang?)." % num(m["pbv"]))
        elif m["pbv"] <= 3:
            n.append("PBV %sx: valuasi wajar-an terhadap nilai buku." % num(m["pbv"]))
        else:
            n.append("PBV %sx: premium tinggi, pasar sudah bayar mahal untuk pertumbuhan/kualitas." % num(m["pbv"]))
    if m["per"] is not None:
        n.append("PER %sx: butuh sekitar %s tahun laba (TTM) segini buat 'balik modal' di harga sekarang." % (num(m["per"]), num(m["per"], 0)))
    elif m["eps"] is not None and m["eps"] <= 0:
        n.append("EPS negatif: perusahaan lagi rugi, PER tidak bisa dipakai.")
    if m["der"] is not None and not m["is_bank_like"]:
        if m["der"] > 2:
            n.append("DER %sx: utang tebal, hati-hati kalau bunga naik/arus kas seret." % num(m["der"]))
        elif m["der"] < 1:
            n.append("DER %sx: struktur utang sehat/aman." % num(m["der"]))
    if m["is_bank_like"]:
        n.append("Tidak ada laba kotor di data (umum untuk bank/lembaga keuangan): GPM tidak relevan, fokus ke ROE, NIM, NPL, CAR dari laporan resmi.")
    if m["ni_yoy"] is not None:
        arah = "naik" if m["ni_yoy"] > 0 else "turun"
        n.append("Laba bersih %s %s YoY (%s) - tren laba itu yang biasanya digerakkan pasar." % (arah, pct(abs(m["ni_yoy"])), m.get("ni_yoy_note") or "kuartal vs kuartal sama tahun lalu"))
    if p.get("price") and p.get("w52_high") and p.get("w52_low"):
        rng = p["w52_high"] - p["w52_low"]
        if rng > 0:
            pos = (p["price"] - p["w52_low"]) / rng * 100
            n.append("Posisi harga di rentang 52 minggu: %s%% (0%% = dasar, 100%% = puncak)." % num(pos, 0))
    return n


# ----------------------------------------------------------------------------
# Pengumuman / aksi korporasi
# ----------------------------------------------------------------------------
CATS = [
    ("Private Placement (PMTHMETD)", r"pmthmetd|tanpa hak memesan efek|tanpa hmetd|private placement"),
    ("Right Issue (PMHMETD)", r"pmhmetd|hak memesan efek terlebih dahulu|hmetd|right issue|rights issue"),
    ("Akuisisi / Pengambilalihan", r"akuisisi|pengambilalihan|pengambilan alih|acquisition|tender offer|penawaran tender|perubahan pengendali|pengendali"),
    ("Merger / Penggabungan", r"penggabungan usaha|merger|peleburan"),
    ("Divestasi / Penjualan Aset", r"divestasi|pelepasan saham|penjualan saham|penjualan aset"),
    ("Transaksi Material / Afiliasi", r"transaksi material|benturan kepentingan|transaksi afiliasi"),
    ("RUPS / RUPSLB", r"rups|rapat umum pemegang saham|rupslb|rupst"),
    ("Dividen", r"dividen|dividend"),
    ("Buyback", r"buy ?back|pembelian kembali"),
    ("Stock Split / Reverse Split", r"stock split|pemecahan saham|reverse stock|penggabungan saham"),
    ("Waran / MESOP / Konversi", r"waran|warrant|mesop|msop|obligasi konversi|konversi"),
    ("Obligasi / Sukuk / Utang", r"obligasi|sukuk|mtn|surat utang|penawaran umum berkelanjutan"),
    ("Laporan Keuangan", r"laporan keuangan|laporan tahunan|financial statement|annual report"),
    ("Direksi / Komisaris", r"direksi|komisaris|pengunduran diri|pengangkatan"),
    ("Suspensi / UMA / Delisting", r"suspen|unusual market activity|\buma\b|penghentian sementara|delisting|relisting|papan pemantauan"),
]

CAT_EXPLAIN = {
    "Private Placement (PMTHMETD)": "Perusahaan jual saham baru ke pihak tertentu (investor strategis) tanpa ditawarkan ke semua pemegang saham. Efek: saham beredar nambah, kepemilikan lama kena dilusi (maks. 10% modal per 2 tahun tanpa RUPS-LB, lebih dari itu perlu persetujuan RUPSLB). Lihat harga pelaksanaan vs harga pasar dan siapa pembelinya.",
    "Right Issue (PMHMETD)": "Perusahaan nawarin saham baru ke SEMUA pemegang saham lama sesuai porsi (ada jadwal cum date, recording date, trading HMETD, exercise). Kalau tidak ditebus, kepemilikan kita terdilusi. Biasanya buat bayar utang atau ekspansi.",
    "Akuisisi / Pengambilalihan": "Perusahaan beli (atau dibeli) bisnis/saham perusahaan lain, atau terjadi perubahan pengendali. Kalau pengendali berubah, bisa muncul kewajiban Tender Offer ke publik. Cek nilai transaksi, sumber dana (kas, utang, atau saham baru), dan sinergi bisnis.",
    "Merger / Penggabungan": "Dua perusahaan digabung. Cek rasio konversi saham dan perusahaan mana yang jadi surviving entity.",
    "Divestasi / Penjualan Aset": "Perusahaan jual anak usaha/aset. Bisa untuk lunasin utang atau fokus bisnis inti; cek harga jual dibanding nilai buku.",
    "Transaksi Material / Afiliasi": "Transaksi besar (>20% ekuitas) atau dengan pihak terafiliasi/benturan kepentingan. Wajib keterbukaan, kadang perlu persetujuan pemegang saham independen. Perhatikan apakah harganya wajar.",
    "RUPS / RUPSLB": "RUPST/RUPS Tahunan = rapat tahunan (laporan keuangan, dividen, pengurus, auditor). RUPSLB = Rapat Umum Pemegang Saham Luar Biasa, dipanggil untuk hal khusus (private placement, right issue, ubah anggaran dasar, akuisisi, dll). Agenda tambahan biasanya tertulis di panggilan/ringkasan risalah.",
    "Dividen": "Pembagian laba ke pemegang saham. Perhatikan cum date (batas terakhir beli biar dapat), ex date, recording date, payment date, dan DPS (dividen per saham).",
    "Buyback": "Perusahaan beli balik sahamnya sendiri. Biasanya sinyal manajemen menilai harga murah, mengurangi saham beredar, dan memberi support harga.",
    "Stock Split / Reverse Split": "Split: harga per lembar dipecah (lebih likuid, nilai total tetap). Reverse split: digabung (harga per lembar naik, kadang untuk keluar dari harga gocap).",
    "Waran / MESOP / Konversi": "Instrumen yang bisa jadi saham baru di kemudian hari. Ada potensi dilusi saat dikonversi/di-exercise.",
    "Obligasi / Sukuk / Utang": "Penerbitan surat utang. Cek bunga/kupon dan tujuan penggunaan dana.",
    "Laporan Keuangan": "Publikasi laporan keuangan kuartalan/tahunan. Ini 'rapor' perusahaan.",
    "Direksi / Komisaris": "Perubahan susunan pengurus. Pergantian mendadak bisa jadi sinyal perubahan strategi atau masalah.",
    "Suspensi / UMA / Delisting": "Pemberitahuan khusus dari bursa: UMA (gerakan harga tidak wajar), suspensi, papan pemantauan khusus, atau delisting. Risiko tinggi, cek detailnya.",
}


def classify(title):
    t = title.lower()
    for name, pat in CATS:
        if re.search(pat, t):
            return name
    return "Lainnya"


def idx_announcements(code, days):
    now = datetime.now()
    d = idx_get("https://www.idx.co.id/primary/ListedCompany/GetAnnouncement", {
        "kodeEmiten": code.upper(), "emitenType": "*", "indexFrom": 0, "pageSize": 100,
        "dateFrom": (now - timedelta(days=days)).strftime("%Y%m%d"),
        "dateTo": now.strftime("%Y%m%d"), "lang": "id", "keyword": ""})
    items = []
    for rep in d.get("Replies", []):
        p = rep.get("pengumuman", {})
        title = p.get("JudulPengumuman", "") or ""
        date = (p.get("TglPengumuman") or p.get("CreatedDate") or "")[:10]
        atts = [{"name": a.get("OriginalFilename", ""), "url": a.get("FullSavePath", "")}
                for a in rep.get("attachments", [])]
        items.append({"date": date, "title": title, "src": "IDX", "cat": classify(title),
                      "atts": atts, "key": "%s|%s" % (date, title)})
    return sorted(items, key=lambda x: x["date"], reverse=True)


def idx_financial_files(code):
    """Daftar file laporan keuangan resmi di IDX (best effort)."""
    out = []
    y = datetime.now().year
    for year in (y, y - 1):
        for per in ("audit", "tw3", "tw2", "tw1"):
            d = idx_get("https://www.idx.co.id/primary/ListedCompany/GetFinancialReport", {
                "indexFrom": 1, "pageSize": 12, "year": year, "reportType": "rdf",
                "EmitenType": "s", "periode": per, "kodeEmiten": code.upper(),
                "SortColumn": "KodeEmiten", "SortOrder": "asc"})
            for r in d.get("Results", []):
                for a in r.get("Attachments", []):
                    path = a.get("File_Path", "")
                    if path:
                        url = path if path.startswith("http") else "https://www.idx.co.id" + path
                        out.append({"periode": "%s %s" % (per.upper(), year),
                                    "name": a.get("File_Name", ""), "url": url})
            if out:
                return out
    return out


def google_news(code, days):
    q = ('"%s" saham (akuisisi OR "private placement" OR "right issue" OR RUPS OR RUPSLB '
         'OR dividen OR buyback OR laba) when:%dd' % (code.upper(), min(days, 365)))
    xml = http_get("https://news.google.com/rss/search?q=" + quote(q) + "&hl=id&gl=ID&ceid=ID:id",
                   kind="text")
    root = ET.fromstring(xml)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        try:
            dt = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "")
            date = dt.strftime("%Y-%m-%d")
        except Exception:  # noqa
            date = ""
        items.append({"date": date, "title": title, "src": it.findtext("source") or "Berita",
                      "cat": classify(title), "atts": [{"name": "baca", "url": it.findtext("link") or ""}],
                      "key": "%s|%s" % (date, title)})
    return items[:25]


# ----------------------------------------------------------------------------
# Baca agenda RUPS dari lampiran PDF (opsional, butuh pypdf)
# ----------------------------------------------------------------------------
BULAN = "Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember"
TOPIK = [("Dividen", r"dividen"), ("Penambahan modal / PMTHMETD / HMETD", r"penambahan modal|pmthmetd|hmetd"),
         ("Perubahan anggaran dasar", r"anggaran dasar"), ("Buyback", r"pembelian kembali|buy ?back"),
         ("Akuisisi / pengambilalihan", r"akuisisi|pengambilalihan"),
         ("Pengangkatan/perubahan direksi & komisaris", r"direksi|komisaris"),
         ("Penunjukan auditor / akuntan publik", r"akuntan publik|auditor"),
         ("MESOP / program opsi saham", r"mesop|msop|opsi saham"),
         ("Transaksi material / afiliasi", r"transaksi material|benturan kepentingan|afiliasi"),
         ("Penggunaan dana penawaran umum", r"realisasi penggunaan dana|penggunaan dana"),
         ("Penjaminan / agunan aset", r"menjaminkan|penjaminan|agunan")]


def read_rups_pdf(url):
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return None
    try:
        raw = http_get(url, kind="bytes", timeout=40, retries=1,
                       headers={"Referer": "https://www.idx.co.id/"})
        rd = PdfReader(io.BytesIO(raw))
        text = "\n".join((pg.extract_text() or "") for pg in rd.pages[:8])
    except Exception:  # noqa
        return None
    info = {"agenda": [], "tanggal": [], "topik": []}
    low = text.lower()
    i = low.find("mata acara")
    if i < 0:
        i = low.find("agenda")
    if i >= 0:
        for ln in text[i:i + 3500].splitlines():
            ln = ln.strip()
            if re.match(r"^(\d{1,2}|[a-z])[\.\)]\s+\S", ln, re.I) and len(ln) > 12:
                info["agenda"].append(ln[:230])
            if len(info["agenda"]) >= 12:
                break
    info["tanggal"] = list(dict.fromkeys(re.findall(r"\d{1,2}\s+(?:%s)\s+\d{4}" % BULAN, text, re.I)))[:3]
    info["topik"] = [n for n, pat in TOPIK if re.search(pat, low)]
    return info


# ----------------------------------------------------------------------------
# Ambil semua data satu emiten
# ----------------------------------------------------------------------------
def collect(code, days, show_all, deep_rups=True):
    data = {"code": code.upper(), "errors": [], "ts": datetime.now().strftime("%d-%m-%Y %H:%M:%S")}
    try:
        data["price"] = get_price(code)
    except Exception as e:  # noqa
        data["price"] = {"name": code.upper(), "price": None}
        data["errors"].append("Harga gagal diambil: %s" % e)
    try:
        data["fund"] = get_fundamentals(code)
        if not data["fund"]:
            data["errors"].append("Data keuangan kosong dari sumber (emiten baru/tidak tersedia).")
    except Exception as e:  # noqa
        data["fund"] = {}
        data["errors"].append("Data keuangan gagal diambil: %s" % e)
    data["m"] = compute(data["fund"], data["price"].get("price")) if data["fund"] else None

    ann = []
    try:
        ann += idx_announcements(code, days)
    except Exception as e:  # noqa
        data["errors"].append("Pengumuman IDX tidak bisa diakses (%s). Biasanya diblokir Cloudflare; "
                              "coba pip install curl_cffi atau buka manual di idx.co.id." % e)
    try:
        news = google_news(code, days)
    except Exception as e:  # noqa
        news = []
        data["errors"].append("Berita gagal diambil: %s" % e)
    try:
        data["files"] = idx_financial_files(code)
    except Exception:  # noqa
        data["files"] = []

    if not show_all:
        ann = [a for a in ann if a["cat"] != "Lainnya"]
    data["ann"] = ann
    data["news"] = [n for n in news if n["cat"] != "Lainnya" or show_all][:15]

    data["rups"] = []
    if deep_rups:
        n = 0
        for a in ann:
            if a["cat"] != "RUPS / RUPSLB" or n >= 3:
                continue
            for att in a["atts"]:
                if att["url"].lower().endswith(".pdf"):
                    info = read_rups_pdf(att["url"])
                    if info:
                        data["rups"].append({"date": a["date"], "title": a["title"], **info})
                        n += 1
                    break
    return data


def fingerprint(data):
    m = data.get("m") or {}
    return {"period": m.get("period"), "ni": m.get("ni"),
            "ann": sorted(a["key"] for a in data.get("ann", []))[:200]}


# ----------------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------------
NAVY = colors.HexColor("#12304f")
LIGHT = colors.HexColor("#eef3f8")
GREEN = colors.HexColor("#0b7a3b")
RED = colors.HexColor("#b3261e")


def styles():
    ss = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=18, textColor=NAVY, alignment=0, spaceAfter=2),
        "h": ParagraphStyle("h", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=12, textColor=NAVY, spaceBefore=10, spaceAfter=4, keepWithNext=1),
        "b": ParagraphStyle("b", parent=ss["Normal"], fontName="Helvetica", fontSize=8.8, leading=11.5),
        "s": ParagraphStyle("s", parent=ss["Normal"], fontName="Helvetica", fontSize=7.6, leading=9.6, textColor=colors.HexColor("#444444")),
        "c": ParagraphStyle("c", parent=ss["Normal"], fontName="Helvetica", fontSize=8, leading=10),
        "ch": ParagraphStyle("ch", parent=ss["Normal"], fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white),
    }


def make_table(rows, widths, st, header=True):
    cells = []
    for ri, row in enumerate(rows):
        sty = st["ch"] if (header and ri == 0) else st["c"]
        cells.append([c if not isinstance(c, str) else Paragraph(escape(safe(c)) if not c.startswith("<") else c, sty) for c in row])
    t = Table(cells, colWidths=widths, repeatRows=1 if header else 0)
    ts = [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c8d2dc")),
          ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if header:
        ts.append(("BACKGROUND", (0, 0), (-1, 0), NAVY))
    for r in range(1 if header else 0, len(rows)):
        if r % 2 == 0:
            ts.append(("BACKGROUND", (0, r), (-1, r), LIGHT))
    t.setStyle(TableStyle(ts))
    return t


def link(url, label):
    return '<link href="%s" color="blue">%s</link>' % (escape(url, {'"': "&quot;"}), escape(safe(label)))


def fin_table(f, st, width):
    """Tabel laporan keuangan: kolom = periode terbaru (maks 5 kuartal)."""
    rows_def = [("Total Aset", "TotalAssets", rp), ("Total Liabilitas", "TotalLiabilitiesNetMinorityInterest", rp),
                ("Ekuitas", "StockholdersEquity", rp), ("Pendapatan", "TotalRevenue", rp),
                ("Laba Kotor", "GrossProfit", rp), ("Laba Operasi", "OperatingIncome", rp),
                ("Laba Bersih", "NetIncomeCommonStockholders", rp),
                ("EPS (Rp)", "DilutedEPS", lambda v: num(v, 2)),
                ("Kas & Setara", "CashAndCashEquivalents", rp), ("Total Utang", "TotalDebt", rp),
                ("Arus Kas Operasi", "OperatingCashFlow", rp), ("Free Cash Flow", "FreeCashFlow", rp)]
    prefix = "quarterly"
    dates = sorted({d for _l, k, _f in rows_def for d, _v in (f.get(prefix + k) or [])})
    if not dates:
        prefix = "annual"
        dates = sorted({d for _l, k, _f in rows_def for d, _v in (f.get(prefix + k) or [])})
    dates = dates[-5:]
    if not dates:
        return None, prefix
    rows = [["Pos (%s)" % ("kuartalan" if prefix == "quarterly" else "tahunan")] + [d for d in dates]]
    for label, k, fmt in rows_def:
        series = dict(f.get(prefix + k) or [])
        rows.append([label] + [fmt(series[d]) if d in series else "-" for d in dates])
    first = width * 0.24
    rest = (width - first) / len(dates)
    return make_table(rows, [first] + [rest] * len(dates), st), prefix


def build_pdf(data, path):
    st = styles()
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.4 * cm, bottomMargin=1.4 * cm,
                            title="Laporan %s" % data["code"], author="IDX Report Bot")
    W = A4[0] - 3 * cm
    s = []
    p, m = data["price"], data["m"]

    s.append(Paragraph(safe("%s - %s" % (data["code"], p.get("name", ""))), st["title"]))
    s.append(Paragraph("Dibuat: %s | Sumber: Yahoo Finance (harga & data keuangan), IDX, Google News | "
                       "Bukan rekomendasi beli/jual" % data["ts"], st["s"]))
    s.append(Spacer(1, 6))

    if p.get("price"):
        chg = p.get("chg_pct")
        col = "#0b7a3b" if (chg or 0) >= 0 else "#b3261e"
        head = [["Harga", "Perubahan", "High / Low hari ini", "52 Mgg High / Low", "Volume (lembar)", "Update harga"],
                ["Rp " + num(p["price"], 0),
                 '<font color="%s">%s</font>' % (col, pct(chg, 2)),
                 "%s / %s" % (num(p.get("day_high"), 0), num(p.get("day_low"), 0)),
                 "%s / %s" % (num(p.get("w52_high"), 0), num(p.get("w52_low"), 0)),
                 num(p.get("volume"), 0), p.get("time", "-")]]
        s.append(make_table(head, [W / 6] * 6, st))

    for e in data["errors"]:
        s.append(Spacer(1, 3))
        s.append(Paragraph('<font color="#b3261e">Catatan: %s</font>' % escape(safe(e)), st["s"]))

    # 1. ringkasan
    if m:
        s.append(Paragraph("1. Ringkasan Cepat (bahasa trader)", st["h"]))
        for line in quick_notes(m, p):
            s.append(Paragraph("&bull; " + escape(safe(line)), st["b"]))
        s.append(Spacer(1, 3))
        s.append(Paragraph("Dasar hitung laba: %s. Posisi neraca per %s." % (m["basis"] or "-", m["period"]), st["s"]))

        # 2. laporan keuangan
        s.append(Paragraph("2. Laporan Keuangan", st["h"]))
        tbl, prefix = fin_table(data["fund"], st, W)
        if tbl is not None:
            s.append(tbl)
        s.append(Paragraph("Angka dalam Rupiah (T = triliun, M = miliar, Jt = juta). Laporan IDX bersifat kumulatif "
                           "(Q3 = 9 bulan); sumber data bisa menyajikan per kuartal. Cocokkan dengan dokumen resmi.", st["s"]))
        if data.get("files"):
            s.append(Spacer(1, 3))
            s.append(Paragraph("<b>File laporan resmi di IDX:</b> " + " | ".join(
                link(f["url"], "%s %s" % (f["periode"], f["name"])) for f in data["files"][:6]), st["s"]))

        # 3. rasio
        s.append(Paragraph("3. Rasio Penting", st["h"]))
        r = [["Rasio", "Nilai", "Artinya"],
             ["EPS (%s)" % (m["basis"] or ""), "Rp " + num(m["eps"], 2), "Laba per lembar saham"],
             ["BVPS", "Rp " + num(m["bvps"], 2), "Nilai buku per lembar saham"],
             ["PER", num(m["per"]) + "x" if m["per"] else "-", "Harga / EPS. Makin kecil makin murah (dengan syarat laba sehat)"],
             ["PBV", num(m["pbv"]) + "x" if m["pbv"] else "-", "Harga / nilai buku. Di bawah 1x = di bawah nilai buku"],
             ["ROE", pct(m["roe"]), "Laba bersih / ekuitas. Seberapa jago modal sendiri menghasilkan laba"],
             ["ROA", pct(m["roa"]), "Laba bersih / total aset"],
             ["GPM (Gross Margin)", pct(m["gpm"]) if m["gpm"] is not None else "n/a", "Laba kotor / pendapatan"],
             ["OPM", pct(m["opm"]), "Laba operasi / pendapatan"],
             ["NPM", pct(m["npm"]), "Laba bersih / pendapatan"],
             ["DER", num(m["der"]) + "x" if m["der"] is not None else "-", "Liabilitas / ekuitas. Bank wajar tinggi"],
             ["Current Ratio", num(m["cr"]) + "x" if m["cr"] is not None else "-", "Aset lancar / liabilitas jangka pendek (di atas 1x aman)"],
             ["Pendapatan QoQ / YoY", "%s / %s" % (pct(m["rev_qoq"]), pct(m["rev_yoy"])), "Pertumbuhan penjualan"],
             ["Laba Bersih QoQ / YoY", "%s / %s" % (pct(m["ni_qoq"]), pct(m["ni_yoy"])), "Pertumbuhan laba"]]
        s.append(make_table(r, [W * 0.24, W * 0.18, W * 0.58], st))

    # 4. aksi korporasi
    s.append(Paragraph("4. Aksi Korporasi & Pengumuman Penting", st["h"]))
    ann = data["ann"]
    if not ann:
        s.append(Paragraph("Tidak ada pengumuman terklasifikasi dari IDX pada periode ini (atau situs IDX tidak bisa diakses, "
                           "lihat catatan di atas).", st["b"]))
    else:
        rows = [["Tanggal", "Kategori", "Judul pengumuman", "Lampiran"]]
        for a in ann[:60]:
            rows.append([a["date"], a["cat"], a["title"],
                         " ".join(link(x["url"], "PDF%d" % (i + 1)) for i, x in enumerate(a["atts"][:3]) if x["url"])])
        s.append(make_table(rows, [W * 0.13, W * 0.22, W * 0.50, W * 0.15], st))

    # RUPS detail
    if data["rups"]:
        s.append(Paragraph("4a. Detail RUPS / RUPSLB (dibaca dari lampiran)", st["h"]))
        for r_ in data["rups"]:
            blk = [Paragraph("<b>%s</b> - %s" % (escape(r_["date"]), escape(safe(r_["title"]))), st["b"])]
            if r_["tanggal"]:
                blk.append(Paragraph("Tanggal tersebut di dokumen: " + escape(", ".join(r_["tanggal"])), st["s"]))
            if r_["topik"]:
                blk.append(Paragraph("Topik yang tersebut (termasuk tambahan agenda): " + escape(", ".join(r_["topik"])), st["s"]))
            for ag in r_["agenda"]:
                blk.append(Paragraph("&bull; " + escape(safe(ag)), st["s"]))
            if not r_["agenda"]:
                blk.append(Paragraph("Daftar mata acara tidak terbaca otomatis, buka PDF-nya.", st["s"]))
            blk.append(Spacer(1, 4))
            s.append(KeepTogether(blk))

    # berita
    if data["news"]:
        s.append(Paragraph("5. Berita Terkait", st["h"]))
        rows = [["Tanggal", "Kategori", "Berita", "Sumber"]]
        for n in data["news"]:
            rows.append([n["date"], n["cat"], link(n["atts"][0]["url"], n["title"]) if n["atts"][0]["url"] else n["title"], n["src"]])
        s.append(make_table(rows, [W * 0.12, W * 0.2, W * 0.52, W * 0.16], st))

    # kamus
    cats_seen = sorted({a["cat"] for a in ann if a["cat"] in CAT_EXPLAIN} | {n["cat"] for n in data["news"] if n["cat"] in CAT_EXPLAIN})
    if cats_seen:
        s.append(Paragraph("6. Kamus Singkat: Artinya Buat Trader", st["h"]))
        for c in cats_seen:
            s.append(Paragraph("<b>%s</b>: %s" % (escape(c), escape(safe(CAT_EXPLAIN[c]))), st["b"]))
            s.append(Spacer(1, 2))

    s.append(Spacer(1, 8))
    s.append(Paragraph("Disclaimer: dokumen ini dibuat otomatis dari sumber publik dan bisa tertunda/keliru. "
                       "Bukan ajakan membeli atau menjual efek. Verifikasi ke keterbukaan informasi resmi di idx.co.id "
                       "dan lakukan riset sendiri (DYOR).", st["s"]))
    doc.build(s)


# ----------------------------------------------------------------------------
# Main + mode realtime
# ----------------------------------------------------------------------------
def load_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa
        return {}


def save_cache(path, cache):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)


def run_once(code, args, cache, force=False):
    data = collect(code, args.days, args.all, deep_rups=not args.no_rups)
    fp = fingerprint(data)
    old = cache.get(code)
    changed = old != fp
    px = data["price"].get("price")
    print("[%s] %s harga: %s" % (datetime.now().strftime("%H:%M:%S"), code, num(px, 0) if px else "-"))
    if changed and old:
        if old.get("period") != fp.get("period"):
            print("   >> LAPORAN KEUANGAN BARU terdeteksi (periode %s -> %s)" % (old.get("period"), fp.get("period")))
        new_keys = set(fp["ann"]) - set(old.get("ann", []))
        for k in sorted(new_keys, reverse=True)[:10]:
            print("   >> PENGUMUMAN BARU: %s" % k)
    if changed or force or not os.path.exists(os.path.join(args.out, "%s_laporan.pdf" % code)):
        os.makedirs(args.out, exist_ok=True)
        path = os.path.join(args.out, "%s_laporan.pdf" % code)
        build_pdf(data, path)
        print("   PDF disimpan: %s" % path)
    cache[code] = fp
    return data


def main():
    ap = argparse.ArgumentParser(description="Laporan keuangan + aksi korporasi emiten IDX -> PDF")
    ap.add_argument("codes", nargs="+", help="kode saham, contoh: BBCA BBRI TLKM")
    ap.add_argument("--days", type=int, default=180, help="cari pengumuman N hari ke belakang (default 180)")
    ap.add_argument("--out", default="laporan_pdf", help="folder output PDF")
    ap.add_argument("--watch", type=int, default=0, help="mode realtime: ulangi tiap N detik (0 = sekali)")
    ap.add_argument("--all", action="store_true", help="tampilkan juga pengumuman kategori 'Lainnya'")
    ap.add_argument("--no-rups", action="store_true", help="jangan unduh/baca PDF RUPS (lebih cepat)")
    ap.add_argument("--refresh-pdf", action="store_true", help="paksa tulis ulang PDF tiap siklus")
    args = ap.parse_args()

    cache_path = os.path.join(args.out, ".cache.json")
    os.makedirs(args.out, exist_ok=True)
    cache = load_cache(cache_path)
    codes = [c.upper().replace(".JK", "") for c in args.codes]

    while True:
        for c in codes:
            try:
                run_once(c, args, cache, force=args.refresh_pdf or not args.watch)
            except Exception as e:  # noqa
                print("[!] %s gagal: %s" % (c, e))
        save_cache(cache_path, cache)
        if not args.watch:
            break
        print("   tidur %d detik... (Ctrl+C untuk berhenti)" % args.watch)
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print("Berhenti.")
            break


if __name__ == "__main__":
    main()
