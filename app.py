import os, re, json, base64, hashlib, sqlite3, mimetypes
from datetime import datetime, timedelta
import requests
from flask import Flask, request, render_template, send_from_directory, abort, redirect
from rapidfuzz import fuzz
from google_play_scraper import reviews, Sort
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
DB = os.environ.get("DB_PATH", "data.db")
UP = os.environ.get("UPLOAD_DIR", "uploads")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "change-me")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
DAYS = 7
os.makedirs(UP, exist_ok=True)


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS subs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_ref TEXT, package TEXT,
            name TEXT, rating INTEGER, comment TEXT, files TEXT,
            status TEXT DEFAULT 'pending', reason TEXT,
            created TEXT, deadline TEXT, checked TEXT)""")
        c.execute("CREATE TABLE IF NOT EXISTS hashes(h TEXT PRIMARY KEY, sub_id INTEGER)")


def now():
    return datetime.utcnow()


def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def pkg_id(v):
    m = re.search(r"id=([\w.]+)", v or "")
    return m.group(1) if m else (v or "").strip()


def ocr(path):
    """Gemini Vision se screenshot padho. Key na ho to {} (manual fields use honge)."""
    if not GEMINI_KEY:
        print("GEMINI_API_KEY set nahi hai", flush=True)
        return {}
    try:
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        data = base64.b64encode(open(path, "rb").read()).decode()
        prompt = ('Google Play Store review screenshot hai. Sirf JSON do: '
                  '{"name": reviewer ka naam, "rating": stars 1-5 integer, "comment": poora review text}. '
                  'Na mile to null.')
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}",
            json={"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": mime, "data": data}}]}],
                  "generationConfig": {"response_mime_type": "application/json"}}, timeout=45)
        j = r.json()
        if "candidates" not in j:
            print("GEMINI ERROR:", r.status_code, str(j)[:300], flush=True)
            return {}
        d = json.loads(j["candidates"][0]["content"]["parts"][0]["text"]) or {}
        if isinstance(d, list):
            d = d[0] if d else {}
        print("OCR RESULT:", str(d)[:300], flush=True)
        return d
    except Exception as e:
        print("OCR EXCEPTION:", repr(e), flush=True)
        return {}


def fetch_reviews(pkg, since):
    out, token = [], None
    for _ in range(20):
        res, token = reviews(pkg, lang="en", country="in", sort=Sort.NEWEST,
                             count=200, continuation_token=token)
        out += res
        if not token or not res or res[-1]["at"] < since:
            break
    return out


def is_match(s, rv):
    if s["rating"] and rv["score"] != s["rating"]:
        return False
    if fuzz.ratio(norm(s["name"]), norm(rv["userName"])) < 85:
        return False
    if s["comment"] and fuzz.partial_ratio(norm(s["comment"]), norm(rv["content"])) < 80:
        return False
    return True


def verify_all():
    with db() as c:
        pend = c.execute("SELECT * FROM subs WHERE status='pending'").fetchall()
    groups = {}
    for s in pend:
        groups.setdefault(s["package"], []).append(s)
    for pkg, subs in groups.items():
        since = min(datetime.fromisoformat(s["created"]) for s in subs) - timedelta(days=2)
        try:
            live = fetch_reviews(pkg, since)
        except Exception:
            continue  # fetch fail hua to reject nahi karenge, agli baar try hoga
        for s in subs:
            ok = any(is_match(s, rv) for rv in live)
            t = now()
            with db() as c:
                if ok:
                    c.execute("UPDATE subs SET status='approved', reason='Review Play Store par live mila', checked=? WHERE id=?", (t.isoformat(), s["id"]))
                elif t > datetime.fromisoformat(s["deadline"]):
                    c.execute("UPDATE subs SET status='rejected', reason='7 din mein review Play Store par live nahi mila', checked=? WHERE id=?", (t.isoformat(), s["id"]))
                else:
                    c.execute("UPDATE subs SET checked=? WHERE id=?", (t.isoformat(), s["id"]))


def my_subs(ref):
    if not ref:
        return []
    with db() as c:
        return c.execute("SELECT * FROM subs WHERE user_ref=? ORDER BY id DESC", (ref,)).fetchall()


@app.route("/")
def index():
    ref = request.args.get("ref", "").strip()
    return render_template("index.html", subs=my_subs(ref), ref=ref, msg=None, ok=False)


@app.route("/submit", methods=["POST"])
def submit():
    f = request.form
    ref, pkg = f.get("user_ref", "").strip(), pkg_id(f.get("package"))
    files = [x for x in request.files.getlist("shots") if x.filename]

    def back(msg, ok=False):
        return render_template("index.html", subs=my_subs(ref), ref=ref, msg=msg, ok=ok)

    if not ref or not pkg or not files:
        return back("User ID, app ka package/link aur screenshot zaroori hain.")
    saved, name, rating, comment = [], f.get("name", "").strip(), int(f.get("rating") or 0), f.get("comment", "").strip()
    with db() as c:
        for x in files:
            raw = x.read()
            h = hashlib.sha256(raw).hexdigest()
            if c.execute("SELECT 1 FROM hashes WHERE h=?", (h,)).fetchone():
                return back("Ye screenshot pehle use ho chuka hai (duplicate).")
            fn = h[:16] + os.path.splitext(x.filename)[1].lower()[:5]
            open(os.path.join(UP, fn), "wb").write(raw)
            saved.append((h, fn))
    if not (name and rating and comment):
        for _, fn in saved:
            d = ocr(os.path.join(UP, fn))
            name = name or (d.get("name") or "").strip()
            m = re.search(r"[1-5]", str(d.get("rating") or ""))
            rating = rating or (int(m.group()) if m else 0)
            comment = comment or (d.get("comment") or "").strip()
    if not name or not rating:
        return back("Screenshot se naam/rating nahi padh paye. Neeche naam aur rating khud bharo.")
    with db() as c:
        dup = c.execute("SELECT 1 FROM subs WHERE package=? AND name=? AND status!='rejected'", (pkg, name)).fetchone()
        if dup:
            return back("Is app ke liye is naam se review pehle hi submit ho chuka hai.")
        t = now()
        cur = c.execute("INSERT INTO subs(user_ref,package,name,rating,comment,files,created,deadline) VALUES(?,?,?,?,?,?,?,?)",
                        (ref, pkg, name, rating, comment, ",".join(fn for _, fn in saved),
                         t.isoformat(), (t + timedelta(days=DAYS)).isoformat()))
        for h, _ in saved:
            c.execute("INSERT INTO hashes(h,sub_id) VALUES(?,?)", (h, cur.lastrowid))
    return back("Submit ho gaya. Review live hote hi (max 7 din) auto-approve ho jayega.", True)


def need_key():
    if request.values.get("key") != ADMIN_KEY:
        abort(403)


@app.route("/admin", methods=["GET", "POST"])
def admin():
    need_key()
    if request.method == "POST":
        a, i = request.form.get("action"), request.form.get("id")
        if a == "run":
            verify_all()
        elif a in ("approve", "reject") and i:
            with db() as c:
                c.execute("UPDATE subs SET status=?, reason='Admin ne manually kiya', checked=? WHERE id=?",
                          ("approved" if a == "approve" else "rejected", now().isoformat(), i))
        return redirect(f"/admin?key={ADMIN_KEY}")
    with db() as c:
        rows = c.execute("SELECT * FROM subs ORDER BY id DESC LIMIT 300").fetchall()
    return render_template("admin.html", rows=rows, key=ADMIN_KEY)


@app.route("/file/<fn>")
def file(fn):
    need_key()
    return send_from_directory(UP, fn)


init()
sched = BackgroundScheduler(daemon=True)
sched.add_job(verify_all, "interval", hours=3, next_run_time=now() + timedelta(minutes=2))
sched.start()

if __name__ == "__main__":
    app.run(debug=True)
