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
    python idx_lap.py BBCA
    python idx_lap.py BBCA BBRI TLKM --days 365
    python idx_lap.py KOKA --watch 300          # realtime: cek ulang tiap 5 menit
    python idx_lap.py BBCA --out laporan --all  # --all = tampilkan juga pengumuman 'Lainnya'

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

# --- FIX error "'usedforsecurity' is an invalid keyword argument for openssl_md5()" ---
# Terjadi di Python/OpenSSL tertentu saat reportlab memanggil hashlib.md5(usedforsecurity=False).
import hashlib as _hl
_orig_md5, _orig_new = _hl.md5, _hl.new


def _md5_compat(*a, **k):
    try:
        return _orig_md5(*a, **k)
    except TypeError:
        k.pop("usedforsecurity", None)
        return _orig_md5(*a, **k)


def _new_compat(*a, **k):
    try:
        return _orig_new(*a, **k)
    except TypeError:
        k.pop("usedforsecurity", None)
        return _orig_new(*a, **k)


_hl.md5, _hl.new = _md5_compat, _new_compat

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


def yget(path, params):
    """Yahoo punya 2 host; coba keduanya biar tahan rate-limit."""
    last = None
    for host in ("query1", "query2"):
        try:
            return http_get("https://%s.finance.yahoo.com%s" % (host, path), params=params, retries=1)
        except Exception as e:  # noqa
            last = e
    raise RuntimeError(last)


def get_chart(sym, rng="1y"):
    d = yget("/v8/finance/chart/%s" % quote(sym, safe=""), {"interval": "1d", "range": rng})
    res = d["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(res.get("timestamp") or []):
        c = q["close"][i]
        if c is None:
            continue
        rows.append({"t": t, "c": c,
                     "o": q["open"][i] if q["open"][i] is not None else c,
                     "h": q["high"][i] if q["high"][i] is not None else c,
                     "l": q["low"][i] if q["low"][i] is not None else c,
                     "v": q["volume"][i] or 0})
    return res["meta"], rows


def get_price(code):
    m, rows = get_chart(yf_symbol(code))
    closes = [r["c"] for r in rows]
    price = m.get("regularMarketPrice") or (closes[-1] if closes else None)
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
        "hist": rows,
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
    d = yget(
        "/ws/fundamentals-timeseries/v1/finance/timeseries/%s" % yf_symbol(code),
        {"type": ",".join(types), "period1": now - 6 * 365 * 86400,
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
    m["fcf"], _ = ttm_or_fy(f, "FreeCashFlow")
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
# Teknikal, kondisi IHSG & kecocokan gaya main (scalping / day trade / swing)
# ----------------------------------------------------------------------------
NAMES = {"scalp": "Scalping (menit-jam)", "day": "Day Trade (hari itu juga)",
         "swing_s": "Swing Pendek (2-10 hari bursa)", "swing_l": "Swing Panjang (1-3 bulan+)"}


def tick(p):
    """Fraksi harga BEI."""
    if p < 200:
        return 1
    if p < 500:
        return 2
    if p < 2000:
        return 5
    if p < 5000:
        return 10
    return 25


def rt(x):
    x = max(x, 1)
    t = tick(x)
    return int(round(x / t)) * t


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def ema_series(vals, n):
    k, out, e = 2.0 / (n + 1), [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = l_ = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0)
        l_ += max(-d, 0)
    ag, al = g / n, l_ / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(rows, n=14):
    if len(rows) < n + 1:
        return None
    trs = []
    for i in range(1, len(rows)):
        h, l_, pc = rows[i]["h"], rows[i]["l"], rows[i - 1]["c"]
        trs.append(max(h - l_, abs(h - pc), abs(l_ - pc)))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def macd_hist(closes):
    if len(closes) < 35:
        return None, None
    line = [a - b for a, b in zip(ema_series(closes, 12), ema_series(closes, 26))]
    sig = ema_series(line, 9)
    h = [a - b for a, b in zip(line, sig)]
    return h[-1], h[-2]


def technicals(rows):
    if len(rows) < 30:
        return None
    c = [r["c"] for r in rows]
    v = [r["v"] for r in rows]
    last = c[-1]
    t = {"last": last, "sma20": sma(c, 20), "sma50": sma(c, 50), "sma200": sma(c, 200),
         "rsi": rsi(c), "atr": atr(rows)}
    t["atr_pct"] = t["atr"] / last * 100 if t["atr"] else None
    t["macd"], t["macd_prev"] = macd_hist(c)
    v20 = sum(v[-21:-1]) / 20 if len(v) >= 21 else None
    t["vol_ratio"] = v[-1] / v20 if v20 else None
    t["val20"] = sum(r["c"] * r["v"] for r in rows[-20:]) / min(20, len(rows))
    t["hi20"] = max(r["h"] for r in rows[-20:])
    t["lo20"] = min(r["l"] for r in rows[-20:])
    t["ret5"] = growth(last, c[-6]) if len(c) >= 6 else None
    t["ret20"] = growth(last, c[-21]) if len(c) >= 21 else None
    return t


_IHSG = {"t": 0, "v": None}


def ihsg_regime():
    if time.time() - _IHSG["t"] < 120 and _IHSG["v"]:
        return _IHSG["v"]
    _, rows = get_chart("^JKSE")
    t = technicals(rows)
    if not t:
        return None
    last, sc = t["last"], 0
    if t["sma20"] and last > t["sma20"]:
        sc += 30
    if t["sma50"] and last > t["sma50"]:
        sc += 30
    if t["sma200"] and last > t["sma200"]:
        sc += 20
    if t["macd"] is not None and t["macd"] > 0:
        sc += 10
    if t["rsi"] and 45 <= t["rsi"] <= 70:
        sc += 10
    label = "Bullish" if sc >= 70 else "Sideways" if sc >= 40 else "Bearish"
    res = {"label": label, "score": sc, "last": last, "ret5": t["ret5"], "ret20": t["ret20"], "rsi": t["rsi"]}
    _IHSG.update(t=time.time(), v=res)
    return res


def fund_score(m):
    if not m:
        return 40, ["Data fundamental tidak tersedia, skor netral."]
    sc, nt = 0, []
    if m["ni"] is not None and m["ni"] > 0:
        sc += 15
    r = m["roe"]
    if r is not None:
        sc += 20 if r >= 15 else 10 if r >= 8 else 0
    y = m["ni_yoy"]
    if y is not None:
        sc += 15 if y > 20 else 8 if y > 0 else -10 if y < -20 else 0
    y = m["rev_yoy"]
    if y is not None:
        sc += 10 if y > 10 else 5 if y > 0 else 0
    if m["pbv"]:
        sc += 10 if m["pbv"] <= 1 else 6 if m["pbv"] <= 3 else 0
    if m["per"]:
        sc += 10 if m["per"] <= 15 else 5 if m["per"] <= 25 else 0
    if not m["is_bank_like"] and m["der"] is not None:
        sc += 10 if m["der"] < 1 else 5 if m["der"] < 2 else -5 if m["der"] > 3 else 0
    if m["cr"] and m["cr"] >= 1.5:
        sc += 5
    if m.get("fcf") and m["fcf"] > 0:
        sc += 5
    return max(0, min(100, sc)), nt


def tech_score(t):
    sc, last = 0, t["last"]
    if t["sma20"] and last > t["sma20"]:
        sc += 15
    if t["sma50"] and last > t["sma50"]:
        sc += 15
    if t["sma200"] and last > t["sma200"]:
        sc += 15
    if t["sma20"] and t["sma50"] and t["sma20"] > t["sma50"]:
        sc += 10
    if t["macd"] is not None:
        if t["macd"] > 0:
            sc += 10
        if t["macd_prev"] is not None and t["macd"] > t["macd_prev"]:
            sc += 5
    r = t["rsi"]
    if r is not None:
        sc += 15 if 50 <= r <= 70 else 5 if 40 <= r < 50 else 5 if r < 30 else -10 if r > 75 else 0
    if t["vol_ratio"] and t["vol_ratio"] >= 1.5 and (t["ret5"] or 0) > 0:
        sc += 10
    return max(0, min(100, sc))


def activity_score(t):
    v = t["val20"] or 0
    sc = 40 if v >= 50e9 else 30 if v >= 10e9 else 18 if v >= 3e9 else 8 if v >= 1e9 else 0
    a = t["atr_pct"] or 0
    sc += 35 if 2 <= a <= 6 else 20 if 1.5 <= a < 2 or 6 < a <= 9 else 10 if a > 9 else 5
    vr = t["vol_ratio"] or 0
    sc += 25 if vr >= 2 else 15 if vr >= 1.3 else 8 if vr >= 1 else 0
    return min(100, sc)


def catalyst_adj(ann):
    adj = {"scalp": 0, "day": 0, "swing_s": 0, "swing_l": 0}
    notes = []
    since = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    cats = {a["cat"] for a in ann if a["date"] >= since}
    if "Suspensi / UMA / Delisting" in cats:
        for k in adj:
            adj[k] -= 10
        notes.append("Ada pengumuman UMA/suspensi/pemantauan bursa dalam 45 hari: gerak harga bisa liar, risiko gembok/suspend. Kurangi ukuran posisi.")
    if "Private Placement (PMTHMETD)" in cats or "Right Issue (PMHMETD)" in cats:
        adj["swing_l"] -= 8
        adj["swing_s"] -= 3
        notes.append("Ada rencana tambah saham baru (private placement/right issue): ada risiko dilusi. Cek harga pelaksanaan & tujuan dana.")
    if "Buyback" in cats:
        adj["swing_s"] += 4
        adj["swing_l"] += 4
        notes.append("Ada buyback: biasanya jadi bantalan harga dan sinyal manajemen merasa sahamnya murah.")
    if "Dividen" in cats:
        adj["swing_s"] += 2
        notes.append("Ada jadwal dividen: perhatikan cum date. Harga sering naik menjelang cum date lalu turun setelah ex date.")
    if "Akuisisi / Pengambilalihan" in cats or "Transaksi Material / Afiliasi" in cats:
        adj["day"] += 3
        notes.append("Ada kabar akuisisi/transaksi material: bisa jadi katalis, tapi cek nilai, sumber dana, dan apakah pihak terafiliasi.")
    if "RUPS / RUPSLB" in cats:
        notes.append("Ada RUPS/RUPSLB dalam 45 hari: baca agendanya (private placement, right issue, anggaran dasar) karena sering jadi penggerak harga.")
    return adj, notes


def analyze_strategy(data):
    p, m = data["price"], data.get("m")
    t = technicals(p.get("hist") or [])
    if not t:
        return None
    ih = data.get("ihsg")
    fs, _ = fund_score(m)
    ts, act = tech_score(t), activity_score(t)
    ihs = ih["score"] if ih else 50
    adj, cnotes = catalyst_adj(data.get("ann", []))
    sc = {"scalp": .05 * fs + .15 * ts + .15 * ihs + .65 * act,
          "day": .15 * fs + .30 * ts + .20 * ihs + .35 * act,
          "swing_s": .25 * fs + .50 * ts + .25 * ihs,
          "swing_l": .55 * fs + .30 * ts + .15 * ihs}
    why = {k: [] for k in sc}
    val = t["val20"] or 0
    for k, need in (("scalp", 10e9), ("day", 3e9), ("swing_s", 1e9), ("swing_l", 1e9)):
        if val < need:
            sc[k] = min(sc[k], 30)
            why[k].append("Kurang likuid (rata-rata transaksi %s/hari, idealnya min. %s): susah masuk-keluar tanpa geser harga." % (rp(val), rp(need)))
    if t["atr_pct"] is not None and t["atr_pct"] < 1.5:
        for k in ("scalp", "day"):
            sc[k] = min(sc[k], 40)
            why[k].append("Gerak harian sempit (ATR %s%%): ruang cuan tipis dibanding biaya beli-jual (~0,3-0,5%%)." % num(t["atr_pct"]))
    if m and m["ni"] is not None and m["ni"] <= 0:
        sc["swing_l"] = min(sc["swing_l"], 40)
        sc["swing_s"] = min(sc["swing_s"], 65)
        why["swing_l"].append("Laba bersih negatif: belum layak ditahan lama berbasis fundamental.")
    if ih and ih["label"] == "Bearish":
        sc["swing_s"] = min(sc["swing_s"], 60)
        sc["swing_l"] = min(sc["swing_l"], 65)
        why["swing_s"].append("IHSG lagi Bearish: saham bagus pun sering ikut ditarik turun. Tunggu IHSG membaik atau perkecil posisi.")
        why["swing_l"].append("IHSG lagi Bearish: kalau mau akumulasi, bertahap (cicil) dan siapkan cash.")
    if t["sma200"] and t["last"] < t["sma200"]:
        sc["swing_l"] = min(sc["swing_l"], 60)
        why["swing_l"].append("Harga masih di bawah SMA200 (belum uptrend jangka panjang).")
    if t["rsi"] and t["rsi"] > 75:
        sc["swing_s"] -= 10
        why["swing_s"].append("RSI %s sudah overbought: rawan koreksi, lebih enak masuk saat pullback." % num(t["rsi"], 0))
    if fs >= 70:
        why["swing_l"].append("Fundamental kuat: laba bagus butuh waktu 'dihargai' pasar, jadi lebih enak ditahan.")
    if fs < 45:
        why["swing_l"].append("Fundamental lemah: tidak ada 'jangkar' kalau harga jatuh.")
    for k in sc:
        sc[k] = max(0, min(100, sc[k] + adj[k]))
    verdict = {k: ("COCOK" if v >= 70 else "BOLEH (selektif)" if v >= 55 else "HINDARI / TUNGGU") for k, v in sc.items()}
    best = max(sc, key=lambda k: sc[k])
    if sc[best] < 55:
        best = None
    flags = []
    last = t["last"]
    for lab, key in (("SMA20", "sma20"), ("SMA50", "sma50"), ("SMA200", "sma200")):
        if t[key]:
            flags.append("%s %s %s" % ("di atas" if last > t[key] else "di bawah", lab, num(t[key], 0)))
    facts = ["Fundamental: skor %d/100 (%s)." % (fs, "kuat" if fs >= 70 else "cukup" if fs >= 45 else "lemah"),
             "Teknikal: skor %d/100; harga %s." % (ts, ", ".join(flags) if flags else "-"),
             "RSI %s, MACD histogram %s." % (num(t["rsi"], 0), "positif" if (t["macd"] or 0) > 0 else "negatif"),
             "Aktivitas: transaksi rata-rata %s/hari, ATR %s%% per hari, volume terakhir %sx rata-rata 20 hari." % (rp(val), num(t["atr_pct"]), num(t["vol_ratio"])),
             "Support/resisten 20 hari: %s / %s." % (num(t["lo20"], 0), num(t["hi20"], 0))]
    if ih:
        facts.append("IHSG: %s (skor %d/100), 5 hari %s, 20 hari %s." % (ih["label"], ih["score"], pct(ih["ret5"]), pct(ih["ret20"])))
    plan = trade_plan(best, last, t) if best else None
    return {"scores": sc, "verdict": verdict, "why": why, "best": best, "plan": plan,
            "fs": fs, "ts": ts, "act": act, "facts": facts, "notes": cnotes, "tech": t, "ihsg": ih}


def trade_plan(key, price, t):
    atr_ = t["atr"] or price * 0.03
    sl_m, rr1, rr2, hold = {
        "scalp": (0.4, 1.5, 2.5, "Menit sampai jam. WAJIB tutup hari ini, jangan dibawa pulang."),
        "day": (0.8, 1.5, 2.5, "Intraday. Tutup sebelum penutupan, hari itu juga selesai."),
        "swing_s": (1.5, 1.5, 2.5, "Sekitar 2-10 hari bursa. Evaluasi tiap hari, geser SL naik kalau sudah profit."),
        "swing_l": (2.5, 2.0, 3.5, "Sekitar 1-3 bulan atau lebih. Pantau laporan keuangan & arah IHSG, boleh cicil masuk."),
    }[key]
    risk = sl_m * atr_
    lo = price - (0.2 if key in ("scalp", "day") else 1.0) * atr_
    if key.startswith("swing") and t["sma20"]:
        lo = max(lo, t["sma20"])
    lo = min(lo, price)
    plan = {"hold": hold, "entry_lo": rt(lo), "entry_hi": rt(price), "sl": rt(price - risk),
            "tp1": rt(price + rr1 * risk), "tp2": rt(price + rr2 * risk), "rr1": rr1, "rr2": rr2,
            "risk_pct": risk / price * 100, "note": ""}
    if plan["tp1"] > t["hi20"] > price:
        plan["note"] = "TP1 melewati resisten 20 hari (%s), bisa tertahan di sana; pertimbangkan ambil sebagian di resisten." % num(t["hi20"], 0)
    per_lot = risk * 100
    plan["lot_example"] = int(1_000_000 / per_lot) if per_lot > 0 else 0
    return plan


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

    try:
        data["ihsg"] = ihsg_regime()
    except Exception as e:  # noqa
        data["ihsg"] = None
        data["errors"].append("Data IHSG gagal diambil: %s" % e)
    data["strat"] = analyze_strategy(data) if data["price"].get("hist") else None

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


def add_strategy_section(s, sg, st, W):
    s.append(Paragraph("1a. Cocoknya Main Apa? (Scalping / Day Trade / Swing)", st["h"]))
    for f_ in sg["facts"]:
        s.append(Paragraph("&bull; " + escape(safe(f_)), st["b"]))
    s.append(Spacer(1, 4))
    rows = [["Gaya main", "Skor", "Vonis", "Catatan"]]
    colr = {"COCOK": "#0b7a3b", "BOLEH (selektif)": "#b26a00", "HINDARI / TUNGGU": "#b3261e"}
    for k in ("scalp", "day", "swing_s", "swing_l"):
        v = sg["verdict"][k]
        rows.append([NAMES[k], "%d" % round(sg["scores"][k]),
                     '<font color="%s"><b>%s</b></font>' % (colr[v], v),
                     " ".join(sg["why"][k]) or "-"])
    s.append(make_table(rows, [W * 0.25, W * 0.08, W * 0.17, W * 0.50], st))
    s.append(Spacer(1, 4))
    b = sg["best"]
    if b is None:
        s.append(Paragraph("<b>Kesimpulan: TUNGGU DULU.</b> Belum ada gaya main yang skornya meyakinkan (di bawah 55). "
                           "Lebih aman pantau sampai tren/volume/IHSG membaik.", st["b"]))
    else:
        pl = sg["plan"]
        s.append(Paragraph("<b>Kesimpulan: paling cocok %s (skor %d).</b>" % (escape(NAMES[b]), round(sg["scores"][b])), st["b"]))
        pr = [["Rencana (contoh, bukan perintah)", "Level"],
              ["Zona entry", "%s - %s" % (num(pl["entry_lo"], 0), num(pl["entry_hi"], 0))],
              ["Stop loss (cut loss)", "%s  (risiko sekitar %s dari harga)" % (num(pl["sl"], 0), pct(pl["risk_pct"]))],
              ["Target 1 (RR 1:%s)" % num(pl["rr1"], 1), num(pl["tp1"], 0)],
              ["Target 2 (RR 1:%s)" % num(pl["rr2"], 1), num(pl["tp2"], 0)],
              ["Durasi pegang", pl["hold"]],
              ["Ukuran posisi", "Risiko maks. 1-2%% modal per transaksi. Contoh modal Rp 100 jt, risiko 1%% = Rp 1 jt, maks. sekitar %d lot." % pl["lot_example"]]]
        s.append(make_table(pr, [W * 0.28, W * 0.72], st))
        if pl["note"]:
            s.append(Paragraph(escape(pl["note"]), st["s"]))
    for n_ in sg["notes"]:
        s.append(Paragraph("&bull; " + escape(safe(n_)), st["b"]))
    s.append(Paragraph("Cara baca: skor dihitung dari fundamental, teknikal harian (SMA, RSI, MACD, ATR, volume), kondisi IHSG, "
                       "likuiditas, dan kabar aksi korporasi. Data harian, jadi untuk scalping/day trade ini hanya penyaring "
                       "'layak dimainkan atau tidak', bukan sinyal entry menit-an. Mengingat ARA/ARB dan biaya transaksi, "
                       "cek chart intraday sebelum eksekusi. Ini model aturan sederhana, bukan saran investasi.", st["s"]))


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

        pass
    if data.get("strat"):
        add_strategy_section(s, data["strat"], st, W)
    if m:
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
    for e in data["errors"]:
        print("   [!] %s" % e)
    sg = data.get("strat")
    if sg:
        ih = sg.get("ihsg")
        print("   IHSG: %s | Fundamental %d | Teknikal %d" % (ih["label"] if ih else "-", sg["fs"], sg["ts"]))
        print("   Skor: " + " | ".join("%s %d" % (NAMES[k].split(" (")[0], round(v)) for k, v in sg["scores"].items()))
        print("   >> Paling cocok: %s" % (NAMES[sg["best"]] if sg["best"] else "TUNGGU DULU (belum ada yang meyakinkan)"))
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
