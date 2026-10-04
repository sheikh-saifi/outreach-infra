"""Pre-flight checks for message content. A campaign can't be activated while any step has an error.

Errors are things carriers or mailbox providers block outright (public URL shorteners, missing
opt-out language, SHAFT content) or things that would send a broken message (unknown merge
fields). Warnings cost money or deliverability but don't block (extra SMS segments, spammy words).
"""

import math
import re

MERGE_FIELDS = {"first_name", "address"}
MERGE_RE = re.compile(r"\{([^{}]*)\}")
# Full URLs, plus bare "domain.tld/path" the way people type links in texts ("bit.ly/abc").
# The lookbehind keeps email addresses (name@gmail.com) from counting as links.
URL_RE = re.compile(r"https?://\S+|www\.\S+|(?<![@\w.-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\."
                    r"(?:com|net|org|ly|co|io|us|info|biz|me|gl|gd|cc|at|gy|link|site|xyz)(?:/\S*)?\b", re.I)

# Carriers filter public shorteners because spammers use them to hide destinations.
SHORTENERS = {"bit.ly", "tinyurl.com", "goo.gl", "t.co", "ow.ly", "is.gd", "buff.ly", "rebrand.ly",
              "cutt.ly", "shorturl.at", "rb.gy", "tiny.cc"}
# SHAFT-C: sex, hate, alcohol, firearms, tobacco, cannabis. Blocked on 10DLC without special approval.
SHAFT = re.compile(r"\b(sex|sexy|porn|beer|wine|liquor|vodka|whiskey|firearms?|guns?|ammo|tobacco|"
                   r"cigarettes?|vape|vaping|cannabis|marijuana|weed|cbd|thc)\b", re.I)
SPAMMY = re.compile(r"\b(act now|guaranteed|winner|risk[- ]free|click here|100% free|urgent|"
                    r"limited time|congratulations|no obligation|once in a lifetime)\b|\${2,}|!{2,}", re.I)

# GSM 03.38: texts using only these characters are 7-bit encoded (160 chars per segment).
GSM7_BASIC = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                 "ÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXTENDED = set("^{}\\[~]|€\f")  # count as two characters


def sms_segments(text: str) -> dict:
    """Encoding and billable segments. Carriers charge per segment, not per message."""
    non_gsm = sorted({c for c in text if c not in GSM7_BASIC and c not in GSM7_EXTENDED})
    if non_gsm:
        n = len(text.encode("utf-16-le")) // 2  # emoji take two UTF-16 units
        segments = 1 if n <= 70 else math.ceil(n / 67)
        return {"encoding": "UCS-2", "length": n, "segments": segments, "non_gsm_chars": non_gsm}
    n = sum(2 if c in GSM7_EXTENDED else 1 for c in text)
    segments = 1 if n <= 160 else math.ceil(n / 153)
    return {"encoding": "GSM-7", "length": n, "segments": segments, "non_gsm_chars": []}


def _issue(level: str, code: str, message: str) -> dict:
    return {"level": level, "code": code, "message": message}


def _common(text: str) -> list[dict]:
    issues = []
    unknown = sorted({f for f in MERGE_RE.findall(text) if f not in MERGE_FIELDS})
    if unknown:
        issues.append(_issue("error", "merge_field", f"Unknown merge field(s): {', '.join('{'+f+'}' for f in unknown)}. "
                                                      f"Available: {{first_name}}, {{address}}."))
    if m := SHAFT.search(text):
        issues.append(_issue("error", "shaft", f"'{m.group(0)}' is restricted (SHAFT-C) content and gets blocked."))
    for url in URL_RE.findall(text):
        host = re.sub(r"^(https?://)?(www\.)?", "", url.lower()).split("/")[0]
        if host in SHORTENERS:
            issues.append(_issue("error", "shortener", f"Public link shortener {host} is filtered by carriers and "
                                                      "spam filters. Use a full link on your own domain."))
    if m := SPAMMY.search(text):
        issues.append(_issue("warning", "spammy", f"'{m.group(0)}' reads as spam and hurts delivery."))
    letters = [c for c in text if c.isalpha()]
    if len(letters) > 20 and sum(c.isupper() for c in letters) / len(letters) > 0.3:
        issues.append(_issue("warning", "caps", "Lots of CAPITAL letters looks like spam."))
    return issues


def lint_sms(body: str, first_message: bool = False) -> dict:
    issues = _common(body)
    seg = sms_segments(body.replace("{first_name}", "Christopher").replace("{address}", "1234 Lakeview Blvd, Dallas, TX"))
    if not body.strip():
        issues.append(_issue("error", "empty", "Message is empty."))
    if first_message and not re.search(r"\bstop\b", body, re.I):
        issues.append(_issue("error", "opt_out", "The first text must tell people how to opt out, e.g. 'Reply STOP to opt out'."))
    if seg["encoding"] == "UCS-2":
        chars = " ".join(seg["non_gsm_chars"][:5])
        issues.append(_issue("warning", "encoding", f"Character(s) {chars} switch the text to UCS-2: 70 characters per segment "
                                                    "instead of 160. Curly quotes (’ “) are the usual culprit."))
    if seg["segments"] > 2:
        issues.append(_issue("warning", "length", f"{seg['segments']} segments per text (you pay per segment). Aim for 1-2."))
    if len(URL_RE.findall(body)) and first_message:
        issues.append(_issue("warning", "link_first", "Links in a first cold text are heavily filtered. Send links only after a reply."))
    return {"issues": issues, "segments": seg}


def lint_email(subject: str | None, body: str) -> dict:
    issues = _common(f"{subject or ''}\n{body}")
    if not (subject or "").strip():
        issues.append(_issue("error", "subject", "Subject is empty."))
    elif len(subject) > 60:
        issues.append(_issue("warning", "subject_length", "Subjects over 60 characters get cut off on phones."))
    if not body.strip():
        issues.append(_issue("error", "empty", "Body is empty."))
    words = len(body.split())
    if words > 150:
        issues.append(_issue("warning", "length", f"{words} words. Cold emails under ~125 words get more replies."))
    if len(URL_RE.findall(body)) > 1:
        issues.append(_issue("warning", "links", "More than one link in a cold email hurts inbox placement."))
    if re.search(r"<\s*(img|table|div|a)\b", body, re.I):
        issues.append(_issue("warning", "html", "Plain text performs better than HTML for cold email."))
    return {"issues": issues, "words": words}


def has_errors(result: dict) -> bool:
    return any(i["level"] == "error" for i in result["issues"])
