# 3DPMS

3DPMS is a Flask + SQLite application for managing a 3D printing side hustle, with a public
customer-facing site and a login-protected internal dashboard.

## Site structure
- `/` — public site: home, about, contact, order form, and order tracking. No login required.
- `/dash/...` — internal dashboard (quotes, invoices, clients, jobs, filament, settings). Requires login.

## First-time setup
There is no public sign-up page. Create the first dashboard account with:
```bash
# Docker
docker compose exec web flask create-admin

# Manual / venv
. .venv/bin/activate
export FLASK_APP=run.py
flask create-admin
```
This prompts for a username, email, and password. Additional users can be added later from
the dashboard under Settings → Users, or by running the command again.

## Deploy with Docker Compose
```bash
cp .env.example .env
# edit .env and set SECRET_KEY — generate one with:
#   python3 -c "import secrets; print(secrets.token_hex(32))"

docker compose up -d --build
docker compose exec web flask create-admin
```
The app is then available at `http://localhost:8000` (public site at `/`, dashboard at `/dash`).

By default this uses SQLite, with the database and all uploaded files (logos, signed quotes,
order attachments) stored under `./data/` on the host — back that folder up as a whole. To use
Postgres instead, uncomment the `db` service in `docker-compose.yml` and set `DATABASE_URL` in
`.env`; `psycopg2` is already installed either way.

Put this behind a reverse proxy (Caddy, Cloudflare Tunnel, nginx, etc.) for TLS — the container
itself only serves plain HTTP on port 8000.

## Run locally without Docker
```bash
cd /home/elijahlsl/Documents/Projects/WEBAPPS/3dpms
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python run.py
```

## Features
- Public order form with model upload / design-request flow, order tracking by job number
  (verified by email or phone)
- Optional Cloudflare Turnstile bot protection on login, the order form, contact form, and
  order tracking — configure under Settings → Security; forms work normally if left blank
- Business settings and branding, with a customer-facing About/Contact page
- Filament library with cost and charge pricing
- Quotes and invoices with PDF export, per-payment-method surcharges, optional hidden markup,
  quote versioning, and a secure signed-quote upload link sent by email
- Customizable HTML email templates for quotes and invoices
- Customer order-status email notifications, opt-in from the order form, quote upload page,
  or manually per-invoice
- Client tracking and a jobs board

## Test
```bash
. .venv/bin/activate
python -m pytest -q
```

