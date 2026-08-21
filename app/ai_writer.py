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
        return text[:3000]

    except Exception as e:
        print(f"Website fetch failed for {website}: {repr(e)}")
        return ""


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

    if company and website_text:
        return (
            f"I wanted to reach out because businesses like {company} often rely on clear follow-up, organized communication, "
            f"and a reliable process for keeping track of prospects, customers, and next steps."
        )

    if company and industry:
        return (
            f"I wanted to reach out because {industry.lower()} businesses like {company} often have leads, follow-ups, "
            f"and customer communication happening across too many places."
        )

    if company:
        return (
            f"I wanted to reach out because businesses like {company} often have leads, follow-ups, and customer communication "
            f"spread across too many places."
        )

    return (
        "I wanted to reach out because many growing businesses have leads, follow-ups, and customer communication spread across "
        "too many places."
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
        return f"I took a quick look at {company} and wanted to reach out with a practical CRM idea."

    if company and industry:
        return f"I wanted to reach out with a practical CRM idea for {company}."

    if company:
        return f"I wanted to reach out with a practical idea for {company}."

    return "I wanted to reach out with a practical CRM idea."


def fallback_render_template(
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

        "{{ personal_line }}": personal_line or "",
        "{personal line}": personal_line or "",

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
                "Use these approved edited examples to match voice, structure, length, warmth, and CTA style. "
                "Do not copy them word-for-word.\n\n"
                + "\n\n---\n\n".join(example_texts)
            )

    return "\n\n".join(parts).strip()


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

    website_text = fetch_website_text(website)

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

    if not GEMINI_API_KEY:
        return fallback_render_template(
            template_subject=template_subject,
            template_body=template_body,
            first_name=first_name,
            company=company,
            offer=offer,
            audience=audience,
            call_to_action=call_to_action,
            intro_para=fallback_intro,
            personal_line=fallback_personal_line,
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
You are writing a concise, natural business outreach email.

Goal:
Create a polished email draft using the template and contact details below.

Most important instruction:
The placeholder {{ intro_para }} should become a useful personalized opening paragraph, not a generic sentence.

Intro paragraph rules:
- Write a specific 1–2 sentence personalized opening paragraph.
- Do not use the generic phrase "I noticed your work with [company] and wanted to reach out with a practical idea."
- If website text is available, use it to infer a relevant business context such as customer communication, scheduling, estimates, lead follow-up, service operations, sales process, or growth needs.
- Do not invent facts, awards, locations, client names, certifications, or services that are not supported by the contact/company/website data.
- If the website text is thin or unclear, use a natural industry-relevant opener without pretending to know specifics.
- The intro should connect naturally to the CRM offer.

General email rules:
- Sound human, warm, direct, and not overly salesy.
- Keep the email brief.
- Avoid hype and buzzwords.
- Avoid fake compliments.
- Avoid "I hope this email finds you well."
- Avoid overly formal language.
- Use short paragraphs.
- Preserve the user's intended offer and call to action.
- Use the preferred voice profile and examples when provided.
- Return only the subject and body using the exact format requested below.

Contact:
First name: {first_name}
Company: {company}
Industry: {industry}
Role/title: {role}
Email: {email}
Website: {website}

Website text excerpt:
{website_text if website_text else "No useful website text was available."}

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

Fallback intro if no useful company details are available:
{fallback_intro}

Fallback personal line if needed:
{fallback_personal_line}

Style profile and approved examples:
{style_guidance if style_guidance else "No custom style profile provided."}

Unsubscribe URL:
{unsubscribe_url}

Template field instructions:
- Replace {{ first_name }} with the contact first name.
- Replace {{ company }} with the company name.
- Replace {{ audience }} with the campaign audience.
- Replace {{ offer }} with the campaign offer.
- Replace {{ call_to_action }} with the call to action.
- Replace {{ intro_para }} with the personalized opening paragraph.
- Replace {{ personal_line }} with a short personalized line if used.
- Replace {{ unsubscribe_url }} with the unsubscribe URL if used.
- Also support the single-brace versions like {{first name}}, {{company}}, and {{intro para}} if they appear.

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

        subject = extract_section(text, "SUBJECT", "BODY")
        body = extract_section(text, "BODY")

        if not subject or not body:
            rendered = fallback_render_template(
                template_subject=template_subject,
                template_body=template_body,
                first_name=first_name,
                company=company,
                offer=offer,
                audience=audience,
                call_to_action=call_to_action,
                intro_para=fallback_intro,
                personal_line=fallback_personal_line,
                unsubscribe_url=unsubscribe_url,
            )

            subject = rendered["subject"]
            body = rendered["body"]

        return {
            "subject": subject.strip(),
            "body": body.strip(),
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
            intro_para=fallback_intro,
            personal_line=fallback_personal_line,
            unsubscribe_url=unsubscribe_url,
        )