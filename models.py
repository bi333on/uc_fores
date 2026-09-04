from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _now():
    return datetime.now(timezone.utc)


class Employee(UserMixin, db.Model):
    __tablename__ = "employees"

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(255), nullable=False)
    last_name = db.Column(db.String(255), nullable=True)
    first_name = db.Column(db.String(255), nullable=True)
    role = db.Column(db.String(20), default="student", nullable=False)  # student | editor | admin
    permissions = db.Column(db.Text, default="[]")  # JSON-список разделов для редактора
    totp_secret = db.Column(db.String(64), nullable=True)
    totp_enabled = db.Column(db.Boolean, default=False)
    password_hash = db.Column(db.String(255), nullable=False)
    must_change_password = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_now)
    last_login = db.Column(db.DateTime, nullable=True)

    def get_id(self):
        return str(self.id)


class AdminUser(UserMixin, db.Model):
    __tablename__ = "admin_users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    totp_secret = db.Column(db.String(64), nullable=True)
    totp_enabled = db.Column(db.Boolean, default=False)

    def get_id(self):
        return str(self.id)


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.Text, default="")
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_now)

    materials = db.relationship("Material", backref="category", order_by="Material.sort_order")
    tests = db.relationship("Test", backref="category", order_by="Test.test_type")


class Material(db.Model):
    __tablename__ = "materials"

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, default="")
    file_path = db.Column(db.String(512), nullable=True)
    content = db.Column(db.Text, default="")
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_now)


class Test(db.Model):
    __tablename__ = "tests"

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, default="")
    test_type = db.Column(db.String(20), nullable=False)  # preliminary | final
    passing_score = db.Column(db.Integer, default=80)
    time_limit_minutes = db.Column(db.Integer, nullable=True)
    attempts_limit = db.Column(db.Integer, nullable=True)
    questions_per_page = db.Column(db.Integer, default=1)  # сколько вопросов на странице (0 = все)
    questions_limit = db.Column(db.Integer, nullable=True)  # сколько всего вопросов показывать в тесте (пусто = все)
    shuffle_questions = db.Column(db.Boolean, default=True)
    show_questions_nav = db.Column(db.Boolean, default=False)  # показывать блок «Страницы теста»
    is_active = db.Column(db.Boolean, default=True)
    external_quiz_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=_now)

    questions = db.relationship("Question", backref="test", order_by="Question.sort_order")


class Question(db.Model):
    __tablename__ = "questions"

    id = db.Column(db.Integer, primary_key=True)
    test_id = db.Column(db.Integer, db.ForeignKey("tests.id"), nullable=False)
    text = db.Column(db.Text, nullable=False)
    image_path = db.Column(db.String(512), nullable=True)
    question_type = db.Column(db.String(20), default="single")  # single | multiple
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)

    options = db.relationship(
        "AnswerOption", backref="question", order_by="AnswerOption.sort_order",
        cascade="all, delete-orphan",
    )


class AnswerOption(db.Model):
    __tablename__ = "answer_options"

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False)
    text = db.Column(db.Text, nullable=False)
    image_path = db.Column(db.String(512), nullable=True)
    is_correct = db.Column(db.Boolean, default=False)
    sort_order = db.Column(db.Integer, default=0)


class Attempt(db.Model):
    __tablename__ = "attempts"

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)
    test_id = db.Column(db.Integer, db.ForeignKey("tests.id"), nullable=False)
    started_at = db.Column(db.DateTime, default=_now)
    finished_at = db.Column(db.DateTime, nullable=True)
    score = db.Column(db.Integer, default=0)
    total = db.Column(db.Integer, default=0)
    percent = db.Column(db.Float, default=0.0)
    passed = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(20), default="in_progress")  # in_progress | finished
    answers_json = db.Column(db.Text, default="[]")

    employee = db.relationship("Employee", backref="attempts")
    test = db.relationship("Test", backref="attempts")


class Setting(db.Model):
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(128), unique=True, nullable=False)
    value = db.Column(db.Text, default="")
