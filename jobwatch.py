#!/usr/bin/env python3
"""
jobwatch.py - Telegram alerts for new jobs on tunisiefreelance.tn, with a match score.

Every run:
  1. reads the public job list (https://tunisiefreelance.tn/en/jobs)
  2. finds the jobs it has not seen before (remembered in seen.json)
  3. opens each new job, scores how well it matches your profile (0-100)
  4. sends you one Telegram message per job

Score:
  - with ANTHROPIC_API_KEY set: Claude reads the job and your profile and gives
    a score plus a one-line reason (costs a fraction of a cent per job)
  - without it: a free keyword score (the keyword lists are below)

Settings (environment variables):
  TELEGRAM_TOKEN      bot token from @BotFather              (required)
  TELEGRAM_CHAT_ID    your chat id (run: python jobwatch.py --chat-id)
  ANTHROPIC_API_KEY   optional, for the Claude score
  CLAUDE_MODEL        optional, default claude-haiku-5-5
  MIN_SCORE           optional, only send jobs scoring at least this (default 0)

Usage:
  python jobwatch.py --chat-id        show your chat id (send your bot a message first)
  python jobwatch.py --test           send a test message
  python jobwatch.py --once           check once (what GitHub Actions runs)
  python jobwatch.py --every 900      check every 15 minutes, forever
  python jobwatch.py --once --dry-run print messages instead of sending them

The first run only remembers the jobs already online (no flood of messages).
Use --notify-existing 5 to also receive the 5 most recent ones.
"""

import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

__version__ = "1.0"

SITE = "https://tunisiefreelance.tn"
LIST_URL = SITE + "/en/jobs"
STATE_FILE = Path(os.environ.get("JOBWATCH_STATE", Path(__file__).with_name("seen.json")))
USER_AGENT = "jobwatch/1.0 (personal job alerts; checks every 15 min)"
REQUEST_PAUSE_S = 1.5          # pause between page requests, to stay polite
MAX_REMEMBERED = 3000

# --------------------------------------------------------------------------
# Your profile - edit freely. Used by the Claude score.
# --------------------------------------------------------------------------
PROFILE = """
AI engineer (SUP'COM, Tunisia, AI major), graduated 2026. Languages: Arabic, French, English.
Strong: Python, PyTorch, LLM apps, RAG, AI agents (LangChain, LangGraph), chatbots,
computer vision (YOLO, OpenCV, vision-language models), deep learning, data pipelines,
scientific data (ERA5, NetCDF), automation and scraping, APIs.
Good: full-stack web apps with React, Node.js / NestJS / Express, PostgreSQL, MongoDB,
dashboards, Streamlit, Docker, AWS, Raspberry Pi / embedded inference.
Built: a school management platform (roles, attendance, payments, messaging),
an agriculture web app with dashboard and real-time alerts, an LLM assistant for
architecture firms, an AI emulator for a physics simulation (CESBIO).
Not a fit: graphic design, logos, video editing, social media / marketing, copywriting,
translation, data entry, accounting, native iOS/Android-only apps.
"""

# --------------------------------------------------------------------------
# Keyword score (used when there is no ANTHROPIC_API_KEY). Lower-case,
# matched as whole words. French, English and Arabic.
# --------------------------------------------------------------------------
STRONG_EXACT_CASE = ["AI", "IA"]  # +25, matched in capitals only (so French "j'ai" doesn't count)
STRONG = [  # +25 each
    "intelligence artificielle", "artificial intelligence", "machine learning",
    "apprentissage automatique", "deep learning", "llm", "llms", "gpt", "chatgpt", "openai",
    "rag", "langchain", "langgraph", "ai agent", "ai agents", "agent ia", "agents ia",
    "multi-agent", "chatbot", "chat bot", "nlp",
    "computer vision", "vision par ordinateur", "yolo", "opencv", "pytorch", "tensorflow",
    "data science", "python", "scraping", "automatisation", "automation",
    "ذكاء اصطناعي", "الذكاء الاصطناعي", "تعلم آلي", "بايثون", "روبوت محادثة", "شات بوت",
]
GOOD = [  # +12 each
    "react", "reactjs", "react.js", "node", "nodejs", "node.js", "express", "express.js",
    "nestjs", "javascript", "typescript", "mongodb", "postgresql", "sql", "api", "rest api",
    "full stack", "fullstack", "full-stack", "web app", "application web", "site web",
    "website", "dashboard", "tableau de bord", "backend", "back-end", "frontend", "front-end",
    "streamlit", "fastapi", "flask", "django", "docker", "aws", "raspberry pi", "iot",
    "notifications", "sms", "plateforme", "platform",
    "موقع ويب", "تطبيق ويب", "منصة", "برمجة", "تطوير",
]
BAD = [  # -30 each
    "logo", "graphic design", "design graphique", "graphiste", "identité visuelle",
    "vidéo", "video", "ugc", "montage", "tiktok", "instagram", "reels", "marketing",
    "marketeur", "community manager", "rédaction", "copywriting", "traduction", "translation",
    "saisie de données", "data entry", "comptabilité", "accounting", "voix off", "voice over",
    "photoshop", "illustrator", "canva",
    "شعار", "هوية بصرية", "تصميم جرافيك", "مونتاج", "فيديو", "تسويق", "ترجمة",
]


# ==========================================================================
# Reading the site
# ==========================================================================
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en,fr;q=0.8"})

# job links look like /en/jobs/<slug>-<6 hex>, e.g. /en/jobs/job-7c8140
JOB_LINK_RE = re.compile(
    r"""(?:https?://(?:www\.)?tunisiefreelance\.tn)?/en/jobs/([^"'?#\s/<>]+-[0-9a-f]{6})(?=["'?#\s/<>]|$)""",
    re.IGNORECASE,
)


STOP_RE = re.compile(
    r"^(similar jobs|other jobs|more jobs|related jobs|offres similaires|terms of service|"
    r"privacy policy|©|copyright|all rights reserved)", re.IGNORECASE)


def fetch(url):
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def job_ids_from_list(page_html):
    """Job slugs in page order, without duplicates."""
    seen, out = set(), []
    for m in JOB_LINK_RE.finditer(page_html):
        slug = m.group(1)
        if slug.lower() not in seen:
            seen.add(slug.lower())
            out.append(slug)
    return out


def _first(pattern, text, flags=re.IGNORECASE):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def parse_job(page_html, slug):
    """Best-effort extraction of the useful parts of a job page."""
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "aside"]):
        tag.decompose()

    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else None
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        title = (og.get("content") if og else "") or (soup.title.string if soup.title else "")
        title = re.sub(r"\s*\|\s*Tunisie Freelance\s*$", "", title or "").strip()
    title = title or slug

    # visible text, starting at the job title (skips the menus above it)
    if h1:
        parts = [s.strip() for s in h1.find_all_next(string=True) if s.strip()]  # starts with the h1 text
    else:
        parts = [s.strip() for s in soup.get_text("\n").splitlines() if s.strip()]
    # stop where the job ends (other jobs, site footer), so their words don't count
    for i, p in enumerate(parts):
        if i and STOP_RE.search(p):
            parts = parts[:i]
            break
    text = "\n".join(parts)
    text = text.replace("postJobWizard.budget.perHour", "/hr")
    body = text[:3500]

    budget = _first(r"((?:up to\s*)?\d[\d ., ]*(?:\s*-\s*\d[\d ., ]*)?\s*(?:د\.ت|TND|DT)(?:\s*/\s*hr)?)", body)
    job_type = _first(r"\b(Hourly|Fixed(?:[- ]price)?)\b", body)
    proposals = _first(r"Proposals?\s*:?\s*\n?\s*((?:less than\s*)?\d+(?:\s*(?:to|-)\s*\d+)?\+?)", body)
    if re.search(r"payment\s+(?:method\s+)?not\s+verified|unverified\s+payment", body, re.I):
        payment = False
    elif re.search(r"payment\s+(?:method\s+)?verified", body, re.I):
        payment = True
    else:
        payment = None
    hire_rate = _first(r"hire\s*rate\s*:?\s*\n?\s*(\d+\s*%)", body)
    spent = _first(r"total\s*spent\s*:?\s*\n?\s*([\d][\d\s.,]*\s*(?:TND|د\.ت|DT))", body)

    return {
        "slug": slug,
        "url": f"{LIST_URL}/{slug}",
        "title": title,
        "text": body,
        "budget": budget,
        "type": job_type,
        "proposals": proposals,
        "payment_verified": payment,
        "hire_rate": hire_rate,
        "spent": spent,
    }


# ==========================================================================
# Scoring
# ==========================================================================
def _has(word, text):
    # whole-word match that also works for Arabic and for words with dots/spaces
    return re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", text) is not None


def keyword_score(job):
    original = job["title"] + "\n" + job["text"]
    text = original.lower()
    plus = [w for w in STRONG_EXACT_CASE if _has(w, original)] + [w for w in STRONG if _has(w, text)]
    good = [w for w in GOOD if _has(w, text)]
    minus = [w for w in BAD if _has(w, text)]

    def dedupe(words):  # "node" and "node.js" count once
        return [w for w in words if not any(w != o and w.lower() in o.lower() for o in words)]
    plus, good, minus = dedupe(plus), dedupe(good), dedupe(minus)
    raw = 25 * len(plus) + 12 * len(good) - 30 * len(minus)
    score = max(0, min(100, raw))
    reason = ""
    if plus or good:
        reason += "matches: " + ", ".join((plus + good)[:8])
    if minus:
        reason += ("; " if reason else "") + "off-profile: " + ", ".join(minus[:4])
    return score, reason or "no profile keywords found"


def claude_score(job, api_key, model):
    prompt = (
        "Rate how well this freelance job fits the freelancer, from 0 (no fit) to 100 "
        "(perfect fit). Judge the skills needed, not the budget.\n\n"
        f"FREELANCER PROFILE:\n{PROFILE.strip()}\n\n"
        f"JOB TITLE: {job['title']}\n\nJOB PAGE TEXT:\n{job['text'][:3000]}\n\n"
        'Answer with JSON only: {"score": <int>, "reason": "<max 15 words, in English>"}'
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": model, "max_tokens": 150,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    out = "".join(b.get("text", "") for b in r.json().get("content", []))
    m = re.search(r"\{.*\}", out, re.S)
    data = json.loads(m.group(0)) if m else {}
    score = int(max(0, min(100, int(data["score"]))))
    return score, str(data.get("reason", "")).strip()


def score_job(job):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        try:
            s, why = claude_score(job, key, os.environ.get("CLAUDE_MODEL", "").strip() or "claude-haiku-5-5")
            return s, why, "Claude"
        except Exception as e:  # never lose an alert because scoring failed
            print(f"  Claude score failed ({e}); using keyword score", file=sys.stderr)
    s, why = keyword_score(job)
    return s, why, "keywords"


# ==========================================================================
# Telegram
# ==========================================================================
def tg(method, token, **params):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description', r.text[:200])}")
    return data["result"]


def format_message(job, score, reason, how):
    dot = "🟢" if score >= 70 else ("🟡" if score >= 40 else "🔴")
    e = html.escape
    lines = [f"{dot} <b>{score}/100</b>  {e(job['title'])}"]
    money = " · ".join(x for x in [job.get("budget"), job.get("type")] if x)
    if money:
        lines.append(f"💰 {e(money)}")
    if job.get("proposals"):
        lines.append(f"👥 Proposals: {e(job['proposals'])}")
    flags = []
    if job.get("payment_verified") is False:
        flags.append("payment not verified")
    if job.get("hire_rate") and job["hire_rate"].replace(" ", "") == "0%":
        flags.append("0% hire rate")
    if flags:
        lines.append("⚠️ " + e(", ".join(flags)))
    if reason:
        lines.append(f"🧠 {e(reason)} <i>({e(how)})</i>")
    lines.append(f'🔗 <a href="{e(job["url"])}">Open the job</a>')
    return "\n".join(lines)


def send(text, dry_run=False):
    if dry_run:
        print("----- message -----\n" + text + "\n")
        return
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        sys.exit("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID (see python jobwatch.py --help).")
    tg("sendMessage", token, chat_id=chat, text=text, parse_mode="HTML",
       disable_web_page_preview=True)


# ==========================================================================
# State
# ==========================================================================
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            print("  seen.json unreadable, starting fresh", file=sys.stderr)
    return {"seen": [], "initialised": False}


def save_state(state):
    state["seen"] = state["seen"][-MAX_REMEMBERED:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ==========================================================================
# One check
# ==========================================================================
def check_once(dry_run=False, notify_existing=0, min_score=0):
    state = load_state()
    seen = set(s.lower() for s in state["seen"])

    ids = job_ids_from_list(fetch(LIST_URL))
    if not ids:
        raise RuntimeError("no job links found on the list page - the site layout may have changed")

    new = [s for s in ids if s.lower() not in seen]
    if not state.get("initialised"):
        to_alert = new[:notify_existing]
        print(f"First run: remembering {len(new)} jobs already online"
              + (f", alerting the first {len(to_alert)}" if to_alert else ""))
    else:
        to_alert = new
    print(f"{len(ids)} jobs on the list, {len(new)} new, {len(to_alert)} to alert")

    sent = 0
    for slug in to_alert:
        time.sleep(REQUEST_PAUSE_S)
        try:
            job = parse_job(fetch(f"{LIST_URL}/{slug}"), slug)
        except Exception as e:
            print(f"  could not read {slug}: {e}; sending a bare alert", file=sys.stderr)
            job = {"slug": slug, "url": f"{LIST_URL}/{slug}", "title": slug, "text": ""}
        score, reason, how = score_job(job)
        print(f"  {score:3d}  {job['title'][:70]}")
        if score >= min_score:
            send(format_message(job, score, reason, how), dry_run=dry_run)
            sent += 1
        # remember it only once the alert went out (or was deliberately skipped)
        state["seen"].append(slug)
        if not dry_run:
            save_state(state)

    for slug in new:            # first run: remember the rest without alerting
        if slug not in state["seen"]:
            state["seen"].append(slug)
    state["initialised"] = True
    if not dry_run:
        save_state(state)
    print(f"Sent {sent} alert(s).")
    return sent


def main():
    ap = argparse.ArgumentParser(description="Telegram alerts for new tunisiefreelance.tn jobs.")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--every", type=int, metavar="SECONDS", help="check repeatedly (minimum 300)")
    ap.add_argument("--dry-run", action="store_true", help="print messages, send nothing, save nothing")
    ap.add_argument("--notify-existing", type=int, default=0, metavar="N",
                    help="on the first run, also alert the N most recent jobs")
    ap.add_argument("--chat-id", action="store_true", help="print the chat ids that messaged your bot")
    ap.add_argument("--test", action="store_true", help="send a test message")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args()

    min_score = int(os.environ.get("MIN_SCORE", "0").strip() or 0)

    if a.chat_id:
        token = os.environ.get("TELEGRAM_TOKEN", "").strip() or sys.exit("Set TELEGRAM_TOKEN first.")
        updates = tg("getUpdates", token)
        chats = {(u.get("message") or {}).get("chat", {}).get("id"):
                 (u.get("message") or {}).get("chat", {}).get("first_name", "")
                 for u in updates if u.get("message")}
        if not chats:
            print("No messages yet: open your bot in Telegram, send it 'hi', then run this again.")
        for cid, name in chats.items():
            print(f"chat id: {cid}  ({name})")
        return
    if a.test:
        send("✅ jobwatch is connected. You'll get new tunisiefreelance.tn jobs here.", dry_run=a.dry_run)
        print("Test message sent." if not a.dry_run else "")
        return
    if a.every:
        every = max(300, a.every)
        while True:
            try:
                check_once(a.dry_run, a.notify_existing, min_score)
            except Exception as e:
                print(f"check failed: {e}", file=sys.stderr)
            time.sleep(every)
    check_once(a.dry_run, a.notify_existing, min_score)


if __name__ == "__main__":
    main()
