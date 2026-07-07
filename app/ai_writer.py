import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)
else:
    client = None


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
    Generates only one personalization line.
    This keeps the email mostly template-based and safer.
    """

    company_display = company or "your team"
    industry_display = industry or "your industry"

    fallback_line = (
        f"I noticed {company_display} is in {industry_display}, "
        f"so having a clean follow-up process can make it easier to manage leads, clients, and opportunities."
    )

    if client is None:
        return fallback_line

    prompt = f"""
Write one short, professional personalization sentence for a B2B outreach email.

Rules:
- Write only one sentence.
- Do not make up specific facts.
- Do not say "I was impressed by".
- Do not pretend to know the company personally.
- Do not mention fake research.
- Keep it natural and useful.
- Mention the company or industry if appropriate.
- No markdown.
- No quotes.

Recipient/company info:
First name: {first_name}
Company: {company}
Industry: {industry}
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
    Renders a mostly fixed email template and inserts one AI-generated personal line.
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
    }