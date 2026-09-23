// SISTEM MENU MBG - Node.js/Express/Nunjucks edition
// Migrated from the Flask version while preserving the same routes, UI templates,
// database model, permanent QR concept, and user flow.

const path = require('path');
const fs = require('fs');
const os = require('os');
const express = require('express');
const session = require('express-session');
const multer = require('multer');
const { DatabaseSync } = require('node:sqlite');
const QRCode = require('qrcode');
const nunjucks = require('nunjucks');

const app = express();
const BASE_DIR = __dirname;
const DB_PATH = path.join(BASE_DIR, 'mbg.db');
const STATIC_DIR = path.join(BASE_DIR, 'static');
const QR_DIR = path.join(STATIC_DIR, 'qrcodes');
const UPLOAD_DIR = path.join(STATIC_DIR, 'uploads');
const FOOD_PHOTO_DIR = path.join(STATIC_DIR, 'food_photos');

for (const dir of [QR_DIR, UPLOAD_DIR, FOOD_PHOTO_DIR]) fs.mkdirSync(dir, { recursive: true });

app.use(express.urlencoded({ extended: true }));
app.use(express.json());
app.use(session({ secret: process.env.MBG_SESSION_SECRET || 'mbg-secret-key', resave: false, saveUninitialized: true }));
app.use('/static', express.static(STATIC_DIR));

// Multer: only the main ompreng/menu photo is accepted.
const upload = multer({
  storage: multer.diskStorage({
    destination: (_req, _file, cb) => cb(null, UPLOAD_DIR),
    filename: (req, file, cb) => {
      const ext = path.extname(file.originalname).toLowerCase();
      cb(null, `menu_${req.body.tanggal || Date.now()}${ext}`);
    }
  }),
  fileFilter: (_req, file, cb) => {
    const ok = ['.jpg', '.jpeg', '.png', '.webp'].includes(path.extname(file.originalname).toLowerCase());
    cb(ok ? null : new Error('Format foto harus JPG, JPEG, PNG, atau WEBP.'), ok);
  }
});

const db = new DatabaseSync(DB_PATH);

// Compatibility helper replacing better-sqlite3's transaction() API.
function transaction(fn) {
  return (...args) => {
    db.exec('BEGIN');
    try {
      const result = fn(...args);
      db.exec('COMMIT');
      return result;
    } catch (err) {
      try { db.exec('ROLLBACK'); } catch {}
      throw err;
    }
  };
}
db.exec('PRAGMA foreign_keys = ON');

db.exec(`
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
);
CREATE TABLE IF NOT EXISTS menu_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  menu_id INTEGER NOT NULL,
  urutan INTEGER NOT NULL,
  nama TEXT NOT NULL,
  foto TEXT,
  FOREIGN KEY(menu_id) REFERENCES menu_mbg(id) ON DELETE CASCADE
);
`);

// Safe migration for databases created by older versions.
const cols = db.prepare('PRAGMA table_info(menu_mbg)').all().map(r => r.name);
if (!cols.includes('updated_at')) db.exec('ALTER TABLE menu_mbg ADD COLUMN updated_at TEXT');
db.prepare("UPDATE menu_mbg SET updated_at = COALESCE(updated_at, dibuat, CURRENT_TIMESTAMP)").run();

const env = nunjucks.configure(path.join(BASE_DIR, 'templates'), {
  autoescape: true,
  express: app,
  noCache: true
});

env.addFilter('format', (value, fmt) => {
  // Supports both Jinja-style '%02d'|format(loop.index) and
  // the natural Nunjucks value|format('%02d') form.
  if (typeof value === 'string' && value === '%02d' && Number.isFinite(Number(fmt))) return String(Number(fmt)).padStart(2, '0');
  if (fmt === '%02d') return String(Number(value)).padStart(2, '0');
  return String(value ?? '');
});
env.addFilter('trim', value => String(value ?? '').trim());
env.addFilter('monthprefix', value => String(value ?? '').substring(0, 7));
env.addFilter('round', (value, digits = 0) => Number(Number(value || 0).toFixed(Number(digits))));
env.addFilter('gizi', value => {
  if (value === null || value === undefined || value === '') return '';
  const str = String(value).replace(',', '.').trim();
  const num = Number(str);
  if (isNaN(num)) return String(value);
  if (Number.isInteger(num)) return String(num);
  const rounded = Math.round(num * 10) / 10;
  if (Number.isInteger(rounded)) return String(rounded);
  return String(rounded).replace('.', ',');
});
env.addFilter('substr', (value, start, length) => String(value ?? '').substring(Number(start) || 0, (Number(start) || 0) + Number(length)));

env.addGlobal('url_for', function(route, kwargs = {}) {
  const routes = {
    index: '/', admin: '/admin', tambah_menu: '/admin/tambah',
    admin_rekap: '/admin/rekap/:tanggal', edit_menu: '/admin/edit/:id',
    hapus_menu: '/admin/hapus/:id', menu_current: '/menu/current',
    menu_detail: '/menu/:id', scan: '/scan', qr_ompreng: '/qr-ompreng',
    qr_download: '/qr-download', static: '/static/:filename'
  };
  let url = routes[route] || '/';
  if (route === 'admin_rekap') url = `/admin/rekap/${encodeURIComponent(kwargs.tanggal)}`;
  else if (route === 'edit_menu') url = `/admin/edit/${encodeURIComponent(kwargs.id)}`;
  else if (route === 'hapus_menu') url = `/admin/hapus/${encodeURIComponent(kwargs.id)}`;
  else if (route === 'menu_detail') url = `/menu/${encodeURIComponent(kwargs.id)}`;
  else if (route === 'static') url = `/static/${String(kwargs.filename).split('/').map(encodeURIComponent).join('/')}`;
  const query = Object.entries(kwargs)
    .filter(([k,v]) => !['tanggal', 'id', 'filename'].includes(k) && v !== undefined)
    .map(([k,v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('&');
  return query ? `${url}?${query}` : url;
});

function templateRequest(req) {
  return {
    args: { get: (key, fallback = '') => req.query?.[key] ?? fallback },
    query: req.query,
    method: req.method
  };
}
function consumeFlashes(req) {
  const flashes = req.session.flashes || [];
  req.session.flashes = [];
  return flashes;
}
function render(res, req, template, data = {}) {
  res.render(template, {
    ...data,
    request: templateRequest(req),
    flash_messages: consumeFlashes(req)
  });
}
function flash(req, message, category = 'success') {
  if (!req.session.flashes) req.session.flashes = [];
  req.session.flashes.push([category, message]);
}
function rows(sql, params = []) { return db.prepare(sql).all(...params); }
function row(sql, params = []) { return db.prepare(sql).get(...params); }

function parseNutritionNumber(value) {
  // Accept both Indonesian decimal comma (517,6) and standard decimal dot (517.6).
  const normalized = String(value ?? '').trim().replace(',', '.');
  if (normalized === '') return 0;
  const n = Number(normalized);
  return Number.isFinite(n) ? n : 0;
}

function parseMenuItems(text) {
  return String(text || '').replace(/;/g, ',').split(',').map(x => x.trim()).filter(Boolean);
}
function saveMenuItems(menuId, names) {
  // This function is intentionally transaction-free because it is called
  // from the outer add/edit transaction. Starting a nested SQLite transaction
  // causes: "cannot start a transaction within a transaction".
  db.prepare('DELETE FROM menu_item WHERE menu_id=?').run(menuId);
  const ins = db.prepare('INSERT INTO menu_item(menu_id,urutan,nama,foto) VALUES(?,?,?,NULL)');
  names.forEach((nama, i) => ins.run(menuId, i + 1, nama));
}
function getMenuItems(menuId) { return rows('SELECT * FROM menu_item WHERE menu_id=? ORDER BY urutan', [menuId]); }
function nowLocalSql() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function getLocalIp() {
  if (process.env.MBG_HOST_IP) return process.env.MBG_HOST_IP.trim();
  const nets = os.networkInterfaces();
  const candidates = [];
  for (const [name, entries] of Object.entries(nets)) {
    for (const net of entries || []) {
      if (net.family === 'IPv4' && !net.internal) {
        const ip = net.address;
        const privateRank = ip.startsWith('192.168.') ? 0 : (ip.startsWith('10.') ? 1 : (/^172\.(1[6-9]|2[0-9]|3[0-1])\./.test(ip) ? 2 : 3));
        const wifiRank = /wi-?fi|wireless/i.test(name) ? -1 : 0;
        candidates.push({ ip, rank: privateRank + wifiRank });
      }
    }
  }
  candidates.sort((a,b) => a.rank - b.rank);
  return candidates[0]?.ip || '127.0.0.1';
}

async function createPermanentQr() {
  // QR OMPRENG PERMANEN:
  // - QR dibuat SATU KALI dan disimpan di static/qrcodes/qr_ompreng.png.
  // - Setelah dibuat, target QR TIDAK diganti ketika menu di-update.
  // - Untuk produksi/stiker, isi MBG_PUBLIC_URL dengan domain tetap, misalnya:
  //   https://menu.sppg.example
  // - Jika MBG_PUBLIC_URL tidak diisi, sistem mengambil IP LAN saat pertama kali
  //   dijalankan dan menguncinya. Ini cocok hanya untuk pengujian lokal.
  const targetFile = path.join(QR_DIR, 'qr_target.txt');
  const qrPath = path.join(QR_DIR, 'qr_ompreng.png');

  let lockedBase = fs.existsSync(targetFile)
    ? fs.readFileSync(targetFile, 'utf8').trim().replace(/\/$/, '')
    : '';

  const configuredBase = (process.env.MBG_PUBLIC_URL || '').trim().replace(/\/$/, '');

  // Jika QR belum pernah dibuat, kunci target pertama.
  if (!lockedBase) {
    lockedBase = configuredBase || `http://${getLocalIp()}:5000`;
    fs.writeFileSync(targetFile, lockedBase, 'utf8');
  }

  const desiredUrl = `${lockedBase}/menu/current`;

  // Jangan regenerate QR hanya karena menu berubah atau server restart.
  if (!fs.existsSync(qrPath)) {
    await QRCode.toFile(qrPath, desiredUrl, {
      margin: 2,
      width: 500,
      errorCorrectionLevel: 'H'
    });
  }

  return desiredUrl;
}
function formatTanggalId(tgl) {
  const d = new Date(`${tgl}T00:00:00`);
  if (Number.isNaN(d.getTime())) return tgl;
  const hari = ['Minggu','Senin','Selasa','Rabu','Kamis','Jumat','Sabtu'][d.getDay()];
  const bulan = ['Januari','Februari','Maret','April','Mei','Juni','Juli','Agustus','September','Oktober','November','Desember'][d.getMonth()];
  return `${hari}, ${d.getDate()} ${bulan} ${d.getFullYear()}`;
}
function monthCalendar(year, month) {
  const first = new Date(year, month - 1, 1);
  const start = new Date(year, month - 1, 1 - ((first.getDay() + 6) % 7)); // Monday-first
  const weeks = [];
  for (let w = 0; w < 6; w++) {
    const week = [];
    for (let i = 0; i < 7; i++) {
      const d = new Date(start); d.setDate(start.getDate() + w * 7 + i);
      const p = n => String(n).padStart(2, '0');
      week.push({ day: d.getDate(), month: d.getMonth() + 1, ds: `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}` });
    }
    weeks.push(week);
    if (w >= 3 && week[6].month === month && week[6].day >= 28) break;
  }
  return weeks;
}

// ENTRY POINT / ADMIN:
// Saat URL domain/IP utama dibuka langsung, pengguna diarahkan ke panel Admin.
// Tampilan menu pengguna TIDAK menjadi landing page umum.
app.get('/', (req,res) => {
  res.redirect('/admin');
});

app.get('/admin', async (req,res) => {
  const dari = String(req.query.dari || '').trim();
  const sampai = String(req.query.sampai || '').trim();
  let sql = 'SELECT * FROM menu_mbg WHERE 1=1'; const params=[];
  if (dari) { sql += ' AND tanggal >= ?'; params.push(dari); }
  if (sampai) { sql += ' AND tanggal <= ?'; params.push(sampai); }
  sql += ' ORDER BY tanggal DESC, id DESC';
  const menus = rows(sql, params);
  const total_semua = row('SELECT COUNT(*) c FROM menu_mbg').c;
  const total_bulan_ini = row("SELECT COUNT(*) c FROM menu_mbg WHERE substr(tanggal,1,7)=strftime('%Y-%m','now','localtime')").c;
  const menu_terbaru = row('SELECT tanggal,updated_at FROM menu_mbg ORDER BY updated_at DESC,id DESC LIMIT 1');
  const bulanParam = String(req.query.bulan || '').trim();
  let calendarDate = /^\d{4}-\d{2}$/.test(bulanParam) ? new Date(`${bulanParam}-01T00:00:00`) : new Date(new Date().getFullYear(), new Date().getMonth(), 1);
  if (Number.isNaN(calendarDate.getTime())) calendarDate = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
  const tahun = calendarDate.getFullYear(), bulan = calendarDate.getMonth()+1;
  const pad=n=>String(n).padStart(2,'0');
  const kalender_minggu = monthCalendar(tahun,bulan);
  const tanggal_ada_menu = rows('SELECT tanggal FROM menu_mbg WHERE substr(tanggal,1,7)=?', [`${tahun}-${pad(bulan)}`]).map(r=>r.tanggal);
  const prev = new Date(tahun,bulan-2,1), next = new Date(tahun,bulan,1);
  const nama_bulan=['','Januari','Februari','Maret','April','Mei','Juni','Juli','Agustus','September','Oktober','November','Desember'][bulan];
  const qr_url = await createPermanentQr();
  render(res,req,'admin.html',{menus,qr_url,tanggal_dari:dari,tanggal_sampai:sampai,total_semua,total_bulan_ini,menu_terbaru,kalender_minggu,tanggal_ada_menu,kalender_tanggal_month:bulan,bulan_sebelum:`${prev.getFullYear()}-${pad(prev.getMonth()+1)}`,bulan_berikut:`${next.getFullYear()}-${pad(next.getMonth()+1)}`,nama_bulan,tahun_kalender:tahun});
});

app.get('/admin/rekap/:tanggal', (req,res)=> {
  const tanggal=req.params.tanggal; const menu=row('SELECT * FROM menu_mbg WHERE tanggal=?',[tanggal]);
  const items=menu?getMenuItems(menu.id):[];
  render(res,req,'admin_detail.html',{menu,items,tanggal,tanggal_bulan:tanggal.substring(0,7)});
});

app.get('/admin/tambah',(req,res)=>render(res,req,'form.html',{menu:null,items_for_form:[],judul:'Tambah Menu MBG'}));
app.post('/admin/tambah', upload.single('foto'), async (req,res)=> {
  try {
    const f=req.body;
    const data=[f.tanggal,String(f.nama_menu||'').trim(),String(f.deskripsi||'').trim(),req.file?.filename||null,
      parseNutritionNumber(f.energi_kecil),parseNutritionNumber(f.protein_kecil),parseNutritionNumber(f.lemak_kecil),parseNutritionNumber(f.karbo_kecil),parseNutritionNumber(f.serat_kecil),
      parseNutritionNumber(f.energi_besar),parseNutritionNumber(f.protein_besar),parseNutritionNumber(f.lemak_besar),parseNutritionNumber(f.karbo_besar),parseNutritionNumber(f.serat_besar)];
    const tx=transaction(()=>{
      const info=db.prepare(`INSERT INTO menu_mbg(tanggal,nama_menu,deskripsi,foto,energi_kecil,protein_kecil,lemak_kecil,karbo_kecil,serat_kecil,energi_besar,protein_besar,lemak_besar,karbo_besar,serat_besar,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(...data,nowLocalSql());
      saveMenuItems(info.lastInsertRowid,parseMenuItems(f.nama_menu));
      return info.lastInsertRowid;
    });
    tx(); await createPermanentQr(); console.log(`Menu tersimpan: ${f.tanggal}`); flash(req,'Menu berhasil disimpan. QR permanen tetap sama dan menampilkan menu terakhir yang diperbarui.','success');
    return res.redirect(`/admin/rekap/${encodeURIComponent(f.tanggal)}`);
  } catch(e){
    if(req.file) { try{fs.unlinkSync(path.join(UPLOAD_DIR,req.file.filename));}catch{} }
    flash(req,e.code==='SQLITE_CONSTRAINT_UNIQUE'?'Menu untuk tanggal tersebut sudah ada. Gunakan Edit.':e.message,'error');
    return res.redirect(req.originalUrl);
  }
});

app.get('/admin/edit/:id',(req,res)=>{
  const id=Number(req.params.id); const menu=row('SELECT * FROM menu_mbg WHERE id=?',[id]);
  if(!menu) return res.status(404).send('Menu tidak ditemukan');
  render(res,req,'form.html',{menu,items_for_form:getMenuItems(id),judul:'Edit Menu MBG'});
});
app.post('/admin/edit/:id', upload.single('foto'), async (req,res)=>{
  const id=Number(req.params.id); const menu=row('SELECT * FROM menu_mbg WHERE id=?',[id]);
  if(!menu) return res.status(404).send('Menu tidak ditemukan');
  try{
    const f=req.body; const foto=req.file?.filename||menu.foto;
    const tx=transaction(()=>{
      db.prepare(`UPDATE menu_mbg SET tanggal=?,nama_menu=?,deskripsi=?,foto=?,energi_kecil=?,protein_kecil=?,lemak_kecil=?,karbo_kecil=?,serat_kecil=?,energi_besar=?,protein_besar=?,lemak_besar=?,karbo_besar=?,serat_besar=?,updated_at=? WHERE id=?`).run(
        f.tanggal,String(f.nama_menu||'').trim(),String(f.deskripsi||'').trim(),foto,parseNutritionNumber(f.energi_kecil),parseNutritionNumber(f.protein_kecil),parseNutritionNumber(f.lemak_kecil),parseNutritionNumber(f.karbo_kecil),parseNutritionNumber(f.serat_kecil),parseNutritionNumber(f.energi_besar),parseNutritionNumber(f.protein_besar),parseNutritionNumber(f.lemak_besar),parseNutritionNumber(f.karbo_besar),parseNutritionNumber(f.serat_besar),nowLocalSql(),id);
      saveMenuItems(id,parseMenuItems(f.nama_menu));
    }); tx(); await createPermanentQr(); console.log(`Menu diperbarui: ${f.tanggal}`);
    if(req.file && menu.foto && menu.foto!==req.file.filename) { const old=path.join(UPLOAD_DIR,menu.foto); if(fs.existsSync(old)) fs.unlinkSync(old); }
    flash(req,'Menu diperbarui. QR permanen tetap sama dan otomatis menampilkan pembaruan terbaru.','success');
    return res.redirect(`/admin/rekap/${encodeURIComponent(f.tanggal)}`);
  }catch(e){ if(req.file){try{fs.unlinkSync(path.join(UPLOAD_DIR,req.file.filename));}catch{}} flash(req,e.code==='SQLITE_CONSTRAINT_UNIQUE'?'Tanggal tersebut sudah memiliki menu lain.':e.message,'error'); return res.redirect(req.originalUrl); }
});

app.post('/admin/hapus/:id',(req,res)=>{
  const id=Number(req.params.id); const menu=row('SELECT foto FROM menu_mbg WHERE id=?',[id]);
  if(menu){ db.prepare('DELETE FROM menu_item WHERE menu_id=?').run(id); db.prepare('DELETE FROM menu_mbg WHERE id=?').run(id); if(menu.foto){const f=path.join(UPLOAD_DIR,menu.foto);if(fs.existsSync(f))fs.unlinkSync(f);} }
  flash(req,'Menu berhasil dihapus.','success'); res.redirect('/admin');
});

// USER / QR ONLY: the permanent QR points here.
// This endpoint always loads the menu with the newest updated_at.
app.get('/menu/current',(req,res)=>{
  const menu=row('SELECT * FROM menu_mbg ORDER BY updated_at DESC,id DESC LIMIT 1');
  if(!menu) return res.status(404).render('not_found.html',{kode:'Belum ada menu yang aktif'});
  render(res,req,'menu.html',{menu,items:getMenuItems(menu.id),tanggal_indonesia:formatTanggalId(menu.tanggal)});
});
// Direct menu detail URLs are not used for the user flow.
// The permanent QR always goes through /menu/current so it shows
// the latest updated menu. Keep old links safe by redirecting them there.
app.get('/menu/:id',(req,res)=>{
  res.redirect('/menu/current');
});
app.get('/scan',(req,res)=>res.redirect('/admin'));
app.get('/qr-ompreng',(req,res)=>res.sendFile(path.join(QR_DIR,'qr_ompreng.png')));
app.get('/qr-download',(req,res)=>res.download(path.join(QR_DIR,'qr_ompreng.png'),'QR_Ompreng_MBG_Permanen.png'));

app.use((err,req,res,next)=>{
  if(err) {
    console.error('MBG ERROR:', err);
    if (res.headersSent) return next(err);
    return res.status(500).send(`<h1>Terjadi kesalahan pada aplikasi</h1><pre style=\"white-space:pre-wrap\">${String(err.stack || err.message || err)}</pre><p><a href=\"/\">Kembali ke halaman utama</a></p>`);
  }
  next();
});

(async()=>{
  await createPermanentQr();
  const port=Number(process.env.PORT||5000);
  const host=process.env.HOST||'0.0.0.0';
  app.listen(port,host,()=>console.log(`Sistem Menu MBG Node.js berjalan.
Local: http://localhost:${port}
Network: http://${getLocalIp()}:${port}
QR: http://${getLocalIp()}:${port}/menu/current`));
})();
