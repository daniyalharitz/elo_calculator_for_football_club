"""
Kalkulator rating Elo klub (zero-sum) untuk dunia fiksi - versi Streamlit.

Rumus:
    P_baru = P_lama + K * (W - We)
    We     = 1 / (10 ** (-dr / PEMBAGI) + 1)
    dr     = rating sendiri - rating lawan (+ bonus tuan rumah, jika dipakai)

Nilai hasil (W):
    menang = 1, kalah = 0, seri = 0,5 / 0,5
    adu penalti: pemenang 0,75, yang kalah 0,25

Perubahan rating kedua klub selalu berlawanan (zero-sum), termasuk di knockout.

Jalankan lokal:   streamlit run main.py
Deploy:           simpan sebagai main.py di repository GitHub bersama requirements.txt
"""

from __future__ import annotations

import copy
import csv
import io
import math
from typing import NamedTuple

import streamlit as st

# ---------------------------------------------------------------------------
# Parameter (nilai awal; rating awal, pembagi, dan bonus bisa diubah di sidebar)
# ---------------------------------------------------------------------------
RATING_AWAL = 1500.0
PEMBAGI = 400.0          # skala peluang menang; FIFA memakai 600
BONUS_TUAN_RUMAH = 0.0   # 0 = tidak dipakai; hanya masuk hitungan We, bukan rating asli

# babak "awal"  = babak awal piala / liga / fase grup atau fase liga Eropa
# babak "akhir" = semifinal dan final piala / fase knockout Eropa
NILAI_K = {
    "persahabatan": {"awal": 0,  "akhir": 0},    # tidak berpengaruh
    "piala_kedua":  {"awal": 15, "akhir": 20},
    "piala_utama":  {"awal": 20, "akhir": 25},
    "liga":         {"awal": 30, "akhir": 30},
    "uel_uecl":     {"awal": 30, "akhir": 40},
    "ucl":          {"awal": 40, "akhir": 50},
}

NILAI_PENALTI_MENANG = 0.75
NILAI_PENALTI_KALAH = 0.25

# Nama lain yang diterima saat input CSV
ALIAS_KOMPETISI = {"uel": "uel_uecl", "uecl": "uel_uecl"}
ALIAS_BABAK = {
    "awal": "awal", "grup": "awal", "fase_liga": "awal",
    "akhir": "akhir", "knockout": "akhir", "semifinal": "akhir", "final": "akhir",
}

BATAS_UNDO = 30
BATAS_ERROR_TAMPIL = 15


# ---------------------------------------------------------------------------
# Fungsi inti perhitungan
# ---------------------------------------------------------------------------
def nilai_k(kompetisi: str, babak: str = "awal") -> float:
    if kompetisi not in NILAI_K:
        raise ValueError(f"Kompetisi tidak dikenal: {kompetisi!r}. Pilihan: {list(NILAI_K)}")
    if babak not in ("awal", "akhir"):
        raise ValueError("babak harus 'awal' atau 'akhir'")
    return NILAI_K[kompetisi][babak]


def hasil_diharapkan(rating_a: float, rating_b: float,
                     bonus_a: float = 0.0, pembagi: float = PEMBAGI) -> float:
    """Peluang 'menang' tim A (We). bonus_a = keuntungan tuan rumah untuk A (boleh negatif)."""
    dr = rating_a + bonus_a - rating_b
    return 1.0 / (10 ** (-dr / pembagi) + 1.0)


def nilai_hasil(gol_a: int, gol_b: int, pemenang_penalti: str | None = None):
    """Mengembalikan (W_A, W_B). pemenang_penalti: 'A', 'B', atau None."""
    if pemenang_penalti is not None:
        if pemenang_penalti not in ("A", "B"):
            raise ValueError("pemenang_penalti harus 'A', 'B', atau None")
        if gol_a != gol_b:
            raise ValueError("adu penalti hanya terjadi jika skor akhir imbang")
        if pemenang_penalti == "A":
            return NILAI_PENALTI_MENANG, NILAI_PENALTI_KALAH
        return NILAI_PENALTI_KALAH, NILAI_PENALTI_MENANG
    if gol_a > gol_b:
        return 1.0, 0.0
    if gol_a < gol_b:
        return 0.0, 1.0
    return 0.5, 0.5


class HasilMatch(NamedTuple):
    k: float
    we_a: float
    w_a: float
    w_b: float
    perubahan_a: float
    perubahan_b: float
    rating_baru_a: float
    rating_baru_b: float


def hitung_match(rating_a: float, rating_b: float, gol_a: int, gol_b: int,
                 kompetisi: str, babak: str = "awal",
                 pemenang_penalti: str | None = None,
                 tuan_rumah: str | None = None,
                 bonus_tuan_rumah: float = BONUS_TUAN_RUMAH,
                 pembagi: float = PEMBAGI) -> HasilMatch:
    """
    Hitung perubahan rating satu match.
    tuan_rumah: 'A', 'B', atau None (netral). pemenang_penalti: 'A', 'B', atau None.
    """
    k = nilai_k(kompetisi, babak)
    bonus_a = {"A": bonus_tuan_rumah, "B": -bonus_tuan_rumah, None: 0.0}[tuan_rumah]
    we_a = hasil_diharapkan(rating_a, rating_b, bonus_a, pembagi)
    w_a, w_b = nilai_hasil(gol_a, gol_b, pemenang_penalti)

    perubahan_a = k * (w_a - we_a) + 0.0   # "+ 0.0" mencegah tampilan -0.00
    perubahan_b = -perubahan_a + 0.0       # zero-sum
    return HasilMatch(k, we_a, w_a, w_b, perubahan_a, perubahan_b,
                      rating_a + perubahan_a, rating_b + perubahan_b)


# ---------------------------------------------------------------------------
# Papan rating (menyimpan rating banyak klub)
# ---------------------------------------------------------------------------
def _rapikan_nama(nama: str) -> str:
    return " ".join(nama.split())


class PapanRating:
    def __init__(self, rating_awal: float = RATING_AWAL,
                 pembagi: float = PEMBAGI,
                 bonus_tuan_rumah: float = BONUS_TUAN_RUMAH):
        self.rating_awal = rating_awal
        self.pembagi = pembagi
        self.bonus_tuan_rumah = bonus_tuan_rumah
        self.rating: dict[str, float] = {}

    def kanonik(self, nama: str) -> str:
        """Samakan nama dengan klub yang sudah ada (tidak peka huruf besar/kecil)."""
        nama = _rapikan_nama(nama)
        for k in self.rating:
            if k.casefold() == nama.casefold():
                return k
        return nama

    def atur(self, klub: str, nilai: float) -> None:
        self.rating[self.kanonik(klub)] = float(nilai)

    def catat_match(self, d: dict, nomor: int) -> dict:
        """Proses satu match (d dari parse_match) dan kembalikan baris log."""
        a, b = self.kanonik(d["klub_a"]), self.kanonik(d["klub_b"])
        if a.casefold() == b.casefold():
            raise ValueError("klub_a dan klub_b tidak boleh sama")

        def ke_kode(nama):
            if not nama:
                return None
            if nama.casefold() == a.casefold():
                return "A"
            if nama.casefold() == b.casefold():
                return "B"
            raise ValueError(f"'{nama}' bukan salah satu klub di match ini")

        pen, tr = ke_kode(d["pemenang_penalti"]), ke_kode(d["tuan_rumah"])
        ra = self.rating.get(a, self.rating_awal)
        rb = self.rating.get(b, self.rating_awal)
        h = hitung_match(ra, rb, d["gol_a"], d["gol_b"], d["kompetisi"], d["babak"],
                         pen, tr, self.bonus_tuan_rumah, self.pembagi)

        catatan = ""
        if h.k == 0:
            catatan = "tidak berpengaruh (K = 0)"   # klub baru tidak ikut dibuat
        else:
            self.rating[a], self.rating[b] = h.rating_baru_a, h.rating_baru_b

        return {
            "no": nomor, "kompetisi": d["kompetisi"], "babak": d["babak"], "k": h.k,
            "klub_a": a, "gol_a": d["gol_a"], "gol_b": d["gol_b"], "klub_b": b,
            "pemenang_penalti": {"A": a, "B": b, None: ""}[pen],
            "tuan_rumah": {"A": a, "B": b, None: ""}[tr],
            "rating_a_sebelum": round(ra, 2), "perubahan_a": round(h.perubahan_a, 2),
            "rating_a_sesudah": round(ra + h.perubahan_a if h.k else ra, 2),
            "rating_b_sebelum": round(rb, 2), "perubahan_b": round(h.perubahan_b, 2),
            "rating_b_sesudah": round(rb + h.perubahan_b if h.k else rb, 2),
            "catatan": catatan,
        }

    def koreksi_akhir_musim(self, alpha: float = 0.2) -> float:
        """Tarik semua rating ke rata-rata: P = P + alpha * (rata_rata - P). Mengembalikan rata-rata."""
        if not self.rating:
            return 0.0
        rata_rata = sum(self.rating.values()) / len(self.rating)
        for klub, nilai in self.rating.items():
            self.rating[klub] = nilai + alpha * (rata_rata - nilai)
        return rata_rata

    def peringkat(self) -> list[tuple[str, float]]:
        return sorted(self.rating.items(), key=lambda x: (-x[1], x[0].casefold()))


# ---------------------------------------------------------------------------
# Membaca CSV (dari kotak teks atau file unggahan)
# ---------------------------------------------------------------------------
FORMAT_BARIS = "klub_a, klub_b, gol_a, gol_b, kompetisi[, babak[, pemenang_penalti[, tuan_rumah]]]"

CONTOH_RATING = (
    "klub,rating\n"
    "Klub A,1600\n"
    "Klub B,1500\n"
    "Klub C,1450\n"
)

CONTOH_MATCH = (
    "klub_a,klub_b,gol_a,gol_b,kompetisi,babak,pemenang_penalti,tuan_rumah\n"
    "Klub A,Klub B,2,1,liga\n"
    "Klub A,Klub C,1,1,ucl,akhir,Klub C\n"
    "Klub B,Klub C,3,0,piala_kedua,akhir\n"
    "Klub A,Klub B,0,4,persahabatan\n"
    "Klub D,Klub A,2,2,uecl,awal\n"
)


def _deteksi_pemisah(teks: str) -> str:
    """Koma (default) atau titik koma (umum dari Excel berbahasa Indonesia)."""
    sampel = "\n".join(
        b for b in teks.splitlines() if b.strip() and not b.strip().startswith("#")
    )[:2000]
    pk = sampel.count(";")
    return ";" if pk > 0 and pk >= sampel.count(",") else ","


def baca_csv(teks: str, header: tuple[str, str]) -> list[tuple[int, list[str]]]:
    """
    Pecah teks CSV menjadi [(nomor_baris, kolom)].
    Baris kosong, baris komentar (#), dan baris header dilewati.
    """
    hasil = []
    try:
        pembaca = csv.reader(io.StringIO(teks), delimiter=_deteksi_pemisah(teks),
                             skipinitialspace=True)
        for kolom in pembaca:
            nomor = pembaca.line_num
            kolom = [k.strip() for k in kolom]
            if not any(kolom) or kolom[0].startswith("#"):
                continue
            if tuple(k.casefold() for k in kolom[:2]) == header:
                continue
            hasil.append((nomor, kolom))
    except csv.Error as e:
        raise ValueError(f"format CSV tidak valid: {e}")
    return hasil


def parse_rating(teks: str) -> tuple[dict[str, float], list[str]]:
    """Baca CSV rating (klub,rating). Mengembalikan (data, daftar_error)."""
    data: dict[str, float] = {}
    errors: list[str] = []
    terlihat: set[str] = set()
    try:
        baris = baca_csv(teks, ("klub", "rating"))
    except ValueError as e:
        return {}, [str(e)]
    for nomor, kolom in baris:
        if len(kolom) < 2 or not kolom[0]:
            errors.append(f"Baris {nomor}: butuh 2 kolom: klub,rating")
            continue
        if len(kolom) > 2 and any(kolom[2:]):
            errors.append(f"Baris {nomor}: hanya boleh 2 kolom (gunakan titik untuk desimal, contoh 1523.5)")
            continue
        nama = _rapikan_nama(kolom[0])
        if nama.casefold() in terlihat:
            errors.append(f"Baris {nomor}: klub '{nama}' tercantum lebih dari sekali")
            continue
        try:
            nilai = float(kolom[1].replace(",", "."))
            if not math.isfinite(nilai):
                raise ValueError
        except ValueError:
            errors.append(f"Baris {nomor}: rating '{kolom[1]}' bukan angka")
            continue
        terlihat.add(nama.casefold())
        data[nama] = nilai
    return data, errors


def _normal(s: str) -> str:
    return s.strip().lower().replace(" ", "_").replace("-", "_")


def parse_match(kolom: list[str]) -> dict:
    """Ubah satu baris CSV menjadi dict match. Melempar ValueError jika tidak valid."""
    kolom = [k.strip() for k in kolom]
    while len(kolom) > 8 and not kolom[-1]:
        kolom.pop()
    if len(kolom) < 5:
        raise ValueError(f"minimal 5 kolom: {FORMAT_BARIS}")
    if len(kolom) > 8:
        raise ValueError("maksimal 8 kolom (nama klub yang mengandung koma harus diberi tanda kutip)")
    kolom += [""] * (8 - len(kolom))

    klub_a, klub_b = _rapikan_nama(kolom[0]), _rapikan_nama(kolom[1])
    if not klub_a or not klub_b:
        raise ValueError("nama klub tidak boleh kosong")
    if klub_a.casefold() == klub_b.casefold():
        raise ValueError("klub_a dan klub_b tidak boleh sama")
    try:
        gol_a, gol_b = int(kolom[2]), int(kolom[3])
    except ValueError:
        raise ValueError("gol_a dan gol_b harus bilangan bulat")
    if gol_a < 0 or gol_b < 0:
        raise ValueError("gol tidak boleh negatif")

    kompetisi = ALIAS_KOMPETISI.get(_normal(kolom[4]), _normal(kolom[4]))
    if kompetisi not in NILAI_K:
        raise ValueError(f"kompetisi '{kolom[4]}' tidak dikenal. Pilihan: {', '.join(NILAI_K)}")
    babak = ALIAS_BABAK.get(_normal(kolom[5] or "awal"))
    if babak is None:
        raise ValueError(f"babak '{kolom[5]}' tidak dikenal. Pilihan: awal atau akhir")

    return {
        "klub_a": klub_a, "klub_b": klub_b, "gol_a": gol_a, "gol_b": gol_b,
        "kompetisi": kompetisi, "babak": babak,
        "pemenang_penalti": _rapikan_nama(kolom[6]) or None,
        "tuan_rumah": _rapikan_nama(kolom[7]) or None,
    }


def proses_match(papan: PapanRating, teks: str, nomor_awal: int):
    """
    Proses semua match pada teks, berurutan, pada SALINAN papan.
    Mengembalikan (papan_baru, entri_log, daftar_error).
    """
    kerja = copy.deepcopy(papan)
    entri: list[dict] = []
    errors: list[str] = []
    try:
        baris = baca_csv(teks, ("klub_a", "klub_b"))
    except ValueError as e:
        return kerja, entri, [str(e)]
    for nomor, kolom in baris:
        try:
            d = parse_match(kolom)
            entri.append(kerja.catat_match(d, nomor_awal + len(entri)))
        except ValueError as e:
            errors.append(f"Baris {nomor}: {e}")
    return kerja, entri, errors


def ke_csv(baris: list[dict], kolom: list[str] | None = None) -> bytes:
    """Serialisasi list dict menjadi CSV (UTF-8 dengan BOM agar terbaca di Excel)."""
    buf = io.StringIO()
    if kolom is None:
        kolom = list(baris[0].keys()) if baris else []
    w = csv.DictWriter(buf, fieldnames=kolom, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(baris)
    return buf.getvalue().encode("utf-8-sig")


def baca_unggahan(berkas) -> str:
    mentah = berkas.getvalue()
    try:
        return mentah.decode("utf-8-sig")
    except UnicodeDecodeError:
        return mentah.decode("latin-1")


# ---------------------------------------------------------------------------
# State sesi & callback tombol
# ---------------------------------------------------------------------------
def inisialisasi() -> None:
    ss = st.session_state
    if "papan" not in ss:
        ss["papan"] = PapanRating()
    ss.setdefault("log", [])             # seluruh match yang diproses di sesi ini
    ss.setdefault("batch_terakhir", [])  # match pada proses terakhir
    ss.setdefault("undo", [])            # [(rating_sebelum, panjang_log, label)]
    ss.setdefault("pesan", [])           # [(jenis, teks)] ditampilkan sekali
    ss.setdefault("versi_unggah", 0)     # untuk mengosongkan file uploader


def sinkron_parameter() -> None:
    """Salin parameter sidebar ke papan (dipanggil tiap render dan di awal tiap callback)."""
    ss = st.session_state
    p = ss["papan"]
    p.rating_awal = float(ss.get("p_rating_awal", RATING_AWAL))
    p.pembagi = float(ss.get("p_pembagi", PEMBAGI))
    p.bonus_tuan_rumah = float(ss.get("p_bonus", BONUS_TUAN_RUMAH))


def _pesan(jenis: str, teks: str, rincian: list[str] | None = None) -> None:
    if rincian:
        tampil = rincian[:BATAS_ERROR_TAMPIL]
        sisa = len(rincian) - len(tampil)
        teks += "\n\n" + "\n".join(f"- {r}" for r in tampil)
        if sisa > 0:
            teks += f"\n- ... dan {sisa} masalah lainnya"
    st.session_state["pesan"].append((jenis, teks))


def _simpan_undo(label: str) -> None:
    ss = st.session_state
    ss["undo"].append((dict(ss["papan"].rating), len(ss["log"]), label))
    del ss["undo"][:-BATAS_UNDO]


def _ambil_teks(kunci_teks: str, kunci_berkas: str) -> str:
    """Isi file unggahan diutamakan; jika tidak ada, pakai teks yang ditempel."""
    ss = st.session_state
    berkas = ss.get(f"{kunci_berkas}_{ss['versi_unggah']}")
    if berkas is not None:
        return baca_unggahan(berkas)
    return ss.get(kunci_teks, "")


def _bersihkan_input(kunci_teks: str) -> None:
    ss = st.session_state
    ss[kunci_teks] = ""
    ss["versi_unggah"] += 1


def aksi_isi_contoh(kunci_teks: str, isi: str) -> None:
    st.session_state[kunci_teks] = isi


def aksi_terapkan_rating() -> None:
    ss = st.session_state
    sinkron_parameter()
    teks = _ambil_teks("teks_rating", "berkas_rating")
    if not teks.strip():
        _pesan("warning", "Belum ada data rating. Tempel CSV rating atau unggah file CSV terlebih dahulu.")
        return
    data, errors = parse_rating(teks)
    if errors:
        _pesan("error", "Rating tidak diterapkan karena ada baris yang salah. Perbaiki lalu coba lagi:", errors)
        return
    if not data:
        _pesan("warning", "Tidak ada baris rating yang terbaca.")
        return

    _simpan_undo("terapkan rating")
    papan = ss["papan"]
    if ss.get("mode_rating") == "Ganti semua rating":
        papan.rating = {}
    for klub, nilai in data.items():
        papan.atur(klub, nilai)
    ss["batch_terakhir"] = []
    _bersihkan_input("teks_rating")
    _pesan("success", f"Rating {len(data)} klub diterapkan. Total klub sekarang: {len(papan.rating)}.")


def aksi_proses_match() -> None:
    ss = st.session_state
    sinkron_parameter()
    teks = _ambil_teks("teks_match", "berkas_match")
    if not teks.strip():
        _pesan("warning", "Belum ada data match. Tempel CSV match atau unggah file CSV terlebih dahulu.")
        return

    papan_lama = ss["papan"]
    papan_baru, entri, errors = proses_match(papan_lama, teks, len(ss["log"]) + 1)

    if not entri and not errors:
        _pesan("warning", "Tidak ada baris match yang terbaca.")
        return
    if errors and ss.get("atomik", True):
        _pesan("error", "Tidak ada match yang diproses karena ada baris yang salah. "
                        "Perbaiki lalu coba lagi (atau matikan opsi pembatalan total):", errors)
        return

    klub_baru = sorted(set(papan_baru.rating) - set(papan_lama.rating), key=str.casefold)
    _simpan_undo(f"proses {len(entri)} match")
    ss["papan"] = papan_baru
    ss["log"].extend(entri)
    ss["batch_terakhir"] = entri
    _bersihkan_input("teks_match")

    teks_ok = f"{len(entri)} match diproses."
    if klub_baru:
        teks_ok += (f" Klub baru otomatis dibuat dengan rating awal {papan_baru.rating_awal:g}: "
                    + ", ".join(klub_baru) + ".")
    if errors:
        _pesan("warning", teks_ok + f" {len(errors)} baris dilewati:", errors)
    else:
        _pesan("success", teks_ok)


def aksi_koreksi() -> None:
    ss = st.session_state
    sinkron_parameter()
    papan = ss["papan"]
    if not papan.rating:
        _pesan("warning", "Belum ada klub, tidak ada yang dikoreksi.")
        return
    alpha = float(ss.get("alpha", 0.2))
    _simpan_undo("koreksi akhir musim")
    rata = papan.koreksi_akhir_musim(alpha)
    ss["batch_terakhir"] = []
    _pesan("success", f"Koreksi akhir musim diterapkan: semua rating ditarik {alpha:.0%} "
                      f"ke rata-rata {rata:.2f}.")


def aksi_undo() -> None:
    ss = st.session_state
    if not ss["undo"]:
        _pesan("info", "Tidak ada aksi yang bisa dibatalkan.")
        return
    rating, panjang_log, label = ss["undo"].pop()
    ss["papan"].rating = rating
    del ss["log"][panjang_log:]
    ss["batch_terakhir"] = []
    _pesan("info", f"Aksi dibatalkan: {label}.")


def aksi_reset() -> None:
    ss = st.session_state
    if not ss.get("konfirmasi_reset"):
        _pesan("warning", "Centang kotak konfirmasi terlebih dahulu untuk menghapus semua data.")
        return
    ss["papan"].rating = {}
    ss["log"] = []
    ss["batch_terakhir"] = []
    ss["undo"] = []
    ss["konfirmasi_reset"] = False
    _pesan("info", "Semua rating dan riwayat match sudah dihapus.")


# ---------------------------------------------------------------------------
# Tampilan
# ---------------------------------------------------------------------------
def tampilkan_pesan() -> None:
    ss = st.session_state
    for jenis, teks in ss["pesan"]:
        getattr(st, jenis)(teks)
    ss["pesan"] = []


def sidebar() -> None:
    with st.sidebar:
        st.header("Parameter")
        st.number_input("Rating awal klub baru", min_value=0.0, value=RATING_AWAL, step=50.0,
                        key="p_rating_awal",
                        help="Dipakai untuk klub yang muncul di data match tetapi belum punya rating.")
        st.number_input("Pembagi di rumus We", min_value=50.0, value=PEMBAGI, step=50.0,
                        key="p_pembagi",
                        help="400 = Elo klasik; 600 = seperti FIFA (selisih rating terasa lebih kecil pengaruhnya).")
        st.number_input("Bonus rating tuan rumah", min_value=0.0, value=BONUS_TUAN_RUMAH, step=10.0,
                        key="p_bonus",
                        help="Ditambahkan ke rating tuan rumah hanya di dalam perhitungan We, bukan ke rating aslinya. "
                             "Isi 0 untuk mematikan. Baru berpengaruh jika kolom tuan_rumah diisi di CSV match.")
        st.caption("Perubahan parameter hanya berlaku untuk match yang diproses sesudahnya.")

        with st.expander("Nilai K per kompetisi"):
            st.table([
                {"kompetisi": nama, "K babak awal": v["awal"], "K babak akhir": v["akhir"]}
                for nama, v in NILAI_K.items()
            ])
            st.caption(f"Adu penalti: pemenang {NILAI_PENALTI_MENANG:g}, yang kalah "
                       f"{NILAI_PENALTI_KALAH:g}. Menang 1 / kalah 0 / seri 0,5 - 0,5. "
                       "Untuk mengubah nilai K, edit konstanta NILAI_K di main.py.")


def panduan() -> None:
    ss = st.session_state
    with st.expander("Panduan singkat", expanded=not ss["papan"].rating and not ss["log"]):
        st.markdown(
            """
**Alur pemakaian**

1. **(Opsional) Rating klub** - tab *1. Rating klub*: masukkan rating awal klub dalam format CSV.
   Klub yang tidak ada di daftar akan otomatis mulai dari *rating awal* (sidebar).
2. **Input match** - tab *2. Input match*: tempel atau unggah daftar match dalam format CSV, lalu tekan **Proses match**.
   Urutan baris dianggap urutan kronologis, karena rating berubah setelah tiap laga.
3. **Aksi** - tab *3. Aksi*: koreksi akhir musim, batalkan aksi terakhir, atau reset.
4. **Unduh** - di bagian bawah halaman, unduh CSV rating. File itu bisa ditempel kembali di tab 1 untuk musim berikutnya.

**Penting:** data hanya disimpan selama halaman ini terbuka. Menutup atau me-refresh halaman
menghapus semuanya, jadi **unduh CSV rating sebelum keluar**.
            """
        )


def tab_rating() -> None:
    ss = st.session_state
    st.markdown(
        """
Format CSV rating: dua kolom, `klub,rating`. Satu klub per baris.

- Baris pertama (header) boleh ada atau tidak.
- Gunakan **titik** untuk desimal (contoh `1523.5`).
- Baris yang diawali `#` diabaikan. Pemisah titik koma (`;`) dari Excel juga dikenali.
- Nama klub tidak peka huruf besar/kecil: `klub a` dan `Klub A` dianggap klub yang sama.
        """
    )
    st.code(CONTOH_RATING, language="text")

    st.text_area("Tempel CSV rating di sini", key="teks_rating", height=160,
                 placeholder="klub,rating\nKlub A,1600\nKlub B,1500")
    st.file_uploader("...atau unggah file CSV rating", type=["csv", "txt"],
                     key=f"berkas_rating_{ss['versi_unggah']}",
                     help="Jika kotak teks dan file sama-sama diisi, isi file yang dipakai.")
    st.radio("Cara menerapkan", ["Ganti semua rating", "Tambah atau perbarui klub"],
             key="mode_rating", horizontal=True,
             help="'Ganti semua' menghapus rating yang ada lalu memakai daftar ini. "
                  "'Tambah atau perbarui' hanya menambah klub baru dan menimpa klub yang namanya sama.")

    c1, c2, c3 = st.columns([1, 1, 1])
    c1.button("Terapkan rating", type="primary", on_click=aksi_terapkan_rating, key="btn_terapkan_rating")
    c2.button("Isi dengan contoh", on_click=aksi_isi_contoh, args=("teks_rating", CONTOH_RATING),
              key="btn_contoh_rating")
    c3.download_button("Unduh template rating", data=CONTOH_RATING.encode("utf-8"),
                       file_name="template_rating.csv", mime="text/csv", key="dl_template_rating")


def tab_match() -> None:
    ss = st.session_state
    st.markdown(f"Format CSV match, satu laga per baris: `{FORMAT_BARIS}`")
    st.markdown(
        """
| Kolom | Wajib | Isi |
|---|---|---|
| `klub_a`, `klub_b` | ya | Nama dua klub yang bertanding |
| `gol_a`, `gol_b` | ya | Skor akhir (bilangan bulat, tanpa adu penalti) |
| `kompetisi` | ya | `persahabatan`, `piala_kedua`, `piala_utama`, `liga`, `uel_uecl`, `ucl` (juga boleh `uel` atau `uecl`) |
| `babak` | tidak | `awal` (default) = babak awal piala, liga, fase grup/liga Eropa. `akhir` = semifinal dan final piala, atau knockout Eropa |
| `pemenang_penalti` | tidak | Nama klub pemenang adu penalti. Hanya boleh diisi jika skor seri |
| `tuan_rumah` | tidak | Nama klub tuan rumah. Berpengaruh hanya jika bonus tuan rumah di sidebar lebih dari 0 |

**Tips:**
- Urutkan baris dari laga paling awal ke paling akhir.
- Kolom opsional yang kosong di tengah ditulis dengan koma berurutan, misalnya `Klub B,Klub C,3,0,piala_kedua,akhir,,Klub B` (penalti kosong, tuan rumah Klub B).
- Nama klub yang mengandung koma harus diberi tanda kutip: `"Klub, FC"`.
- Header boleh ada atau tidak. Baris diawali `#` diabaikan. Pemisah `;` juga dikenali.
- Laga `persahabatan` tidak mengubah rating (K = 0) dan tidak membuat klub baru.
        """
    )
    st.code(CONTOH_MATCH, language="text")

    st.text_area("Tempel CSV match di sini", key="teks_match", height=220,
                 placeholder="Klub A,Klub B,2,1,liga\nKlub A,Klub C,1,1,ucl,akhir,Klub C")
    st.file_uploader("...atau unggah file CSV match", type=["csv", "txt"],
                     key=f"berkas_match_{ss['versi_unggah']}",
                     help="Jika kotak teks dan file sama-sama diisi, isi file yang dipakai.")
    st.checkbox("Batalkan seluruh input jika ada baris yang salah", value=True, key="atomik",
                help="Aktif (disarankan): tidak ada match yang diproses sampai semua baris benar. "
                     "Nonaktif: baris yang salah dilewati dan sisanya tetap diproses.")

    c1, c2, c3 = st.columns([1, 1, 1])
    c1.button("Proses match", type="primary", on_click=aksi_proses_match, key="btn_proses_match")
    c2.button("Isi dengan contoh", on_click=aksi_isi_contoh, args=("teks_match", CONTOH_MATCH),
              key="btn_contoh_match")
    c3.download_button("Unduh template match", data=CONTOH_MATCH.encode("utf-8"),
                       file_name="template_match.csv", mime="text/csv", key="dl_template_match")


def tab_aksi() -> None:
    ss = st.session_state

    st.markdown("**Koreksi akhir musim**")
    st.caption("Menarik semua rating sedikit ke rata-rata: P = P + alpha x (rata-rata - P). "
               "Tekan sekali per akhir musim; setiap tekan menerapkan koreksi lagi. "
               "Jika salah tekan, gunakan *Batalkan aksi terakhir*.")
    st.number_input("Alpha (0 = tanpa koreksi, 1 = semua jadi rata-rata)", min_value=0.0, max_value=1.0,
                    value=0.2, step=0.05, key="alpha")
    st.button("Terapkan koreksi akhir musim", on_click=aksi_koreksi, key="btn_koreksi")

    st.divider()
    st.markdown("**Batalkan aksi terakhir**")
    if ss["undo"]:
        st.caption(f"Aksi terakhir: {ss['undo'][-1][2]}. Riwayat yang bisa dibatalkan: {len(ss['undo'])} aksi.")
    else:
        st.caption("Belum ada aksi yang bisa dibatalkan.")
    st.button("Batalkan aksi terakhir", on_click=aksi_undo, disabled=not ss["undo"], key="btn_undo")

    st.divider()
    st.markdown("**Reset semua**")
    st.caption("Menghapus semua rating, riwayat match, dan riwayat pembatalan.")
    st.checkbox("Saya yakin ingin menghapus semuanya", key="konfirmasi_reset")
    st.button("Reset semua data", on_click=aksi_reset, key="btn_reset")


def tampilkan_hasil() -> None:
    ss = st.session_state
    papan = ss["papan"]

    st.divider()
    st.subheader("Peringkat rating")
    m1, m2, m3 = st.columns(3)
    m1.metric("Jumlah klub", len(papan.rating))
    m2.metric("Match diproses (sesi ini)", len(ss["log"]))
    m3.metric("Rata-rata rating",
              f"{sum(papan.rating.values()) / len(papan.rating):.2f}" if papan.rating else "-")

    if not papan.rating:
        st.info("Belum ada klub. Isi rating di tab 1 atau proses match di tab 2.")
        return

    dasar = ss["undo"][-1][0] if ss["undo"] else None
    baris = []
    for i, (klub, nilai) in enumerate(papan.peringkat(), start=1):
        b = {"Peringkat": i, "Klub": klub, "Rating": round(nilai, 2)}
        if dasar is not None:
            b["Perubahan aksi terakhir"] = round(nilai - dasar.get(klub, papan.rating_awal), 2)
        baris.append(b)
    st.dataframe(
        baris, hide_index=True,
        column_config={
            "Rating": st.column_config.NumberColumn(format="%.2f"),
            "Perubahan aksi terakhir": st.column_config.NumberColumn(format="%+.2f"),
        },
    )

    rating_csv = ke_csv([{"klub": k, "rating": f"{v:.2f}"} for k, v in papan.peringkat()],
                        ["klub", "rating"])
    d1, d2 = st.columns(2)
    d1.download_button("Unduh rating (CSV)", data=rating_csv, file_name="rating_klub.csv",
                       mime="text/csv", key="dl_rating", type="primary",
                       help="Simpan file ini. Tempel kembali di tab 1 untuk melanjutkan di lain waktu.")
    if ss["log"]:
        d2.download_button("Unduh rincian semua match (CSV)", data=ke_csv(ss["log"]),
                           file_name="rincian_match.csv", mime="text/csv", key="dl_log")

    if ss["batch_terakhir"]:
        st.subheader("Hasil proses terakhir")
        st.dataframe(_tabel_match(ss["batch_terakhir"]), hide_index=True)

    if ss["log"]:
        with st.expander(f"Riwayat seluruh match di sesi ini ({len(ss['log'])})"):
            st.dataframe(_tabel_match(ss["log"]), hide_index=True)


def _tabel_match(entri: list[dict]) -> list[dict]:
    hasil = []
    for e in entri:
        pen = f" (pen. {e['pemenang_penalti']})" if e["pemenang_penalti"] else ""
        hasil.append({
            "No": e["no"],
            "Laga": f"{e['klub_a']} {e['gol_a']}-{e['gol_b']} {e['klub_b']}{pen}",
            "Kompetisi": e["kompetisi"], "Babak": e["babak"], "K": e["k"],
            "Rating A": f"{e['rating_a_sebelum']:.2f} -> {e['rating_a_sesudah']:.2f} ({e['perubahan_a']:+.2f})",
            "Rating B": f"{e['rating_b_sebelum']:.2f} -> {e['rating_b_sesudah']:.2f} ({e['perubahan_b']:+.2f})",
            "Catatan": e["catatan"],
        })
    return hasil


def main() -> None:
    st.set_page_config(page_title="Kalkulator Elo Klub", layout="wide")
    inisialisasi()
    sidebar()
    sinkron_parameter()

    st.title("Kalkulator Rating Elo Klub")
    st.caption("Sistem rating zero-sum untuk kompetisi dunia fiksi. "
               "Input memakai format CSV; hasil bisa diunduh sebagai CSV.")

    tampilkan_pesan()
    panduan()

    tab1, tab2, tab3 = st.tabs(["1. Rating klub", "2. Input match", "3. Aksi"])
    with tab1:
        tab_rating()
    with tab2:
        tab_match()
    with tab3:
        tab_aksi()

    tampilkan_hasil()


if __name__ == "__main__":
    main()
