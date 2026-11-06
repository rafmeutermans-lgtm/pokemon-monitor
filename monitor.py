#!/usr/bin/env python3
"""Volgt productpagina's en stuurt een melding (ntfy-push en/of mail) bij NIEUWE producten.

Gebruik:
    python monitor.py            # normale run (bedoeld voor GitHub Actions)
    python monitor.py --list     # toon gevonden producten per site, zonder state/melding
    python monitor.py --test     # stuur een testmelding

Instellingen via omgevingsvariabelen (in GitHub: Settings > Secrets):
    NTFY_TOPIC                   verplicht voor push (kies een lange, willekeurige naam)
    NTFY_SERVER                  optioneel, standaard https://ntfy.sh
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_TO   optioneel, voor mail
"""
import json
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# CONFIG: voeg hier sites toe. Per site:
#   name        korte naam (ook de sleutel in state.json, dus niet hernoemen na de start)
#   url         de pagina met de productlijst (categorie/zoekpagina)
#   link_regex  regex waaraan een productlink moet voldoen (na weglaten van ?query)
#   keywords    optioneel: alleen links waarvan tekst of URL een van deze woorden bevat
# --------------------------------------------------------------------------
SITES = [
    {
        "name": "Panini BE",
        "url": "https://www.paninibelgium.com/shp_bel_nl/panini-stickers/jeugd/harry-potter.html",
        "link_regex": r"-be01\.html$",
    },
]

STATE_FILE = Path(__file__).with_name("state.json")
USER_AGENT = "Mozilla/5.0 (compatible; PersonalProductMonitor/1.0; hobby, low frequency)"
TIMEOUT = 30
MAX_LISTED = 5  # max. aantal producten in één melding

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.5"})
_robots_cache = {}


def robots_allowed(url):
    """Respecteer robots.txt. Bij een onbereikbare robots.txt slaan we deze run over."""
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    if origin not in _robots_cache:
        rp = robotparser.RobotFileParser()
        r = session.get(f"{origin}/robots.txt", timeout=TIMEOUT)
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
        elif r.status_code in (404, 410):
            rp.parse([])  # geen robots.txt = alles toegestaan
        else:
            raise RuntimeError(f"robots.txt gaf status {r.status_code}")
        _robots_cache[origin] = rp
    return _robots_cache[origin].can_fetch(USER_AGENT, url)


def fetch_products(site):
    """Geef {productlink: titel} terug voor één site."""
    url = site["url"]
    if not robots_allowed(url):
        raise RuntimeError("robots.txt verbiedt deze URL voor geautomatiseerde toegang")
    resp = session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    pattern = re.compile(site["link_regex"])
    keywords = [k.lower() for k in site.get("keywords", [])]

    found = {}
    for a in soup.find_all("a", href=True):
        link = urljoin(url, a["href"]).split("#")[0].split("?")[0]
        if not pattern.search(link):
            continue
        title = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
        if keywords and not any(k in (title + " " + link).lower() for k in keywords):
            continue
        if len(title) > len(found.get(link, "")):
            found[link] = title
        else:
            found.setdefault(link, title)
    return found


def send_ntfy(title, body, click):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title, "Priority": "high", "Tags": "rotating_light"}  # headers: ASCII houden
    if click:
        headers["Click"] = click
    r = requests.post(f"{server}/{topic}", data=body.encode("utf-8"), headers=headers, timeout=15)
    r.raise_for_status()
    return True


def send_mail(title, body):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("MAIL_TO")
    if not (host and to):
        return False
    user, pwd = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = title, user or to, to
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as s:
        s.starttls()
        if user and pwd:
            s.login(user, pwd)
        s.send_message(msg)
    return True


def notify(title, body, click=None):
    """True als minstens één kanaal gelukt is."""
    ok = False
    for fn, args in ((send_ntfy, (title, body, click)), (send_mail, (title, body))):
        try:
            ok = fn(*args) or ok
        except Exception as e:  # een kapot kanaal mag het andere niet blokkeren
            print(f"  ! {fn.__name__} mislukt: {e}")
    if not ok:
        print("  ! geen enkel meldingskanaal gelukt of ingesteld (NTFY_TOPIC / SMTP_*)")
    return ok


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def main():
    if "--test" in sys.argv:
        sys.exit(0 if notify("Test Pokemon-monitor", "Als je dit ziet, werkt de melding.") else 1)

    list_only = "--list" in sys.argv
    state = {} if list_only else load_state()
    failures = 0

    for i, site in enumerate(SITES):
        if i:
            time.sleep(2)
        name = site["name"]
        try:
            products = fetch_products(site)
        except Exception as e:
            print(f"[{name}] FOUT: {e}")
            failures += 1
            continue
        print(f"[{name}] {len(products)} producten gevonden")

        if list_only:
            for link, title in products.items():
                print(f"  - {title or '(geen titel)'}  {link}")
            continue

        if name not in state:  # eerste run: alleen basislijn vastleggen, niet melden
            state[name] = sorted(products)
            print("  basislijn vastgelegd, geen melding")
            continue

        seen = set(state[name])
        new = {l: t for l, t in products.items() if l not in seen}
        if not new:
            continue

        lines = [f"- {t or 'Nieuw product'}\n  {l}" for l, t in list(new.items())[:MAX_LISTED]]
        if len(new) > MAX_LISTED:
            lines.append(f"... en nog {len(new) - MAX_LISTED}")
        first_link = next(iter(new))
        print(f"  {len(new)} NIEUW")
        if notify(f"Nieuw product bij {name}", "\n".join(lines), click=first_link):
            state[name] = sorted(seen | set(products))  # pas bij gelukte melding bijwerken
        else:
            failures += 1  # state blijft ongewijzigd, volgende run probeert opnieuw

    if not list_only:
        save_state(state)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
