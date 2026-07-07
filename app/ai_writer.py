import os
import re
from typing import Optional

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
    Cleans website text so the AI gets useful company information,
    not scripts, menus, repeated whitespace, or footer clutter.
    """
    if not text:
        return ""

    text = re.sub(r"\s+", " ", text)
    text = text.strip()

    # Keep the text short so this stays fast and inexpensive.
    return text[:5000]


def fetch_company_website_text(website: str) -> str:
    """
    Fetches public homepage text from the company website.
    If the website fails or blocks the request, returns an empty string.
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

        # Remove code, styles, navigation-heavy clutter, and forms.
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

        # Prefer visible page text.
        page_text = soup.get_text(separator=" ", strip=True)

        return clean_website_text(page_text)

    except Exception as e:
        print(f"Website fetch exception for {url}: {e}")
        return ""


def build_fallback_personal_line(
    company: str,
    industry: str,
) -> str:
    company_display = company or "your team"
    industry_display = industry or "your industry"

    return (
        f"For a {industry_display} business like {company_display}, "
        f"consistent CRM follow-up can make it easier to manage leads, clients, and missed opportunities."
    )


def generate_personal_line(
    first_name: str,
    company: str,
    industry: str,
    role: str,
    website: str = "",
    audience: str = "",
    tone: str = "friendly, consultative, concise",
) -> str:
    """
    Generates one company-specific personalization line.

    Best case:
    - Uses public website text to write a specific but safe opener.

    Fallback:
    - Uses company/industry only.
    """

    company_display = company or "your team"
    industry_display = industry or "your industry"

    fallback_line = build_fallback_personal_line(
        company=company_display,
        industry=industry_display,
    )

    if client is None:
        return fallback_line

    website_text = fetch_company_website_text(website)

    if website_text:
        prompt = f"""
Write one short, accurate personalization sentence for a B2B outreach email.

The sentence should be based only on the company website text provided below.

Rules:
- Write only one sentence.
- Keep it under 35 words.
- Use only information supported by the website text.
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
Website: {website}
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
- If the website text does not contain enough useful company-specific information, write a general industry-specific CRM sentence instead.
- Do not pretend to know the company personally.
- Mention the company or industry if appropriate.
- No markdown.
- No quotes.

Recipient/company info:
First name: {first_name}
Company: {company_display}
Industry: {industry_display}
Role: {role}
Website: {website}
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

        # Safety cleanup in case the model returns quotes or extra spacing.
        line = line.strip().strip('"').strip("'").strip()

        # Keep it to one sentence if the model returns more.
        sentences = re.split(r"(?<=[.!?])\s+", line)
        if sentences:
            line = sentences[0].strip()

        if not line:
            return fallback_line

        return line

    except Exception as e:
        print(f"Gemini personal line generation failed: {e}")
        return fallback_line


def render_template_email(
    template_subject: str,
    template_body: str,
    first_name: str,
    company: str,
    industry: str,
    role: str,
    website: str,
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
    Renders a mostly fixed email template and inserts one AI-generated company-specific personal line.
    """

    company_display = company or "your team"
    first_name_display = first_name or "there"

    personal_line = generate_personal_line(
        first_name=first_name_display,
        company=company_display,
        industry=industry or "",
        role=role or "",
        website=website or "",
        audience=audience or "",
        tone=tone or "",
    )

    replacements = {
        "{{ first_name }}": first_name_display,
        "{{ company }}": company_display,
        "{{ industry }}": industry or "",
        "{{ role }}": role or "",
        "{{ website }}": website or "",
        "{{ offer }}": offer or "",
        "{{ audience }}": audience or "",
        "{{ tone }}": tone or "",
        "{{ call_to_action }}": call_to_action or "",
        "{{ personal_line }}": personal_line,
        "{{ company_blurb }}": personal_line,
        "{{ unsubscribe_url }}": unsubscribe_url or "",
        "{{ cadence_step_name }}": cadence_step_name or "",
        "{{ cadence_step_purpose }}": cadence_step_purpose or "",
        "{{ step_number }}": str(step_number),
    }

    subject = template_subject or "Quick question for {{ company }}"

    body = template_body or """Hi {{ first_name }},

{{ personal_line }}

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
        "company_blurb": personal_line,
    }