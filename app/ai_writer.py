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
}


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return str(value).strip()


def infer_website_from_email(email: str | None) -> str:
    if not email or "@" not in email:
        return ""

    domain = email.split("@")[-1].strip().lower()

    if domain in FREE_EMAIL_DOMAINS:
        return ""

    return f"https://{domain}"


def fetch_website_text(website: str | None) -> str:
    if not website:
        return ""

    website = website.strip()

    if not website:
        return ""

    if not website.startswith("http://") and not website.startswith("https://"):
        website = "https://" + website

    try:
        response = requests.get(
            website,
            timeout=8,
            headers={
                "User-Agent": "Mozilla/5.0 AI Emailer Research Bot"
            },
        )

        if response.status_code >= 400:
            return ""

        soup = BeautifulSoup(response.text, "html.parser")

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        text = " ".join(soup.get_text(separator=" ").split())
        return text[:2500]

    except Exception as e:
        print(f"Website fetch failed for {website}: {repr(e)}")
        return ""


def fallback_render_template(
    template_subject: str,
    template_body: str,
    first_name: str,
    company: str,
    offer: str,
    audience: str,
    call_to_action: str,
    intro_para: str,
    unsubscribe_url: str,
) -> dict:
    replacements = {
        "{{ first_name }}": first_name or "there",
        "{first name}": first_name or "there",
        "{{ company }}": company or "your company",
        "{company}": company or "your company",
        "{{ offer }}": offer or "",
        "{offer}": offer or "",
        "{{ audience }}": audience or "businesses",
        "{audience}": audience or "businesses",
        "{{ call_to_action }}": call_to_action or "Would you be open to a quick conversation?",
        "{call to action}": call_to_action or "Would you be open to a quick conversation?",
        "{{ intro_para }}": intro_para or "",
        "{intro para}": intro_para or "",
        "{{ unsubscribe_url }}": unsubscribe_url or "",
        "{unsubscribe url}": unsubscribe_url or "",
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
                "Use these approved edited examples to match voice, structure, length, warmth, and CTA style. Do not copy them word-for-word.\n\n"
                + "\n\n---\n\n".join(example_texts)
            )

    return "\n\n".join(parts).strip()


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
    call_to_action = clean_text(preferred_cta) or clean_text(call_to_action) or "Would you be open to a quick conversation?"
    unsubscribe_url = clean_text(unsubscribe_url)

    if not website:
        website = infer_website_from_email(email)

    website_text = fetch_website_text(website)

    intro_para = ""
    personal_line = ""

    if company:
        personal_line = f"I noticed your work with {company}."
        intro_para = f"I noticed your work with {company} and wanted to reach out with a practical idea."
    else:
        personal_line = "I wanted to reach out with a practical idea."
        intro_para = "I wanted to reach out with a practical idea."

    if not GEMINI_API_KEY:
        return fallback_render_template(
            template_subject=template_subject,
            template_body=template_body,
            first_name=first_name,
            company=company,
            offer=offer,
            audience=audience,
            call_to_action=call_to_action,
            intro_para=intro_para,
            unsubscribe_url=unsubscribe_url,
        )

    client = genai.Client(api_key=GEMINI_API_KEY)

    style_guidance = build_style_guidance(
        brand_voice=brand_voice,
        avoid_phrases=avoid_phrases,
        preferred_cta=preferred_cta,
        signature_name=signature_name,
        signature_title=signature_title,
        signature_company=signature_company,
        style_examples=style_examples,
    )

    prompt = f"""
You are writing a concise business outreach email.

Goal:
Create a polished, natural email draft using the template and contact details below.

Important rules:
- Sound human, warm, direct, and not overly salesy.
- Keep it brief.
- Do not make fake claims about the prospect.
- Do not overdo personalization.
- Do not say "I hope this email finds you well."
- Do not use hype, buzzwords, or exaggerated promises.
- If website details are thin or unclear, keep personalization general.
- Preserve the user's intended offer and call to action.
- Return only a subject and body in the format below.

Contact:
First name: {first_name}
Company: {company}
Industry: {industry}
Role/title: {role}
Website: {website}
Website text excerpt:
{website_text}

Campaign:
Audience: {audience}
Offer: {offer}

Cadence step:
Step number: {step_number}
Step name: {cadence_step_name}
Step purpose: {cadence_step_purpose}
Tone: {tone}
Call to action: {call_to_action}

Template subject:
{template_subject}

Template body:
{template_body}

Style profile and approved examples:
{style_guidance if style_guidance else "No custom style profile provided."}

Unsubscribe URL:
{unsubscribe_url}

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

        subject_match = re.search(r"SUBJECT:\s*(.*?)\s*BODY:", text, re.DOTALL | re.IGNORECASE)
        body_match = re.search(r"BODY:\s*(.*)", text, re.DOTALL | re.IGNORECASE)

        if subject_match and body_match:
            subject = subject_match.group(1).strip()
            body = body_match.group(1).strip()
        else:
            rendered = fallback_render_template(
                template_subject=template_subject,
                template_body=template_body,
                first_name=first_name,
                company=company,
                offer=offer,
                audience=audience,
                call_to_action=call_to_action,
                intro_para=intro_para,
                unsubscribe_url=unsubscribe_url,
            )
            subject = rendered["subject"]
            body = rendered["body"]

        return {
            "subject": subject,
            "body": body,
        }

    except Exception as e:
        print(f"AI generation failed: {repr(e)}")

        return fallback_render_template(
            template_subject=template_subject,
            template_body=template_body,
            first_name=first_name,
            company=company,
            offer=offer,
            audience=audience,
            call_to_action=call_to_action,
            intro_para=intro_para,
            unsubscribe_url=unsubscribe_url,
        )