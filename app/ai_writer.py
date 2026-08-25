import os
import re
import time
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "aol.com", "hotmail.com", "outlook.com",
    "icloud.com", "msn.com", "live.com", "comcast.net", "verizon.net",
    "att.net", "sbcglobal.net", "me.com", "protonmail.com", "mail.com",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return str(value).strip()


def infer_website_from_email(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    domain = email.split("@")[-1].strip().lower()
    if not domain or domain in FREE_EMAIL_DOMAINS:
        return ""
    return f"https://{domain}"


def normalize_website_url(website: str | None) -> str:
    website = clean_text(website)
    if not website:
        return ""
    if website.startswith("http://") or website.startswith("https://"):
        return website
    return f"https://{website}"


# ---------------------------------------------------------------------------
# Improved scraper
# ---------------------------------------------------------------------------

def fetch_website_text(website: str | None, max_chars: int = 5000) -> tuple[str, str]:
    """
    Returns (website_text, final_url_used)
    Tries the given URL, then www. version, with retries.
    """
    website = normalize_website_url(website)
    if not website:
        return "", ""

    urls_to_try = [website]
    if "://" in website:
        scheme, rest = website.split("://", 1)
        if not rest.startswith("www."):
            urls_to_try.append(f"{scheme}://www.{rest}")
        else:
            # also try without www
            urls_to_try.append(f"{scheme}://{rest[4:]}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    for url in urls_to_try:
        for attempt in range(2):  # simple retry
            try:
                response = requests.get(
                    url,
                    timeout=12,
                    headers=headers,
                    allow_redirects=True,
                )

                if response.status_code >= 400:
                    print(f"Website fetch {response.status_code} for {url}")
                    break  # don't retry on 4xx/5xx

                soup = BeautifulSoup(response.text, "html.parser")

                # Remove noise
                for tag in soup(["script", "style", "noscript", "svg", "form", "nav", "footer", "header"]):
                    tag.decompose()

                title = soup.title.get_text(" ", strip=True) if soup.title else ""

                meta_description = ""
                meta = soup.find("meta", attrs={"name": re.compile(r"description", re.I)})
                if meta and meta.get("content"):
                    meta_description = meta["content"].strip()

                # Also try og:description
                if not meta_description:
                    og = soup.find("meta", property="og:description")
                    if og and og.get("content"):
                        meta_description = og["content"].strip()

                headings = []
                for h in soup.find_all(["h1", "h2", "h3"], limit=15):
                    t = h.get_text(" ", strip=True)
                    if t and len(t) > 3:
                        headings.append(t)

                # Main content – prefer <main> or <article> if present
                main = soup.find("main") or soup.find("article") or soup.body
                page_text = ""
                if main:
                    page_text = " ".join(main.get_text(separator=" ").split())

                combined = " ".join(
                    part for part in [
                        title,
                        meta_description,
                        " | ".join(headings[:10]),
                        page_text,
                    ] if part
                ).strip()

                if combined and len(combined) > 80:  # require some real content
                    print(f"✓ Scrape success: {url} ({len(combined)} chars)")
                    return combined[:max_chars], str(response.url)

            except requests.exceptions.Timeout:
                print(f"Timeout on {url} (attempt {attempt + 1})")
                time.sleep(0.8)
            except Exception as e:
                print(f"Scrape error for {url}: {repr(e)}")
                break

    print(f"✗ Scrape failed or empty for: {website}")
    return "", website


# ---------------------------------------------------------------------------
# Rule-based fallbacks
# ---------------------------------------------------------------------------

def build_fallback_intro(
    company: str = "",
    industry: str = "",
    role: str = "",
) -> str:
    company = clean_text(company)
    industry = clean_text(industry)

    if company and industry:
        return (
            f"I came across {company} while reviewing companies in the {industry.lower()} space "
            f"and thought this may be worth a quick conversation."
        )
    if company:
        return f"I came across {company} and thought this may be worth a quick conversation."
    return "I thought this may be worth a quick conversation."


def build_fallback_personal_line(company: str = "", industry: str = "") -> str:
    company = clean_text(company)
    industry = clean_text(industry)

    if company and industry:
        return f"I thought this may be a practical CRM conversation for {company}."
    if company:
        return f"I thought this may be a practical idea for {company}."
    return "I thought this may be a practical CRM conversation."


# ---------------------------------------------------------------------------
# Style guidance
# ---------------------------------------------------------------------------

def build_style_guidance(
    brand_voice: str | None = None,
    avoid_phrases: str | None = None,
    preferred_cta: str | None = None,
    signature_name: str | None = None,
    signature_title: str | None = None,
    signature_company: str | None = None,
    style_examples: list[dict] | None = None,
) -> str:
    style_examples = style_examples or []
    parts = []

    if brand_voice:
        parts.append(f"Preferred writing style:\n{brand_voice}")
    if avoid_phrases:
        parts.append(f"Avoid these words/phrases/style habits:\n{avoid_phrases}")
    if preferred_cta:
        parts.append(f"Preferred call to action:\n{preferred_cta}")

    signature_lines = [s for s in [signature_name, signature_title, signature_company] if s]
    if signature_lines:
        parts.append("Preferred signature:\n" + "\n".join(signature_lines))

    if style_examples:
        example_texts = []
        for i, ex in enumerate(style_examples[:5], 1):
            subj = clean_text(ex.get("subject"))
            body = clean_text(ex.get("body"))
            if subj or body:
                example_texts.append(f"Example {i}\nSubject: {subj}\nBody:\n{body}")
        if example_texts:
            parts.append(
                "Approved examples (match voice/length/warmth, do not copy):\n\n"
                + "\n\n---\n\n".join(example_texts)
            )

    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Template replacement
# ---------------------------------------------------------------------------

def replace_template_fields(
    template_subject: str,
    template_body: str,
    first_name: str,
    company: str,
    offer: str,
    audience: str,
    call_to_action: str,
    intro_para: str,
    personal_line: str,
    unsubscribe_url: str,
) -> dict:
    replacements = {
        "{{ first_name }}": first_name or "there",
        "{first name}": first_name or "there",
        "{{first_name}}": first_name or "there",
        "{{ company }}": company or "your company",
        "{company}": company or "your company",
        "{{company}}": company or "your company",
        "{{ offer }}": offer or "",
        "{offer}": offer or "",
        "{{offer}}": offer or "",
        "{{ audience }}": audience or "businesses",
        "{audience}": audience or "businesses",
        "{{audience}}": audience or "businesses",
        "{{ call_to_action }}": call_to_action or "Would you be open to a quick conversation?",
        "{call to action}": call_to_action or "Would you be open to a quick conversation?",
        "{{call_to_action}}": call_to_action or "Would you be open to a quick conversation?",
        "{{ intro_para }}": intro_para or "",
        "{intro para}": intro_para or "",
        "{{intro_para}}": intro_para or "",
        "{{ personal_line }}": personal_line or "",
        "{personal line}": personal_line or "",
        "{{personal_line}}": personal_line or "",
        "{{ unsubscribe_url }}": unsubscribe_url or "",
        "{unsubscribe url}": unsubscribe_url or "",
        "{{unsubscribe_url}}": unsubscribe_url or "",
    }

    subject = template_subject or "Quick question"
    body = template_body or ""

    for key, value in replacements.items():
        subject = subject.replace(key, value)
        body = body.replace(key, value)

    return {"subject": subject.strip(), "body": body.strip()}


def extract_section(text: str, section_name: str, next_section_name: str | None = None) -> str:
    if not text:
        return ""
    if next_section_name:
        pattern = rf"{section_name}:\s*(.*?)\s*{next_section_name}:"
    else:
        pattern = rf"{section_name}:\s*(.*)"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""


# ---------------------------------------------------------------------------
# AI generation – intro + personal line
# ---------------------------------------------------------------------------

def generate_personalized_pieces(
    client,
    first_name: str,
    company: str,
    industry: str,
    role: str,
    website: str,
    website_used: str,
    website_text: str,
    offer: str,
    audience: str,
    tone: str,
    fallback_intro: str,
    fallback_personal_line: str,
    brand_voice: str | None = None,
    avoid_phrases: str | None = None,
) -> tuple[str, str]:
    """
    Returns (intro_para, personal_line)
    """
    if not client or not website_text:
        print("No client or no website text → using fallbacks")
        return fallback_intro, fallback_personal_line

    prompt = f"""You are writing two short pieces for a business outreach email.

1. INTRO PARAGRAPH
- Exactly 1–2 sentences.
- Use ONE specific, real detail from the website text.
- Show relevance to the company without pitching CRM yet.
- Do NOT invent facts, awards, clients, locations, years, or services.
- Do NOT mention scraping, reviewing, or visiting the website.
- Do NOT start with a greeting.
- Do NOT use these generic phrases:
  "businesses like", "companies like", "I noticed your work",
  "I wanted to reach out because", "I hope this email finds you well".

Good pattern:
"I saw that [Company] focuses on [specific thing from site]. That kind of work usually needs clear visibility across prospects, customers, and next steps."

2. PERSONAL LINE
- One short sentence that can sit later in the email.
- Lightly references the company or the specific detail.
- Still not a hard pitch.

Contact:
First name: {first_name}
Company: {company}
Industry: {industry}
Role: {role}
Website used: {website_used or website}

Website text (use only facts that appear here):
{website_text[:4200]}

Campaign audience: {audience}
Offer (do NOT put this in the intro): {offer}
Tone: {tone}
Brand voice: {brand_voice or "Warm, direct, brief, consultative, practical."}
Avoid: {avoid_phrases or "Fake compliments, hype, overly formal language."}

Fallback intro if site is useless: {fallback_intro}
Fallback personal line: {fallback_personal_line}

Return EXACTLY in this format:

INTRO:
[the paragraph]

PERSONAL_LINE:
[the one sentence]
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",          # or "gemini-1.5-flash" if needed
            contents=prompt,
        )
        text = clean_text(response.text)

        intro = extract_section(text, "INTRO", "PERSONAL_LINE") or extract_section(text, "INTRO")
        personal = extract_section(text, "PERSONAL_LINE")

        # Clean common prefixes
        for prefix in ["INTRO:", "Intro:", "PERSONAL_LINE:", "Personal line:"]:
            intro = intro.replace(prefix, "").strip()
            personal = personal.replace(prefix, "").strip()

        # Safety checks
        blocked = [
            "businesses like", "companies like",
            "i wanted to reach out because", "i hope this email finds you well",
            "i noticed your work",
        ]
        intro_lower = intro.lower()
        if not intro or any(p in intro_lower for p in blocked) or len(intro) > 550:
            print("AI intro rejected (empty/generic/too long) → fallback")
            intro = fallback_intro

        if not personal or len(personal) > 250:
            personal = fallback_personal_line

        print(f"✓ AI intro: {intro[:120]}...")
        print(f"✓ AI personal line: {personal}")
        return intro, personal

    except Exception as e:
        print(f"AI generation failed: {repr(e)}")
        return fallback_intro, fallback_personal_line


# ---------------------------------------------------------------------------
# Light polish (preserves the intro)
# ---------------------------------------------------------------------------

def polish_full_email(
    client,
    subject: str,
    body: str,
    first_name: str,
    company: str,
    industry: str,
    role: str,
    website_text: str,
    offer: str,
    audience: str,
    tone: str,
    call_to_action: str,
    style_guidance: str,
) -> dict:
    if not client:
        return {"subject": subject.strip(), "body": body.strip()}

    prompt = f"""Lightly polish this outreach email.

CRITICAL RULES:
- Keep the personalized intro paragraph almost exactly as written. Do not make it more generic or salesy.
- Do not add "I wanted to reach out because" or "businesses like / companies like".
- Do not invent any facts.
- Keep short paragraphs and the overall length.
- Preserve the offer, CTA, and any unsubscribe line.
- Tone: {tone}

Contact: {first_name} / {company} / {industry} / {role}
Website excerpt: {website_text[:2000] if website_text else "None"}
Audience: {audience}
Offer: {offer}
CTA: {call_to_action}
Style guidance: {style_guidance or "None"}

Current subject:
{subject}

Current body:
{body}

Return exactly:

SUBJECT:
[subject]

BODY:
[body]
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        text = response.text or ""
        polished_subject = extract_section(text, "SUBJECT", "BODY")
        polished_body = extract_section(text, "BODY")

        if polished_subject and polished_body:
            return {
                "subject": polished_subject.strip(),
                "body": polished_body.strip(),
            }
    except Exception as e:
        print(f"Polish failed: {repr(e)}")

    return {"subject": subject.strip(), "body": body.strip()}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def render_template_email(
    template_subject: str,
    template_body: str,
    first_name: str,
    company: str = "",
    industry: str = "",
    role: str = "",
    website: str = "",
    email: str = "",
    offer: str = "",
    audience: str = "small businesses",
    tone: str = "friendly, consultative, concise",
    call_to_action: str = "Would you be open to a quick conversation?",
    unsubscribe_url: str = "",
    cadence_step_name: str = "",
    cadence_step_purpose: str = "",
    step_number: int | None = None,
    brand_voice: str | None = None,
    avoid_phrases: str | None = None,
    preferred_cta: str | None = None,
    signature_name: str | None = None,
    signature_title: str | None = None,
    signature_company: str | None = None,
    style_examples: list[dict] | None = None,
) -> dict:
    first_name = clean_text(first_name) or "there"
    company = clean_text(company)
    industry = clean_text(industry)
    role = clean_text(role)
    website = clean_text(website)
    email = clean_text(email)
    offer = clean_text(offer)
    audience = clean_text(audience) or "small businesses"
    tone = clean_text(tone) or "friendly, consultative, concise"
    call_to_action = (
        clean_text(preferred_cta)
        or clean_text(call_to_action)
        or "Would you be open to a quick conversation?"
    )
    unsubscribe_url = clean_text(unsubscribe_url)

    if not website:
        website = infer_website_from_email(email)

    website_text, website_used = fetch_website_text(website)

    fallback_intro = build_fallback_intro(company=company, industry=industry, role=role)
    fallback_personal = build_fallback_personal_line(company=company, industry=industry)

    print("\n--- AI writer context ---")
    print(f"Company: {company}")
    print(f"Email: {email}")
    print(f"Website input: {website}")
    print(f"Website used: {website_used}")
    print(f"Website text length: {len(website_text)}")

    client = None
    if GEMINI_API_KEY:
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
        except Exception as e:
            print(f"Could not init Gemini client: {e}")
    else:
        print("GEMINI_API_KEY missing → pure fallback mode")

    style_guidance = build_style_guidance(
        brand_voice=brand_voice,
        avoid_phrases=avoid_phrases,
        preferred_cta=preferred_cta,
        signature_name=signature_name,
        signature_title=signature_title,
        signature_company=signature_company,
        style_examples=style_examples,
    )

    intro_para, personal_line = generate_personalized_pieces(
        client=client,
        first_name=first_name,
        company=company,
        industry=industry,
        role=role,
        website=website,
        website_used=website_used,
        website_text=website_text,
        offer=offer,
        audience=audience,
        tone=tone,
        fallback_intro=fallback_intro,
        fallback_personal_line=fallback_personal,
        brand_voice=brand_voice,
        avoid_phrases=avoid_phrases,
    )

    rendered = replace_template_fields(
        template_subject=template_subject,
        template_body=template_body,
        first_name=first_name,
        company=company,
        offer=offer,
        audience=audience,
        call_to_action=call_to_action,
        intro_para=intro_para,
        personal_line=personal_line,
        unsubscribe_url=unsubscribe_url,
    )

    polished = polish_full_email(
        client=client,
        subject=rendered["subject"],
        body=rendered["body"],
        first_name=first_name,
        company=company,
        industry=industry,
        role=role,
        website_text=website_text,
        offer=offer,
        audience=audience,
        tone=tone,
        call_to_action=call_to_action,
        style_guidance=style_guidance,
    )

    return {
        "subject": polished["subject"].strip(),
        "body": polished["body"].strip(),
    }