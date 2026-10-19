#!/usr/bin/env python3
"""
Beobachtet die LeasingMarkt-Seite für den Range Rover Evoque und mailt bei neuen Inseraten.

- Ein Inserat = eine Zahl (ID) am Ende der Inserat-URL.
- Erster Lauf: speichert den Ausgangsbestand und schickt eine Startmail (kein Alarm für Altbestand).
- Danach: Mail nur für IDs, die noch nie gesehen wurden.
- Zustand liegt in seen.json (wird vom Workflow ins Repo zurückgeschrieben).

Alle Zugangsdaten kommen aus Umgebungsvariablen (GitHub Secrets). Im Code steht nichts Persönliches.
"""
import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.leasingmarkt.de"
URL = os.environ.get("WATCH_URL", f"{BASE}/leasing-land_rover-range_rover_evoque")
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen.json"))
FAIL_ALERT_AFTER = int(os.environ.get("FAIL_ALERT_AFTER", "6"))  # Warnmail nach N Fehlern in Folge

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "")  # mehrere Adressen mit Komma trennen
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")  # Push aufs Handy über ntfy.sh (geheimes Topic, nur als Secret!)
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

ID_RE = re.compile(r"/leasing/pkw/land_rover-range_rover_evoque/(\d+)")
SKIP_LINES = {"vergleichen", "anpassbar"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("watcher")


# ------------------------------------------------------------------ Zustand
def load_state() -> dict:
    default = {"initialized": False, "seen": {}, "fails": 0,
               "fail_alert_sent": False, "zero_alert_sent": False}
    if STATE_FILE.exists():
        try:
            default.update(json.loads(STATE_FILE.read_text()))
        except Exception:
            log.warning("seen.json defekt, starte mit leerem Zustand.")
    return default


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))


# ------------------------------------------------------------------ Abruf & Parsing
def fetch_html() -> str:
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def card_details(anchor, listing_id: str) -> str:
    """Steigt im HTML nach oben, bis die Karte eines Inserats komplett ist (ohne andere Inserate zu erfassen)."""
    best = anchor
    node = anchor
    for _ in range(8):
        parent = node.parent
        if parent is None or parent.name in ("body", "html"):
            break
        ids = {m.group(1) for a in parent.find_all("a", href=True) if (m := ID_RE.search(a["href"]))}
        if ids - {listing_id}:
            break
        node = best = parent
    lines: list[str] = []
    for line in best.get_text("\n", strip=True).split("\n"):
        line = " ".join(line.split())
        if line and line.lower() not in SKIP_LINES and line not in lines:
            lines.append(line)
    return " | ".join(lines)[:600]


def parse(html: str) -> dict[str, dict]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict] = {}
    anchors: dict[str, object] = {}
    for a in soup.find_all("a", href=True):
        m = ID_RE.search(a["href"])
        if not m:
            continue
        lid = m.group(1)
        title = " ".join(a.get_text(" ", strip=True).split())
        if lid not in found:
            found[lid] = {"id": lid, "title": title,
                          "url": f"{BASE}/leasing/pkw/land_rover-range_rover_evoque/{lid}"}
            anchors[lid] = a
        elif title and not found[lid]["title"]:
            found[lid]["title"] = title
            anchors[lid] = a
    for lid, item in found.items():
        item["title"] = item["title"] or "Range Rover Evoque"
        item["details"] = card_details(anchors[lid], lid)
    return found


# ------------------------------------------------------------------ Benachrichtigung
def email_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASS and MAIL_TO)


def _send_email(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, SMTP_USER, MAIL_TO
    msg.set_content(body)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def _send_push(title: str, body: str, click: str, priority: int) -> None:
    # JSON-Veröffentlichung an die Server-Wurzel: das Topic steht im Body, nicht in der URL (taucht also in keiner Fehlermeldung auf)
    payload = {"topic": NTFY_TOPIC, "title": title, "message": body[:3500], "priority": priority}
    if click:
        payload["click"] = click
    r = requests.post(NTFY_SERVER, json=payload, timeout=15)
    r.raise_for_status()


def notify(subject: str, body: str, click: str = "", priority: int = 3, push_body: str | None = None) -> None:
    """Sendet über alle konfigurierten Kanäle (ntfy und/oder E-Mail). Klappt mindestens einer, gilt es als zugestellt."""
    channels = []
    if NTFY_TOPIC:
        channels.append(("ntfy", lambda: _send_push(subject, push_body or body, click, priority)))
    if email_configured():
        channels.append(("E-Mail", lambda: _send_email(subject, body)))
    if not channels:
        raise RuntimeError("Kein Kanal konfiguriert: Secret NTFY_TOPIC oder SMTP_USER/SMTP_PASS/MAIL_TO setzen.")
    delivered = False
    for name, send in channels:
        try:
            send()
            delivered = True
            log.info("Benachrichtigung über %s gesendet: %s", name, subject)
        except Exception as e:
            log.error("Versand über %s fehlgeschlagen: %s", name, type(e).__name__)
    if not delivered:
        raise RuntimeError("Alle Benachrichtigungskanäle sind fehlgeschlagen.")


def format_items(items: list[dict]) -> str:
    return "\n\n".join(f"{i['title']}\n{i['details']}\n{i['url']}" for i in items)


def format_items_short(items: list[dict]) -> str:
    return "\n\n".join(f"{i['title']}\n{i['details'][:200]}\n{i['url']}" for i in items)


# ------------------------------------------------------------------ Hauptlogik
def run_once() -> None:
    state = load_state()

    try:
        items = parse(fetch_html())
    except Exception as e:
        state["fails"] += 1
        log.error("Abruf fehlgeschlagen (%d in Folge): %s", state["fails"], e)
        if state["fails"] >= FAIL_ALERT_AFTER and not state["fail_alert_sent"]:
            notify("⚠️ Evoque-Watcher: Seite nicht abrufbar",
                       f"{state['fails']} Fehlversuche in Folge.\nLetzter Fehler: {e}\nURL: {URL}")
            state["fail_alert_sent"] = True
        save_state(state)
        return
    state["fails"], state["fail_alert_sent"] = 0, False

    if not items:
        log.warning("Keine Inserate gefunden.")
        if not state["zero_alert_sent"]:
            notify("⚠️ Evoque-Watcher: 0 Inserate gefunden",
                       "Die Seite wurde abgerufen, aber es wurden keine Inserate erkannt. Entweder gibt es "
                       "wirklich keine, oder die Seite hat ihre Struktur geändert bzw. blockiert den Abruf.\n"
                       f"URL: {URL}")
            state["zero_alert_sent"] = True
        save_state(state)
        return
    state["zero_alert_sent"] = False

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Erster Lauf: Ausgangsbestand speichern, Startmail schicken
    if not state["initialized"]:
        listing = list(items.values())
        notify(f"✅ Evoque-Watcher gestartet ({len(items)} Inserate als Ausgangsbestand)",
               "Ab jetzt kommt eine Nachricht, sobald ein neues Inserat auftaucht.\n\n"
               f"Aktueller Bestand ({len(items)}):\n\n{format_items(listing)}\n\nQuelle: {URL}",
               click=URL,
               push_body=f"Ab jetzt kommt eine Nachricht bei neuen Inseraten. Aktuell {len(items)} Inserate im Bestand.\n"
                         + "\n".join(f"- {i['title']}" for i in listing))
        state["seen"] = {lid: {"first_seen": now, "title": it["title"]} for lid, it in items.items()}
        state["initialized"] = True
        save_state(state)
        return

    new_items = [it for lid, it in items.items() if lid not in state["seen"]]
    log.info("%d Inserate gefunden, %d neu.", len(items), len(new_items))
    if new_items:
        subject = f"🚗 {len(new_items)} neues Evoque-Leasingangebot auf LeasingMarkt"
        body = format_items(new_items) + f"\n\nAlle Angebote: {URL}"
        notify(subject, body, click=new_items[0]["url"], priority=4, push_body=format_items_short(new_items))
        # schlägt notify() fehl, wird NICHT als gesehen markiert -> nächster Lauf versucht es erneut
        for it in new_items:
            state["seen"][it["id"]] = {"first_seen": now, "title": it["title"]}
    save_state(state)


if __name__ == "__main__":
    run_once()
    sys.exit(0)
