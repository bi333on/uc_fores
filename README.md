# UC-Fores — сайт онлайн-обучения и тестирования

Сайт учебного центра: обучающие материалы + предварительное и итоговое тестирование
сотрудников по технике безопасности (аналог uc.foresltd.com).

## Возможности

- Категории курсов (CRUD в админке).
- Обучающие материалы: PDF-файлы и/или текст (CRUD в админке).
- Два вида тестирования: предварительное и итоговое.
- Конструктор тестов: вопросы с одним или несколькими правильными ответами,
  изображения в вопросах и вариантах, перемешивание вопросов.
- Настройки теста: проходной балл, лимит времени, лимит попыток.
- Вход сотрудников по табельному номеру + паролю (Flask-Login).
- Массовый импорт сотрудников из CSV/XLSX.
- Итоговое тестирование открывается только после пройденного предварительного.
- Журнал результатов с экспортом CSV и сертификат PDF (reportlab).
- Админка с 2FA (TOTP) и кнопкой «Обновить» (git pull + перезапуск).

## Стек

Flask + Flask-SQLAlchemy + Flask-Login, Gunicorn, Caddy, systemd, SQLite.

## Локальный запуск

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# или: source venv/bin/activate  # Linux
pip install -r requirements.txt
copy .env.example .env         # Windows
python app.py
```

Сайт: http://127.0.0.1:5000
Админка: http://127.0.0.1:5000/admin/ (по умолчанию admin / admin123 — см. `.env`).

## Переменные окружения (`.env`)

| Переменная | Описание | По умолчанию |
|---|---|---|
| `SECRET_KEY` | секрет Flask | генерируется |
| `DATABASE_URL` | строка подключения к БД | `sqlite:///uc_fores.db` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | доступ в админку | `admin` / `admin123` |
| `SITE_NAME` | название сайта | `Учебный центр` |
| `SITE_URL` | URL сайта | `https://uc-fores.example.com` |
| `DEFAULT_PASS_SCORE` | проходной балл по умолчанию | `80` |
| `MAX_UPLOAD_MB` | макс. размер загрузки | `32` |
| `EMPLOYEE_DEFAULT_PASSWORD` | пароль по умолчанию при импорте | `123456` |
| `UPDATE_BRANCH` | ветка для кнопки «Обновить» | `main` |
| `UPDATE_RESTART_CMD` | команда перезапуска (для кнопки «Обновить») | пусто |

## Кнопка «Обновить» в админке

Кнопка выполняет `git fetch` + `git reset --hard origin/<UPDATE_BRANCH>` +
`pip install -r requirements.txt` + команду `UPDATE_RESTART_CMD`.
На VPS задайте `UPDATE_RESTART_CMD=sudo -n systemctl restart uc-fores-site`
и добавьте в sudoers:

```
www-data ALL=(root) NOPASSWD: /usr/bin/systemctl restart uc-fores-site
```

Подробный деплой: см. [DEPLOY.md](DEPLOY.md).
