ALPHA SNIPER - VPS PRODUCTION NOTES

Admin panel:
http://YOUR-VPS-IP:8000/admin.html

User panel:
http://YOUR-VPS-IP:8000/spot-momentum-scanner.html

Admin password:
ZPXIPQ549I4J

Persistent VPS data:
/root/alpha-sniper-data/

This folder stores PINs, sessions, user slider settings, auto-load preferences, admin password, and site content.
Do not delete /root/alpha-sniper-data/ when updating the app ZIP.

Ubuntu service command:
uvicorn app.main:app --host 0.0.0.0 --port 8000

Health check:
http://YOUR-VPS-IP:8000/health

Quick restart:
systemctl restart alpha-sniper

Logs:
journalctl -u alpha-sniper -f

Admin ZIP update:
Open admin.html, login, upload a full platform ZIP in Deployment Update, wait 10-20 seconds, then refresh.
The app validates the ZIP, creates a backup, preserves data, deploys files, and restarts.

Rollback:
Open admin.html, login, click Rollback Last Update, wait 10-20 seconds, then refresh.
