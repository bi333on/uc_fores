import base64
import csv
import io
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from functools import wraps

import pyotp
import qrcode
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from config import config
from models import (
    AdminUser,
    AnswerOption,
    Attempt,
    Category,
    Employee,
    Material,
    Question,
    Setting,
    Test,
    db,
)

load_dotenv()

app = Flask(__name__)
app.config.from_object(config)
app.config["SQLALCHEMY_DATABASE_URI"] = config.SQLALCHEMY_DATABASE_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024

db.init_app(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
PDF_EXTS = {".pdf"}
DOCUMENT_EXTS = {".pdf", ".jpg", ".jpeg"}
IMPORT_EXTS = {".csv", ".xlsx", ".xls"}

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Войдите в личный кабинет для прохождения обучения."
login_manager.login_message_category = "warning"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Employee, int(user_id))


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Требуется авторизация"}), 401
    return redirect(url_for("login", next=request.full_path))


# ---------------------------------------------------------------- helpers

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            if request.path.startswith("/admin/api/"):
                return jsonify({"ok": False, "error": "Требуется вход администратора"}), 401
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)

    return wrapper


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(value: str) -> str:
    value = value.lower().strip()
    out = []
    for ch in value:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isalnum() or ch in "-_":
            out.append(ch)
        else:
            out.append("-")
    value = re.sub(r"-+", "-", "".join(out)).strip("-")
    return value or "untitled"


def unique_slug(value: str, model, existing_id=None) -> str:
    base = slugify(value)
    slug = base
    i = 2
    while True:
        q = model.query.filter_by(slug=slug)
        if existing_id:
            q = q.filter(model.id != existing_id)
        if not q.first():
            return slug
        slug = f"{base}-{i}"
        i += 1


def get_setting(key: str, default: str = "") -> str:
    s = Setting.query.filter_by(key=key).first()
    return s.value if s else default


def set_setting(key: str, value: str) -> None:
    s = Setting.query.filter_by(key=key).first()
    if not s:
        s = Setting(key=key, value=value)
        db.session.add(s)
    else:
        s.value = value


def save_upload(file_storage, subdir: str, allowed_exts: set):
    """Сохраняет файл в uploads/<subdir>/, возвращает (относительный путь, ошибка)."""
    if not file_storage or not file_storage.filename:
        return None, "Файл не выбран"
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in allowed_exts:
        return None, "Недопустимый тип файла"
    fname = secure_filename(file_storage.filename) or "file"
    unique = f"{secrets.token_hex(8)}_{fname}"
    target_dir = os.path.join(UPLOAD_DIR, subdir)
    os.makedirs(target_dir, exist_ok=True)
    file_storage.save(os.path.join(target_dir, unique))
    return f"{subdir}/{unique}", None


def attempts_count(employee_id: int, test_id: int) -> int:
    return Attempt.query.filter_by(
        employee_id=employee_id, test_id=test_id, status="finished"
    ).count()


def best_attempt(employee_id: int, test_id: int):
    return (
        Attempt.query.filter_by(employee_id=employee_id, test_id=test_id, status="finished")
        .order_by(Attempt.percent.desc(), Attempt.finished_at.desc())
        .first()
    )


def passed_test_in_category(employee_id: int, category_id: int, test_type: str) -> bool:
    tests = Test.query.filter_by(category_id=category_id, test_type=test_type, is_active=True).all()
    for t in tests:
        att = Attempt.query.filter_by(
            employee_id=employee_id, test_id=t.id, status="finished", passed=True
        ).first()
        if att:
            return True
    return False


def _run(cmd, cwd=BASE_DIR):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=180)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def _git_info():
    rc, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    rc2, commit = _run(["git", "rev-parse", "--short", "HEAD"])
    return {
        "branch": branch.strip() if rc == 0 else "-",
        "commit": commit.strip() if rc2 == 0 else "-",
    }


def _find_fonts():
    cand_regular = [
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    cand_bold = [
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]
    regular = next((p for p in cand_regular if os.path.exists(p)), None)
    bold = next((p for p in cand_bold if os.path.exists(p)), None)
    return regular, bold


def generate_certificate_pdf(attempt: Attempt):
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    regular, bold = _find_fonts()
    if regular:
        pdfmetrics.registerFont(TTFont("Cert", regular))
    if bold:
        pdfmetrics.registerFont(TTFont("CertBold", bold))
    font = "CertBold" if bold else ("Cert" if regular else "Helvetica-Bold")
    font_r = "Cert" if regular else "Helvetica"

    buf = io.BytesIO()
    w, h = landscape(A4)
    c = canvas.Canvas(buf, pagesize=landscape(A4))

    c.setLineWidth(2)
    c.setStrokeColorRGB(0.15, 0.35, 0.75)
    c.rect(30, 30, w - 60, h - 60)

    org = get_setting("cert_org_name", get_setting("site_name", "Учебный центр"))
    c.setFont(font, 16)
    c.drawCentredString(w / 2, h - 80, org)

    c.setFont(font, 30)
    c.setFillColorRGB(0.15, 0.35, 0.75)
    c.drawCentredString(w / 2, h - 130, "СЕРТИФИКАТ")

    c.setFillColorRGB(0, 0, 0)
    c.setFont(font_r, 14)
    c.drawCentredString(w / 2, h - 165, "о прохождении обучения и итогового тестирования")

    employee = attempt.employee
    test = attempt.test
    c.setFont(font, 24)
    c.drawCentredString(w / 2, h - 220, employee.full_name)

    c.setFont(font_r, 14)
    c.drawCentredString(w / 2, h - 255, f"по курсу: {test.title}")

    finished = attempt.finished_at.strftime("%d.%m.%Y") if attempt.finished_at else ""
    c.drawCentredString(w / 2, h - 285, f"Результат: {attempt.percent:.0f}% ({attempt.score} из {attempt.total})")
    c.drawCentredString(w / 2, h - 310, f"Дата: {finished}")

    signature = get_setting("cert_signature", "")
    if signature:
        c.drawString(90, 80, "Руководитель: ______________")
        c.drawString(90, 60, signature)
    c.showPage()
    c.save()
    buf.seek(0)
    return buf


# ---------------------------------------------------------------- init db

def _ensure_columns():
    from sqlalchemy import inspect, text

    try:
        inspector = inspect(db.engine)
        existing = set(inspector.get_table_names())
        for model in (Employee, AdminUser, Category, Material, Test, Question, AnswerOption, Attempt, Setting):
            table = model.__tablename__
            if table not in existing:
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            for col in model.__table__.columns:
                if col.name in cols:
                    continue
                try:
                    coltype = col.type.compile(db.engine.dialect)
                    with db.engine.begin() as conn:
                        conn.execute(text(f'ALTER TABLE {table} ADD COLUMN "{col.name}" {coltype}'))
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass


def init_db():
    db.create_all()
    _ensure_columns()
    if not AdminUser.query.first():
        admin = AdminUser(
            username=config.ADMIN_USERNAME,
            password_hash=generate_password_hash(config.ADMIN_PASSWORD),
        )
        db.session.add(admin)
    db.session.commit()


with app.app_context():
    init_db()


@app.context_processor
def inject_globals():
    return {
        "site_name": get_setting("site_name", config.SITE_NAME),
        "site_url": config.SITE_URL,
        "now_year": datetime.now().year,
    }


@app.template_filter("dt")
def dt_filter(value, fmt="%d.%m.%Y %H:%M"):
    if not value:
        return "-"
    return value.strftime(fmt)


@app.before_request
def force_password_change():
    if current_user.is_authenticated and getattr(current_user, "must_change_password", False):
        ep = request.endpoint
        if ep not in ("change_password", "logout", "static") and not request.path.startswith("/static/"):
            return redirect(url_for("change_password"))


# ---------------------------------------------------------------- public

@app.route("/")
def index():
    categories = (
        Category.query.filter_by(is_active=True)
        .order_by(Category.sort_order, Category.id)
        .all()
    )
    return render_template("index.html", categories=categories)


@app.route("/login/", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        employee_id = (request.form.get("employee_id") or "").strip()
        password = request.form.get("password") or ""
        employee = Employee.query.filter_by(employee_id=employee_id, is_active=True).first()
        if employee and check_password_hash(employee.password_hash, password):
            login_user(employee)
            employee.last_login = datetime.now(timezone.utc)
            db.session.commit()
            if employee.must_change_password:
                return redirect(url_for("change_password"))
            nxt = request.args.get("next")
            if nxt and nxt.startswith("/"):
                return redirect(nxt)
            return redirect(url_for("index"))
        error = "Неверный табельный номер или пароль"
    return render_template("login.html", error=error)


@app.route("/logout/")
@login_required
def logout():
    logout_user()
    flash("Вы вышли из личного кабинета.", "info")
    return redirect(url_for("index"))


@app.route("/password/change/", methods=["GET", "POST"])
@login_required
def change_password():
    error = None
    if request.method == "POST":
        p1 = request.form.get("password") or ""
        p2 = request.form.get("password2") or ""
        if len(p1) < 6:
            error = "Пароль должен быть не короче 6 символов"
        elif p1 != p2:
            error = "Пароли не совпадают"
        else:
            current_user.password_hash = generate_password_hash(p1)
            current_user.must_change_password = False
            db.session.commit()
            flash("Пароль изменён.", "success")
            return redirect(url_for("index"))
    return render_template("password_change.html", error=error)


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/category/<slug>/")
def category_page(slug):
    category = Category.query.filter_by(slug=slug, is_active=True).first_or_404()
    materials = [m for m in category.materials if m.is_active]
    tests = [t for t in category.tests if t.is_active]
    tests.sort(key=lambda t: 0 if t.test_type == "preliminary" else 1)

    statuses = {}
    if current_user.is_authenticated:
        for t in tests:
            best = best_attempt(current_user.id, t.id)
            statuses[t.id] = {
                "attempts": attempts_count(current_user.id, t.id),
                "best": best,
            }
        pred_passed = passed_test_in_category(current_user.id, category.id, "preliminary")
    else:
        pred_passed = False

    return render_template(
        "category.html",
        category=category,
        materials=materials,
        tests=tests,
        statuses=statuses,
        pred_passed=pred_passed,
    )


@app.route("/category/<slug>/documents/")
@login_required
def category_documents(slug):
    category = Category.query.filter_by(slug=slug, is_active=True).first_or_404()
    materials = [m for m in category.materials if m.is_active]
    return render_template(
        "documents.html",
        category=category,
        materials=materials,
    )


@app.route("/material/<int:material_id>/file/")
@login_required
def material_file(material_id):
    material = Material.query.get_or_404(material_id)
    if not material.file_path:
        abort(404)
    ext = os.path.splitext(material.file_path)[1].lower()
    as_attachment = ext not in {".jpg", ".jpeg"}
    return send_from_directory(UPLOAD_DIR, material.file_path, as_attachment=as_attachment)


@app.route("/test/<int:test_id>/")
@login_required
def take_test(test_id):
    test = Test.query.get_or_404(test_id)
    if not test.is_active:
        abort(404)
    if test.test_type == "final" and not passed_test_in_category(
        current_user.id, test.category_id, "preliminary"
    ):
        flash("Сначала пройдите предварительное тестирование.", "warning")
        return redirect(url_for("category_page", slug=test.category.slug))
    if test.attempts_limit and attempts_count(current_user.id, test.id) >= test.attempts_limit:
        flash("Лимит попыток исчерпан.", "danger")
        return redirect(url_for("category_page", slug=test.category.slug))

    questions = (
        Question.query.filter_by(test_id=test.id, is_active=True)
        .order_by(Question.sort_order, Question.id)
        .all()
    )
    if not questions:
        flash("В тесте пока нет вопросов.", "warning")
        return redirect(url_for("category_page", slug=test.category.slug))

    if test.shuffle_questions:
        import random

        random.shuffle(questions)

    attempt = Attempt(
        employee_id=current_user.id,
        test_id=test.id,
        started_at=datetime.now(timezone.utc),
        status="in_progress",
        total=len(questions),
    )
    db.session.add(attempt)
    db.session.commit()

    return render_template("test.html", test=test, attempt=attempt, questions=questions)


@app.route("/test/<int:test_id>/submit/", methods=["POST"])
@login_required
def submit_test(test_id):
    test = Test.query.get_or_404(test_id)
    data = request.get_json(silent=True) or {}
    answers = data.get("answers") or {}

    attempt = (
        Attempt.query.filter_by(employee_id=current_user.id, test_id=test.id, status="in_progress")
        .order_by(Attempt.started_at.desc())
        .first()
    )
    if not attempt:
        return jsonify({"ok": False, "error": "Попытка не найдена"}), 400

    questions = {
        q.id: q for q in Question.query.filter_by(test_id=test.id, is_active=True).all()
    }
    correct = 0
    details = []
    for qid, q in questions.items():
        selected = answers.get(str(qid), [])
        if isinstance(selected, (int, str)):
            selected = [selected]
        try:
            selected = {int(x) for x in selected}
        except (TypeError, ValueError):
            selected = set()
        correct_ids = {o.id for o in q.options if o.is_correct}
        ok = bool(correct_ids) and selected == correct_ids
        if ok:
            correct += 1
        details.append(
            {
                "question_id": qid,
                "selected": sorted(selected),
                "correct": sorted(correct_ids),
                "ok": ok,
            }
        )

    total = len(questions)
    percent = round(correct / total * 100, 1) if total else 0.0
    passed = percent >= test.passing_score

    attempt.score = correct
    attempt.total = total
    attempt.percent = percent
    attempt.passed = passed
    attempt.status = "finished"
    attempt.finished_at = datetime.now(timezone.utc)
    attempt.answers_json = json.dumps(details, ensure_ascii=False)
    db.session.commit()

    return jsonify({"ok": True, "redirect": url_for("test_result", attempt_id=attempt.id)})


@app.route("/test/result/<int:attempt_id>/")
@login_required
def test_result(attempt_id):
    attempt = Attempt.query.get_or_404(attempt_id)
    if attempt.employee_id != current_user.id:
        abort(403)
    details = json.loads(attempt.answers_json or "[]")
    questions_map = {q.id: q for q in attempt.test.questions}
    return render_template(
        "test_result.html",
        attempt=attempt,
        test=attempt.test,
        details=details,
        questions_map=questions_map,
    )


@app.route("/certificate/<int:attempt_id>/")
@login_required
def certificate(attempt_id):
    attempt = Attempt.query.get_or_404(attempt_id)
    if attempt.employee_id != current_user.id:
        abort(403)
    if not attempt.passed or attempt.status != "finished":
        abort(404)
    if attempt.test.test_type != "final":
        abort(404)
    buf = generate_certificate_pdf(attempt)
    fname = f"certificate_{attempt.employee.employee_id}_{attempt.test_id}.pdf"
    return send_file(
        buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=fname,
    )


# ---------------------------------------------------------------- admin auth

@app.route("/admin/")
def admin_index():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("admin_login"))


@app.route("/admin/login/", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        admin = AdminUser.query.filter_by(username=username).first()
        if admin and check_password_hash(admin.password_hash, password):
            if admin.totp_enabled:
                session["admin_pending"] = admin.id
                return redirect(url_for("admin_2fa"))
            session["admin_logged_in"] = True
            session["admin_id"] = admin.id
            return redirect(url_for("admin_dashboard"))
        error = "Неверный логин или пароль"
    return render_template("admin/login.html", error=error)


@app.route("/admin/2fa/", methods=["GET", "POST"])
def admin_2fa():
    pending_id = session.get("admin_pending")
    if not pending_id:
        return redirect(url_for("admin_login"))
    admin = db.session.get(AdminUser, pending_id)
    if not admin:
        return redirect(url_for("admin_login"))
    error = None
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        if pyotp.TOTP(admin.totp_secret).verify(code, valid_window=1):
            session.pop("admin_pending", None)
            session["admin_logged_in"] = True
            session["admin_id"] = admin.id
            return redirect(url_for("admin_dashboard"))
        error = "Неверный код"
    return render_template("admin/2fa.html", error=error)


@app.route("/admin/logout/")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin/dashboard/")
@admin_required
def admin_dashboard():
    stats = {
        "categories": Category.query.count(),
        "materials": Material.query.count(),
        "tests": Test.query.count(),
        "employees": Employee.query.count(),
        "attempts": Attempt.query.filter_by(status="finished").count(),
    }
    recent = (
        Attempt.query.filter_by(status="finished")
        .order_by(Attempt.finished_at.desc())
        .limit(10)
        .all()
    )
    git = _git_info()
    return render_template("admin/dashboard.html", stats=stats, recent=recent, git=git)


@app.route("/admin/update/", methods=["POST"])
@admin_required
def admin_update():
    branch = config.UPDATE_BRANCH
    lines = []
    rc, out = _run(["git", "fetch", "origin"])
    lines.append(f"[fetch] rc={rc}\n{out}")
    rc, out = _run(["git", "reset", "--hard", f"origin/{branch}"])
    lines.append(f"[reset --hard origin/{branch}] rc={rc}\n{out}")
    rc, out = _run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    lines.append(f"[pip install] rc={rc}\n{out}")
    if config.UPDATE_RESTART_CMD:
        rc, out = _run(config.UPDATE_RESTART_CMD.split())
        lines.append(f"[restart] rc={rc}\n{out}")
    else:
        lines.append("[restart] команда не задана (UPDATE_RESTART_CMD) — перезапустите сервис вручную")
    return jsonify({"ok": True, "output": "\n\n".join(lines)})


# ---------------------------------------------------------------- admin: 2FA setup

@app.route("/admin/2fa/setup/", methods=["GET", "POST"])
@admin_required
def admin_2fa_setup():
    admin = db.session.get(AdminUser, session["admin_id"])
    if request.method == "POST":
        action = request.form.get("action")
        if action == "enable":
            code = (request.form.get("code") or "").strip()
            if pyotp.TOTP(admin.totp_secret).verify(code, valid_window=1):
                admin.totp_enabled = True
                db.session.commit()
                flash("Двухфакторная аутентификация включена.", "success")
                return redirect(url_for("admin_dashboard"))
            flash("Неверный код, попробуйте ещё раз.", "danger")
        elif action == "disable":
            admin.totp_enabled = False
            admin.totp_secret = None
            db.session.commit()
            flash("Двухфакторная аутентификация отключена.", "success")
            return redirect(url_for("admin_dashboard"))
    if not admin.totp_secret:
        admin.totp_secret = pyotp.random_base32()
        db.session.commit()
    otp = pyotp.TOTP(admin.totp_secret)
    uri = otp.provisioning_uri(name=admin.username, issuer_name="UC-Fores")
    qr_img = qrcode.make(uri)
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    return render_template("admin/2fa_setup.html", qr=qr_b64, secret=admin.totp_secret)


# ---------------------------------------------------------------- admin: categories

@app.route("/admin/categories/")
@admin_required
def admin_categories():
    categories = Category.query.order_by(Category.sort_order, Category.id).all()
    return render_template("admin/categories.html", categories=categories)


@app.route("/admin/categories/new/", methods=["GET", "POST"])
@admin_required
def admin_category_new():
    return _category_form(None)


@app.route("/admin/categories/<int:category_id>/edit/", methods=["GET", "POST"])
@admin_required
def admin_category_edit(category_id):
    category = Category.query.get_or_404(category_id)
    return _category_form(category)


def _category_form(category):
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        if not title:
            flash("Название обязательно.", "danger")
            return render_template("admin/category_edit.html", category=category)
        slug = (request.form.get("slug") or "").strip() or slugify(title)
        slug = unique_slug(slug, Category, existing_id=category.id if category else None)
        if not category:
            category = Category()
            db.session.add(category)
        category.title = title
        category.slug = slug
        category.description = (request.form.get("description") or "").strip()
        category.sort_order = int(request.form.get("sort_order") or 0)
        category.is_active = request.form.get("is_active") == "on"
        db.session.commit()
        flash("Категория сохранена.", "success")
        return redirect(url_for("admin_categories"))
    return render_template("admin/category_edit.html", category=category)


@app.route("/admin/categories/<int:category_id>/delete/", methods=["POST"])
@admin_required
def admin_category_delete(category_id):
    category = Category.query.get_or_404(category_id)
    Material.query.filter_by(category_id=category.id).delete()
    for t in Test.query.filter_by(category_id=category.id).all():
        Question.query.filter_by(test_id=t.id).delete()
        Attempt.query.filter_by(test_id=t.id).delete()
    Test.query.filter_by(category_id=category.id).delete()
    db.session.delete(category)
    db.session.commit()
    flash("Категория удалена.", "success")
    return redirect(url_for("admin_categories"))


# ---------------------------------------------------------------- admin: materials

@app.route("/admin/materials/")
@admin_required
def admin_materials():
    materials = Material.query.order_by(Material.category_id, Material.sort_order).all()
    return render_template("admin/materials.html", materials=materials)


@app.route("/admin/materials/new/", methods=["GET", "POST"])
@admin_required
def admin_material_new():
    return _material_form(None)


@app.route("/admin/materials/<int:material_id>/edit/", methods=["GET", "POST"])
@admin_required
def admin_material_edit(material_id):
    material = Material.query.get_or_404(material_id)
    return _material_form(material)


def _material_form(material):
    categories = Category.query.order_by(Category.sort_order, Category.id).all()
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        if not title:
            flash("Название обязательно.", "danger")
            return render_template("admin/material_edit.html", material=material, categories=categories)
        if not material:
            material = Material()
            db.session.add(material)
        material.title = title
        material.category_id = int(request.form.get("category_id") or 0)
        material.description = (request.form.get("description") or "").strip()
        material.content = (request.form.get("content") or "").strip()
        material.sort_order = int(request.form.get("sort_order") or 0)
        material.is_active = request.form.get("is_active") == "on"
        upload = request.files.get("file")
        if upload and upload.filename:
            path, err = save_upload(upload, "materials", DOCUMENT_EXTS)
            if err:
                flash(err, "danger")
                return render_template("admin/material_edit.html", material=material, categories=categories)
            material.file_path = path
        db.session.commit()
        flash("Документ сохранён.", "success")
        return redirect(url_for("admin_materials"))
    return render_template("admin/material_edit.html", material=material, categories=categories)


@app.route("/admin/materials/<int:material_id>/delete/", methods=["POST"])
@admin_required
def admin_material_delete(material_id):
    material = Material.query.get_or_404(material_id)
    db.session.delete(material)
    db.session.commit()
    flash("Документ удалён.", "success")
    return redirect(url_for("admin_materials"))


# ---------------------------------------------------------------- admin: tests

@app.route("/admin/tests/")
@admin_required
def admin_tests():
    tests = Test.query.order_by(Test.category_id, Test.test_type, Test.id).all()
    return render_template("admin/tests.html", tests=tests)


@app.route("/admin/tests/new/", methods=["GET", "POST"])
@admin_required
def admin_test_new():
    return _test_form(None)


@app.route("/admin/tests/<int:test_id>/edit/", methods=["GET", "POST"])
@admin_required
def admin_test_edit(test_id):
    test = Test.query.get_or_404(test_id)
    return _test_form(test)


def _test_form(test):
    categories = Category.query.order_by(Category.sort_order, Category.id).all()
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        if not title:
            flash("Название обязательно.", "danger")
            return render_template("admin/test_edit.html", test=test, categories=categories)
        if not test:
            test = Test()
            db.session.add(test)
        test.title = title
        test.category_id = int(request.form.get("category_id") or 0)
        test.test_type = request.form.get("test_type") or "preliminary"
        test.passing_score = int(request.form.get("passing_score") or config.DEFAULT_PASS_SCORE)
        tl = (request.form.get("time_limit_minutes") or "").strip()
        test.time_limit_minutes = int(tl) if tl.isdigit() else None
        al = (request.form.get("attempts_limit") or "").strip()
        test.attempts_limit = int(al) if al.isdigit() else None
        test.shuffle_questions = request.form.get("shuffle_questions") == "on"
        test.is_active = request.form.get("is_active") == "on"
        test.description = (request.form.get("description") or "").strip()
        db.session.commit()
        flash("Тест сохранён.", "success")
        return redirect(url_for("admin_tests"))
    return render_template("admin/test_edit.html", test=test, categories=categories)


@app.route("/admin/tests/<int:test_id>/delete/", methods=["POST"])
@admin_required
def admin_test_delete(test_id):
    test = Test.query.get_or_404(test_id)
    Question.query.filter_by(test_id=test.id).delete()
    Attempt.query.filter_by(test_id=test.id).delete()
    db.session.delete(test)
    db.session.commit()
    flash("Тест удалён.", "success")
    return redirect(url_for("admin_tests"))


@app.route("/admin/tests/<int:test_id>/editor/")
@admin_required
def admin_test_editor(test_id):
    test = Test.query.get_or_404(test_id)
    questions = (
        Question.query.filter_by(test_id=test.id)
        .order_by(Question.sort_order, Question.id)
        .all()
    )
    return render_template("admin/test_editor.html", test=test, questions=questions)


# ---------------------------------------------------------------- admin: test editor API

@app.route("/admin/api/tests/<int:test_id>/questions/", methods=["POST"])
@admin_required
def api_question_create(test_id):
    test = Test.query.get_or_404(test_id)
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Текст вопроса обязателен"}), 400
    qtype = data.get("question_type") or "single"
    if qtype not in ("single", "multiple"):
        qtype = "single"
    max_sort = (
        db.session.query(db.func.max(Question.sort_order))
        .filter_by(test_id=test.id)
        .scalar()
        or 0
    )
    q = Question(test_id=test.id, text=text, question_type=qtype, sort_order=max_sort + 1)
    db.session.add(q)
    db.session.commit()
    return jsonify({"ok": True, "question_id": q.id})


@app.route("/admin/api/questions/<int:question_id>/", methods=["PUT"])
@admin_required
def api_question_update(question_id):
    q = Question.query.get_or_404(question_id)
    data = request.get_json(silent=True) or {}
    if "text" in data:
        q.text = (data.get("text") or "").strip()
    if "question_type" in data and data["question_type"] in ("single", "multiple"):
        q.question_type = data["question_type"]
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/api/questions/<int:question_id>/", methods=["DELETE"])
@admin_required
def api_question_delete(question_id):
    q = Question.query.get_or_404(question_id)
    db.session.delete(q)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/api/questions/<int:question_id>/options/", methods=["POST"])
@admin_required
def api_option_create(question_id):
    q = Question.query.get_or_404(question_id)
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Текст варианта обязателен"}), 400
    max_sort = (
        db.session.query(db.func.max(AnswerOption.sort_order))
        .filter_by(question_id=question_id)
        .scalar()
        or 0
    )
    opt = AnswerOption(
        question_id=question_id,
        text=text,
        is_correct=bool(data.get("is_correct")),
        sort_order=max_sort + 1,
    )
    db.session.add(opt)
    db.session.commit()
    return jsonify({"ok": True, "option_id": opt.id})


@app.route("/admin/api/options/<int:option_id>/", methods=["PUT"])
@admin_required
def api_option_update(option_id):
    opt = AnswerOption.query.get_or_404(option_id)
    data = request.get_json(silent=True) or {}
    if "text" in data:
        opt.text = (data.get("text") or "").strip()
    if "is_correct" in data:
        opt.is_correct = bool(data["is_correct"])
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/api/options/<int:option_id>/", methods=["DELETE"])
@admin_required
def api_option_delete(option_id):
    opt = AnswerOption.query.get_or_404(option_id)
    db.session.delete(opt)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/api/questions/<int:question_id>/image/", methods=["POST"])
@admin_required
def api_question_image(question_id):
    q = Question.query.get_or_404(question_id)
    path, err = save_upload(request.files.get("image"), "images", IMAGE_EXTS)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    q.image_path = path
    db.session.commit()
    return jsonify({"ok": True, "url": url_for("uploaded_file", filename=path)})


@app.route("/admin/api/options/<int:option_id>/image/", methods=["POST"])
@admin_required
def api_option_image(option_id):
    opt = AnswerOption.query.get_or_404(option_id)
    path, err = save_upload(request.files.get("image"), "images", IMAGE_EXTS)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    opt.image_path = path
    db.session.commit()
    return jsonify({"ok": True, "url": url_for("uploaded_file", filename=path)})


# ---------------------------------------------------------------- admin: employees

@app.route("/admin/employees/")
@admin_required
def admin_employees():
    q = (request.args.get("q") or "").strip()
    query = Employee.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(Employee.employee_id.ilike(like), Employee.full_name.ilike(like))
        )
    employees = query.order_by(Employee.id).all()
    return render_template("admin/employees.html", employees=employees, q=q)


@app.route("/admin/employees/new/", methods=["GET", "POST"])
@admin_required
def admin_employee_new():
    return _employee_form(None)


@app.route("/admin/employees/<int:employee_id>/edit/", methods=["GET", "POST"])
@admin_required
def admin_employee_edit(employee_id):
    employee = Employee.query.get_or_404(employee_id)
    return _employee_form(employee)


def _employee_form(employee):
    if request.method == "POST":
        employee_id = (request.form.get("employee_id") or "").strip()
        full_name = (request.form.get("full_name") or "").strip()
        if not employee_id or not full_name:
            flash("Табельный номер и ФИО обязательны.", "danger")
            return render_template("admin/employee_edit.html", employee=employee)
        exists = Employee.query.filter_by(employee_id=employee_id)
        if employee:
            exists = exists.filter(Employee.id != employee.id)
        if exists.first():
            flash("Сотрудник с таким табельным номером уже существует.", "danger")
            return render_template("admin/employee_edit.html", employee=employee)
        if not employee:
            employee = Employee()
            db.session.add(employee)
        employee.employee_id = employee_id
        employee.full_name = full_name
        password = (request.form.get("password") or "").strip()
        if password:
            employee.password_hash = generate_password_hash(password)
        employee.is_active = request.form.get("is_active") == "on"
        db.session.commit()
        flash("Сотрудник сохранён.", "success")
        return redirect(url_for("admin_employees"))
    return render_template("admin/employee_edit.html", employee=employee)


@app.route("/admin/employees/import/", methods=["GET", "POST"])
@admin_required
def admin_employee_import():
    result = None
    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or not upload.filename:
            result = ("danger", "Файл не выбран")
        else:
            ext = os.path.splitext(upload.filename)[1].lower()
            if ext not in IMPORT_EXTS:
                result = ("danger", "Поддерживаются только CSV или XLSX")
            else:
                count, msg = _import_employees(upload, ext)
                result = ("success" if count else "danger", msg)
    return render_template("admin/employee_import.html", result=result)


def _import_employees(upload, ext):
    rows = []
    try:
        if ext == ".csv":
            text = upload.read().decode("utf-8-sig", errors="replace")
            rows = [r for r in csv.reader(io.StringIO(text))]
        else:
            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(upload.read()))
            ws = wb.active
            rows = [
                [str(c.value).strip() if c.value is not None else "" for c in row]
                for row in ws.iter_rows()
            ]
    except Exception as exc:  # noqa: BLE001
        return 0, f"Ошибка чтения файла: {exc}"

    if not rows:
        return 0, "Файл пустой"

    header = [str(h).strip().lower() for h in rows[0]]

    def col(*names):
        for n in names:
            n = n.lower()
            for i, h in enumerate(header):
                if n in h:
                    return i
        return None

    i_emp = col("табельный", "employee_id", "таб. номер", "номер", "id")
    i_name = col("фио", "full_name", "имя", "сотрудник", "name")
    i_pass = col("пароль", "password")
    if i_emp is None or i_name is None:
        return 0, "Не найдены колонки «табельный номер» и «ФИО»"

    created = updated = skipped = 0
    for row in rows[1:]:
        if not row:
            continue
        emp_id = (row[i_emp] or "").strip() if i_emp < len(row) else ""
        name = (row[i_name] or "").strip() if i_name < len(row) else ""
        if not emp_id or not name:
            skipped += 1
            continue
        has_password = i_pass is not None and i_pass < len(row) and bool(row[i_pass].strip())
        password = row[i_pass].strip() if has_password else config.EMPLOYEE_DEFAULT_PASSWORD
        existing = Employee.query.filter_by(employee_id=emp_id).first()
        if existing:
            existing.full_name = name
            if has_password:
                existing.password_hash = generate_password_hash(password)
            updated += 1
        else:
            db.session.add(
                Employee(
                    employee_id=emp_id,
                    full_name=name,
                    password_hash=generate_password_hash(password),
                )
            )
            created += 1
    db.session.commit()
    return created + updated, f"Создано: {created}, обновлено: {updated}, пропущено: {skipped}"


@app.route("/admin/employees/<int:employee_id>/delete/", methods=["POST"])
@admin_required
def admin_employee_delete(employee_id):
    employee = Employee.query.get_or_404(employee_id)
    Attempt.query.filter_by(employee_id=employee.id).delete()
    db.session.delete(employee)
    db.session.commit()
    flash("Сотрудник удалён.", "success")
    return redirect(url_for("admin_employees"))


# ---------------------------------------------------------------- admin: results

@app.route("/admin/results/")
@admin_required
def admin_results():
    category_id = request.args.get("category_id", type=int)
    test_id = request.args.get("test_id", type=int)
    employee_id = request.args.get("employee_id", type=int)

    query = Attempt.query.filter_by(status="finished").join(Test)
    if category_id:
        query = query.filter(Test.category_id == category_id)
    if test_id:
        query = query.filter(Attempt.test_id == test_id)
    if employee_id:
        query = query.filter(Attempt.employee_id == employee_id)

    attempts = query.order_by(Attempt.finished_at.desc()).limit(500).all()
    categories = Category.query.order_by(Category.sort_order).all()
    tests = Test.query.order_by(Test.category_id, Test.id).all()
    employees = Employee.query.order_by(Employee.id).all()
    return render_template(
        "admin/results.html",
        attempts=attempts,
        categories=categories,
        tests=tests,
        employees=employees,
        filters={"category_id": category_id, "test_id": test_id, "employee_id": employee_id},
    )


@app.route("/admin/results/export/")
@admin_required
def admin_results_export():
    attempts = (
        Attempt.query.filter_by(status="finished")
        .order_by(Attempt.finished_at.desc())
        .all()
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["ID", "Сотрудник", "Табельный", "Категория", "Тест", "Тип", "Балл", "Всего",
         "Процент", "Пройден", "Дата"]
    )
    for a in attempts:
        writer.writerow(
            [
                a.id,
                a.employee.full_name,
                a.employee.employee_id,
                a.test.category.title,
                a.test.title,
                "Предварительный" if a.test.test_type == "preliminary" else "Итоговый",
                a.score,
                a.total,
                f"{a.percent:.1f}",
                "Да" if a.passed else "Нет",
                a.finished_at.strftime("%d.%m.%Y %H:%M") if a.finished_at else "",
            ]
        )
    buf.seek(0)
    from flask import Response

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=results.csv"},
    )


@app.route("/admin/results/<int:attempt_id>/print/")
@admin_required
def admin_result_print(attempt_id):
    attempt = Attempt.query.get_or_404(attempt_id)
    org_name = get_setting("cert_org_name", get_setting("site_name", config.SITE_NAME))
    signature = get_setting("cert_signature", "")
    return render_template(
        "admin/result_print.html",
        attempt=attempt,
        org_name=org_name,
        signature=signature,
    )


# ---------------------------------------------------------------- admin: WP import

@app.route("/admin/import/")
@admin_required
def admin_import():
    import wp_import

    cfg = wp_import.get_wp_settings()
    categories = Category.query.order_by(Category.sort_order, Category.id).all()
    return render_template("admin/import.html", cfg=cfg, categories=categories)


@app.route("/admin/import/settings/", methods=["POST"])
@admin_required
def admin_import_settings():
    import wp_import

    data = {k: request.form.get(k) or "" for k in wp_import.WP_SETTING_KEYS}
    wp_import.save_wp_settings(data)
    flash("Настройки подключения к WordPress сохранены.", "success")
    return redirect(url_for("admin_import"))


def _run_import(func, *args):
    try:
        msg = func(*args)
        return jsonify({"ok": True, "message": msg})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/admin/import/check/", methods=["POST"])
@admin_required
def admin_import_check():
    import wp_import

    try:
        cfg = wp_import.get_wp_settings()
        conn = wp_import.wp_connect(cfg)
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = sorted(list(r.values())[0] for r in cur.fetchall())
            cur.execute("SELECT VERSION()")
            version = list(cur.fetchone().values())[0]
        conn.close()
        return jsonify({"ok": True, "version": version, "tables": tables})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/admin/import/users/", methods=["POST"])
@admin_required
def admin_import_users():
    import wp_import

    cfg = wp_import.get_wp_settings()
    return _run_import(wp_import.import_users, cfg)


@app.route("/admin/import/quizzes/", methods=["POST"])
@admin_required
def admin_import_quizzes():
    import wp_import

    cfg = wp_import.get_wp_settings()
    return _run_import(wp_import.import_quizzes, cfg)


@app.route("/admin/import/results/", methods=["POST"])
@admin_required
def admin_import_results():
    import wp_import

    cfg = wp_import.get_wp_settings()
    return _run_import(wp_import.import_results, cfg)


@app.route("/admin/import/documents/", methods=["POST"])
@admin_required
def admin_import_documents():
    import wp_import

    cfg = wp_import.get_wp_settings()
    category_id = request.form.get("category_id", type=int)
    if not category_id:
        return jsonify({"ok": False, "error": "Выберите категорию для документов"})
    return _run_import(wp_import.import_documents, cfg, category_id)


@app.route("/admin/import/dump/", methods=["POST"])
@admin_required
def admin_import_dump():
    import wp_import

    upload = request.files.get("dump")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "error": "Файл дампа не выбран"})
    cfg = wp_import.get_wp_settings()
    category_id = request.form.get("category_id", type=int)
    flags = {
        "users": request.form.get("users") == "on",
        "quizzes": request.form.get("quizzes") == "on",
        "results": request.form.get("results") == "on",
        "documents": request.form.get("documents") == "on",
    }
    try:
        data = upload.read()
        fname = (upload.filename or "").lower()
        if fname.endswith(".gz"):
            import gzip

            data = gzip.decompress(data)
        msg = wp_import.import_dump(data, cfg, category_id, flags)
        return jsonify({"ok": True, "message": msg})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------- admin: settings

@app.route("/admin/settings/", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        for key in ("site_name", "cert_org_name", "cert_signature"):
            set_setting(key, (request.form.get(key) or "").strip())
        db.session.commit()
        flash("Настройки сохранены.", "success")
        return redirect(url_for("admin_settings"))
    data = {
        "site_name": get_setting("site_name", config.SITE_NAME),
        "cert_org_name": get_setting("cert_org_name", ""),
        "cert_signature": get_setting("cert_signature", ""),
    }
    return render_template("admin/settings.html", data=data)


# ---------------------------------------------------------------- errors

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(413)
def too_large(e):
    if request.path.startswith("/admin/"):
        return jsonify({"ok": False, "error": "Файл слишком большой. Увеличьте MAX_UPLOAD_MB в .env и перезапустите сервер"}), 413
    return "Файл слишком большой", 413


@app.errorhandler(500)
def server_error(e):
    if request.path.startswith("/admin/"):
        return jsonify({"ok": False, "error": "Внутренняя ошибка сервера"}), 500
    return "Внутренняя ошибка сервера", 500


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=5000)
