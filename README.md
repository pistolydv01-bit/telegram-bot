# Telegram Search Bot — Render Ready

## Files
- `main.py` — bot code
- `requirements.txt` — Python dependencies
- `.gitignore` — keeps `.env` out of Git
- `.env.example` — variable template (no real secrets)
- `render.yaml` — optional Render worker configuration

## Local setup
1. Copy `.env.example` to `.env`.
2. Put your real `BOT_TOKEN` and `API_TOKEN` in `.env`.
3. Run:
   `pip install -r requirements.txt`
4. Start:
   `python main.py`

## Render
Create a Background Worker from your GitHub repository.
Build command: `pip install -r requirements.txt`
Start command: `python main.py`

Add these Environment Variables in Render:
- `BOT_TOKEN`
- `API_TOKEN`

IMPORTANT: Never put real tokens in GitHub, `.gitignore`, or the ZIP.
