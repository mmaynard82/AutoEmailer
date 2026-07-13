import os
import re

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)
else:
    client = None


FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "aol.com",
    "icloud.com",
    "msn.com",
    "live.com",
    "comcast.net",
    "protonmail.com",
    "proton.me",
    "mail.com",
    "me.com",
    "mac.com",
}


def infer_website_from_email(email: str) -> str:
    """
    If the contact has no website, infer a likely website from their business email domain.
    Skips common personal/free email domains.
    """
    email = (email or "").strip().lower()

    if "@" not in email:
        return ""

    domain = email.split("@")[-1].strip()

    if not domain:
        return ""

    if domain in FREE_EMAIL_DOMAINS:
        return ""

    if "." not in domain:
        return ""

    return f"https://{domain}"


def normalize_website_url(website: str) -> str:
    """
    Makes sure the website has http:// or https://.
    """
    website = (website or "").strip()

    if not website:
        return ""

    if website.startswith("http://") or website.startswith("https://"):
        return website

    return f"https://{website}"


def clean_website_text(text: str) -> str:
    """
    Cleans website text so Gemini gets useful company information,
    not scripts, menus, repeated whitespace, or footer clutter.
    """
    if not text:
        return ""

    text = re.sub(r"\s+", " ", text)
    text = text.strip()

    # Keep the text short so this stays fast and inexpensive.
    return text[:7000]


def fetch_company_website_text(website: str) -> str:
    """
    Fetches public homepage text from the company website.
    If the website fails, blocks the request, or has no useful text,
    returns an empty string.
    """
    url = normalize_website_url(website)

    if not url:
        return ""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 AppleWebKit/537.36 "
            "KHTML, like Gecko; compatible; AIEmailerBot/1.0"
        )
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=8,
            allow_redirects=True,
        )

        if response.status_code >= 400:
            print(f"Website fetch failed for {url}: HTTP {response.status_code}")
            return ""

        soup = BeautifulSoup(response.text, "html.parser")

        for tag in soup([
            "script",
            "style",
            "noscript",
            "svg",
            "form",
            "input",
            "button",
            "iframe",
        ]):
            tag.decompose()

        page_text = soup.get_text(separator=" ", strip=True)
        return clean_website_text(page_text)

    except Exception as e:
        print(f"Website fetch exception for {url}: {e}")
        return ""


def resolve_website_to_use(website: str, email: str) -> str:
    """
    Uses website field first.
    If blank, tries to infer the company website from business email domain.
    """
    if website and website.strip():
        return website.strip()

    return infer_website_from_email(email)


def build_fallback_personal_line(
    company: str,
    industry: str,
) -> str:
    company_display = company.strip() if company and company.strip() else ""
    industry_display = industry.strip() if industry and industry.strip() else "small business"

    if company_display:
        return (
            f"For a {industry_display} business like {company_display}, "
            f"consistent CRM follow-up can make it easier to manage leads, clients, and missed opportunities."
        )

    return (
        f"For a {industry_display} business, consistent CRM follow-up can make it easier "
        f"to manage leads, clients, and missed opportunities."
    )


def build_fallback_intro_para(
    company: str,
    industry: str,
) -> str:
    company_display = company.strip() if company and company.strip() else ""
    industry_display = industry.strip() if industry and industry.strip() else "small business"

    if company_display:
        return (
            f"I saw that {company_display} works in the {industry_display} space, and I wanted to reach out because "
            f"consistent follow-up and clear client communication can make a meaningful difference when managing leads, "
            f"relationships, and new opportunities."
        )

    return (
        f"I wanted to reach out because businesses in the {industry_display} space often need a simple, reliable way "
        f"to manage leads, follow-up, client communication, and new opportunities."
    )


def generate_personal_line(
    first_name: str,
    company: str,
    industry: str,
    role: str,
    website: str = "",
    email: str = "",
    audience: str = "",
    tone: str = "friendly, consultative, concise",
    website_text: str = "",
) -> str:
    """
    Generates one short company-specific personalization sentence.

    Best case:
    - Uses scraped website text.

    Fallback:
    - Uses company/industry only.
    """

    company_display = company.strip() if company and company.strip() else ""
    industry_display = industry.strip() if industry and industry.strip() else "small business"
    website_to_use = resolve_website_to_use(website, email)

    fallback_line = build_fallback_personal_line(
        company=company_display,
        industry=industry_display,
    )

    if client is None:
        return fallback_line

    if not website_text:
        website_text = fetch_company_website_text(website_to_use)

    if website_text:
        prompt = f"""
Write one short, accurate personalization sentence for a B2B outreach email.

The sentence should be based only on the company website text provided below.

Rules:
- Write only one sentence.
- Keep it under 35 words.
- Use only information supported by the website text.
- If the website text is mostly navigation, contact information, legal text, generic slogans, or does not contain enough useful company-specific information, write a general industry-specific CRM sentence instead.
- Do not make up specific facts.
- Do not mention trends, news, research, awards, growth, funding, hiring, or anything you cannot verify.
- Do not say "I was impressed by".
- Do not say "I noticed your website".
- Do not say "I was browsing your website".
- Do not pretend to know the company personally.
- Connect the sentence naturally to CRM follow-up, client communication, lead tracking, sales visibility, missed opportunities, or relationship management.
- No markdown.
- No quotes.

Recipient/company info:
First name: {first_name}
Company: {company_display}
Industry: {industry_display}
Role: {role}
Email: {email}
Website used: {website_to_use}
Audience: {audience}
Tone: {tone}

Company website text:
{website_text}

Return only the sentence.
"""
    else:
        prompt = f"""
Write one short personalization sentence for a B2B outreach email.

Rules:
- Write only one sentence.
- Keep it under 30 words.
- Make the sentence about CRM follow-up, lead tracking, client communication, sales visibility, missed opportunities, or relationship management.
- Do not make up specific facts.
- Do not mention trends, news, research, awards, growth, funding, hiring, or anything you cannot verify.
- Do not say "I was impressed by".
- Do not pretend to know the company personally.
- If company name is missing, do not say "your team."
- Mention the company or industry only if available.
- No markdown.
- No quotes.

Recipient/company info:
First name: {first_name}
Company: {company_display}
Industry: {industry_display}
Role: {role}
Email: {email}
Website attempted: {website_to_use}
Audience: {audience}
Tone: {tone}

Return only the sentence.
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )

        line = response.text.strip() if response.text else ""

        if not line:
            return fallback_line

        line = line.strip().strip('"').strip("'").strip()

        sentences = re.split(r"(?<=[.!?])\s+", line)
        if sentences:
            line = sentences[0].strip()

        if not line:
            return fallback_line

        return line

    except Exception as e:
        print(f"Gemini personal line generation failed: {e}")
        return fallback_line


def generate_intro_para(
    first_name: str,
    company: str,
    industry: str,
    role: str,
    website: str = "",
    email: str = "",
    audience: str = "",
    tone: str = "friendly, consultative, concise",
    website_text: str = "",
) -> str:
    """
    Generates a short introductory paragraph for Email #1 in a cold outreach sequence.

    Best case:
    - Uses public website text.
    - Recognizes who the company is.
    - References a recent announcement/update only if clearly supported by website text.

    Fallback:
    - Uses company/industry only and does not invent announcements.
    """

    company_display = company.strip() if company and company.strip() else ""
    industry_display = industry.strip() if industry and industry.strip() else "small business"
    website_to_use = resolve_website_to_use(website, email)

    fallback_intro = build_fallback_intro_para(
        company=company_display,
        industry=industry_display,
    )

    if client is None:
        return fallback_intro

    if not website_text:
        website_text = fetch_company_website_text(website_to_use)

    if not website_text:
        return fallback_intro

    prompt = f"""
Write a short introductory paragraph for Email #1 in a cold B2B outreach sequence.

The paragraph should:
- Be 2 to 3 short sentences.
- Recognize who the company is.
- Acknowledge one recent announcement, update, project, news item, event, milestone, or public-facing change only if it is clearly supported by the website text.
- If the website text mentions multiple announcements, choose the most concrete and relevant one.
- Connect naturally to CRM follow-up, customer/client communication, sales visibility, lead tracking, missed opportunities, or relationship management.
- Sound human, warm, and consultative.
- Avoid sounding like spam.
- Avoid exaggerated praise.
- Avoid "I was impressed by".
- Avoid "I noticed your website".
- Avoid "I was browsing your website".
- Do not make up an announcement.
- If the website text does not contain a recent announcement or update, write a general company-aware intro paragraph without mentioning a recent announcement.
- Do not mention dates unless the date is clearly present in the website text.
- No markdown.
- No quotes.

Recipient/company info:
First name: {first_name}
Company: {company_display}
Industry: {industry_display}
Role: {role}
Email: {email}
Website used: {website_to_use}
Audience: {audience}
Tone: {tone}

Company website text:
{website_text}

Return only the paragraph.
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )

        intro = response.text.strip() if response.text else ""

        if not intro:
            return fallback_intro

        intro = intro.strip().strip('"').strip("'").strip()

        return intro

    except Exception as e:
        print(f"Gemini intro paragraph generation failed: {e}")
        return fallback_intro


def render_template_email(
    template_subject: str,
    template_body: str,
    first_name: str,
    company: str,
    industry: str,
    role: str,
    website: str,
    email: str,
    offer: str,
    audience: str,
    tone: str,
    call_to_action: str,
    unsubscribe_url: str = "",
    cadence_step_name: str = "",
    cadence_step_purpose: str = "",
    step_number: int = 1,
) -> dict:
    """
    Renders the email template and inserts AI-generated personalization fields.

    Supported placeholders include:
    - {{ first_name }}
    - {{ company }}
    - {{ industry }}
    - {{ role }}
    - {{ website }}
    - {{ email }}
    - {{ personal_line }}
    - {{ intro_para }}
    - {{ offer }}
    - {{ audience }}
    - {{ call_to_action }}
    - {{ unsubscribe_url }}
    """

    company_display = company.strip() if company and company.strip() else ""
    first_name_display = first_name.strip() if first_name and first_name.strip() else "there"

    website_to_use = resolve_website_to_use(website, email)
    website_text = fetch_company_website_text(website_to_use)

    personal_line = generate_personal_line(
        first_name=first_name_display,
        company=company_display,
        industry=industry or "",
        role=role or "",
        website=website or "",
        email=email or "",
        audience=audience or "",
        tone=tone or "",
        website_text=website_text,
    )

    intro_para = generate_intro_para(
        first_name=first_name_display,
        company=company_display,
        industry=industry or "",
        role=role or "",
        website=website or "",
        email=email or "",
        audience=audience or "",
        tone=tone or "",
        website_text=website_text,
    )

    replacements = {
        "{{ first_name }}": first_name_display,
        "{{first_name}}": first_name_display,
        "{first_name}": first_name_display,
        "{first name}": first_name_display,

        "{{ company }}": company_display,
        "{{company}}": company_display,
        "{company}": company_display,

        "{{ industry }}": industry or "",
        "{{industry}}": industry or "",
        "{industry}": industry or "",

        "{{ role }}": role or "",
        "{{role}}": role or "",
        "{role}": role or "",

        "{{ website }}": website_to_use or website or "",
        "{{website}}": website_to_use or website or "",
        "{website}": website_to_use or website or "",

        "{{ email }}": email or "",
        "{{email}}": email or "",
        "{email}": email or "",

        "{{ offer }}": offer or "",
        "{{offer}}": offer or "",
        "{offer}": offer or "",

        "{{ audience }}": audience or "",
        "{{audience}}": audience or "",
        "{audience}": audience or "",

        "{{ tone }}": tone or "",
        "{{tone}}": tone or "",
        "{tone}": tone or "",

        "{{ call_to_action }}": call_to_action or "",
        "{{call_to_action}}": call_to_action or "",
        "{call_to_action}": call_to_action or "",
        "{call to action}": call_to_action or "",

        "{{ personal_line }}": personal_line,
        "{{personal_line}}": personal_line,
        "{personal_line}": personal_line,
        "{personal line}": personal_line,

        "{{ intro_para }}": intro_para,
        "{{intro_para}}": intro_para,
        "{intro_para}": intro_para,
        "{intro para}": intro_para,
        "{intro paragraph}": intro_para,

        "{{ company_blurb }}": personal_line,
        "{{company_blurb}}": personal_line,
        "{company_blurb}": personal_line,
        "{company blurb}": personal_line,

        "{{ unsubscribe_url }}": unsubscribe_url or "",
        "{{unsubscribe_url}}": unsubscribe_url or "",
        "{unsubscribe_url}": unsubscribe_url or "",
        "{unsubscribe url}": unsubscribe_url or "",

        "{{ cadence_step_name }}": cadence_step_name or "",
        "{{cadence_step_name}}": cadence_step_name or "",
        "{cadence_step_name}": cadence_step_name or "",

        "{{ cadence_step_purpose }}": cadence_step_purpose or "",
        "{{cadence_step_purpose}}": cadence_step_purpose or "",
        "{cadence_step_purpose}": cadence_step_purpose or "",

        "{{ step_number }}": str(step_number),
        "{{step_number}}": str(step_number),
        "{step_number}": str(step_number),
        "{step number}": str(step_number),
    }

    subject = template_subject or "Quick question for {{ company }}"

    body = template_body or """Hi {{ first_name }},

{{ intro_para }}

I’m reaching out because we help {{ audience }} improve CRM follow-up, sales visibility, and client communication.

{{ offer }}

{{ call_to_action }}

Best,
Evolution CRM"""

    for key, value in replacements.items():
        subject = subject.replace(key, value)
        body = body.replace(key, value)

    return {
        "subject": subject.strip(),
        "body": body.strip(),
        "personal_line": personal_line,
        "intro_para": intro_para,
        "company_blurb": personal_line,
        "website_used": website_to_use,
    }