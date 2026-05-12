"""
Bot Telegram — Tracking des VA Instagram
-----------------------------------------
Vérifie automatiquement 2 fois par jour (09h30 et 20h00 FR) si les comptes
Instagram listés dans accounts.py ont bien posté un Reel aux horaires prévus
(07h30 et 16h30 FR), et envoie un rapport groupé dans un canal Telegram.

Variables d'environnement requises (configurées dans Railway) :
    - TELEGRAM_TOKEN     : token du bot Telegram (BotFather)
    - TELEGRAM_CHAT_ID   : ID du canal (commence par -100...)
    - RAPIDAPI_KEY       : clé d'API RapidAPI (Instagram Scraper 2025)
"""

import logging
import os
from datetime import datetime, time
from typing import Optional

import pytz
import requests
from apscheduler.schedulers.blocking import BlockingScheduler

from accounts import ACCOUNTS

# =====================================================================
#  CONFIGURATION
# =====================================================================

# --- Variables d'environnement (mises dans Railway) ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
RAPIDAPI_KEY = os.environ["RAPIDAPI_KEY"]

# --- API RapidAPI ---
# Le HOST se récupère dans "Code Snippets" sur la page de l'endpoint User Reels.
# Si ton API a un host différent, modifie cette ligne.
RAPIDAPI_HOST = os.environ.get("RAPIDAPI_HOST", "instagram-scraper-20251.p.rapidapi.com")

# --- Fuseau horaire ---
PARIS_TZ = pytz.timezone("Europe/Paris")

# --- Créneaux de post attendus (heure FR) ---
MATIN_TARGET = time(7, 30)   # post matin attendu à 07h30
SOIR_TARGET = time(16, 30)   # post soir attendu à 16h30
WINDOW_MINUTES = 30          # tolérance ±30 min autour de la cible

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bot")


# =====================================================================
#  TELEGRAM
# =====================================================================

def send_telegram(text: str) -> None:
    """Envoie un message dans le canal Telegram configuré."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if not r.ok:
            log.error("Telegram error %s: %s", r.status_code, r.text)
    except Exception as e:
        log.error("Telegram exception: %s", e)


# =====================================================================
#  INSTAGRAM (via RapidAPI)
# =====================================================================

def fetch_recent_reels(username: str) -> list:
    """Récupère les derniers Reels d'un compte Instagram."""
    url = f"https://{RAPIDAPI_HOST}/userreels"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }
    try:
        r = requests.get(
            url,
            params={"username_or_id": username},
            headers=headers,
            timeout=30,
        )
        if not r.ok:
            log.warning("API %s -> %s %s", username, r.status_code, r.text[:200])
            return []
        data = r.json()
        # Plusieurs formats possibles selon l'API : on essaie les plus courants
        items = (
            data.get("data", {}).get("items")
            or data.get("items")
            or data.get("reels")
            or []
        )
        return items
    except Exception as e:
        log.error("Fetch error %s: %s", username, e)
        return []


def parse_reel_stats(reel: dict) -> tuple:
    """Extrait timestamp, vues, likes, commentaires d'un Reel."""
    taken_at = (
        reel.get("taken_at")
        or reel.get("date")
        or reel.get("created_time")
        or reel.get("timestamp")
    )
    views = (
        reel.get("play_count")
        or reel.get("video_view_count")
        or reel.get("views")
        or reel.get("view_count")
        or 0
    )
    likes = reel.get("like_count") or reel.get("likes") or 0
    comments = reel.get("comment_count") or reel.get("comments") or 0
    return taken_at, views, likes, comments


def format_number(n) -> str:
    """Formate un nombre : 12400 -> 12.4k, 1200000 -> 1.2M."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def find_post_in_window(items: list, target_time_paris: time):
    """
    Cherche un post du jour autour de target_time_paris ± WINDOW_MINUTES.
    Renvoie (status, post_dt, item) avec status ∈ {in_window, out_of_window, no_post}.
    """
    today_paris = datetime.now(PARIS_TZ).date()
    target_dt = PARIS_TZ.localize(
        datetime.combine(today_paris, target_time_paris)
    )

    out_of_window: Optional[tuple] = None

    for item in items:
        taken_at, _, _, _ = parse_reel_stats(item)
        if not taken_at:
            continue
        try:
            ts = int(taken_at)
            post_dt = datetime.fromtimestamp(ts, tz=pytz.UTC).astimezone(PARIS_TZ)
        except (ValueError, TypeError):
            continue
        if post_dt.date() != today_paris:
            continue
        delta_min = abs((post_dt - target_dt).total_seconds()) / 60
        if delta_min <= WINDOW_MINUTES:
            return "in_window", post_dt, item
        if out_of_window is None:
            out_of_window = (post_dt, item)

    if out_of_window:
        return "out_of_window", out_of_window[0], out_of_window[1]
    return "no_post", None, None


# =====================================================================
#  RAPPORTS
# =====================================================================

def generate_report(target_time_paris: time, label: str) -> str:
    """Construit le rapport Telegram pour le créneau matin ou soir."""
    now_paris = datetime.now(PARIS_TZ)

    # Regroupement par VA en conservant l'ordre du fichier accounts.py
    va_groups: dict = {}
    for username, va_name in ACCOUNTS:
        va_groups.setdefault(va_name, []).append(username)

    lines = []
    date_str = now_paris.strftime("%A %d %B %Y %H:%M")
    lines.append(f"📊 <b>RAPPORT {label}</b> — {date_str}")
    lines.append("")

    total_ok = 0
    total_out = 0
    total_missing = 0
    total_accounts = 0

    for va_name, usernames in va_groups.items():
        va_ok = va_out = va_missing = 0
        va_lines = []
        for username in usernames:
            total_accounts += 1
            reels = fetch_recent_reels(username)
            status, post_dt, item = find_post_in_window(reels, target_time_paris)

            if status == "in_window":
                va_ok += 1
                total_ok += 1
                _, views, likes, comments = parse_reel_stats(item)
                hhmm = post_dt.strftime("%Hh%M")
                va_lines.append(
                    f"  ✅ <code>{username}</code> — Posté {hhmm}\n"
                    f"     👁 {format_number(views)} vues · "
                    f"❤️ {format_number(likes)} · "
                    f"💬 {format_number(comments)}"
                )
            elif status == "out_of_window":
                va_out += 1
                total_out += 1
                _, views, likes, comments = parse_reel_stats(item)
                hhmm = post_dt.strftime("%Hh%M")
                va_lines.append(
                    f"  ⚠️ <code>{username}</code> — Hors créneau ({hhmm})\n"
                    f"     👁 {format_number(views)} vues · "
                    f"❤️ {format_number(likes)} · "
                    f"💬 {format_number(comments)}"
                )
            else:
                va_missing += 1
                total_missing += 1
                va_lines.append(
                    f"  ❌ <code>{username}</code> — Pas de post {label.lower()}"
                )

        lines.append(
            f"👤 <b>{va_name}</b> ({len(usernames)} comptes) "
            f"→ {va_ok}✅ / {va_out}⚠️ / {va_missing}❌"
        )
        lines.extend(va_lines)
        lines.append("")

    lines.append(
        f"📈 <b>TOTAL : {total_ok}✅ / {total_out}⚠️ / {total_missing}❌</b> "
        f"sur {total_accounts} comptes"
    )

    return "\n".join(lines)


def job_matin() -> None:
    log.info("Running morning job")
    msg = generate_report(MATIN_TARGET, "MATIN")
    send_telegram(msg)


def job_soir() -> None:
    log.info("Running evening job")
    msg = generate_report(SOIR_TARGET, "SOIR")
    send_telegram(msg)


def send_startup_message() -> None:
    """Message envoyé au démarrage du bot pour confirmer qu'il tourne."""
    nb_comptes = len(ACCOUNTS)
    nb_va = len({va for _, va in ACCOUNTS})
    msg = (
        "🟢 <b>Bot démarré</b>\n"
        f"📊 {nb_comptes} comptes surveillés\n"
        f"👥 {nb_va} VA\n"
        "⏰ Rapports automatiques : 09h30 et 20h00 FR"
    )
    send_telegram(msg)


# =====================================================================
#  POINT D'ENTRÉE
# =====================================================================

def main() -> None:
    log.info("Starting bot — %d comptes surveillés", len(ACCOUNTS))
    send_startup_message()

    scheduler = BlockingScheduler(timezone=PARIS_TZ)

    # ===== JOBS PROGRAMMÉS =====
    # Pour changer les horaires de vérification, modifie hour= et minute= ici.
    scheduler.add_job(job_matin, "cron", hour=9, minute=30)
    scheduler.add_job(job_soir, "cron", hour=20, minute=0)
    # ===========================

    log.info("Scheduler started — waiting for jobs")
    scheduler.start()


if __name__ == "__main__":
    main()
