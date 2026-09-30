# Faculty Recruitment Tracker
1. Push this repo to GitHub. Settings > Pages > Deploy from branch `main`, folder `/docs`.
2. Settings > Actions > General > Workflow permissions > Read and write.
3. Edit `scraper/institutes.json`: set each `url` to the institute's actual faculty-recruitment page, then `"verified": true`.
4. Run the "Scrape faculty ads" workflow manually once (first run builds the baseline and sends no email).
5. Optional email: add repo secrets SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO.
Local run: `pip install -r requirements.txt && python scraper/scraper.py`
