# Smart Trading — organized Flask project

A responsive market-research dashboard with a teal-and-gold visual identity, Arabic/English interface, sample price charts, watchlist, movers, heatmap, and Flask-backed accounts/preferences.

## Important market-data note

Without a configured data provider, prices/charts are illustrative sample data, not live quotes. The optional `TWELVE_DATA_API_KEY` can be added in Render Environment Variables if you have a suitable provider account and permissions. Do not share the key publicly.

## Project structure

- `app.py` — Flask application
- `templates/index.html` — main HTML template
- `static/style.css` — styles
- `static/app.js` — browser logic
- `requirements.txt` — Python dependencies
- `render.yaml` — Render Blueprint configured for Free tier, without a paid disk

## Deploy on Render Free

1. Extract this ZIP on your computer.
2. Upload the **contents of the `smart-trading` folder** to the root of your GitHub repository, preserving the `templates` and `static` folders. Do not upload only the files inside those folders to the repository root.
3. In Render, create a Blueprint from the repository, or create a Web Service with Python, build command `pip install -r requirements.txt`, and start command `gunicorn app:app --workers 1 --bind 0.0.0.0:$PORT`. Choose **Free** and review the price before applying.
4. Set a new secret in Render as `FLASK_SECRET_KEY` (at least 32 random characters). The included Blueprint generates one automatically. Set `COOKIE_SECURE=1` when served over HTTPS.
5. After deployment, open `/health`; it should return JSON with `"status":"ok"`.

## Free-tier limitation

This deployment uses `/tmp/smart_trading.db`, which is ephemeral. SQLite account and watchlist data can be lost when the service restarts, redeploys, or sleeps. Do not use this configuration for important or production user data. The service may sleep while idle on the Free tier.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.
