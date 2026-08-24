import os
import re
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

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
}


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


def fetch_website_text(website: str | None) -> tuple[str, str]:
    """
    Returns:
        website_text, final_url_used

    This tries:
    1. Provided website
    2. www version if the first request fails
    """

    website = normalize_website_url(website)

    if not website:
        return "", ""

    urls_to_try = [website]

    if "://" in website:
        scheme, rest = website.split("://", 1)

        if not rest.startswith("www."):
            urls_to_try.append(f"{scheme}://www.{rest}")

    for url in urls_to_try:
        try:
            response = requests.get(
                url,
                timeout=10,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"
                    )
                },
                allow_redirects=True,
            )

            if response.status_code >= 400:
                print(f"Website fetch returned {response.status_code} for {url}")
                continue

            soup = BeautifulSoup(response.text, "html.parser")

            for tag in soup(["script", "style", "noscript", "svg", "form"]):
                tag.decompose()

            title = soup.title.get_text(" ", strip=True) if soup.title else ""

            meta_description = ""
            meta_tag = soup.find("meta", attrs={"name": "description"})

            if meta_tag and meta_tag.get("content"):
                meta_description = meta_tag.get("content", "").strip()

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
                    " ".join(headings[:12]),
                    page_text,
                ]
                if part
            )

            combined_text = combined_text.strip()

            if combined_text:
                print(f"Website scrape successful: {url}")
                print(f"Website text length: {len(combined_text)}")
                return combined_text[:5000], url

        except Exception as e:
            print(f"Website fetch failed for {url}: {repr(e)}")
            continue

    print(f"Website scrape failed or returned no useful text for: {website}")
    return "", website


def build_fallback_intro(
    company: str = "",
    industry: str = "",
    role: str = "",
    website_text: str = "",
) -> str:
    company = clean_text(company)
    industry = clean_text(industry)
    role = clean_text(role)
    website_text = clean_text(website_text)

    if company and industry:
        return (
            f"I came across {company} while reviewing companies in the {industry.lower()} space and thought "
            f"this may be worth a quick conversation."
        )

    if company:
        return (
            f"I came across {company} and thought this may be worth a quick conversation."
        )

    return (
        "I thought this may be worth a quick conversation."
    )

def build_personal_line(
    company: str = "",
    industry: str = "",
    website_text: str = "",
) -> str:
    company = clean_text(company)
    industry = clean_text(industry)
    website_text = clean_text(website_text)

    if company and website_text:
        return f"I came across {company} and thought this may be a practical CRM conversation."

    if company and industry:
        return f"I thought this may be a practical CRM conversation for {company}."

    if company:
        return f"I thought this may be a practical idea for {company}."

    return "I thought this may be a practical CRM conversation."


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


def generate_personalized_intro(
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
    brand_voice: str | None = None,
    avoid_phrases: str | None = None,
) -> str:
    """
    Generates only the intro paragraph.
    This is intentionally separate from full email generation so {{ intro_para }}
    can use a real company-specific detail when the website supports it.
    """

    if not client:
        return fallback_intro

    if not website_text:
        print("Intro generation: no website text available, using fallback intro.")
        return fallback_intro

    prompt = f"""
You are writing ONLY the personalized opening paragraph for a business outreach email.

Write 1 short paragraph, 1-2 sentences maximum.

Your job:
- Use ONE specific detail from the website text.
- Show that the email is relevant to the company.
- Do NOT start by pitching CRM.
- Do NOT make the intro about Evolution CRM.
- Do NOT repeat the campaign offer.
- Do NOT use generic phrases such as:
  "businesses like"
  "companies like"
  "I noticed your work"
  "I wanted to reach out because"
  "I hope this email finds you well"
- Do NOT invent facts, awards, clients, locations, years in business, certifications, or services.
- Do NOT mention that you scraped, reviewed, visited, or looked at the website.
- Do NOT include a greeting.
- Do NOT include a sign-off.
- Keep it natural, brief, and human.

Good intro pattern:
"I saw that [Company] focuses on [specific thing from website]. That kind of work usually requires clear visibility across prospects, customers, and next steps."

Bad intro pattern:
"I wanted to reach out because businesses like [Company] often depend on organized follow-up."

Contact:
First name: {first_name}
Company: {company}
Industry: {industry}
Role/title: {role}

Website used:
{website_used or website}

Website text excerpt:
{website_text[:4500]}

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

Fallback intro if the website is not useful:
{fallback_intro}

Return only the paragraph text.
"""

    try:
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
        )

        intro = clean_text(response.text)

        intro = intro.replace("INTRO:", "").strip()
        intro = intro.replace("Intro:", "").strip()

        if not intro:
            print("Intro generation returned blank, using fallback intro.")
            return fallback_intro

        blocked_generic_phrases = [
            "businesses like",
            "companies like",
            "i wanted to reach out because",
            "i noticed your work",
            "i hope this email finds you well",
        ]

        intro_lower = intro.lower()

        if any(phrase in intro_lower for phrase in blocked_generic_phrases):
            print("Intro generation was too generic, using fallback intro.")
            return fallback_intro

        if len(intro) > 600:
            intro = intro[:600].rsplit(".", 1)[0] + "."

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

    if not client:
        return {
            "subject": subject.strip(),
            "body": body.strip(),
        }

    prompt = f"""
Polish this business outreach email lightly.

Rules:
- Keep the personalized intro paragraph specific.
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

Website text excerpt:
{website_text[:2500] if website_text else "No useful website text available."}

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
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
        )

        text = response.text or ""

        polished_subject = extract_section(text, "SUBJECT", "BODY")
        polished_body = extract_section(text, "BODY")

        if not polished_subject or not polished_body:
            return {
                "subject": subject.strip(),
                "body": body.strip(),
            }

        return {
            "subject": polished_subject.strip(),
            "body": polished_body.strip(),
        }

    except Exception as e:
        print(f"Full email polish failed: {repr(e)}")
        return {
            "subject": subject.strip(),
            "body": body.strip(),
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
        website_text=website_text,
    )

    fallback_personal_line = build_personal_line(
        company=company,
        industry=industry,
        website_text=website_text,
    )

    print("AI writer contact context")
    print(f"Company: {company}")
    print(f"Email: {email}")
    print(f"Website input/inferred: {website}")
    print(f"Website used: {website_used}")
    print(f"Website text length: {len(website_text)}")

    client = None

    if GEMINI_API_KEY:
        client = genai.Client(api_key=GEMINI_API_KEY)
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