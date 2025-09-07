import email, imaplib, os, re, sqlite3
from email.header import decode_header
from bs4 import BeautifulSoup
import tomli

GMAIL = os.environ["GMAIL_ADDRESS"]
APP_PW = os.environ["GMAIL_APP_PASSWORD"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT  = os.environ["TELEGRAM_CHAT_ID"]

with open("config.toml","rb") as f:
    CFG = tomli.load(f)

DB_PATH = "seen.sqlite3"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
    return conn

def already_seen(conn, uid):
    cur = conn.execute("SELECT 1 FROM seen WHERE id=?", (uid,))
    return cur.fetchone() is not None

def mark_seen(conn, uid):
    conn.execute("INSERT OR IGNORE INTO seen(id) VALUES (?)", (uid,))
    conn.commit()

def to_text(part):
    charset = part.get_content_charset() or "utf-8"
    try:
        return part.get_payload(decode=True).decode(charset, errors="ignore")
    except Exception:
        raw = part.get_payload(decode=True)
        return raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else (raw or "")

def parse_price(text):
    m = re.search(r"\$?\s?([0-9]{3,}[,0-9]*)", text)
    if not m: return None
    val = int(re.sub(r"[^\d]","", m.group(1)))
    return val

def parse_acres(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:ac|acre|acres)\b", text, re.I)
    return float(m.group(1)) if m else None

def extract_url(text):
    m = re.search(r"https?://\S+", text)
    return m.group(0).rstrip(').,;') if m else None

def clean_text(html_or_plain):
    low = html_or_plain.lower()
    if "<html" in low or "<body" in low:
        soup = BeautifulSoup(html_or_plain, "lxml")
        return soup.get_text("\n", strip=True)
    return html_or_plain

def score_listing(text, price, acres, cfg):
    s = 0.0
    if price and price <= cfg["filters"]["max_price"]:
        s += cfg["scoring"]["price_weight"]
    if acres and acres >= cfg["filters"]["min_acres"]:
        s += cfg["scoring"]["acre_weight"]
    low = text.lower()
    if any(k in low for k in cfg["keywords"]["water"]):
        s += cfg["scoring"]["water_weight"]
    if any(k in low for k in cfg["keywords"]["farm"]):
        s += cfg["scoring"]["farm_weight"]
    if any(k.lower() in low for k in cfg["keywords"]["proximity"]):
        s += cfg["scoring"]["proximity_weight"]
    if any(x.lower() in low for x in [w for w in cfg["filters"]["exclude_any"]]):
        s -= 3.0
    return s

def telegram(msg):
    import urllib.parse, urllib.request
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg, "disable_web_page_preview": True}).encode()
    urllib.request.urlopen(url, data=data).read()

def run():
    conn = db()
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL, APP_PW)
    label = CFG["mail"]["gmail_label"]
    M.select(f'"{label}"')  # quotes support spaces
    typ, data = M.search(None, 'UNSEEN')
    ids = data[0].split()
    for i in ids:
        typ, msg_data = M.fetch(i, '(RFC822)')
        if typ != 'OK': continue
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        message_id = msg.get("Message-ID") or f"UID-{i.decode()}"
        if already_seen(conn, message_id):
            continue

        subject, enc = decode_header(msg.get("Subject",""))[0]
        if isinstance(subject, bytes): subject = subject.decode(enc or "utf-8", errors="ignore")

        parts = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("text/plain", "text/html"):
                    parts.append(to_text(part))
        else:
            parts.append(to_text(msg))

        body_text = clean_text("\n\n".join(parts))
        body_text = re.sub(r'\n{3,}', '\n\n', body_text)
        url = extract_url(body_text) or extract_url(subject) or "N/A"
        price = parse_price(subject) or parse_price(body_text)
        acres = parse_acres(subject) or parse_acres(body_text)
        full_text = f"{subject}\n\n{body_text}\n\n{url}"

        sc = score_listing(full_text, price, acres, CFG)

        if sc >= 3.5:
            p = f"${price:,}" if price else "Unknown price"
            a = f"{acres} acres" if acres else "acres n/a"
            note = f"🏡 Candidate: {p}, {a}\n{subject}\n{url}"
            try:
                telegram(note)
            except Exception as e:
                print("Telegram error:", e)

        mark_seen(conn, message_id)

    try:
        M.close()
    except: pass
    M.logout()

if __name__ == "__main__":
    run()
