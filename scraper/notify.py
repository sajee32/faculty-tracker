"""Email new ads. Needs repo secrets: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO."""
import json, os, smtplib
from email.message import EmailMessage
from pathlib import Path

d = json.loads((Path(__file__).resolve().parent.parent / "docs/data/changes.json").read_text())
new = [c for c in d["changes"] if c["type"] == "new"]
if d["bootstrap"] or not new or not os.environ.get("SMTP_HOST"):
    print("No email sent."); raise SystemExit
body = "\n\n".join(f"Institute: {c['institution']}\nPosition: {c['position'] or 'See advertisement'}\n"
                   f"Deadline: {c['application_deadline'] or 'Not detected'}\nDrive: {c['special_drive'] or '-'}\n"
                   f"Advertisement Link:\n{c['pdf_url'] or c['recruitment_url']}" for c in new)
m = EmailMessage(); m["Subject"] = f"New Faculty Recruitment Detected ({len(new)})"
m["From"] = os.environ["SMTP_USER"]; m["To"] = os.environ["MAIL_TO"]; m.set_content(body)
with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT") or 587)) as s:
    s.starttls(); s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(m)
print("Email sent for", len(new), "ads")
