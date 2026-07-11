# 3DPMS

3DPMS is a Flask + SQLite starter application for managing a 3D printing side hustle.

## Features
- Business settings and branding
- Filament library with cost and charge pricing
- Quote creation with PDF export and email placeholders
- Invoice creation with PDF export and email placeholders
- Client tracking and a simple jobs board

## Run locally
```bash
cd /home/elijahlsl/Documents/Projects/WEBAPPS/3dpms
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python run.py
```

## Test
```bash
. .venv/bin/activate
python -m pytest -q
```
