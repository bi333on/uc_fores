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
        r"итоговое\s+тестирование|предварительный\s+тест|итоговый\s+тест|"
        r"предворительный\s+тест|предворительное\s+тестирование)",
        " ",
        name or "",
    )
    n = re.sub(r"\([^)]*\)", " ", n)  # убрать скобки: "(Предворительный тест)"
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


def _truthy(v):
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    return s not in ("", "0", "0.0", "false", "null", "none")


def _parse_question(row):
    text = (row.get("question_name") or "").strip()
    if not text:
        return None
    qtype = _qtype(row.get("question_type_new"), row.get("question_type"))
    if qtype is None:
        return None

    raw = php_unserialize(row.get("answer_array"))
    answers = []
    correct = set()
    if raw is not None:
        items = _to_list(raw)
        nested = bool(items) and isinstance(items[0], (list, dict))
        if nested:
            # Вложенный формат QSM: каждый ответ = [текст, правильный?, баллы]
            for it in items:
                parts = _to_list(it)
                if not parts:
                    continue
                ans_text = str(parts[0]).strip()
                if not ans_text:
                    continue
                answers.append(ans_text)
                flag = parts[1] if len(parts) > 1 else 0
                if _truthy(flag):
                    correct.add(ans_text)
        else:
            answers = [str(a).strip() for a in items if str(a).strip()]

    if not answers:
        answers = []
        for i in range(1, 7):
            a = (row.get(f"answer_{i}") or "").strip()
            if a:
                answers.append(a)
    if not answers:
        return None

    if not correct:
        info = _to_list(php_unserialize(row.get("question_answer_info")))
        correct = {str(c).strip() for c in info if str(c).strip()}
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


def _ensure_test(quiz_id, quiz_name):
    """Создаёт категорию и тест при необходимости. Возвращает (тест, создан ли)."""
    existing = Test.query.filter_by(external_quiz_id=quiz_id).first()
    if existing:
        return existing, False
    cat_title = _clean_quiz_name(quiz_name)
    slug = re.sub(r"[^a-z0-9\-_]+", "-", cat_title.lower()).strip("-") or "course"
    category = Category.query.filter_by(title=cat_title).first()
    if not category:
        base = slug
        i = 2
        while Category.query.filter_by(slug=slug).first():
            slug = f"{base}-{i}"
            i += 1
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
    return test, True


def _add_question(test, qrow):
    """Добавляет вопрос и варианты к тесту. Возвращает кол-во созданных вопросов (0/1)."""
    qdata = _parse_question(qrow)
    if not qdata:
        return 0
    order = Question.query.filter_by(test_id=test.id).count() + 1
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
    return 1


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

    new_quiz_ids = set()
    skipped_quizzes = 0
    for qz in quizzes:
        quiz_id = int(qz.get("quiz_id") or 0)
        quiz_name = (qz.get("quiz_name") or "").strip()
        if not quiz_name:
            continue
        test, created = _ensure_test(quiz_id, quiz_name)
        if created:
            new_quiz_ids.add(quiz_id)
        else:
            skipped_quizzes += 1

    created_questions = 0
    for q in all_questions:
        try:
            quiz_id = int(q.get("quiz_id") or 0)
        except Exception:
            continue
        if quiz_id not in new_quiz_ids:
            continue
        test = Test.query.filter_by(external_quiz_id=quiz_id).first()
        if test:
            created_questions += _add_question(test, q)

    db.session.commit()
    return (
        f"Тестов: {len(new_quiz_ids)}, пропущено: {skipped_quizzes}, "
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
    pattern = re.compile(r"INSERT\s+(?:IGNORE\s+)?INTO", re.IGNORECASE)
    while True:
        m = pattern.search(text, pos)
        if not m:
            break
        start = m.start()
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


def _parse_insert(stmt, schema=None):
    m = re.match(
        r"INSERT\s+(?:IGNORE\s+)?INTO\s+`?([A-Za-z0-9_]+)`?\s*\((?P<cols>.*?)\)\s*VALUES\s*(?P<vals>.*)",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        table = m.group(1)
        cols = [c.strip().strip("`") for c in _split_top_level(m.group("cols"))]
        rows = _extract_rows(m.group("vals"))
        return table, cols, rows
    # mysqldump: INSERT INTO `table` VALUES (...) — без списка колонок
    m = re.match(
        r"INSERT\s+(?:IGNORE\s+)?INTO\s+`?([A-Za-z0-9_]+)`?\s*VALUES\s*(?P<vals>.*)",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        table = m.group(1)
        cols = list((schema or {}).get(table.lower()) or [])
        rows = _extract_rows(m.group("vals"))
        return table, cols, rows
    return None, [], []


def _parse_create_table(stmt):
    m = re.search(r"CREATE\s+TABLE\s+`?([A-Za-z0-9_]+)`?\s*\(", stmt, re.IGNORECASE)
    if not m:
        return None, []
    table = m.group(1)
    cols = re.findall(r"(?m)^\s*`([^`]+)`\s", stmt)
    return table, cols


def _iter_create_tables(text):
    pos = 0
    pattern = re.compile(r"CREATE\s+TABLE\s+`?([A-Za-z0-9_]+)`?", re.IGNORECASE)
    while True:
        m = pattern.search(text, pos)
        if not m:
            break
        start = m.start()
        end = _find_stmt_end(text, start)
        yield text[start:end]
        pos = end + 1


def _build_schema(text):
    """Строит карту таблица -> список колонок из CREATE TABLE."""
    schema = {}
    for stmt in _iter_create_tables(text):
        table, cols = _parse_create_table(stmt)
        if table and cols:
            schema[table.lower()] = cols
    return schema


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


def _iter_table_rows(text, table_suffix, schema=None):
    """Потоковый генератор строк таблицы, имя которой оканчивается на table_suffix."""
    for stmt in _iter_inserts(text):
        table, cols, rows = _parse_insert(stmt, schema)
        if not table or not cols:
            continue
        if not table.lower().endswith(table_suffix):
            continue
        for row in rows:
            values = _split_top_level(row)
            if len(values) != len(cols):
                continue
            yield dict(zip(cols, [_parse_value(v) for v in values]))


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


def _import_quizzes_stream(text, schema=None):
    """Потоковый импорт тестов и вопросов из текста дампа. Возвращает (тестов, пропущено, вопросов)."""
    new_quiz_ids = set()
    skipped = 0
    for qz in _iter_table_rows(text, "mlw_quizzes", schema):
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
        test, created = _ensure_test(quiz_id, quiz_name)
        if created:
            new_quiz_ids.add(quiz_id)
        else:
            skipped += 1
    db.session.commit()

    created_questions = 0
    for q in _iter_table_rows(text, "mlw_questions", schema):
        try:
            if int(q.get("deleted") or 0):
                continue
        except Exception:
            pass
        try:
            quiz_id = int(q.get("quiz_id") or 0)
        except Exception:
            continue
        if quiz_id not in new_quiz_ids:
            continue
        test = Test.query.filter_by(external_quiz_id=quiz_id).first()
        if test:
            created_questions += _add_question(test, q)
    db.session.commit()
    return len(new_quiz_ids), skipped, created_questions


def import_dump_quizzes(quizzes_rows, questions_rows):
    """Совместимый вариант (для внешних вызовов с готовыми списками строк)."""
    new_quiz_ids = set()
    skipped = 0
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
        test, created = _ensure_test(quiz_id, quiz_name)
        if created:
            new_quiz_ids.add(quiz_id)
        else:
            skipped += 1
    db.session.commit()

    created_questions = 0
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
        if quiz_id not in new_quiz_ids:
            continue
        test = Test.query.filter_by(external_quiz_id=quiz_id).first()
        if test:
            created_questions += _add_question(test, q)
    db.session.commit()
    return (
        f"Тестов: {len(new_quiz_ids)}, пропущено: {skipped}, "
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
        try:
            if int(r.get("deleted") or 0):
                continue
        except Exception:
            pass
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

    schema = _build_schema(text)
    parts = []
    if flags.get("users"):
        parts.append(import_dump_users(_iter_table_rows(text, "users", schema), cfg["wp_default_password"]))
    if flags.get("quizzes"):
        created_qz, skipped_qz, created_q = _import_quizzes_stream(text, schema)
        parts.append(f"Тестов: {created_qz}, пропущено: {skipped_qz}, вопросов импортировано: {created_q}")
    if flags.get("results"):
        parts.append(import_dump_results(
            _iter_table_rows(text, "mlw_results", schema),
            _iter_table_rows(text, "users", schema),
        ))
    if flags.get("documents"):
        if not category_id:
            parts.append("Документы: категория не выбрана")
        else:
            parts.append(import_dump_documents(_iter_table_rows(text, "posts", schema), category_id))
    return "; ".join(p for p in parts if p) or "В дампе не найдено подходящих таблиц"
