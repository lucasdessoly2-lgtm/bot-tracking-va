"""
Bot Telegram — Tracking des VA Instagram + GetMySocial
------------------------------------------------------
Envoie 4 rapports par jour dans le canal Telegram configuré :
    - 00h00 FR : Rapport CLICS (jour J-1 complet via GetMySocial)
    - 09h30 FR : Rapport INSTAGRAM MATIN (vérif post 07h30)
    - 12h00 FR : Rapport CLICS (depuis 00h00 du jour via GetMySocial)
    - 20h00 FR : Rapport INSTAGRAM SOIR (vérif post 16h30)

Variables d'environnement requises (Railway) :
    - TELEGRAM_TOKEN     : token du bot Telegram (BotFather)
    - TELEGRAM_CHAT_ID   : ID du canal (commence par -100...)
    - RAPIDAPI_KEY       : clé API RapidAPI (Instagram Scraper 2025)
    - GMS_API_KEY        : clé API GetMySocial (format gms_live_*)

Variables optionnelles :
    - RAPIDAPI_HOST      : host RapidAPI Instagram Scraper
    - GMS_HOST           : host API GetMySocial (par défaut api.getmysocial.com)
"""

import logging
import os
import re
from datetime import datetime, time, timedelta
from typing import Optional

import pytz
import requests
from apscheduler.schedulers.blocking import BlockingScheduler

from accounts import ACCOUNTS

# =====================================================================
#  CONFIGURATION
# =====================================================================

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
RAPIDAPI_KEY = os.environ["RAPIDAPI_KEY"]
GMS_API_KEY = os.environ.get("GMS_API_KEY")

# RapidAPI Instagram
RAPIDAPI_HOST = os.environ.get("RAPIDAPI_HOST", "instagram-scraper-20251.p.rapidapi.com")

# GetMySocial
GMS_HOST = os.environ.get("GMS_HOST", "api.getmysocial.com")
GMS_BASE_URL = f"https://{GMS_HOST}"

# Timezone
PARIS_TZ = pytz.timezone("Europe/Paris")

# Créneaux de post Instagram attendus
MATIN_TARGET = time(7, 30)
SOIR_TARGET = time(16, 30)
WINDOW_MINUTES = 30

# Cache mémoire pour le mapping shortcode GMS -> link_id
_GMS_LINKS_CACHE: dict = {}
_GMS_CACHE_LAST_REFRESH: Optional[datetime] = None
_GMS_CACHE_TTL_HOURS = 6  # rafraîchit le cache toutes les 6h

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
        items = (
            data.get("data", {}).get("items")
            or data.get("items")
            or data.get("reels")
            or []
        )
        return items
    except Exception as e:
        log.error("Fetch Insta error %s: %s", username, e)
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
    """Cherche un post du jour autour de target_time_paris ± WINDOW_MINUTES."""
    today_paris = datetime.now(PARIS_TZ).date()
    target_dt = PARIS_TZ.localize(datetime.combine(today_paris, target_time_paris))

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
#  GETMYSOCIAL (clics + top pays)
# =====================================================================

def username_to_gms_shortcode(username: str) -> str:
    """
    Convertit un username Instagram en shortcode GMS probable.
    Règle : minuscules, pas de points ni caractères spéciaux.
    Ex : 'Laura.sensoryx' -> 'laurasensoryx'
    """
    return re.sub(r"[^a-z0-9_-]", "", username.lower())


def gms_request(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """Appel HTTP GET vers l'API GMS avec auth Bearer. Renvoie le JSON ou None."""
    if not GMS_API_KEY:
        return None
    url = f"{GMS_BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {GMS_API_KEY}",
        "Accept": "application/json",
    }
    try:
        r = requests.get(url, headers=headers, params=params or {}, timeout=30)
        if not r.ok:
            log.warning("GMS %s -> %s %s", path, r.status_code, r.text[:200])
            return None
        return r.json()
    except Exception as e:
        log.error("GMS exception on %s: %s", path, e)
        return None


def load_gms_links_map() -> dict:
    """
    Récupère la liste de tous les liens GMS et construit un mapping :
        { shortcode_lowercase : link_id }
    Pagination cursor. Cache TTL = 6h.
    """
    global _GMS_LINKS_CACHE, _GMS_CACHE_LAST_REFRESH

    now = datetime.now(PARIS_TZ)
    if (
        _GMS_LINKS_CACHE
        and _GMS_CACHE_LAST_REFRESH
        and (now - _GMS_CACHE_LAST_REFRESH).total_seconds() < _GMS_CACHE_TTL_HOURS * 3600
    ):
        return _GMS_LINKS_CACHE

    mapping: dict = {}
    cursor = None
    page = 0
    while True:
        page += 1
        params = {"limit": 100, "sort": "-created"}
        if cursor:
            params["cursor"] = cursor
        data = gms_request("/v3/links", params=params)
        if not data:
            break
        for item in data.get("data", []):
            sc = (item.get("shortcode") or "").lower()
            link_id = item.get("id")
            if sc and link_id:
                mapping[sc] = link_id
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
        if page > 20:  # garde-fou
            log.warning("GMS pagination > 20 pages, stop")
            break

    _GMS_LINKS_CACHE = mapping
    _GMS_CACHE_LAST_REFRESH = now
    log.info("GMS links map refreshed: %d entries", len(mapping))
    return mapping


def find_gms_link_id(username: str, links_map: dict) -> Optional[str]:
    """Cherche le link_id GMS pour un username Insta, avec plusieurs variantes."""
    candidates = [
        username_to_gms_shortcode(username),   # laurasensoryx
        username.lower(),                      # laura.sensoryx
        username.lower().replace(".", "-"),    # laura-sensoryx
        username.lower().replace(".", "_"),    # laura_sensoryx
    ]
    for c in candidates:
        if c in links_map:
            return links_map[c]
    return None


def gms_date_range(period: str) -> tuple:
    """
    Renvoie (start_iso, end_iso) pour la période voulue.
    'today'     -> aujourd'hui 00h00 à maintenant (rapport midi)
    'yesterday' -> hier 00h00 à hier 23h59 (rapport minuit)
    Format ISO 8601 en UTC.
    """
    now_paris = datetime.now(PARIS_TZ)
    if period == "yesterday":
        d = (now_paris - timedelta(days=1)).date()
        start = PARIS_TZ.localize(datetime.combine(d, time(0, 0)))
        end = PARIS_TZ.localize(datetime.combine(d, time(23, 59, 59)))
    else:  # today
        d = now_paris.date()
        start = PARIS_TZ.localize(datetime.combine(d, time(0, 0)))
        end = now_paris
    return start.astimezone(pytz.UTC).isoformat(), end.astimezone(pytz.UTC).isoformat()


def fetch_gms_clicks_and_countries(link_id: str, period: str) -> tuple:
    """
    Renvoie (total_clicks, top3_countries) pour un lien GMS sur la période voulue.
    top3_countries = [(country_code, pct), ...] max 3 éléments.
    Si erreur API, renvoie (None, []).
    """
    start_iso, end_iso = gms_date_range(period)

    # Essai plusieurs formats de params (la doc ne détaille pas exactement)
    common_params_variants = [
        {"link_id": link_id, "start_date": start_iso, "end_date": end_iso},
        {"link_id": link_id, "from": start_iso, "to": end_iso},
        {"link_id": link_id, "start": start_iso, "end": end_iso},
    ]

    total_clicks: Optional[int] = None
    countries: list = []

    # 1) Overview pour les clics totaux
    for params in common_params_variants:
        overview = gms_request("/v3/analytics/overview", params=params)
        if overview:
            total_clicks = (
                overview.get("clicks")
                or overview.get("total_clicks")
                or overview.get("visits")
                or (overview.get("data") or {}).get("clicks")
            )
            if total_clicks is not None:
                break

    # 2) Breakdown country pour top 3 pays
    for params in common_params_variants:
        breakdown = gms_request("/v3/analytics/breakdowns/country", params=params)
        if breakdown:
            rows = breakdown.get("data") or breakdown.get("rows") or []
            if rows:
                # Récupération des pays + valeurs (champs probables)
                parsed = []
                for r in rows:
                    code = (
                        r.get("country_code")
                        or r.get("code")
                        or r.get("country")
                        or r.get("key")
                        or "??"
                    )
                    val = (
                        r.get("clicks")
                        or r.get("visits")
                        or r.get("count")
                        or r.get("value")
                        or 0
                    )
                    parsed.append((code, val))
                # Tri descendant et calcul des %
                parsed.sort(key=lambda x: x[1], reverse=True)
                total = sum(v for _, v in parsed) or 1
                countries = [(c, round(v * 100 / total)) for c, v in parsed[:3]]
                break

    return total_clicks, countries


# =====================================================================
#  RAPPORTS INSTAGRAM (matin / soir)
# =====================================================================

def generate_insta_report(target_time_paris: time, label: str) -> str:
    """Construit le rapport Insta pour le créneau matin ou soir."""
    now_paris = datetime.now(PARIS_TZ)

    va_groups: dict = {}
    for username, va_name in ACCOUNTS:
        va_groups.setdefault(va_name, []).append(username)

    lines = []
    date_str = now_paris.strftime("%A %d %B %Y %H:%M")
    lines.append(f"📊 <b>RAPPORT {label}</b> — {date_str}")
    lines.append("")

    total_ok = total_out = total_missing = total_accounts = 0

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


# =====================================================================
#  RAPPORTS CLICS (00h et 12h)
# =====================================================================

def generate_clicks_report(period: str, label: str, header_emoji: str) -> str:
    """
    Génère le rapport CLICS avec top 3 pays par compte.
    period = 'today' (depuis 00h) ou 'yesterday' (jour complet J-1)
    """
    now_paris = datetime.now(PARIS_TZ)

    va_groups: dict = {}
    for username, va_name in ACCOUNTS:
        va_groups.setdefault(va_name, []).append(username)

    lines = []
    date_str = now_paris.strftime("%A %d %B %Y %H:%M")
    lines.append(f"{header_emoji} <b>RAPPORT {label}</b> — {date_str}")
    lines.append("")

    if not GMS_API_KEY:
        lines.append("⚠️ <i>GMS_API_KEY non configurée — clics indisponibles</i>")
        return "\n".join(lines)

    links_map = load_gms_links_map()
    if not links_map:
        lines.append("⚠️ <i>Impossible de récupérer la liste des liens GMS</i>")
        return "\n".join(lines)

    total_clicks_global = 0

    for va_name, usernames in va_groups.items():
        va_lines = []
        for username in usernames:
            link_id = find_gms_link_id(username, links_map)
            if not link_id:
                va_lines.append(
                    f"  ❓ <code>{username}</code> — Lien GMS introuvable"
                )
                continue

            clicks, countries = fetch_gms_clicks_and_countries(link_id, period)
            if clicks is None:
                va_lines.append(
                    f"  ⚠️ <code>{username}</code> — Stats indisponibles"
                )
                continue

            total_clicks_global += clicks
            countries_str = " ".join(f"{c} ({p}%)" for c, p in countries) if countries else "—"
            va_lines.append(
                f"  🔗 <code>{username}</code> — {format_number(clicks)} clics\n"
                f"     🌍 {countries_str}"
            )

        lines.append(f"👤 <b>{va_name}</b> ({len(usernames)} comptes)")
        lines.extend(va_lines)
        lines.append("")

    lines.append(f"📈 <b>TOTAL : {format_number(total_clicks_global)} clics</b>")

    return "\n".join(lines)


# =====================================================================
#  JOBS PROGRAMMÉS
# =====================================================================

def job_insta_matin() -> None:
    log.info("Running INSTA MATIN job")
    send_telegram(generate_insta_report(MATIN_TARGET, "INSTA MATIN"))


def job_insta_soir() -> None:
    log.info("Running INSTA SOIR job")
    send_telegram(generate_insta_report(SOIR_TARGET, "INSTA SOIR"))


def job_clics_minuit() -> None:
    log.info("Running CLICS MINUIT job (jour J-1)")
    send_telegram(generate_clicks_report("yesterday", "CLICS — JOUR COMPLET", "🌙"))


def job_clics_midi() -> None:
    log.info("Running CLICS MIDI job (depuis 00h)")
    send_telegram(generate_clicks_report("today", "CLICS — MI-JOURNÉE", "☀️"))


# =====================================================================
#  STARTUP / MAIN
# =====================================================================

def send_startup_message() -> None:
    nb_comptes = len(ACCOUNTS)
    nb_va = len({va for _, va in ACCOUNTS})
    gms_status = "✅ activé" if GMS_API_KEY else "⚠️ désactivé (GMS_API_KEY manquante)"
    msg = (
        "🟢 <b>Bot démarré</b>\n"
        f"📊 {nb_comptes} comptes surveillés\n"
        f"👥 {nb_va} VA\n"
        f"🔗 GetMySocial : {gms_status}\n"
        "⏰ Rapports automatiques :\n"
        "   🌙 00h00 — Clics jour complet\n"
        "   🌅 09h30 — Insta matin\n"
        "   ☀️ 12h00 — Clics mi-journée\n"
        "   🌆 20h00 — Insta soir"
    )
    send_telegram(msg)


def main() -> None:
    log.info("Starting bot — %d comptes surveillés", len(ACCOUNTS))
    send_startup_message()

    scheduler = BlockingScheduler(timezone=PARIS_TZ)

    # ===== JOBS PROGRAMMÉS =====
    # Pour modifier les horaires, change hour= et minute= ci-dessous.
    scheduler.add_job(job_clics_minuit, "cron", hour=0,  minute=0)
    scheduler.add_job(job_insta_matin,  "cron", hour=9,  minute=30)
    scheduler.add_job(job_clics_midi,   "cron", hour=12, minute=0)
    scheduler.add_job(job_insta_soir,   "cron", hour=20, minute=0)
    # ===========================

    log.info("Scheduler started — waiting for jobs")
    scheduler.start()


if __name__ == "__main__":
    main()
