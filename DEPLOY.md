# Деплой UC-Fores на VPS

Предполагается Ubuntu/Debian с уже установленным Caddy.

## 1. Код

```bash
mkdir -p /opt/uc-fores
git clone https://github.com/bi333on/uc_fores.git /opt/uc-fores
chown -R www-data:www-data /opt/uc-fores
```

## 2. Виртуальное окружение

```bash
cd /opt/uc-fores
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 3. Конфиг

```bash
cp .env.example .env
nano .env   # задать SECRET_KEY, ADMIN_PASSWORD, SITE_URL, UPDATE_RESTART_CMD и т.д.
```

## 4. systemd

```bash
cp uc-fores.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now uc-fores-site
```

Для кнопки «Обновить» в админке добавьте sudoers:

```bash
echo 'www-data ALL=(root) NOPASSWD: /usr/bin/systemctl restart uc-fores-site' \
  > /etc/sudoers.d/uc-fores-restart
chmod 440 /etc/sudoers.d/uc-fores-restart
```

## 5. Caddy

Поправьте домен в `Caddyfile` (по умолчанию `uc-fores.example.com`), затем:

```bash
cp Caddyfile /etc/caddy/Caddyfile   # или отдельный конфиг в /etc/caddy/conf.d/
systemctl reload caddy
```

## 6. Обновление

Либо вручную:

```bash
cd /opt/uc-fores && git pull origin main && systemctl restart uc-fores-site
```

Либо кнопкой «Обновить» в админке (см. README).
