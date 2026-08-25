import os
import re
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# You can override this in Render if needed:
# GEMINI_MODEL=gemini-3.6-flash
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()

# Fallback models if the first one ever fails.
GEMINI_MODEL_CANDIDATES = [
    GEMINI_MODEL,
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-flash-latest",
]

# Remove duplicates while preserving order.
GEMINI_MODEL_CANDIDATES = list(dict.fromkeys([m for m in GEMINI_MODEL_CANDIDATES if m]))


FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "aol.com",
    "hotmail.com",
    "outlook.com",
    "icloud.com",
    "msn.com",
    "live.com",
    "comcast.net",
    "verizon.net",
    "att.net",
    "sbcglobal.net",
    "me.com",
    "proton.me",
    "protonmail.com",
}


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return str(value).strip()


def clean_ai_output(value: str | None) -> str:
    text = clean_text(value)

    text = text.replace("INTRO:", "").replace("Intro:", "").strip()
    text = text.replace("SUBJECT:", "SUBJECT:").replace("BODY:", "BODY:")

    # Remove surrounding quotes if Gemini adds them.
    if len(text) >= 2 and text[0] in ['"', "'"] and text[-1] in ['"', "'"]:
        text = text[1:-1].strip()

    return text


def extract_domain_from_email(email: str | None) -> str:
    if not email or "@" not in email:
        return ""

    domain = email.split("@")[-1].strip().lower()
    domain = domain.replace(">", "").replace("<", "")
    return domain


def infer_website_from_email(email: str | None) -> str:
    domain = extract_domain_from_email(email)

    if not domain or domain in FREE_EMAIL_DOMAINS:
        return ""

    return f"https://{domain}"


def normalize_website_url(website: str | None) -> str:
    website = clean_text(website)

    if not website:
        return ""

    website = website.replace("[", "").replace("]", "").strip()

    if website.startswith("http://") or website.startswith("https://"):
        return website

    return f"https://{website}"


def build_url_attempts(website: str | None) -> list[str]:
    website = normalize_website_url(website)

    if not website:
        return []

    attempts = []

    def add(url: str):
        if url and url not in attempts:
            attempts.append(url)

    add(website)

    if "://" in website:
        scheme, rest = website.split("://", 1)

        if not rest.startswith("www."):
            add(f"{scheme}://www.{rest}")

        if scheme == "https":
            add(f"http://{rest}")

            if not rest.startswith("www."):
                add(f"http://www.{rest}")

    return attempts


def fetch_website_text(website: str | None) -> tuple[str, str]:
    """
    Returns:
        website_text, final_url_used

    This attempts to scrape the homepage. Some company sites block scraping,
    so a zero-length scrape should not prevent Gemini from writing an intro.
    """

    urls_to_try = build_url_attempts(website)

    if not urls_to_try:
        return "", ""

    for url in urls_to_try:
        try:
            response = requests.get(
                url,
                timeout=12,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                allow_redirects=True,
            )

            if response.status_code >= 400:
                print(f"Website fetch returned {response.status_code} for {url}")
                continue

            content_type = response.headers.get("content-type", "").lower()

            if "text/html" not in content_type and "application/xhtml" not in content_type:
                print(f"Website fetch skipped non-HTML content for {url}: {content_type}")
                continue

            soup = BeautifulSoup(response.text, "html.parser")

            for tag in soup(["script", "style", "noscript", "svg", "form", "iframe"]):
                tag.decompose()

            title = soup.title.get_text(" ", strip=True) if soup.title else ""

            meta_description = ""
            meta_tag = soup.find("meta", attrs={"name": "description"})

            if meta_tag and meta_tag.get("content"):
                meta_description = meta_tag.get("content", "").strip()

            og_description = ""
            og_tag = soup.find("meta", attrs={"property": "og:description"})

            if og_tag and og_tag.get("content"):
                og_description = og_tag.get("content", "").strip()

            headings = []

            for heading_tag in soup.find_all(["h1", "h2", "h3"]):
                heading_text = heading_tag.get_text(" ", strip=True)

                if heading_text:
                    headings.append(heading_text)

            page_text = " ".join(soup.get_text(separator=" ").split())

            combined_text = " ".join(
                part
                for part in [
                    title,
                    meta_description,
                    og_description,
                    " ".join(headings[:16]),
                    page_text,
                ]
                if part
            )

            combined_text = combined_text.strip()

            # Avoid treating very tiny pages as useful website text.
            if len(combined_text) >= 120:
                print(f"✓ Website scrape successful: {url}")
                print(f"Website text length: {len(combined_text)}")
                return combined_text[:7000], url

            print(f"Website scrape too short for {url}: {len(combined_text)} chars")

        except Exception as e:
            print(f"Website fetch failed for {url}: {repr(e)}")
            continue

    print(f"✗ Scrape failed or empty for: {normalize_website_url(website)}")
    return "", normalize_website_url(website)


def build_fallback_intro(
    company: str = "",
    industry: str = "",
    role: str = "",
    email_domain: str = "",
) -> str:
    company = clean_text(company)
    industry = clean_text(industry)
    role = clean_text(role)
    email_domain = clean_text(email_domain)

    if company and industry:
        return (
            f"I came across {company} while reviewing companies in the {industry.lower()} space and thought "
            f"this may be worth a quick conversation."
        )

    if company:
        return f"I came across {company} and thought this may be worth a quick conversation."

    if email_domain:
        return f"I came across your team at {email_domain} and thought this may be worth a quick conversation."

    return "I thought this may be worth a quick conversation."


def build_personal_line(
    company: str = "",
    industry: str = "",
    email_domain: str = "",
) -> str:
    company = clean_text(company)
    industry = clean_text(industry)
    email_domain = clean_text(email_domain)

    if company:
        return f"I came across {company} and thought this may be relevant."

    if email_domain:
        return f"I came across your team at {email_domain} and thought this may be relevant."

    if industry:
        return f"I thought this may be relevant for your work in the {industry.lower()} space."

    return "I thought this may be relevant."


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

    signature_lines = []

    if signature_name:
        signature_lines.append(signature_name)

    if signature_title:
        signature_lines.append(signature_title)

    if signature_company:
        signature_lines.append(signature_company)

    if signature_lines:
        parts.append("Preferred signature:\n" + "\n".join(signature_lines))

    if style_examples:
        example_texts = []

        for index, example in enumerate(style_examples[:5], start=1):
            subject = clean_text(example.get("subject"))
            body = clean_text(example.get("body"))

            if not subject and not body:
                continue

            example_texts.append(
                f"Example {index}\nSubject: {subject}\nBody:\n{body}"
            )

        if example_texts:
            parts.append(
                "Approved edited examples. Use these to match voice, length, warmth, and CTA style. "
                "Do not copy them word-for-word.\n\n"
                + "\n\n---\n\n".join(example_texts)
            )

    return "\n\n".join(parts).strip()


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

    return {
        "subject": subject.strip(),
        "body": body.strip(),
    }


def extract_section(text: str, section_name: str, next_section_name: str | None = None) -> str:
    if not text:
        return ""

    if next_section_name:
        pattern = rf"{section_name}:\s*(.*?)\s*{next_section_name}:"
    else:
        pattern = rf"{section_name}:\s*(.*)"

    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)

    if not match:
        return ""

    return match.group(1).strip()


def generate_with_gemini(client, prompt: str, purpose: str) -> str:
    """
    Tries the configured Gemini model first, then fallbacks.
    This prevents the app from breaking again if one model is retired.
    """

    last_error = None

    for model_name in GEMINI_MODEL_CANDIDATES:
        try:
            print(f"Gemini {purpose}: trying model {model_name}")

            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )

            text = clean_text(response.text)

            if text:
                print(f"Gemini {purpose}: success with model {model_name}")
                return text

            print(f"Gemini {purpose}: blank response from model {model_name}")

        except Exception as e:
            last_error = e
            print(f"Gemini {purpose}: failed with model {model_name}: {repr(e)}")
            continue

    raise RuntimeError(f"All Gemini models failed for {purpose}: {repr(last_error)}")


def generate_personalized_intro(
    client,
    first_name: str,
    company: str,
    industry: str,
    role: str,
    website: str,
    website_used: str,
    website_text: str,
    email_domain: str,
    offer: str,
    audience: str,
    tone: str,
    fallback_intro: str,
    brand_voice: str | None = None,
    avoid_phrases: str | None = None,
) -> str:
    """
    Generates only the intro paragraph.

    Important:
    - If website text exists, Gemini should use one real company-specific detail.
    - If scraping fails, Gemini should still write a better company/domain-based opener.
    - It should not hard-fallback just because website_text is empty.
    """

    if not client:
        print("Intro generation: no Gemini client, using fallback intro.")
        return fallback_intro

    company = clean_text(company)
    industry = clean_text(industry)
    role = clean_text(role)
    website = clean_text(website)
    website_used = clean_text(website_used)
    website_text = clean_text(website_text)
    email_domain = clean_text(email_domain)

    if website_text:
        website_context = f"""
Useful website text was extracted. Use one specific detail from this website text if it is relevant.

Website text excerpt:
{website_text[:5500]}
"""
    else:
        print("Intro generation: no website text available, asking Gemini for a company/domain-based intro.")
        website_context = f"""
No useful website text could be extracted.

Do not pretend you reviewed the website.
Do not invent specific services, products, clients, awards, locations, certifications, or years in business.
Use only the company name, email domain, industry, and role/title.
Write a neutral opener that still feels more human than a hard-coded fallback.
"""

    prompt = f"""
You are writing ONLY the personalized opening paragraph for a business outreach email.

Write 1 short paragraph, 1-2 sentences maximum.

Goal:
Create an opener that feels relevant to the contact/company and naturally leads into a CRM/process improvement conversation.

Rules:
- If useful website text is available, use ONE specific detail from it.
- If website text is unavailable, use the company name, domain, industry, or role without pretending you know specific facts.
- Do not start with "I wanted to reach out because".
- Do not use "businesses like" or "companies like".
- Do not use "I noticed your work".
- Do not use "I hope this email finds you well".
- Do not mention scraping, reviewing, visiting, or looking at the website.
- Do not include a greeting.
- Do not include a sign-off.
- Do not repeat the campaign offer.
- Do not make the first sentence a generic CRM pitch.
- Do not invent facts.
- Keep it warm, brief, direct, and practical.

Good website-based example:
"I saw that Ampt focuses on power conversion technology for solar and energy storage systems. That kind of project-based sales process usually depends on clear visibility across prospects, customers, and next steps."

Good no-website-text example:
"I came across Laser Technology, Inc. and thought this may be relevant for a team managing customer conversations, quotes, follow-up, and next steps across a technical sales process."

Bad example:
"I wanted to reach out because businesses like Laser Technology often depend on organized follow-up and clear communication."

Contact:
First name: {first_name}
Company: {company}
Industry: {industry}
Role/title: {role}
Email domain: {email_domain}
Website input: {website}
Website used: {website_used or website}

{website_context}

Campaign audience:
{audience}

Campaign offer:
{offer}

Tone:
{tone}

Brand voice:
{brand_voice or "Warm, direct, brief, consultative, practical."}

Avoid phrases:
{avoid_phrases or "Avoid fake compliments, hype, and overly formal corporate language."}

Fallback intro:
{fallback_intro}

Return only the paragraph text.
"""

    try:
        intro = generate_with_gemini(client, prompt, "intro")
        intro = clean_ai_output(intro)

        if not intro:
            print("Intro generation returned blank, using fallback intro.")
            return fallback_intro

        if len(intro) > 650:
            intro = intro[:650].rsplit(".", 1)[0] + "."

        print(f"Generated intro paragraph: {intro}")
        return intro

    except Exception as e:
        print(f"Intro generation failed: {repr(e)}")
        return fallback_intro


def polish_full_email(
    client,
    subject: str,
    body: str,
    first_name: str,
    company: str,
    industry: str,
    role: str,
    website_text: str,
    email_domain: str,
    offer: str,
    audience: str,
    tone: str,
    call_to_action: str,
    style_guidance: str,
) -> dict:
    """
    Polishes the already-rendered email.
    The intro_para has already been generated and inserted before this step.
    """

    subject = clean_text(subject)
    body = clean_text(body)

    if not client:
        return {
            "subject": subject,
            "body": body,
        }

    website_context = (
        website_text[:3000]
        if website_text
        else "No useful website text was extracted. Do not add specific company facts."
    )

    prompt = f"""
Polish this business outreach email lightly.

Rules:
- Keep the personalized intro paragraph specific if it is specific.
- If the intro is neutral because website text was unavailable, do not invent website details.
- Do not replace the intro with a generic opener.
- Do not make the intro more salesy.
- Do not add "I wanted to reach out because."
- Do not add "businesses like" or "companies like."
- Do not repeat the same idea twice.
- If the body already explains CRM pain points, do not add CRM pain points to the intro.
- Keep the email brief.
- Keep short paragraphs.
- Do not add fake details.
- Do not add claims that are not supported.
- Preserve the offer and CTA.
- Preserve any unsubscribe line or unsubscribe URL if present.
- Do not use "I hope this email finds you well."
- Do not use hype or buzzwords.
- Keep the tone: {tone}

Contact:
First name: {first_name}
Company: {company}
Industry: {industry}
Role/title: {role}
Email domain: {email_domain}

Website context:
{website_context}

Campaign:
Audience: {audience}
Offer: {offer}
CTA: {call_to_action}

Style guidance:
{style_guidance if style_guidance else "No custom style guidance."}

Current subject:
{subject}

Current body:
{body}

Return exactly this structure:

SUBJECT:
<subject here>

BODY:
<body here>
"""

    try:
        text = generate_with_gemini(client, prompt, "polish")

        polished_subject = extract_section(text, "SUBJECT", "BODY")
        polished_body = extract_section(text, "BODY")

        if not polished_subject or not polished_body:
            return {
                "subject": subject,
                "body": body,
            }

        return {
            "subject": polished_subject.strip(),
            "body": polished_body.strip(),
        }

    except Exception as e:
        print(f"Polish failed: {repr(e)}")
        return {
            "subject": subject,
            "body": body,
        }


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

    email_domain = extract_domain_from_email(email)

    call_to_action = (
        clean_text(preferred_cta)
        or clean_text(call_to_action)
        or "Would you be open to a quick conversation?"
    )

    unsubscribe_url = clean_text(unsubscribe_url)

    if not website:
        website = infer_website_from_email(email)

    website_text, website_used = fetch_website_text(website)

    fallback_intro = build_fallback_intro(
        company=company,
        industry=industry,
        role=role,
        email_domain=email_domain,
    )

    fallback_personal_line = build_personal_line(
        company=company,
        industry=industry,
        email_domain=email_domain,
    )

    print("--- AI writer context ---")
    print(f"Company: {company}")
    print(f"Email: {email}")
    print(f"Email domain: {email_domain}")
    print(f"Website input: {website}")
    print(f"Website used: {website_used}")
    print(f"Website text length: {len(website_text)}")
    print(f"Gemini API key present: {bool(GEMINI_API_KEY)}")
    print(f"Gemini model candidates: {GEMINI_MODEL_CANDIDATES}")

    client = None

    if GEMINI_API_KEY:
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
        except Exception as e:
            print(f"Could not create Gemini client: {repr(e)}")
            client = None
    else:
        print("GEMINI_API_KEY missing. Using fallback template rendering.")

    style_guidance = build_style_guidance(
        brand_voice=brand_voice,
        avoid_phrases=avoid_phrases,
        preferred_cta=preferred_cta,
        signature_name=signature_name,
        signature_title=signature_title,
        signature_company=signature_company,
        style_examples=style_examples,
    )

    intro_para = generate_personalized_intro(
        client=client,
        first_name=first_name,
        company=company,
        industry=industry,
        role=role,
        website=website,
        website_used=website_used,
        website_text=website_text,
        email_domain=email_domain,
        offer=offer,
        audience=audience,
        tone=tone,
        fallback_intro=fallback_intro,
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
        personal_line=fallback_personal_line,
        unsubscribe_url=unsubscribe_url,
    )

    subject = rendered["subject"]
    body = rendered["body"]

    polished = polish_full_email(
        client=client,
        subject=subject,
        body=body,
        first_name=first_name,
        company=company,
        industry=industry,
        role=role,
        website_text=website_text,
        email_domain=email_domain,
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