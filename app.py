from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, send_file, session
from functools import wraps
from pathlib import Path
from PIL import Image
import sqlite3
import qrcode
import socket
import os
import io
import calendar
from datetime import date, datetime
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "mbg.db"
QR_DIR = BASE_DIR / "static" / "qrcodes"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
FOOD_PHOTO_DIR = BASE_DIR / "static" / "food_photos"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.template_filter('substr')
def substr_filter(s, start, length):
    """Filter Jinja2: ambil substring dari string s mulai index start sepanjang length."""
    s = str(s) if s is not None else ''
    return s[start:start + length]

QR_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
FOOD_PHOTO_DIR.mkdir(parents=True, exist_ok=True)

def safe_float(value, default=0.0):
    """Konversi nilai ke float dengan aman; mendukung koma sebagai pemisah desimal."""
    try:
        if value:
            value = str(value).replace(',', '.')
        return float(value or 0)
    except (ValueError, TypeError):
        return default

def compress_image(file_obj, save_path, max_size=(800, 800), quality=75):
    """Kompres dan resize gambar upload. Selalu disimpan sebagai JPEG."""
    img = Image.open(file_obj)
    # Konversi ke RGB agar bisa disimpan sebagai JPEG (hapus alpha channel)
    if img.mode in ('RGBA', 'P', 'LA'):
        background = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
        img = background
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    # Resize jika lebih besar dari max_size (pertahankan rasio aspek)
    img.thumbnail(max_size, Image.LANCZOS)
    img.save(save_path, 'JPEG', quality=quality, optimize=True)

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # Izinkan baca bersamaan tulis
    conn.execute("PRAGMA busy_timeout=5000")  # Tunggu 5 detik sebelum error
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS menu_mbg (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tanggal TEXT UNIQUE NOT NULL,
            nama_menu TEXT NOT NULL,
            deskripsi TEXT,
            foto TEXT,
            energi_kecil REAL DEFAULT 0,
            protein_kecil REAL DEFAULT 0,
            lemak_kecil REAL DEFAULT 0,
            karbo_kecil REAL DEFAULT 0,
            serat_kecil REAL DEFAULT 0,
            energi_besar REAL DEFAULT 0,
            protein_besar REAL DEFAULT 0,
            lemak_besar REAL DEFAULT 0,
            karbo_besar REAL DEFAULT 0,
            serat_besar REAL DEFAULT 0,
            dibuat TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        )
    """)
    # Migrasi aman untuk database versi lama yang belum memiliki updated_at.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(menu_mbg)").fetchall()}
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE menu_mbg ADD COLUMN updated_at TEXT")
    conn.execute("UPDATE menu_mbg SET updated_at = COALESCE(updated_at, dibuat, CURRENT_TIMESTAMP)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS menu_item (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id INTEGER NOT NULL,
            urutan INTEGER NOT NULL,
            nama TEXT NOT NULL,
            foto TEXT,
            FOREIGN KEY(menu_id) REFERENCES menu_mbg(id) ON DELETE CASCADE
        )
    """)
    # Migrasi menu lama: pecah nama menu menjadi item dan gunakan foto pustaka
    # jika tersedia. Data lama tetap dipertahankan.
    legacy_rules = [
        (['nasi','beras'], 'nasi_putih.jpg'), (['telur','egg'], 'telur_balado.jpg'),
        (['acar','wortel','timun','ketimun','mentimun'], 'acar_wortel_timun.jpg'),
        (['tahu','tofu'], 'tahu_goreng.jpg'), (['apel','apple'], 'apel.jpg')
    ]
    old_menus = conn.execute("SELECT id,nama_menu FROM menu_mbg").fetchall()
    for m in old_menus:
        count = conn.execute("SELECT COUNT(*) FROM menu_item WHERE menu_id=?", (m['id'],)).fetchone()[0]
        if count == 0:
            items = [x.strip() for x in (m['nama_menu'] or '').replace(';', ',').split(',') if x.strip()]
            for idx, nama in enumerate(items, 1):
                low = nama.lower()
                foto = None
                for keys, fn in legacy_rules:
                    if any(k in low for k in keys) and (FOOD_PHOTO_DIR / fn).exists():
                        foto = fn; break
                conn.execute("INSERT INTO menu_item(menu_id,urutan,nama,foto) VALUES(?,?,?,?)", (m['id'], idx, nama, foto))
    conn.commit()
    conn.close()

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def get_qr_url():
    """Mengembalikan satu alamat QR yang tetap selama target QR tidak diubah."""
    return create_permanent_qr()


def create_permanent_qr():
    # QR dibuat satu kali. Upload/edit menu tidak mengubah gambar QR.
    # Jika MBG_PUBLIC_URL diubah secara sengaja, QR akan dibuat ulang sekali
    # agar target baru sesuai konfigurasi.
    target_file = QR_DIR / "qr_target.txt"
    public_url = os.environ.get("MBG_PUBLIC_URL", "").strip().rstrip("/")

    if public_url:
        desired_base = public_url
    elif target_file.exists():
        desired_base = target_file.read_text(encoding="utf-8").strip().rstrip("/")
    else:
        # Untuk LAN, gunakan IP saat pertama kali membuat QR. Agar benar-benar
        # permanen di jaringan lokal, sebaiknya gunakan DHCP reservation/static IP.
        desired_base = f"http://{get_local_ip()}:5000"

    desired_url = f"{desired_base}/menu/current"
    existing_base = target_file.read_text(encoding="utf-8").strip().rstrip("/") if target_file.exists() else ""

    path = QR_DIR / "qr_ompreng.png"
    target_changed = existing_base != desired_base
    if not path.exists() or target_changed:
        img = qrcode.make(desired_url)
        img.save(path)

    if not target_file.exists() or target_changed:
        target_file.write_text(desired_base, encoding="utf-8")

    return desired_url

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        admin_pass = os.environ.get("ADMIN_PASSWORD", "admin123")
        if password == admin_pass:
            session['logged_in'] = True
            flash("Berhasil login.", "success")
            next_page = request.args.get('next')
            return redirect(next_page or url_for('admin'))
        else:
            flash("Password salah.", "error")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash("Berhasil logout.", "success")
    return redirect(url_for('login'))

@app.route("/")
def index():
    conn = get_db()
    menus = conn.execute(
        "SELECT * FROM menu_mbg ORDER BY tanggal DESC, id DESC"
    ).fetchall()
    conn.close()
    return render_template("index.html", menus=menus)

@app.route("/admin")
@login_required
def admin():
    tanggal_dari = request.args.get("dari", "").strip()
    tanggal_sampai = request.args.get("sampai", "").strip()

    query = "SELECT * FROM menu_mbg WHERE 1=1"
    params = []
    if tanggal_dari:
        query += " AND tanggal >= ?"
        params.append(tanggal_dari)
    if tanggal_sampai:
        query += " AND tanggal <= ?"
        params.append(tanggal_sampai)
    query += " ORDER BY tanggal DESC, id DESC"

    conn = get_db()
    menus = conn.execute(query, params).fetchall()
    total_semua = conn.execute("SELECT COUNT(*) FROM menu_mbg").fetchone()[0]
    total_bulan_ini = conn.execute(
        "SELECT COUNT(*) FROM menu_mbg WHERE strftime('%Y-%m', tanggal)=strftime('%Y-%m','now','localtime')"
    ).fetchone()[0]
    menu_terbaru = conn.execute(
        "SELECT tanggal, updated_at FROM menu_mbg ORDER BY updated_at DESC, id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    # Kalender bulanan admin. Tanggal yang berisi menu dan tanggal kosong sama-sama dapat diklik.
    bulan_param = request.args.get("bulan", "").strip()
    try:
        kalender_tanggal = datetime.strptime(bulan_param, "%Y-%m").date().replace(day=1) if bulan_param else date.today().replace(day=1)
    except ValueError:
        kalender_tanggal = date.today().replace(day=1)
    tahun, bulan = kalender_tanggal.year, kalender_tanggal.month
    cal = calendar.Calendar(firstweekday=0)
    kalender_minggu = cal.monthdatescalendar(tahun, bulan)
    conn = get_db()
    menu_bulan = conn.execute(
        "SELECT tanggal FROM menu_mbg WHERE substr(tanggal,1,7)=?",
        (f"{tahun:04d}-{bulan:02d}",)
    ).fetchall()
    conn.close()
    tanggal_ada_menu = {r["tanggal"] for r in menu_bulan}
    bulan_sebelum = date(tahun - 1, 12, 1) if bulan == 1 else date(tahun, bulan - 1, 1)
    bulan_berikut = date(tahun + 1, 1, 1) if bulan == 12 else date(tahun, bulan + 1, 1)
    nama_bulan = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"][bulan]

    qr_url = create_permanent_qr()
    return render_template(
        "admin.html", menus=menus, qr_url=qr_url,
        tanggal_dari=tanggal_dari, tanggal_sampai=tanggal_sampai,
        total_semua=total_semua, total_bulan_ini=total_bulan_ini,
        menu_terbaru=menu_terbaru, kalender_minggu=kalender_minggu,
        kalender_tanggal=kalender_tanggal, tanggal_ada_menu=tanggal_ada_menu,
        bulan_sebelum=bulan_sebelum.strftime("%Y-%m"),
        bulan_berikut=bulan_berikut.strftime("%Y-%m"),
        nama_bulan=nama_bulan, tahun_kalender=tahun
    )

@app.route("/admin/rekap/<tanggal>")
@login_required
def admin_rekap(tanggal):
    conn = get_db()
    menu = conn.execute("SELECT * FROM menu_mbg WHERE tanggal=?", (tanggal,)).fetchone()
    items = conn.execute("SELECT * FROM menu_item WHERE menu_id=? ORDER BY urutan", (menu['id'],)).fetchall() if menu else []
    conn.close()
    return render_template("admin_detail.html", menu=menu, items=items, tanggal=tanggal)

def parse_menu_items(text):
    return [x.strip() for x in (text or '').replace(';', ',').split(',') if x.strip()]

FOOD_PHOTO_RULES = [
    (['nasi', 'beras'], 'nasi_putih.jpg'),
    (['telur', 'egg'], 'telur_balado.jpg'),
    (['acar', 'wortel', 'timun', 'ketimun', 'mentimun'], 'acar_wortel_timun.jpg'),
    (['tahu', 'tofu'], 'tahu_goreng.jpg'),
    (['apel', 'apple'], 'apel.jpg'),
]

def library_food_photo(nama):
    text = (nama or '').lower()
    for keys, filename in FOOD_PHOTO_RULES:
        if any(k in text for k in keys):
            path = FOOD_PHOTO_DIR / filename
            if path.exists():
                return filename
    return None

def get_menu_items(menu_id):
    conn = get_db()
    rows = conn.execute("SELECT * FROM menu_item WHERE menu_id=? ORDER BY urutan", (menu_id,)).fetchall()
    conn.close()
    return rows

def save_menu_items(conn, menu_id, names, files=None, old_rows=None):
    files = files or {}
    old_rows = old_rows or []
    conn.execute("DELETE FROM menu_item WHERE menu_id=?", (menu_id,))
    for idx, nama in enumerate(names, start=1):
        uploaded = files.get(f'item_foto_{idx}')
        foto_nama = None
        if uploaded and uploaded.filename:
            ext = Path(uploaded.filename).suffix.lower()
            if ext not in ['.jpg', '.jpeg', '.png', '.webp']:
                raise ValueError('Format foto makanan harus JPG, JPEG, PNG, atau WEBP.')
            ts = datetime.now().strftime('%Y%m%d%H%M%S')
            foto_nama = secure_filename(f'menuitem_{menu_id}_{idx}_{ts}.jpg')
            compress_image(uploaded, UPLOAD_DIR / foto_nama)
        elif idx <= len(old_rows) and old_rows[idx-1]['nama'].strip().lower() == nama.strip().lower() and old_rows[idx-1]['foto']:
            foto_nama = old_rows[idx-1]['foto']
        else:
            foto_nama = library_food_photo(nama)
        conn.execute("INSERT INTO menu_item(menu_id,urutan,nama,foto) VALUES(?,?,?,?)", (menu_id, idx, nama, foto_nama))

@app.route("/admin/tambah", methods=["GET", "POST"])
@login_required
def tambah_menu():
    if request.method == "POST":
        tanggal = request.form["tanggal"]
        nama_menu = request.form["nama_menu"].strip()
        deskripsi = request.form.get("deskripsi", "").strip()

        foto_nama = None
        foto = request.files.get("foto")
        if foto and foto.filename:
            ext = Path(foto.filename).suffix.lower()
            if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
                flash("Format foto harus JPG, JPEG, PNG, atau WEBP.", "error")
                return redirect(request.url)
            foto_nama = secure_filename(f"menu_{tanggal}.jpg")
            compress_image(foto, UPLOAD_DIR / foto_nama)

        data = (
            tanggal, nama_menu, deskripsi, foto_nama,
            safe_float(request.form.get("energi_kecil")),
            safe_float(request.form.get("protein_kecil")),
            safe_float(request.form.get("lemak_kecil")),
            safe_float(request.form.get("karbo_kecil")),
            safe_float(request.form.get("serat_kecil")),
            safe_float(request.form.get("energi_besar")),
            safe_float(request.form.get("protein_besar")),
            safe_float(request.form.get("lemak_besar")),
            safe_float(request.form.get("karbo_besar")),
            safe_float(request.form.get("serat_besar")),
        )

        try:
            conn = get_db()
            cur = conn.execute("""
                INSERT INTO menu_mbg
                (tanggal,nama_menu,deskripsi,foto,
                 energi_kecil,protein_kecil,lemak_kecil,karbo_kecil,serat_kecil,
                 energi_besar,protein_besar,lemak_besar,karbo_besar,serat_besar)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, data)
            menu_id = cur.lastrowid
            save_menu_items(conn, menu_id, parse_menu_items(nama_menu))
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE menu_mbg SET updated_at=? WHERE tanggal=?", (now, tanggal))
            conn.commit()
            create_permanent_qr()
            flash("Menu berhasil disimpan. QR permanen tetap sama dan menampilkan menu terakhir yang diperbarui.", "success")
            return redirect(url_for("admin_rekap", tanggal=tanggal))
        except sqlite3.IntegrityError:
            conn.rollback()
            flash("Menu untuk tanggal tersebut sudah ada. Gunakan Edit.", "error")
        except sqlite3.OperationalError:
            conn.rollback()
            flash("Database sedang sibuk, coba lagi.", "error")
        finally:
            conn.close()

    return render_template("form.html", menu=None, items_for_form=[], judul="Tambah Menu MBG")

@app.route("/admin/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit_menu(id):
    conn = get_db()
    menu = conn.execute("SELECT * FROM menu_mbg WHERE id=?", (id,)).fetchone()
    old_items = conn.execute("SELECT * FROM menu_item WHERE menu_id=? ORDER BY urutan", (id,)).fetchall() if menu else []
    conn.close()

    if not menu:
        return "Menu tidak ditemukan", 404

    if request.method == "POST":
        tanggal = request.form["tanggal"]
        nama_menu = request.form["nama_menu"].strip()
        deskripsi = request.form.get("deskripsi", "").strip()

        foto_nama = menu["foto"]
        foto = request.files.get("foto")
        if foto and foto.filename:
            ext = Path(foto.filename).suffix.lower()
            if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
                flash("Format foto harus JPG, JPEG, PNG, atau WEBP.", "error")
                return redirect(request.url)
            ts = datetime.now().strftime('%Y%m%d%H%M%S')
            foto_nama = secure_filename(f"menu_{tanggal}_{ts}.jpg")
            compress_image(foto, UPLOAD_DIR / foto_nama)

        conn = get_db()
        try:
            conn.execute("""
                UPDATE menu_mbg SET
                tanggal=?, nama_menu=?, deskripsi=?, foto=?,
                energi_kecil=?, protein_kecil=?, lemak_kecil=?, karbo_kecil=?, serat_kecil=?,
                energi_besar=?, protein_besar=?, lemak_besar=?, karbo_besar=?, serat_besar=?
                WHERE id=?
            """, (
                tanggal, nama_menu, deskripsi, foto_nama,
                safe_float(request.form.get("energi_kecil")),
                safe_float(request.form.get("protein_kecil")),
                safe_float(request.form.get("lemak_kecil")),
                safe_float(request.form.get("karbo_kecil")),
                safe_float(request.form.get("serat_kecil")),
                safe_float(request.form.get("energi_besar")),
                safe_float(request.form.get("protein_besar")),
                safe_float(request.form.get("lemak_besar")),
                safe_float(request.form.get("karbo_besar")),
                safe_float(request.form.get("serat_besar")),
                id
            ))
            save_menu_items(conn, id, parse_menu_items(nama_menu), old_rows=old_items)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE menu_mbg SET updated_at=? WHERE id=?", (now, id))
            conn.commit()
            create_permanent_qr()
            flash("Menu diperbarui. QR permanen tetap sama dan otomatis menampilkan pembaruan terbaru.", "success")
            return redirect(url_for("admin_rekap", tanggal=tanggal))
        except sqlite3.IntegrityError:
            conn.rollback()
            flash("Tanggal tersebut sudah memiliki menu lain.", "error")
        except sqlite3.OperationalError as e:
            conn.rollback()
            flash(f"Database sedang sibuk, coba lagi.", "error")
        finally:
            conn.close()

    return render_template("form.html", menu=menu, items_for_form=[dict(x) for x in old_items], judul="Edit Menu MBG")

@app.post("/admin/hapus/<int:id>")
@login_required
def hapus_menu(id):
    conn = get_db()
    menu = conn.execute("SELECT foto FROM menu_mbg WHERE id=?", (id,)).fetchone()
    item_rows = conn.execute("SELECT foto FROM menu_item WHERE menu_id=?", (id,)).fetchall()
    if menu:
        conn.execute("DELETE FROM menu_item WHERE menu_id=?", (id,))
        conn.execute("DELETE FROM menu_mbg WHERE id=?", (id,))
        conn.commit()
        if menu["foto"]:
            foto = UPLOAD_DIR / menu["foto"]
            if foto.exists():
                foto.unlink()
        for row in item_rows:
            if row["foto"]:
                foto_item = UPLOAD_DIR / row["foto"]
                if foto_item.exists():
                    foto_item.unlink()
    conn.close()
    flash("Menu berhasil dihapus.", "success")
    return redirect(url_for("admin"))



FOOD_ICON_RULES = [
    (['nasi', 'beras'], '🍚'),
    (['ayam', 'chicken'], '🍗'),
    (['telur', 'egg'], '🥚'),
    (['ikan', 'fish'], '🐟'),
    (['daging', 'sapi', 'rendang', 'beef'], '🥩'),
    (['udang', 'shrimp'], '🦐'),
    (['tahu', 'tofu'], '🟨'),
    (['tempe'], '🟫'),
    (['acar', 'wortel', 'timun', 'ketimun', 'mentimun', 'sayur'], '🥕'),
    (['kangkung', 'bayam', 'brokoli', 'kol', 'kubis', 'sawi', 'buncis'], '🥬'),
    (['jagung'], '🌽'),
    (['kentang'], '🥔'),
    (['sup', 'sop'], '🍲'),
    (['mie', 'mi'], '🍜'),
    (['bakso'], '🍢'),
    (['sambal', 'sambel'], '🌶️'),
    (['pisang', 'banana'], '🍌'),
    (['apel', 'apple'], '🍎'),
    (['jeruk', 'orange'], '🍊'),
    (['mangga', 'mango'], '🥭'),
    (['semangka', 'watermelon'], '🍉'),
    (['melon'], '🍈'),
    (['pepaya', 'papaya'], '🍈'),
    (['nanas', 'pineapple'], '🍍'),
    (['anggur', 'grape'], '🍇'),
    (['stroberi', 'strawberry'], '🍓'),
    (['kelengkeng', 'longan'], '🍈'),
    (['roti', 'bread'], '🍞'),
    (['susu', 'milk'], '🥛'),
    (['keju', 'cheese'], '🧀'),
]

def food_icon(nama):
    text = (nama or '').strip().lower()
    for keywords, icon in FOOD_ICON_RULES:
        if any(k in text for k in keywords):
            return icon
    return '🍱'

app.jinja_env.globals['food_icon'] = food_icon

def format_tanggal_id(tgl):
    try:
        d = datetime.strptime(tgl, "%Y-%m-%d")
        hari = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"][d.weekday()]
        bulan = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"][d.month-1]
        return f"{hari}, {d.day} {bulan} {d.year}"
    except Exception:
        return tgl

@app.route("/menu/current")
def menu_current():
    conn = get_db()
    # QR permanen selalu menampilkan menu terakhir yang diupload admin.
    # Tidak bergantung pada tanggal hari server, sehingga menu tetap tampil saat admin
    # melakukan upload untuk tanggal distribusi berikutnya.
    menu = conn.execute("""
        SELECT * FROM menu_mbg
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
    """).fetchone()

    if not menu:
        conn.close()
        return render_template("not_found.html", kode="Belum ada menu yang aktif"), 404
    items = conn.execute("SELECT * FROM menu_item WHERE menu_id=? ORDER BY urutan", (menu["id"],)).fetchall()
    conn.close()
    return render_template("menu.html", menu=menu, items=items, tanggal_indonesia=format_tanggal_id(menu["tanggal"]))

@app.route("/menu/<int:id>")
def menu_detail(id):
    conn = get_db()
    menu = conn.execute("SELECT * FROM menu_mbg WHERE id=?", (id,)).fetchone()
    items = conn.execute("SELECT * FROM menu_item WHERE menu_id=? ORDER BY urutan", (id,)).fetchall() if menu else []
    conn.close()
    if not menu:
        return render_template("not_found.html", kode=str(id)), 404
    return render_template("menu.html", menu=menu, items=items, tanggal_indonesia=format_tanggal_id(menu["tanggal"]))

@app.route("/scan")
def scan():
    return render_template("scan.html")

@app.route("/qr-ompreng")
def qr_ompreng():
    return send_from_directory(QR_DIR, "qr_ompreng.png")

@app.route("/qr-download")
def qr_download():
    return send_file(QR_DIR / "qr_ompreng.png", as_attachment=True, download_name="QR_Ompreng_MBG_Permanen.png")

if __name__ == "__main__":
    init_db()
    qr_url = create_permanent_qr()
    print("=" * 55)
    print(" SISTEM MENU MBG - QR OMPRENG PERMANEN")
    print("=" * 55)
    print(f" QR permanen: {qr_url}")
    print(" Admin       : http://127.0.0.1:5000/admin")
    print(" Laptop      : http://127.0.0.1:5000")
    print("=" * 55)
    debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1", "t")
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)