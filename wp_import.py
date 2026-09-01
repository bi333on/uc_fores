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


def _create_test_and_questions(quiz_id, quiz_name, qrows):
    """Создаёт категорию, тест и вопросы для одного теста QSM.
    Возвращает (создан ли тест, кол-во созданных вопросов)."""
    if Test.query.filter_by(external_quiz_id=quiz_id).first():
        return 0, 0
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

    created_q = 0
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
        created_q += 1
    return 1, created_q


def _import_result_row(r, user_map, imported_ids):
    """Импортирует одну строку результата. Возвращает код результата."""
    try:
        result_id = int(r.get("result_id") or 0)
    except Exception:
        result_id = 0
    if result_id and result_id in imported_ids:
        return "skipped"
    try:
        quiz_id = int(r.get("quiz_id") or 0)
    except Exception:
        quiz_id = 0
    test = Test.query.filter_by(external_quiz_id=quiz_id).first()
    if not test:
        return "no_test"

    employee = None
    try:
        user_id = int(r.get("user") or 0)
    except Exception:
        user_id = 0
    login = user_map.get(user_id)
    if login:
        employee = Employee.query.filter_by(employee_id=str(login).strip()).first()
    if not employee:
        name = str(r.get("name") or "").strip()
        if name:
            employee = Employee.query.filter_by(full_name=name).first()
    if not employee:
        return "no_employee"

    try:
        correct = int(r.get("correct") or 0)
        total = int(r.get("total") or 0)
    except Exception:
        correct = total = 0
    percent = round(correct / total * 100, 1) if total else 0.0
    passed = percent >= test.passing_score

    finished = r.get("time_taken_real")
    if isinstance(finished, str) and finished:
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
    if result_id:
        imported_ids.add(result_id)
    return "created"


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
            cur.execute(
                f"SELECT * FROM {prefix}mlw_questions WHERE deleted=0 "
                f"ORDER BY question_order, question_id"
            )
            all_questions = cur.fetchall()
    finally:
        conn.close()

    q_by_quiz = {}
    for q in all_questions:
        try:
            quiz_id = int(q.get("quiz_id") or 0)
        except Exception:
            continue
        q_by_quiz.setdefault(quiz_id, []).append(q)

    created_quizzes = created_questions = skipped_quizzes = 0
    for qz in quizzes:
        quiz_id = int(qz.get("quiz_id") or 0)
        quiz_name = (qz.get("quiz_name") or "").strip()
        if not quiz_name:
            continue
        made, qcount = _create_test_and_questions(quiz_id, quiz_name, q_by_quiz.get(quiz_id, []))
        if not made:
            skipped_quizzes += 1
        else:
            created_quizzes += 1
        created_questions += qcount

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
    counts = {"created": 0, "skipped": 0, "no_employee": 0, "no_test": 0}
    for r in rows:
        code = _import_result_row(r, user_map, imported_ids)
        if code in counts:
            counts[code] += 1
    _save_imported_ids("wp_imported_results", imported_ids)
    db.session.commit()
    return (
        f"Результатов: {counts['created']}, пропущено (уже есть): {counts['skipped']}, "
        f"без теста: {counts['no_test']}, без сотрудника: {counts['no_employee']}"
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


# ------------------------------------------------------------------ SQL dump

def _unescape_mysql(s):
    out = []
    i = 0
    mapping = {
        "n": "\n", "r": "\r", "t": "\t", "0": "\0",
        "Z": "\x1a", "b": "\b", "\\": "\\", "'": "'", '"': '"',
    }
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] in mapping:
            out.append(mapping[s[i + 1]])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_top_level(s):
    parts = []
    cur = []
    depth = 0
    in_str = False
    i = 0
    while i < len(s):
        ch = s[i]
        if in_str:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 1
            elif ch == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    cur.append("'")
                    i += 1
                else:
                    in_str = False
        else:
            if ch == "'":
                in_str = True
                cur.append(ch)
            elif ch in "([":
                depth += 1
                cur.append(ch)
            elif ch in ")]":
                depth -= 1
                cur.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        i += 1
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _extract_rows(s):
    rows = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "(":
            depth = 0
            j = i
            in_str = False
            while j < n:
                ch = s[j]
                if in_str:
                    if ch == "\\":
                        j += 1
                    elif ch == "'":
                        if j + 1 < n and s[j + 1] == "'":
                            j += 1
                        else:
                            in_str = False
                else:
                    if ch == "'":
                        in_str = True
                    elif ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            rows.append(s[i + 1:j])
                            i = j
                            break
                j += 1
        i += 1
    return rows


def _find_stmt_end(text, start):
    i = start
    in_str = False
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 1
                else:
                    in_str = False
        else:
            if ch == "'":
                in_str = True
            elif ch == ";":
                return i
        i += 1
    return n - 1


def _iter_inserts(text):
    pos = 0
    while True:
        m = re.search(r"INSERT\s+(?:IGNORE\s+)?INTO", text[pos:], re.IGNORECASE)
        if not m:
            break
        start = pos + m.start()
        end = _find_stmt_end(text, start)
        yield text[start:end]
        pos = end + 1


def _parse_value(v):
    v = v.strip()
    if v.upper() == "NULL":
        return None
    if v.startswith("'") and v.endswith("'"):
        return _unescape_mysql(v[1:-1])
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _parse_insert(stmt):
    m = re.match(
        r"INSERT\s+(?:IGNORE\s+)?INTO\s+`?([A-Za-z0-9_]+)`?\s*\((?P<cols>.*?)\)\s*VALUES\s*(?P<vals>.*)",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None, [], []
    table = m.group(1)
    cols = [c.strip().strip("`") for c in _split_top_level(m.group("cols"))]
    rows = _extract_rows(m.group("vals"))
    return table, cols, rows


def parse_dump(text):
    tables = {}
    for stmt in _iter_inserts(text):
        table, cols, rows = _parse_insert(stmt)
        if not table or not cols:
            continue
        bucket = tables.setdefault(table.lower(), [])
        for row in rows:
            values = _split_top_level(row)
            if len(values) != len(cols):
                continue
            bucket.append(dict(zip(cols, [_parse_value(v) for v in values])))
    return tables


def _find_table(tables, suffix, prefix):
    key = f"{prefix}{suffix}".lower()
    if key in tables:
        return tables[key]
    for k, v in tables.items():
        if k.endswith(suffix):
            return v
    return []


def import_dump_users(users_rows, default_password):
    created = skipped = 0
    for r in users_rows:
        login = str(r.get("user_login") or "").strip()
        name = str(r.get("display_name") or "").strip() or login
        if not login:
            continue
        if Employee.query.filter_by(employee_id=login).first():
            skipped += 1
            continue
        db.session.add(
            Employee(
                employee_id=login,
                full_name=name,
                password_hash=generate_password_hash(default_password),
                must_change_password=True,
                is_active=True,
            )
        )
        created += 1
    db.session.commit()
    return f"Пользователей создано: {created}, пропущено: {skipped}"


def import_dump_quizzes(quizzes_rows, questions_rows):
    q_by_quiz = {}
    for q in questions_rows:
        try:
            if int(q.get("deleted") or 0):
                continue
        except Exception:
            pass
        try:
            quiz_id = int(q.get("quiz_id") or 0)
        except Exception:
            continue
        q_by_quiz.setdefault(quiz_id, []).append(q)

    created_quizzes = created_questions = skipped_quizzes = 0
    for qz in quizzes_rows:
        try:
            if int(qz.get("deleted") or 0):
                continue
        except Exception:
            pass
        try:
            quiz_id = int(qz.get("quiz_id") or 0)
        except Exception:
            continue
        quiz_name = str(qz.get("quiz_name") or "").strip()
        if not quiz_name:
            continue
        made, qcount = _create_test_and_questions(quiz_id, quiz_name, q_by_quiz.get(quiz_id, []))
        if not made:
            skipped_quizzes += 1
        else:
            created_quizzes += 1
        created_questions += qcount
    db.session.commit()
    return (
        f"Тестов: {created_quizzes}, пропущено: {skipped_quizzes}, "
        f"вопросов импортировано: {created_questions}"
    )


def import_dump_results(results_rows, users_rows):
    user_map = {}
    for u in users_rows:
        try:
            user_map[int(u.get("ID") or 0)] = str(u.get("user_login") or "").strip()
        except Exception:
            continue

    imported_ids = _get_imported_ids("wp_imported_results")
    counts = {"created": 0, "skipped": 0, "no_employee": 0, "no_test": 0}
    for r in results_rows:
        code = _import_result_row(r, user_map, imported_ids)
        if code in counts:
            counts[code] += 1
    _save_imported_ids("wp_imported_results", imported_ids)
    db.session.commit()
    return (
        f"Результатов: {counts['created']}, пропущено: {counts['skipped']}, "
        f"без теста: {counts['no_test']}, без сотрудника: {counts['no_employee']}"
    )


def import_dump_documents(posts_rows, category_id):
    import requests

    category = db.session.get(Category, category_id)
    if not category:
        return "Документы: категория не найдена"
    imported_ids = _get_imported_ids("wp_imported_documents")
    created = failed = skipped = 0
    materials_dir = os.path.join(UPLOAD_DIR, "materials")
    os.makedirs(materials_dir, exist_ok=True)

    for r in posts_rows:
        if str(r.get("post_type") or "").lower() != "attachment":
            continue
        if str(r.get("post_mime_type") or "").lower() != "application/pdf":
            continue
        try:
            att_id = int(r.get("ID") or 0)
        except Exception:
            att_id = 0
        if att_id and att_id in imported_ids:
            skipped += 1
            continue
        url = str(r.get("guid") or "").strip()
        title = str(r.get("post_title") or "").strip() or f"Документ {att_id}"
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
        if att_id:
            imported_ids.add(att_id)
        created += 1

    _save_imported_ids("wp_imported_documents", imported_ids)
    db.session.commit()
    return f"Документов: {created}, пропущено: {skipped}, ошибок загрузки: {failed}"


def import_dump(file_bytes, cfg, category_id, flags):
    text = None
    for enc in ("utf-8-sig", "utf-8", "latin1"):
        try:
            text = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return "Не удалось прочитать файл (неизвестная кодировка)"

    tables = parse_dump(text)
    prefix = cfg["wp_prefix"]
    parts = []
    if flags.get("users"):
        parts.append(import_dump_users(_find_table(tables, "users", prefix), cfg["wp_default_password"]))
    if flags.get("quizzes"):
        parts.append(import_dump_quizzes(
            _find_table(tables, "mlw_quizzes", prefix),
            _find_table(tables, "mlw_questions", prefix),
        ))
    if flags.get("results"):
        parts.append(import_dump_results(
            _find_table(tables, "mlw_results", prefix),
            _find_table(tables, "users", prefix),
        ))
    if flags.get("documents"):
        if not category_id:
            parts.append("Документы: категория не выбрана")
        else:
            parts.append(import_dump_documents(_find_table(tables, "posts", prefix), category_id))
    return "; ".join(p for p in parts if p) or "В дампе не найдено подходящих таблиц"
