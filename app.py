import os, sqlite3, secrets, re, csv, io, re
from functools import wraps
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
from werkzeug.security import generate_password_hash, check_password_hash

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.path.expanduser("~"), ".contact-crm", "crm.sqlite3")

app = Flask(__name__)
app.secret_key = os.environ.get("CRM_SECRET_KEY", secrets.token_hex(32))

def db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'manager',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS roles (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        permissions TEXT NOT NULL DEFAULT '[]',
        is_system INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS regions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS statuses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        color TEXT NOT NULL DEFAULT '#64748b',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT DEFAULT '',
        phone_code TEXT NOT NULL DEFAULT '+380',
        telegram TEXT DEFAULT '',
        email TEXT DEFAULT '',
        company TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
        status_id INTEGER REFERENCES statuses(id) ON DELETE SET NULL,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        assigned_to INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS status_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        old_status TEXT,
        new_status TEXT,
        changed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        changed_by_name TEXT NOT NULL,
        changed_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_status_history_contact ON status_history(contact_id, id DESC);
    CREATE TABLE IF NOT EXISTS lead_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        author_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        author_name TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_lead_comments_contact ON lead_comments(contact_id, id DESC);
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
        message TEXT NOT NULL,
        is_read INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, id DESC);
    CREATE TABLE IF NOT EXISTS password_reset_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        resolved_at TEXT,
        resolved_by INTEGER REFERENCES users(id) ON DELETE SET NULL
    );
    CREATE INDEX IF NOT EXISTS idx_password_requests_user ON password_reset_requests(user_id, status);
    CREATE TABLE IF NOT EXISTS calendar_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        starts_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_events_user_date ON calendar_events(user_id, starts_at);

    """)
    import json
    conn.execute("INSERT OR IGNORE INTO roles(code,name,permissions,is_system) VALUES(?,?,?,1)",
                 ("admin", "Хед", json.dumps(["all_leads", "assign_leads", "create_leads", "edit_leads", "delete_leads", "import_leads", "export_leads", "comments", "calendar", "notifications", "manage_settings"])))
    conn.execute("INSERT OR IGNORE INTO roles(code,name,permissions,is_system) VALUES(?,?,?,1)",
                 ("manager", "Рекрутёр", json.dumps(["create_leads", "edit_leads", "import_leads", "export_leads", "comments", "calendar", "notifications"])))
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        conn.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                     ("admin", generate_password_hash("admin123", method="pbkdf2:sha256", salt_length=16), "admin", datetime.now().isoformat(timespec="seconds")))
    if conn.execute("SELECT COUNT(*) FROM statuses").fetchone()[0] == 0:
        for name, color in [("Новый","#64748b"),("В работе","#3b82f6"),("Ожидает ответа","#f59e0b"),("Успешно завершён","#10b981"),("Отказ","#ef4444")]:
            conn.execute("INSERT INTO statuses(name,color,created_at) VALUES(?,?,?)", (name,color,datetime.now().isoformat(timespec="seconds")))
    # Миграции для баз, созданных предыдущими версиями LedsGirls.
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "avatar_data" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_data BLOB")
    if "avatar_mime" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_mime TEXT")
    if "region_id" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN region_id INTEGER REFERENCES regions(id) ON DELETE SET NULL")
    # Preserve existing free-text lead regions when switching to a managed catalog.
    for existing in conn.execute("SELECT DISTINCT TRIM(company) FROM contacts WHERE TRIM(COALESCE(company,'')) <> ''").fetchall():
        conn.execute("INSERT OR IGNORE INTO regions(name,created_at) VALUES(?,?)",
                     (existing[0],datetime.now().isoformat(timespec="seconds")))
    contact_columns = {row["name"] for row in conn.execute("PRAGMA table_info(contacts)").fetchall()}
    if "phone_code" not in contact_columns:
        conn.execute("ALTER TABLE contacts ADD COLUMN phone_code TEXT NOT NULL DEFAULT '+380'")
    if "telegram" not in contact_columns:
        conn.execute("ALTER TABLE contacts ADD COLUMN telegram TEXT DEFAULT ''")
    if "assigned_to" not in contact_columns:
        conn.execute("ALTER TABLE contacts ADD COLUMN assigned_to INTEGER REFERENCES users(id) ON DELETE SET NULL")
    conn.commit()

def current_user():
    if "user_id" not in session:
        return None
    return db().execute("SELECT id,username,role,avatar_mime,region_id FROM users WHERE id=?", (session["user_id"],)).fetchone()

@app.template_filter("from_json")
def from_json(value):
    import json
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return []

@app.context_processor
def inject_user():
    user = current_user()
    if not user:
        return {"current_user": None, "unread_notifications": 0, "can": has_permission}
    count = db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (user["id"],)).fetchone()[0]
    role = db().execute("SELECT name FROM roles WHERE code=?", (user["role"],)).fetchone()
    return {"current_user": user, "unread_notifications": count,
            "role_label": role["name"] if role else user["role"],
            "can": has_permission}

PERMISSIONS = {
    "all_leads": "Просмотр всех лидов",
    "assign_leads": "Назначение лидов сотрудникам",
    "create_leads": "Создание лидов",
    "edit_leads": "Редактирование лидов",
    "delete_leads": "Удаление лидов",
    "import_leads": "Импорт таблиц",
    "export_leads": "Экспорт в Excel",
    "comments": "Комментарии к лидам",
    "calendar": "Календарь и события",
    "notifications": "Просмотр уведомлений",
    "manage_settings": "Источники, статусы, пользователи и роли",
}

def has_permission(permission, user=None):
    import json
    user = user if user is not None else current_user()
    if not user:
        return False
    # Хед — неснимаемый суперпользователь, чтобы не заблокировать управление CRM.
    if user["role"] == "admin":
        return True
    if permission == "manage_settings":
        return False
    role = db().execute("SELECT permissions FROM roles WHERE code=?", (user["role"],)).fetchone()
    if not role:
        return False
    try:
        return permission in json.loads(role["permissions"])
    except (ValueError, TypeError):
        return False

def permission_required(permission):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not current_user():
                return redirect(url_for("login"))
            if not has_permission(permission):
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator

def can_assign_to_others():
    return has_permission("assign_leads")

def role_choices():
    return db().execute("SELECT code,name FROM roles ORDER BY is_system DESC, name").fetchall()

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped

def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        if not has_permission("manage_settings", user):
            abort(403)
        return fn(*args, **kwargs)
    return wrapped

def lead_access_sql(alias="c"):
    user = current_user()
    if has_permission("all_leads", user):
        return "1=1", []
    return f"{alias}.assigned_to = ?", [user["id"]]


def can_access_lead(contact):
    user = current_user()
    return has_permission("all_leads", user) or contact["assigned_to"] == user["id"]


def notify_assignment(conn, contact_id, lead_name, new_owner, previous_owner=None):
    """Notify only a newly assigned manager; never leak leads to another account."""
    if new_owner is None or new_owner == previous_owner:
        return
    recipient = conn.execute("SELECT id,role FROM users WHERE id=?", (new_owner,)).fetchone()
    if not recipient or recipient["role"] == "admin":
        return
    conn.execute("""INSERT INTO notifications(user_id,contact_id,message,created_at)
        VALUES(?,?,?,?)""", (new_owner,contact_id,
        "Вам назначен новый лид: " + lead_name,
        datetime.now().isoformat(timespec="seconds")))

def record_status_change(conn, contact_id, old_id, new_id, actor):
    if old_id == new_id:
        return
    def status_name(sid):
        if sid is None:
            return "Без статуса"
        row = conn.execute("SELECT name FROM statuses WHERE id=?", (sid,)).fetchone()
        return row["name"] if row else "Удалённый статус"
    conn.execute("""INSERT INTO status_history
        (contact_id,old_status,new_status,changed_by,changed_by_name,changed_at)
        VALUES(?,?,?,?,?,?)""", (contact_id, status_name(old_id), status_name(new_id),
        actor["id"], actor["username"], datetime.now().isoformat(timespec="seconds")))

def avatar_mime_type(data):
    """Accept only common raster image signatures, never SVG or arbitrary content."""
    if data.startswith(b"\\x89PNG\\r\\n\\x1a\\n"):
        return "image/png"
    if data.startswith(b"\\xff\\xd8\\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@app.get("/users/<int:user_id>/avatar")
@login_required
def user_avatar(user_id):
    from flask import Response
    actor = current_user()
    if actor["id"] != user_id and actor["role"] != "admin":
        abort(403)
    record = db().execute(
        "SELECT avatar_data,avatar_mime FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not record or not record["avatar_data"]:
        abort(404)
    response = Response(bytes(record["avatar_data"]), mimetype=record["avatar_mime"])
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.post("/users/<int:user_id>/avatar")
@login_required
def user_avatar_update(user_id):
    actor = current_user()
    if actor["id"] != user_id and actor["role"] != "admin":
        abort(403)
    conn = db()
    if not conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
        abort(404)
    if request.form.get("remove_avatar") == "1":
        conn.execute("UPDATE users SET avatar_data=NULL,avatar_mime=NULL WHERE id=?", (user_id,))
        conn.commit()
        flash("Аватар удалён", "success")
    else:
        upload = request.files.get("avatar")
        if not upload or not upload.filename:
            flash("Выберите изображение", "error")
        else:
            data = upload.stream.read(2 * 1024 * 1024 + 1)
            mime = avatar_mime_type(data)
            if len(data) > 2 * 1024 * 1024:
                flash("Размер аватара не должен превышать 2 МБ", "error")
            elif not mime:
                flash("Поддерживаются только PNG, JPG и WebP", "error")
            else:
                conn.execute(
                    "UPDATE users SET avatar_data=?,avatar_mime=? WHERE id=?",
                    (data,mime,user_id)
                )
                conn.commit()
                flash("Аватар обновлён", "success")
    return redirect(url_for("settings") if actor["role"] == "admin" and user_id != actor["id"] else url_for("profile"))


@app.route("/profile")
@login_required
def profile():
    return render_template("profile.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        conn = db()
        account = conn.execute("SELECT id,role FROM users WHERE username=?", (username,)).fetchone()
        # Generic response prevents disclosure of account existence.
        if account and account["role"] != "admin":
            pending = conn.execute("SELECT id FROM password_reset_requests WHERE user_id=? AND status='pending'", (account["id"],)).fetchone()
            if not pending:
                conn.execute("INSERT INTO password_reset_requests(user_id,created_at) VALUES(?,?)", (account["id"], datetime.now().isoformat(timespec="seconds")))
                for head in conn.execute("SELECT id FROM users WHERE role='admin'").fetchall():
                    conn.execute("INSERT INTO notifications(user_id,message,created_at) VALUES(?,?,?)", (head["id"], "Запрос на восстановление пароля учётной записи", datetime.now().isoformat(timespec="seconds")))
                conn.commit()
        flash("Если учётная запись найдена, заявка отправлена Хеду. Обратитесь к Хеду за новым паролем.", "success")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")


@app.post("/profile/change-password")
@login_required
def change_password():
    old = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    account = db().execute("SELECT password_hash FROM users WHERE id=?", (current_user()["id"],)).fetchone()
    if not check_password_hash(account["password_hash"], old):
        flash("Текущий пароль неверный", "error")
    elif len(new) < 10 or len(new) > 128:
        flash("Новый пароль должен содержать от 10 до 128 символов", "error")
    elif new != confirm:
        flash("Новые пароли не совпадают", "error")
    else:
        db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new, method="pbkdf2:sha256"), current_user()["id"]))
        db().commit()
        flash("Пароль успешно изменён", "success")
    return redirect(url_for("profile"))


@app.get("/password-requests")
@admin_required
def password_requests():
    requests = db().execute("""SELECT r.*,u.username FROM password_reset_requests r
        JOIN users u ON u.id=r.user_id ORDER BY (r.status='pending') DESC,r.id DESC""").fetchall()
    return render_template("password_requests.html", requests=requests)


@app.post("/password-requests/<int:request_id>/resolve")
@admin_required
def password_request_resolve(request_id):
    conn = db()
    entry = conn.execute("""SELECT r.*,u.username,u.role FROM password_reset_requests r
        JOIN users u ON u.id=r.user_id WHERE r.id=?""", (request_id,)).fetchone()
    if not entry: abort(404)
    if entry["status"] != "pending":
        flash("Эта заявка уже обработана", "error")
        return redirect(url_for("password_requests"))
    action = request.form.get("action")
    if action == "reject":
        conn.execute("UPDATE password_reset_requests SET status='rejected',resolved_at=?,resolved_by=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"),current_user()["id"],request_id))
        conn.commit()
        flash("Заявка отклонена", "success")
    elif action == "reset" and entry["role"] != "admin":
        new_password = request.form.get("new_password", "")
        if not 10 <= len(new_password) <= 128:
            flash("Пароль должен содержать от 10 до 128 символов", "error")
        else:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password,method="pbkdf2:sha256"),entry["user_id"]))
            conn.execute("UPDATE password_reset_requests SET status='completed',resolved_at=?,resolved_by=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"),current_user()["id"],request_id))
            conn.execute("INSERT INTO notifications(user_id,message,created_at) VALUES(?,?,?)", (entry["user_id"],"Хед изменил пароль вашей учётной записи",datetime.now().isoformat(timespec="seconds")))
            conn.commit()
            flash("Пароль изменён. Передайте новый пароль сотруднику безопасным способом.", "success")
    else:
        abort(400)
    return redirect(url_for("password_requests"))


@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        user = db().execute("SELECT * FROM users WHERE username=?", (request.form.get("username","").strip(),)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password","")):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        flash("Неверный логин или пароль", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def dashboard():
    conn = db()
    scope, scope_params = lead_access_sql("c")
    total = conn.execute(
        "SELECT COUNT(*) FROM contacts c WHERE " + scope, scope_params
    ).fetchone()[0]
    by_status = conn.execute("""SELECT s.name,s.color,COUNT(c.id) count FROM statuses s
        LEFT JOIN contacts c ON c.status_id=s.id AND """ + scope +
        " GROUP BY s.id ORDER BY s.id", scope_params).fetchall()
    recent = conn.execute("""SELECT c.*,s.name status_name,s.color status_color,
        so.name source_name,u.username manager_name
        FROM contacts c LEFT JOIN statuses s ON s.id=c.status_id
        LEFT JOIN sources so ON so.id=c.source_id
        LEFT JOIN users u ON u.id=c.assigned_to
        WHERE """ + scope + " ORDER BY c.id DESC LIMIT 6", scope_params).fetchall()
    return render_template("dashboard.html", total=total, by_status=by_status, recent=recent)

@app.route("/contacts")
@login_required
def contacts():
    q = request.args.get("q","").strip()
    status_id = request.args.get("status","")
    source_id = request.args.get("source","")
    # Отдельный режим списка Telegram-контактов включается ссылкой ?telegram=1.
    telegram_only = request.args.get("telegram") == "1"
    sql = """SELECT c.*,s.name status_name,s.color status_color,so.name source_name,u.username manager_name
             FROM contacts c LEFT JOIN statuses s ON s.id=c.status_id
             LEFT JOIN sources so ON so.id=c.source_id LEFT JOIN users u ON u.id=c.assigned_to WHERE 1=1"""
    access_sql, access_params = lead_access_sql("c")
    sql += " AND " + access_sql
    params = list(access_params)
    if q:
        sql += " AND (c.name LIKE ? OR c.phone LIKE ? OR c.phone_code LIKE ? OR c.email LIKE ? OR c.company LIKE ? OR c.telegram LIKE ?)"
        params += [f"%{q}%"]*6
    if telegram_only:
        sql += " AND COALESCE(c.telegram,'') <> ''"
    if status_id:
        sql += " AND c.status_id=?"; params.append(status_id)
    if source_id:
        sql += " AND c.source_id=?"; params.append(source_id)
    sql += " ORDER BY c.id DESC"
    rows = db().execute(sql, params).fetchall()
    statuses = db().execute("SELECT * FROM statuses ORDER BY id").fetchall()
    sources = db().execute("SELECT * FROM sources ORDER BY name").fetchall()
    return render_template("contacts.html", contacts=rows, statuses=statuses, sources=sources,
                           q=q, selected_status=status_id, selected_source=source_id, telegram_only=telegram_only)

def lead_region_from_form(existing=None):
    """Recruiters use their assigned region; Head selects a catalog region."""
    user = current_user()
    if user["role"] != "admin":
        if user["region_id"]:
            row = db().execute("SELECT name FROM regions WHERE id=?", (user["region_id"],)).fetchone()
            if row:
                return row["name"]
        return existing or ""
    region_id = request.form.get("region_id", "").strip()
    if not region_id:
        return ""
    if not region_id.isdecimal():
        abort(400)
    row = db().execute("SELECT name FROM regions WHERE id=?", (int(region_id),)).fetchone()
    if not row:
        abort(400)
    return row["name"]


@app.route("/contacts/new", methods=["GET","POST"])
@permission_required("create_leads")
def contact_new():
    if request.method == "POST":
        name = request.form.get("name","").strip()
        if not name:
            flash("Укажите имя контакта", "error")
        else:
            db().execute("""INSERT INTO contacts(name,phone,phone_code,telegram,email,company,notes,source_id,status_id,created_by,assigned_to,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (name, request.form.get("phone","").strip(), request.form.get("phone_code","+380"),
                 request.form.get("telegram","").strip(), request.form.get("email","").strip(),
                 lead_region_from_form(), request.form.get("notes","").strip(),
                 request.form.get("source_id") or None, request.form.get("status_id") or None,
                 current_user()["id"],
                 (request.form.get("assigned_to") or None) if can_assign_to_others() else current_user()["id"],
                 datetime.now().isoformat(timespec="seconds")))
            new_id = db().execute("SELECT last_insert_rowid()").fetchone()[0]
            new_status = request.form.get("status_id") or None
            record_status_change(db(), new_id, None, int(new_status) if new_status else None, current_user())
            owner = db().execute("SELECT assigned_to FROM contacts WHERE id=?", (new_id,)).fetchone()["assigned_to"]
            notify_assignment(db(), new_id, name, owner)
            db().commit()
            flash("Контакт добавлен", "success")
            return redirect(url_for("contacts"))
    return render_template("contact_form.html", contact=None,
        regions=db().execute("SELECT * FROM regions ORDER BY name").fetchall(),
        assigned_region=(db().execute("SELECT name FROM regions WHERE id=?", (current_user()["region_id"],)).fetchone() if current_user()["region_id"] else None),
        sources=db().execute("SELECT * FROM sources ORDER BY name").fetchall(),
        statuses=db().execute("SELECT * FROM statuses ORDER BY id").fetchall(),
        managers=(db().execute("SELECT id,username FROM users ORDER BY username").fetchall()
                  if can_assign_to_others() else [current_user()]))

@app.route("/contacts/<int:contact_id>/edit", methods=["GET","POST"])
@permission_required("edit_leads")
def contact_edit(contact_id):
    conn = db()
    contact = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if not contact: abort(404)
    if not can_access_lead(contact): abort(403)
    if request.method == "POST":
        name = request.form.get("name","").strip()
        if not name:
            flash("Укажите имя контакта", "error")
        else:
            conn.execute("""UPDATE contacts SET name=?,phone=?,phone_code=?,telegram=?,email=?,company=?,notes=?,source_id=?,status_id=?,assigned_to=? WHERE id=?""",
                (name, request.form.get("phone","").strip(), request.form.get("phone_code","+380"),
                 request.form.get("telegram","").strip(), request.form.get("email","").strip(),
                 lead_region_from_form(contact["company"]), request.form.get("notes","").strip(),
                 request.form.get("source_id") or None, request.form.get("status_id") or None,
                 (request.form.get("assigned_to") or None) if can_assign_to_others() else contact["assigned_to"],
                 contact_id))
            updated_owner = conn.execute("SELECT assigned_to FROM contacts WHERE id=?", (contact_id,)).fetchone()["assigned_to"]
            notify_assignment(conn, contact_id, name, updated_owner, contact["assigned_to"])
            new_status = request.form.get("status_id") or None
            record_status_change(conn, contact_id, contact["status_id"], int(new_status) if new_status else None, current_user())
            conn.commit()
            flash("Изменения сохранены", "success")
            return redirect(url_for("contact_edit", contact_id=contact_id))
    history = conn.execute("""SELECT * FROM status_history WHERE contact_id=? ORDER BY id DESC""", (contact_id,)).fetchall()
    comments = conn.execute("""SELECT * FROM lead_comments WHERE contact_id=? ORDER BY id DESC""", (contact_id,)).fetchall()
    tab = request.args.get("tab", "details")
    if tab not in ("details", "comments", "history"):
        tab = "details"
    return render_template("contact_form.html", contact=contact, history=history,
        comments=comments, active_tab=tab,
        regions=conn.execute("SELECT * FROM regions ORDER BY name").fetchall(),
        assigned_region=(conn.execute("SELECT name FROM regions WHERE id=?", (current_user()["region_id"],)).fetchone() if current_user()["region_id"] else None),
        sources=conn.execute("SELECT * FROM sources ORDER BY name").fetchall(),
        statuses=conn.execute("SELECT * FROM statuses ORDER BY id").fetchall(),
        managers=(conn.execute("SELECT id,username FROM users ORDER BY username").fetchall()
                  if can_assign_to_others() else [current_user()]))

@app.post("/contacts/<int:contact_id>/comments")
@permission_required("comments")
def add_lead_comment(contact_id):
    conn = db()
    contact = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if not contact:
        abort(404)
    if not can_access_lead(contact):
        abort(403)
    body = request.form.get("comment", "").strip()
    if not body:
        flash("Напишите комментарий", "error")
    elif len(body) > 4000:
        flash("Комментарий не должен превышать 4000 символов", "error")
    else:
        actor = current_user()
        conn.execute("""INSERT INTO lead_comments
            (contact_id,author_id,author_name,body,created_at)
            VALUES(?,?,?,?,?)""",
            (contact_id,actor["id"],actor["username"],body,
             datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        flash("Комментарий добавлен", "success")
    return redirect(url_for("contact_edit", contact_id=contact_id, tab="comments"))


@app.post("/contacts/<int:contact_id>/delete")
@permission_required("delete_leads")
def contact_delete(contact_id):
    user = current_user()
    if not has_permission("delete_leads"): abort(403)
    db().execute("DELETE FROM contacts WHERE id=?", (contact_id,))
    db().commit()
    flash("Контакт удалён", "success")
    return redirect(url_for("contacts"))


@app.route("/contacts/import", methods=["GET", "POST"])
@permission_required("import_leads")
def import_contacts():
    user = current_user()
    if request.method == "GET":
        return render_template("import.html")
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Выберите файл .xlsx или .csv", "error")
        return redirect(url_for("import_contacts"))
    suffix = os.path.splitext(upload.filename.lower())[1]
    if suffix not in (".xlsx", ".csv"):
        flash("Поддерживаются только .xlsx и .csv", "error")
        return redirect(url_for("import_contacts"))
    content = upload.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        flash("Файл больше 5 МБ", "error")
        return redirect(url_for("import_contacts"))
    try:
        if suffix == ".xlsx":
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            try:
                sheet = workbook.active
                iterator = sheet.iter_rows(values_only=True)
                headers = next(iterator, None)
                rows = list(iterator)
            finally:
                workbook.close()
        else:
            decoded = content.decode("utf-8-sig")
            sample = decoded[:4096]
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                rows_all = list(csv.reader(io.StringIO(decoded), dialect))
            except csv.Error:
                rows_all = list(csv.reader(io.StringIO(decoded)))
            headers = rows_all[0] if rows_all else None
            rows = rows_all[1:]
        if not headers:
            raise ValueError("В файле отсутствует строка заголовков")
        if len(rows) > 2000:
            raise ValueError("За один раз можно импортировать не больше 2000 строк")
        aliases = {
            "имя":"name", "имя и фамилия":"name", "контакт":"name", "name":"name",
            "телефон":"phone", "номер телефона":"phone", "phone":"phone",
            "код страны":"phone_code", "код региона":"phone_code", "phone_code":"phone_code",
            "telegram":"telegram", "телеграм":"telegram", "тг":"telegram",
            "email":"email", "e-mail":"email", "почта":"email",
            "регион":"company", "region":"company", "компания":"company", "company":"company", "источник":"source", "source":"source",
            "статус":"status", "status":"status", "менеджер":"manager",
            "ответственный менеджер":"manager", "manager":"manager",
            "заметки":"notes", "комментарий":"notes", "notes":"notes"
        }
        fields = [aliases.get(str(h or "").strip().lower()) for h in headers]
        if "name" not in fields:
            raise ValueError("Нужна колонка 'Имя' или 'Name'")
        conn = db()
        source_map = {r["name"].casefold():r["id"] for r in conn.execute("SELECT id,name FROM sources")}
        status_map = {r["name"].casefold():r["id"] for r in conn.execute("SELECT id,name FROM statuses")}
        user_map = {r["username"].casefold():r["id"] for r in conn.execute("SELECT id,username FROM users")}
        prepared = []
        for line, values in enumerate(rows, start=2):
            if not any(v is not None and str(v).strip() for v in values):
                continue
            entry = {key: str(values[i] if i < len(values) and values[i] is not None else "").strip()
                     for i,key in enumerate(fields) if key}
            name = entry.get("name", "")
            if not name:
                raise ValueError(f"Строка {line}: не указано имя")
            source = entry.get("source", "")
            status = entry.get("status", "")
            manager = entry.get("manager", "")
            if source and source.casefold() not in source_map:
                raise ValueError(f"Строка {line}: неизвестный источник '{source}' (добавьте его в настройках)")
            if status and status.casefold() not in status_map:
                raise ValueError(f"Строка {line}: неизвестный статус '{status}' (добавьте его в настройках)")
            if can_assign_to_others():
                if manager and manager.casefold() not in user_map:
                    raise ValueError(f"Строка {line}: неизвестный менеджер '{manager}'")
                assigned = user_map.get(manager.casefold()) if manager else None
            else:
                assigned = user["id"]
            phone_code = entry.get("phone_code", "+380") or "+380"
            if not re.fullmatch(r"\+\d{1,4}", phone_code):
                raise ValueError(f"Строка {line}: некорректный код страны '{phone_code}'")
            # Imported leads follow the recruiter's assigned region, not the spreadsheet.
            imported_region = entry.get("company", "")
            if user["role"] != "admin":
                imported_region = lead_region_from_form()
            prepared.append((name,entry.get("phone",""),phone_code,entry.get("telegram",""),
                entry.get("email",""),imported_region,entry.get("notes",""),
                source_map.get(source.casefold()),status_map.get(status.casefold()),user["id"],assigned,
                datetime.now().isoformat(timespec="seconds")))
        with conn:
            for record in prepared:
                cursor = conn.execute("""INSERT INTO contacts
                  (name,phone,phone_code,telegram,email,company,notes,source_id,status_id,created_by,assigned_to,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", record)
                record_status_change(conn, cursor.lastrowid, None, record[8], user)
                notify_assignment(conn, cursor.lastrowid, record[0], record[10])
        flash(f"Импортировано контактов: {len(prepared)}", "success")
        return redirect(url_for("contacts"))
    except (ValueError, UnicodeError, csv.Error, OSError, KeyError) as exc:
        flash(f"Ошибка импорта: {exc}", "error")
        return redirect(url_for("import_contacts"))


@app.route("/contacts/export.xlsx")
@permission_required("export_leads")
def export_contacts():
    from io import BytesIO
    from flask import send_file
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    q = request.args.get("q", "").strip()
    status_id = request.args.get("status", "")
    source_id = request.args.get("source", "")
    telegram_only = request.args.get("telegram") == "1"

    sql = """SELECT c.*, s.name AS status_name, so.name AS source_name, u.username AS manager_name
             FROM contacts c
             LEFT JOIN statuses s ON s.id=c.status_id
             LEFT JOIN sources so ON so.id=c.source_id
             LEFT JOIN users u ON u.id=c.assigned_to
             WHERE 1=1"""
    access_sql, access_params = lead_access_sql("c")
    sql += " AND " + access_sql
    params = list(access_params)
    if q:
        sql += " AND (c.name LIKE ? OR c.phone LIKE ? OR c.email LIKE ? OR c.company LIKE ? OR c.telegram LIKE ?)"
        params += [f"%{q}%"] * 5
    if status_id:
        sql += " AND c.status_id=?"
        params.append(status_id)
    if source_id:
        sql += " AND c.source_id=?"
        params.append(source_id)
    if telegram_only:
        sql += " AND COALESCE(c.telegram,'') <> ''"
    sql += " ORDER BY c.id DESC"
    rows = db().execute(sql, params).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Контакты"
    headers = ["ID", "Имя", "Код страны", "Телефон", "Telegram", "Email",
               "Регион", "Источник", "Статус", "Ответственный менеджер",
               "Заметки", "Дата создания"]
    ws.append(headers)
    for c in rows:
        ws.append([
            c["id"], c["name"], c["phone_code"] or "", c["phone"] or "",
            c["telegram"] or "", c["email"] or "", c["company"] or "",
            c["source_name"] or "", c["status_name"] or "",
            c["manager_name"] or "", c["notes"] or "", c["created_at"] or ""
        ])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="5144B8")
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    widths = [8, 24, 14, 20, 26, 28, 24, 20, 20, 24, 40, 22]
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output, as_attachment=True,
                     download_name="ledsgirls_contacts.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/users/<int:user_id>/delete")
@admin_required
def user_delete(user_id):
    conn = db()
    user = conn.execute("SELECT id, username, role FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        flash("Пользователь не найден", "error")
        return redirect(url_for("settings"))
    if user["id"] == current_user()["id"]:
        flash("Нельзя удалить собственную учётную запись", "error")
        return redirect(url_for("settings"))
    if user["role"] == "admin":
        admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        if admin_count <= 1:
            flash("Нельзя удалить последнего администратора", "error")
            return redirect(url_for("settings"))
    conn.execute("UPDATE contacts SET assigned_to=NULL WHERE assigned_to=?", (user_id,))
    conn.execute("UPDATE contacts SET created_by=NULL WHERE created_by=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    flash("Пользователь удалён. Его лиды сохранены, ответственный менеджер снят.", "success")
    return redirect(url_for("settings"))

@app.get("/notifications")
@permission_required("notifications")
def notifications():
    user = current_user()
    rows = db().execute("""SELECT n.*, c.assigned_to FROM notifications n
        LEFT JOIN contacts c ON c.id=n.contact_id
        WHERE n.user_id=? ORDER BY n.id DESC LIMIT 200""", (user["id"],)).fetchall()
    return render_template("notifications.html", notifications=rows)


@app.post("/notifications/<int:notification_id>/read")
@permission_required("notifications")
def notification_read(notification_id):
    user = current_user()
    conn = db()
    row = conn.execute("SELECT * FROM notifications WHERE id=? AND user_id=?", (notification_id,user["id"])).fetchone()
    if not row:
        abort(404)
    conn.execute("UPDATE notifications SET is_read=1 WHERE id=?", (notification_id,))
    conn.commit()
    contact = conn.execute("SELECT * FROM contacts WHERE id=?", (row["contact_id"],)).fetchone() if row["contact_id"] else None
    if contact and can_access_lead(contact):
        return redirect(url_for("contact_edit",contact_id=contact["id"]))
    return redirect(url_for("notifications"))


@app.post("/notifications/read-all")
@permission_required("notifications")
def notifications_read_all():
    conn = db()
    conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (current_user()["id"],))
    conn.commit()
    return redirect(url_for("notifications"))


@app.route("/calendar", methods=["GET", "POST"])
@permission_required("calendar")
def calendar():
    from calendar import monthcalendar, month_name
    from datetime import date
    user = current_user()
    conn = db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        raw_start = request.form.get("starts_at", "")
        raw_contact = request.form.get("contact_id", "").strip()
        try:
            starts = datetime.strptime(raw_start, "%Y-%m-%dT%H:%M")
        except ValueError:
            flash("Укажите корректную дату и время события", "error")
            return redirect(url_for("calendar"))
        if not title or len(title)>150 or len(description)>2000:
            flash("Укажите название (до 150 символов) и описание (до 2000)", "error")
            return redirect(url_for("calendar"))
        contact_id = None
        if raw_contact:
            try: contact_id = int(raw_contact)
            except ValueError: abort(400)
            contact = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
            if not contact or not can_access_lead(contact): abort(403)
        conn.execute("""INSERT INTO calendar_events(user_id,contact_id,title,description,starts_at,created_at)
            VALUES(?,?,?,?,?,?)""", (user["id"],contact_id,title,description,
            starts.isoformat(timespec="minutes"),datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        flash("Событие запланировано", "success")
        return redirect(url_for("calendar",month=starts.strftime("%Y-%m")))
    raw_month = request.args.get("month", "")
    try:
        chosen = datetime.strptime(raw_month, "%Y-%m") if raw_month else datetime.now()
    except ValueError:
        chosen = datetime.now()
    year, month = chosen.year, chosen.month
    first = f"{year:04d}-{month:02d}-01"
    next_month = date(year+1,1,1) if month==12 else date(year,month+1,1)
    end = next_month.isoformat()
    events = conn.execute("""SELECT e.*,c.name contact_name FROM calendar_events e
        LEFT JOIN contacts c ON c.id=e.contact_id
        WHERE e.user_id=? AND e.starts_at>=? AND e.starts_at<?
        ORDER BY e.starts_at,e.id""", (user["id"],first,end)).fetchall()
    scope, params = lead_access_sql("c")
    leads = conn.execute("SELECT c.id,c.name FROM contacts c WHERE "+scope+" ORDER BY c.name",params).fetchall()
    by_day = {}
    for event in events:
        by_day.setdefault(int(event["starts_at"][8:10]),[]).append(event)
    previous = date(year-1,12,1) if month==1 else date(year,month-1,1)
    return render_template("calendar.html", weeks=monthcalendar(year,month),events_by_day=by_day,
        events=events,leads=leads,year=year,month=month,month_label=month_name[month],
        previous=previous.strftime("%Y-%m"),following=next_month.strftime("%Y-%m"),
        today=date.today().isoformat())


@app.post("/calendar/<int:event_id>/delete")
@permission_required("calendar")
def calendar_delete(event_id):
    conn = db()
    event = conn.execute("SELECT * FROM calendar_events WHERE id=? AND user_id=?",
                         (event_id,current_user()["id"])).fetchone()
    if not event: abort(404)
    conn.execute("DELETE FROM calendar_events WHERE id=?", (event_id,))
    conn.commit()
    flash("Событие удалено", "success")
    return redirect(url_for("calendar",month=event["starts_at"][:7]))


@app.post("/roles/create")
@admin_required
def role_create():
    import json
    name = request.form.get("name", "").strip()
    if not name or len(name) > 50:
        flash("Название роли должно содержать от 1 до 50 символов", "error")
        return redirect(url_for("settings"))
    code = "custom_" + secrets.token_hex(8)
    permissions = [key for key in PERMISSIONS if key in request.form.getlist("permissions")]
    try:
        db().execute("INSERT INTO roles(code,name,permissions,is_system) VALUES(?,?,?,0)",
                     (code,name,json.dumps(permissions,ensure_ascii=False)))
        db().commit()
        flash("Роль создана", "success")
    except sqlite3.IntegrityError:
        flash("Роль с таким названием уже существует", "error")
    return redirect(url_for("settings"))


@app.post("/roles/<code>/update")
@admin_required
def role_update(code):
    import json
    conn = db()
    role = conn.execute("SELECT * FROM roles WHERE code=?", (code,)).fetchone()
    if not role:
        abort(404)
    if code == "admin":
        flash("Права роли Хед защищены от изменения", "error")
        return redirect(url_for("settings"))
    name = request.form.get("name", "").strip()
    if not name or len(name) > 50:
        flash("Укажите название роли (до 50 символов)", "error")
        return redirect(url_for("settings"))
    permissions = [key for key in PERMISSIONS if key in request.form.getlist("permissions")]
    # Only the head can manage settings, roles and users; prevent role escalation.
    permissions = [p for p in permissions if p != "manage_settings"]
    try:
        conn.execute("UPDATE roles SET name=?, permissions=? WHERE code=?",
                     (name,json.dumps(permissions,ensure_ascii=False),code))
        conn.commit()
        flash("Права роли обновлены", "success")
    except sqlite3.IntegrityError:
        flash("Название роли уже занято", "error")
    return redirect(url_for("settings"))


@app.post("/users/<int:user_id>/role")
@admin_required
def user_role_update(user_id):
    conn = db()
    user = conn.execute("SELECT id,role FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        abort(404)
    new_role = request.form.get("role", "")
    if not conn.execute("SELECT 1 FROM roles WHERE code=?", (new_role,)).fetchone():
        abort(400)
    if user_id == current_user()["id"] and new_role != "admin":
        flash("Нельзя снять роль Хед с собственной учётной записи", "error")
        return redirect(url_for("settings"))
    if user["role"] == "admin" and new_role != "admin":
        if conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
            flash("Нельзя снять роль с последнего Хеда", "error")
            return redirect(url_for("settings"))
    conn.execute("UPDATE users SET role=? WHERE id=?", (new_role,user_id))
    conn.commit()
    flash("Роль пользователя обновлена", "success")
    return redirect(url_for("settings"))


@app.post("/users/<int:user_id>/rename")
@admin_required
def user_rename(user_id):
    conn = db()
    user = conn.execute("SELECT id,username FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        abort(404)

    new_username = request.form.get("username", "").strip()
    if not 3 <= len(new_username) <= 50:
        flash("Логин должен содержать от 3 до 50 символов", "error")
        return redirect(url_for("settings"))

    if new_username == user["username"]:
        flash("Имя учётной записи не изменилось", "success")
        return redirect(url_for("settings"))

    try:
        conn.execute("UPDATE users SET username=? WHERE id=?", (new_username, user_id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("Это имя учётной записи уже занято", "error")
        return redirect(url_for("settings"))

    flash("Имя учётной записи изменено. Лиды, роль и пароль сохранены.", "success")
    return redirect(url_for("settings"))


@app.post("/users/<int:user_id>/region")
@admin_required
def user_region_update(user_id):
    conn = db()
    user = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        abort(404)
    value = request.form.get("region_id", "").strip()
    if value:
        if not value.isdecimal() or not conn.execute("SELECT 1 FROM regions WHERE id=?", (int(value),)).fetchone():
            flash("Выберите существующий регион", "error")
            return redirect(url_for("settings"))
        region_id = int(value)
    else:
        region_id = None
    conn.execute("UPDATE users SET region_id=? WHERE id=?", (region_id,user_id))
    # Synchronize the region already stored on leads created by this recruiter.
    # Keep existing regions untouched if the account's region is cleared.
    synced = 0
    if region_id is not None:
        region_name = conn.execute("SELECT name FROM regions WHERE id=?", (region_id,)).fetchone()["name"]
        synced = conn.execute(
            "UPDATE contacts SET company=? WHERE created_by=? AND COALESCE(company,'')<>?",
            (region_name, user_id, region_name)
        ).rowcount
    conn.commit()
    flash(f"Регион сотрудника сохранён. Обновлено лидов: {synced}", "success")
    return redirect(url_for("settings"))


@app.route("/settings", methods=["GET","POST"])
@admin_required
def settings():
    conn = db()
    if request.method == "POST":
        action = request.form.get("action")
        name = request.form.get("name","").strip()
        if action == "region":
            if not name:
                flash("Укажите название региона", "error")
            else:
                try:
                    conn.execute("INSERT INTO regions(name,created_at) VALUES(?,?)", (name,datetime.now().isoformat(timespec="seconds")))
                    conn.commit()
                    flash("Регион добавлен", "success")
                except sqlite3.IntegrityError:
                    flash("Такой регион уже существует", "error")
        elif action == "source" and name:
            try:
                conn.execute("INSERT INTO sources(name,created_at) VALUES(?,?)", (name,datetime.now().isoformat(timespec="seconds")))
                conn.commit(); flash("Источник добавлен","success")
            except sqlite3.IntegrityError: flash("Такой источник уже существует","error")
        elif action == "status" and name:
            color = request.form.get("color","#64748b")
            try:
                conn.execute("INSERT INTO statuses(name,color,created_at) VALUES(?,?,?)", (name,color,datetime.now().isoformat(timespec="seconds")))
                conn.commit(); flash("Статус добавлен","success")
            except sqlite3.IntegrityError: flash("Такой статус уже существует","error")
        elif action == "status_color":
            status_id = request.form.get("status_id", "")
            color = request.form.get("color", "").strip()
            if not status_id.isdecimal() or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                flash("Некорректный цвет или статус", "error")
            else:
                result = conn.execute("UPDATE statuses SET color=? WHERE id=?", (color, int(status_id)))
                conn.commit()
                flash("Цвет статуса обновлён" if result.rowcount else "Статус не найден",
                      "success" if result.rowcount else "error")
        elif action == "user":
            username = request.form.get("username","").strip()
            password = request.form.get("password","")
            role = request.form.get("role","manager")
            if username and password and conn.execute("SELECT 1 FROM roles WHERE code=?", (role,)).fetchone():
                try:
                    region_id = request.form.get("region_id") or None
                    if region_id and (not region_id.isdecimal() or not conn.execute("SELECT 1 FROM regions WHERE id=?", (int(region_id),)).fetchone()):
                        flash("Выберите существующий регион", "error")
                        return redirect(url_for("settings"))
                    conn.execute("INSERT INTO users(username,password_hash,role,created_at,region_id) VALUES(?,?,?,?,?)",
                        (username,generate_password_hash(password, method="pbkdf2:sha256", salt_length=16),role,datetime.now().isoformat(timespec="seconds"),region_id))
                    conn.commit(); flash("Пользователь создан","success")
                except sqlite3.IntegrityError: flash("Такой логин уже существует","error")
            else: flash("Заполните логин и пароль","error")
        return redirect(url_for("settings"))
    return render_template("settings.html",
        regions=conn.execute("SELECT * FROM regions ORDER BY name").fetchall(),
        sources=conn.execute("SELECT * FROM sources ORDER BY name").fetchall(),
        statuses=conn.execute("SELECT * FROM statuses ORDER BY id").fetchall(),
        users=conn.execute("SELECT id,username,role,created_at,avatar_mime,region_id FROM users ORDER BY id").fetchall(),
        roles=conn.execute("SELECT * FROM roles ORDER BY is_system DESC,name").fetchall(),
        permissions=PERMISSIONS)

@app.errorhandler(403)
def forbidden(_): return render_template("error.html", code=403, message="Недостаточно прав для этого действия"), 403
@app.errorhandler(404)
def not_found(_): return render_template("error.html", code=404, message="Страница не найдена"), 404

@app.errorhandler(500)
def internal_error(error):
    app.logger.exception("Внутренняя ошибка CRM: %s", error)
    return render_template("error.html", code=500, message="Внутренняя ошибка. Подробности записаны в терминал."), 500

# Создаём базу и таблицы один раз при запуске приложения.
with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
