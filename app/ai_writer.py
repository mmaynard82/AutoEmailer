import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)
else:
    client = None


def generate_sales_email(
    first_name: str,
    company: str,
    industry: str,
    role: str,
    offer: str,
    audience: str,
    tone: str,
    call_to_action: str,
    cadence_step_name: str = "Intro email",
    cadence_step_purpose: str = "Introduce the offer",
    step_number: int = 1,
) -> dict:
    """
    Generates one outbound email.

    If OpenAI is not configured, this creates a basic safe draft
    so the app can still work during development.
    """

    company_display = company or "your team"
    first_name_display = first_name or "there"

    fallback_subject = f"Quick question for {company_display}"

    if step_number == 1:
        fallback_body = f"""Hi {first_name_display},

I’m reaching out because we help {audience} improve CRM follow-up, sales visibility, and client communication.

{offer}

Would it be worth a quick conversation to see if this could be useful for {company_display}?

Best,
Evolution CRM"""
    elif step_number == 2:
        fallback_body = f"""Hi {first_name_display},

I wanted to quickly follow up on my earlier note.

The main reason I reached out is that many teams have a CRM in place, but still struggle with missed follow-ups, messy records, unclear reporting, or inconsistent use by staff.

{offer}

Would a quick CRM health check be useful?

Best,
Evolution CRM"""
    elif step_number == 3:
        fallback_body = f"""Hi {first_name_display},

One thing we often see with {audience} is that the CRM itself is not always the problem. The issue is usually how the system is structured, how follow-up is tracked, and whether the team actually trusts the data.

A short review can usually identify a few quick wins.

{call_to_action}

Best,
Evolution CRM"""
    else:
        fallback_body = f"""Hi {first_name_display},

I do not want to keep following up if this is not relevant.

Should I close the loop, or would it be worth keeping {company_display} on the list for a future CRM review?

Best,
Evolution CRM"""

    if client is None:
        return {
            "subject": fallback_subject,
            "body": fallback_body,
        }

    prompt = f"""
You are writing a compliant, professional B2B outbound email.

Rules:
- Do not use deceptive claims.
- Do not pretend there is a prior relationship.
- Keep it concise.
- Make it useful, not spammy.
- Include a clear reason for outreach.
- Include a simple call to action.
- Do not over-personalize.
- Do not include fake case studies or fabricated facts.
- Do not include markdown.
- Do not use pushy or manipulative language.

Recipient:
First name: {first_name}
Company: {company}
Industry: {industry}
Role: {role}

Campaign:
Audience: {audience}
Offer: {offer}
Tone: {tone}
CTA: {call_to_action}

Cadence Step:
Step number: {step_number}
Step name: {cadence_step_name}
Step purpose: {cadence_step_purpose}

Return exactly this format:

SUBJECT:
[subject line]

BODY:
[email body]
"""

    try:
        response = client.responses.create(
            model="gpt-4o-mini",
            input=prompt,
            temperature=0.6,
        )

        text = response.output_text.strip()

        if "SUBJECT:" in text and "BODY:" in text:
            subject_part = text.split("SUBJECT:", 1)[1].split("BODY:", 1)[0]
            body_part = text.split("BODY:", 1)[1]

            subject = subject_part.strip()
            body = body_part.strip()
        else:
            subject = fallback_subject
            body = text

        return {
            "subject": subject,
            "body": body,
        }

    except Exception as e:
        print(f"AI generation failed: {e}")

        return {
            "subject": fallback_subject,
            "body": fallback_body,
        }