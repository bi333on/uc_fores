"""Импорт данных из WordPress (плагин Quiz And Survey Master) в UC-Fores.

Поддерживает:
- пользователи WordPress (wp_users) -> Employee
- тесты QSM (mlw_quizzes + mlw_questions) -> Category / Test / Question / AnswerOption
- результаты QSM (mlw_results) -> Attempt
- нормативные документы (wp_posts attachments, PDF) -> Material
"""
import json
import os
import re
import secrets
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from models import (
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

WP_SETTING_KEYS = [
    "wp_host", "wp_port", "wp_db", "wp_user", "wp_pass",
    "wp_prefix", "wp_site_url", "wp_default_password",
]


# ------------------------------------------------------------------ settings

def get_wp_settings():
    cfg = {}
    for k in WP_SETTING_KEYS:
        s = Setting.query.filter_by(key=k).first()
        cfg[k] = (s.value if s else "").strip()
    cfg["wp_prefix"] = cfg.get("wp_prefix") or "wp_"
    cfg["wp_port"] = cfg.get("wp_port") or "3306"
    cfg["wp_default_password"] = cfg.get("wp_default_password") or "123456"
    return cfg


def save_wp_settings(data):
    for k in WP_SETTING_KEYS:
        s = Setting.query.filter_by(key=k).first()
        value = (data.get(k) or "").strip()
        if not s:
            s = Setting(key=k, value=value)
            db.session.add(s)
        else:
            s.value = value
    db.session.commit()


def wp_connect(cfg):
    import pymysql

    return pymysql.connect(
        host=cfg["wp_host"],
        port=int(cfg["wp_port"]),
        user=cfg["wp_user"],
        password=cfg["wp_pass"],
        database=cfg["wp_db"],
        charset="utf8mb4",
        connect_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
    )


# ------------------------------------------------------------------ helpers

def php_unserialize(data):
    """Распаковка PHP serialize(); фолбэк на JSON."""
    if data is None or data == "":
        return None
    try:
        from phpserialize import loads

        b = data.encode("utf-8") if isinstance(data, str) else data
        return loads(b, decode_strings=True)
    except Exception:
        pass
    try:
        return json.loads(data)
    except Exception:
        return None


def _to_list(val):
    if val is None:
        return []
    if isinstance(val, dict):
        return list(val.values())
    if isinstance(val, (list, tuple)):
        return list(val)
    return [val]


def _table_exists(cur, table):
    cur.execute("SHOW TABLES")
    tables = {list(r.values())[0].lower() for r in cur.fetchall()}
    return table.lower() in tables


def _clean_quiz_name(name):
    n = re.sub(
        r"(?i)(онлайн\s+обучение\s+и\s+тестирование|предварительное\s+тестирование|"
        r"итоговое\s+тестирование|предварительный\s+тест|итоговый\s+тест)",
        " ",
        name or "",
    )
    n = re.sub(r"\b20\d{2}\b", " ", n)
    n = re.sub(r"[—\-–:]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip(" .-")
    return n or (name or "").strip()


def _test_type(name):
    if re.search(r"(?i)итог", name or ""):
        return "final"
    return "preliminary"


def _qtype(question_type_new, question_type):
    t = str(question_type_new or "").strip()
    if t in ("", "question_type"):
        t = str(question_type or "0").strip()
    if t in ("0", "1", "2"):
        return "single"
    if t in ("4", "14"):
        return "multiple"
    return None


def _parse_question(row):
    text = (row.get("question_name") or "").strip()
    if not text:
        return None
    qtype = _qtype(row.get("question_type_new"), row.get("question_type"))
    if qtype is None:
        return None

    answers = _to_list(php_unserialize(row.get("answer_array")))
    answers = [str(a).strip() for a in answers if str(a).strip()]
    if not answers:
        answers = []
        for i in range(1, 7):
            a = (row.get(f"answer_{i}") or "").strip()
            if a:
                answers.append(a)
    if not answers:
        return None

    correct = _to_list(php_unserialize(row.get("question_answer_info")))
    correct = {str(c).strip() for c in correct if str(c).strip()}
    if not correct:
        ca = row.get("correct_answer")
        if ca is not None and str(ca).strip() != "":
            ca_s = str(ca).strip()
            try:
                idx = int(ca_s)
                # QSM legacy: correct_answer хранит номер ответа (1-based)
                if 1 <= idx <= len(answers):
                    correct = {answers[idx - 1]}
                elif 0 <= idx < len(answers):
                    correct = {answers[idx]}
            except ValueError:
                correct = {ca_s}

    return {"text": text, "type": qtype, "answers": answers, "correct": correct}


def _get_imported_ids(key):
    s = Setting.query.filter_by(key=key).first()
    try:
        return set(json.loads(s.value)) if s and s.value else set()
    except Exception:
        return set()


def _save_imported_ids(key, ids):
    s = Setting.query.filter_by(key=key).first()
    if not s:
        s = Setting(key=key, value="")
        db.session.add(s)
    s.value = json.dumps(sorted(int(i) for i in ids))
    db.session.commit()


# ------------------------------------------------------------------ importers

def import_users(cfg):
    conn = wp_connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT ID, user_login, display_name, user_email "
                f"FROM {cfg['wp_prefix']}users ORDER BY ID"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    created = skipped = 0
    for r in rows:
        login = (r.get("user_login") or "").strip()
        name = (r.get("display_name") or "").strip() or login
        if not login:
            continue
        if Employee.query.filter_by(employee_id=login).first():
            skipped += 1
            continue
        db.session.add(
            Employee(
                employee_id=login,
                full_name=name,
                password_hash=generate_password_hash(cfg["wp_default_password"]),
                must_change_password=True,
                is_active=True,
            )
        )
        created += 1
    db.session.commit()
    return f"Создано: {created}, пропущено (уже есть): {skipped}"


def import_quizzes(cfg):
    conn = wp_connect(cfg)
    prefix = cfg["wp_prefix"]
    try:
        with conn.cursor() as cur:
            if not _table_exists(cur, f"{prefix}mlw_quizzes"):
                return "Таблица mlw_quizzes не найдена"
            cur.execute(f"SELECT * FROM {prefix}mlw_quizzes WHERE deleted=0 ORDER BY quiz_id")
            quizzes = cur.fetchall()
            if not _table_exists(cur, f"{prefix}mlw_questions"):
                return "Таблица mlw_questions не найдена"
    finally:
        conn.close()

    created_quizzes = created_questions = skipped_quizzes = 0
    for qz in quizzes:
        quiz_id = int(qz.get("quiz_id") or 0)
        quiz_name = (qz.get("quiz_name") or "").strip()
        if not quiz_name:
            continue
        if Test.query.filter_by(external_quiz_id=quiz_id).first():
            skipped_quizzes += 1
            continue

        cat_title = _clean_quiz_name(quiz_name)
        slug = re.sub(r"[^a-z0-9\-_]+", "-", cat_title.lower()).strip("-") or "course"
        category = Category.query.filter_by(title=cat_title).first()
        if not category:
            category = Category(title=cat_title, slug=slug, is_active=True)
            db.session.add(category)
            db.session.flush()

        test = Test(
            category_id=category.id,
            title=quiz_name,
            test_type=_test_type(quiz_name),
            passing_score=80,
            is_active=True,
            external_quiz_id=quiz_id,
        )
        db.session.add(test)
        db.session.flush()
        created_quizzes += 1

        # вопросы
        conn2 = wp_connect(cfg)
        try:
            with conn2.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {prefix}mlw_questions "
                    f"WHERE quiz_id=%s AND deleted=0 ORDER BY question_order, question_id",
                    (quiz_id,),
                )
                qrows = cur.fetchall()
        finally:
            conn2.close()

        order = 0
        for qrow in qrows:
            qdata = _parse_question(qrow)
            if not qdata:
                continue
            order += 1
            q = Question(
                test_id=test.id,
                text=qdata["text"],
                question_type=qdata["type"],
                sort_order=order,
            )
            db.session.add(q)
            db.session.flush()
            for i, ans in enumerate(qdata["answers"]):
                db.session.add(
                    AnswerOption(
                        question_id=q.id,
                        text=ans,
                        is_correct=ans in qdata["correct"],
                        sort_order=i + 1,
                    )
                )
            created_questions += 1

    db.session.commit()
    return (
        f"Тестов: {created_quizzes}, пропущено: {skipped_quizzes}, "
        f"вопросов импортировано: {created_questions}"
    )


def import_results(cfg):
    conn = wp_connect(cfg)
    prefix = cfg["wp_prefix"]
    try:
        with conn.cursor() as cur:
            if not _table_exists(cur, f"{prefix}mlw_results"):
                return "Таблица mlw_results не найдена"
            cur.execute(f"SELECT ID, user_login FROM {prefix}users")
            user_map = {int(r["ID"]): (r["user_login"] or "").strip() for r in cur.fetchall()}
            cur.execute(f"SELECT * FROM {prefix}mlw_results WHERE deleted=0 ORDER BY result_id")
            rows = cur.fetchall()
    finally:
        conn.close()

    imported_ids = _get_imported_ids("wp_imported_results")
    created = skipped = no_employee = no_test = 0
    for r in rows:
        result_id = int(r.get("result_id") or 0)
        if result_id in imported_ids:
            skipped += 1
            continue

        quiz_id = int(r.get("quiz_id") or 0)
        test = Test.query.filter_by(external_quiz_id=quiz_id).first()
        if not test:
            no_test += 1
            continue

        employee = None
        user_id = int(r.get("user") or 0)
        login = user_map.get(user_id)
        if login:
            employee = Employee.query.filter_by(employee_id=login).first()
        if not employee:
            name = (r.get("name") or "").strip()
            if name:
                employee = Employee.query.filter_by(full_name=name).first()
        if not employee:
            no_employee += 1
            continue

        correct = int(r.get("correct") or 0)
        total = int(r.get("total") or 0)
        percent = round(correct / total * 100, 1) if total else 0.0
        passed = percent >= test.passing_score

        finished = r.get("time_taken_real")
        if finished and isinstance(finished, str):
            try:
                finished = datetime.strptime(finished, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                finished = None
        finished = finished or datetime.now(timezone.utc)

        db.session.add(
            Attempt(
                employee_id=employee.id,
                test_id=test.id,
                started_at=finished,
                finished_at=finished,
                score=correct,
                total=total,
                percent=percent,
                passed=passed,
                status="finished",
                answers_json="[]",
            )
        )
        imported_ids.add(result_id)
        created += 1

    _save_imported_ids("wp_imported_results", imported_ids)
    db.session.commit()
    return (
        f"Результатов: {created}, пропущено (уже есть): {skipped}, "
        f"без теста: {no_test}, без сотрудника: {no_employee}"
    )


def import_documents(cfg, category_id):
    import requests

    conn = wp_connect(cfg)
    prefix = cfg["wp_prefix"]
    try:
        with conn.cursor() as cur:
            if not _table_exists(cur, f"{prefix}posts"):
                return "Таблица posts не найдена"
            cur.execute(
                f"SELECT ID, post_title, guid FROM {prefix}posts "
                f"WHERE post_type='attachment' AND post_mime_type='application/pdf' "
                f"ORDER BY ID"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    category = db.session.get(Category, category_id)
    if not category:
        return "Категория не выбрана"

    imported_ids = _get_imported_ids("wp_imported_documents")
    created = failed = skipped = 0
    materials_dir = os.path.join(UPLOAD_DIR, "materials")
    os.makedirs(materials_dir, exist_ok=True)

    for r in rows:
        att_id = int(r.get("ID") or 0)
        if att_id in imported_ids:
            skipped += 1
            continue
        url = (r.get("guid") or "").strip()
        title = (r.get("post_title") or "").strip() or f"Документ {att_id}"
        if not url:
            failed += 1
            continue
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
        except Exception:
            failed += 1
            continue

        base = secure_filename(os.path.splitext(os.path.basename(url.split("?")[0]))[0]) or "document"
        fname = f"{secrets.token_hex(6)}_{base}.pdf"
        with open(os.path.join(materials_dir, fname), "wb") as fh:
            fh.write(resp.content)

        db.session.add(
            Material(
                category_id=category.id,
                title=title,
                file_path=f"materials/{fname}",
                is_active=True,
            )
        )
        imported_ids.add(att_id)
        created += 1

    _save_imported_ids("wp_imported_documents", imported_ids)
    db.session.commit()
    return f"Документов: {created}, пропущено: {skipped}, ошибок загрузки: {failed}"
