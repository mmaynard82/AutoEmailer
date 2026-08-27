import os
import hmac
import hashlib
import json
import requests
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import quote

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, Request, Form, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from sqlmodel import Session, select

from app.database import create_db_and_tables, get_session
from app.models import (
    Organization,
    AppUser,
    Contact,
    Campaign,
    CadenceStep,
    EmailDraft,
    EmailEvent,
    StyleExample,
    Suppression,
)
from app.ai_writer import render_template_email
from app.ses_sender import send_email_via_ses
from app.hubspot_client import (
    get_hubspot_contacts,
    export_contact_to_hubspot,
    update_hubspot_contact_dnc_by_email,
)


load_dotenv()

app = FastAPI(title="AI Emailer MVP")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
SECRET_KEY = os.getenv("SECRET_KEY", "local-dev-secret")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
DEFAULT_SES_FROM_EMAIL = os.getenv("SES_FROM_EMAIL")
CRON_SECRET = os.getenv("CRON_SECRET", "")
SES_EVENT_WEBHOOK_SECRET = os.getenv("SES_EVENT_WEBHOOK_SECRET", "")


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


def redirect_with_message(url: str, message: str):
    separator = "&" if "?" in url else "?"
    return RedirectResponse(
        url=f"{url}{separator}message={quote(message)}",
        status_code=303,
    )


def make_auth_token() -> str:
    message = ADMIN_PASSWORD.encode("utf-8")
    secret = SECRET_KEY.encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def is_logged_in(request: Request) -> bool:
    token = request.cookies.get("ai_emailer_auth")
    expected_token = make_auth_token()

    if not token:
        return False

    return hmac.compare_digest(token, expected_token)


def current_user_email(request: Request) -> str:
    return request.cookies.get("ai_emailer_user", "")


def is_admin(request: Request) -> bool:
    return current_user_email(request) == "admin"


def require_dashboard_login(request: Request):
    if not is_logged_in(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


def require_admin_login(request: Request):
    require_dashboard_login(request)

    if not is_admin(request):
        raise HTTPException(status_code=403, detail="Admin access required.")


def get_current_app_user(
    request: Request,
    session: Session,
) -> Optional[AppUser]:
    user_email = current_user_email(request)

    if not user_email or user_email == "admin":
        return None

    return session.exec(
        select(AppUser).where(AppUser.email == user_email)
    ).first()


def get_current_organization_id(
    request: Request,
    session: Session,
) -> Optional[int]:
    if is_admin(request):
        return None

    user = get_current_app_user(request, session)

    if not user or not user.organization_id:
        raise HTTPException(status_code=403, detail="No workspace assigned.")

    return user.organization_id


def user_can_access_campaign(
    request: Request,
    session: Session,
    campaign: Campaign,
) -> bool:
    if is_admin(request):
        return True

    org_id = get_current_organization_id(request, session)
    return campaign.organization_id == org_id


def get_campaign_or_404_for_user(
    campaign_id: int,
    request: Request,
    session: Session,
) -> Campaign:
    campaign = session.get(Campaign, campaign_id)

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    if not user_can_access_campaign(request, session, campaign):
        raise HTTPException(
            status_code=403,
            detail="You do not have access to this workspace.",
        )

    return campaign


def require_contact_access(
    contact: Contact,
    request: Request,
    session: Session,
):
    if is_admin(request):
        return

    org_id = get_current_organization_id(request, session)

    if contact.organization_id != org_id:
        raise HTTPException(status_code=403, detail="Contact access denied.")


def require_draft_access(
    draft: EmailDraft,
    request: Request,
    session: Session,
):
    if is_admin(request):
        return

    org_id = get_current_organization_id(request, session)

    if draft.organization_id != org_id:
        raise HTTPException(status_code=403, detail="Draft access denied.")


def require_step_access(
    step: CadenceStep,
    request: Request,
    session: Session,
):
    if is_admin(request):
        return

    org_id = get_current_organization_id(request, session)

    if step.organization_id != org_id:
        raise HTTPException(status_code=403, detail="Email step access denied.")


def get_sender_email_for_organization(
    organization_id: Optional[int],
    session: Session,
) -> Optional[str]:
    if organization_id:
        organization = session.get(Organization, organization_id)

        if organization and organization.sender_email:
            return organization.sender_email.strip().lower()

    return DEFAULT_SES_FROM_EMAIL


def get_reply_to_email_for_sender(sender_email: str) -> str:
    if not sender_email:
        return sender_email

    sender_email = sender_email.strip().lower()

    if "@mail.evolutioncrm.us" in sender_email:
        return sender_email.replace("@mail.evolutioncrm.us", "@evolutioncrm.us")

    return sender_email


def get_style_examples_for_organization(
    organization_id: Optional[int],
    session: Session,
    limit: int = 5,
) -> list[dict]:
    if not organization_id:
        return []

    examples = session.exec(
        select(StyleExample)
        .where(StyleExample.organization_id == organization_id)
        .order_by(StyleExample.created_at.desc())
    ).all()

    examples = examples[:limit]

    return [
        {
            "subject": example.subject,
            "body": example.body,
        }
        for example in examples
    ]


def safe_update_hubspot_dnc(email: str):
    if not email:
        return {
            "status": "skipped",
            "reason": "Missing email",
        }

    try:
        result = update_hubspot_contact_dnc_by_email(email)
        print(f"HubSpot DNC update result for {email}: {result}")
        return result
    except Exception as e:
        print(f"HubSpot DNC update failed for {email}: {repr(e)}")
        return {
            "status": "failed",
            "error": repr(e),
        }


def make_unsubscribe_token(contact_id: int, email: str) -> str:
    message = f"{contact_id}:{email.lower()}".encode("utf-8")
    secret = SECRET_KEY.encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify_unsubscribe_token(contact_id: int, email: str, token: str) -> bool:
    expected_token = make_unsubscribe_token(contact_id, email)
    return hmac.compare_digest(expected_token, token)


def build_unsubscribe_url(contact: Contact) -> str:
    token = make_unsubscribe_token(contact.id, contact.email)
    return f"{APP_BASE_URL}/unsubscribe/{contact.id}/{token}"


def get_message_tag(tags: dict, key: str) -> Optional[int]:
    value = tags.get(key)

    if isinstance(value, list):
        value = value[0] if value else None

    if value in [None, "", "None"]:
        return None

    try:
        return int(value)
    except Exception:
        return None


def parse_ses_event_time(value: Optional[str]) -> datetime:
    if not value:
        return datetime.utcnow()

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow()


def get_or_create_email_event(
    session: Session,
    event_type: str,
    message_id: Optional[str],
    recipient_email: Optional[str],
    campaign_id: Optional[int],
    contact_id: Optional[int],
    draft_id: Optional[int],
    organization_id: Optional[int],
    event_time: datetime,
    raw_event: dict,
    bounce_type: Optional[str] = None,
    complaint_feedback_type: Optional[str] = None,
    link_url: Optional[str] = None,
) -> EmailEvent:
    existing = session.exec(
        select(EmailEvent).where(
            EmailEvent.message_id == message_id,
            EmailEvent.event_type == event_type,
            EmailEvent.recipient_email == recipient_email,
            EmailEvent.draft_id == draft_id,
            EmailEvent.event_time == event_time,
        )
    ).first()

    if existing:
        return existing

    email_event = EmailEvent(
        organization_id=organization_id,
        campaign_id=campaign_id,
        contact_id=contact_id,
        draft_id=draft_id,
        message_id=message_id,
        event_type=event_type,
        recipient_email=recipient_email,
        bounce_type=bounce_type,
        complaint_feedback_type=complaint_feedback_type,
        link_url=link_url,
        raw_event=json.dumps(raw_event)[:10000],
        event_time=event_time,
    )

    session.add(email_event)
    session.commit()
    session.refresh(email_event)

    return email_event


def suppress_contact_from_event(
    session: Session,
    contact_id: Optional[int],
    organization_id: Optional[int],
    recipient_email: Optional[str],
    reason: str,
):
    contact = session.get(Contact, contact_id) if contact_id else None

    if not contact and recipient_email:
        contact = session.exec(
            select(Contact).where(
                Contact.email == recipient_email.lower(),
                Contact.organization_id == organization_id,
            )
        ).first()

    if contact:
        contact.suppressed = True
        session.add(contact)

    if recipient_email:
        existing = session.exec(
            select(Suppression).where(
                Suppression.email == recipient_email.lower(),
                Suppression.organization_id == organization_id,
            )
        ).first()

        if not existing:
            suppression = Suppression(
                organization_id=organization_id,
                email=recipient_email.lower(),
                reason=reason,
            )
            session.add(suppression)

    session.commit()


def calculate_campaign_email_performance(
    campaign_id: int,
    session: Session,
) -> dict:
    events = session.exec(
        select(EmailEvent).where(EmailEvent.campaign_id == campaign_id)
    ).all()

    sent_drafts = session.exec(
        select(EmailDraft).where(
            EmailDraft.campaign_id == campaign_id,
            EmailDraft.sent == True,
        )
    ).all()

    sent_count = len(sent_drafts)

    delivery_events = [e for e in events if e.event_type == "Delivery"]
    bounce_events = [e for e in events if e.event_type == "Bounce"]
    complaint_events = [e for e in events if e.event_type == "Complaint"]
    open_events = [e for e in events if e.event_type == "Open"]
    click_events = [e for e in events if e.event_type == "Click"]
    reject_events = [e for e in events if e.event_type == "Reject"]
    delivery_delay_events = [e for e in events if e.event_type == "DeliveryDelay"]
    send_events = [e for e in events if e.event_type == "Send"]

    delivered_unique = len(set(e.draft_id or e.message_id or e.recipient_email for e in delivery_events))
    bounced_unique = len(set(e.draft_id or e.message_id or e.recipient_email for e in bounce_events))
    complaint_unique = len(set(e.draft_id or e.message_id or e.recipient_email for e in complaint_events))
    opened_unique = len(set(e.draft_id or e.message_id or e.recipient_email for e in open_events))
    clicked_unique = len(set(e.draft_id or e.message_id or e.recipient_email for e in click_events))

    def rate(numerator: int, denominator: int) -> float:
        if not denominator:
            return 0.0
        return round((numerator / denominator) * 100, 1)

    latest_events = sorted(events, key=lambda e: e.event_time, reverse=True)[:20]

    return {
        "sent": sent_count,
        "ses_send_events": len(send_events),
        "delivered": delivered_unique,
        "bounced": bounced_unique,
        "complaints": complaint_unique,
        "opens": len(open_events),
        "unique_opens": opened_unique,
        "clicks": len(click_events),
        "unique_clicks": clicked_unique,
        "rejects": len(reject_events),
        "delivery_delays": len(delivery_delay_events),
        "delivery_rate": rate(delivered_unique, sent_count),
        "bounce_rate": rate(bounced_unique, sent_count),
        "complaint_rate": rate(complaint_unique, sent_count),
        "open_rate": rate(opened_unique, delivered_unique or sent_count),
        "click_rate": rate(clicked_unique, delivered_unique or sent_count),
        "latest_events": latest_events,
    }


@app.get("/login")
def login_page(request: Request):
    if is_logged_in(request):
        return RedirectResponse(url="/dashboard", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": "",
            "demo_mode": DEMO_MODE,
        },
    )


@app.post("/login")
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    if password == ADMIN_PASSWORD:
        response = RedirectResponse(url="/dashboard", status_code=303)
        response.set_cookie(
            key="ai_emailer_auth",
            value=make_auth_token(),
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 8,
        )
        response.set_cookie(
            key="ai_emailer_user",
            value="admin",
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 8,
        )
        return response

    email_clean = email.strip().lower()

    if not email_clean:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Enter your email and password.",
                "demo_mode": DEMO_MODE,
            },
        )

    user = session.exec(
        select(AppUser).where(AppUser.email == email_clean)
    ).first()

    if not user or not user.is_active:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Incorrect email or password.",
                "demo_mode": DEMO_MODE,
            },
        )

    if not pwd_context.verify(password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Incorrect email or password.",
                "demo_mode": DEMO_MODE,
            },
        )

    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        key="ai_emailer_auth",
        value=make_auth_token(),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 8,
    )
    response.set_cookie(
        key="ai_emailer_user",
        value=user.email,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 8,
    )

    return response


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("ai_emailer_auth")
    response.delete_cookie("ai_emailer_user")
    return response


@app.get("/admin/workspaces/new", response_class=HTMLResponse)
def new_workspace_page(
    request: Request,
    message: str = "",
):
    require_admin_login(request)

    return HTMLResponse(
        content=f"""
        <html>
            <head>
                <title>Create Workspace</title>
                <style>
                    body {{
                        font-family: Arial, sans-serif;
                        background: #f6f7f9;
                        padding: 40px;
                    }}
                    .card {{
                        background: white;
                        max-width: 600px;
                        padding: 28px;
                        border-radius: 12px;
                        box-shadow: 0 1px 6px rgba(0,0,0,0.10);
                    }}
                    label {{
                        display: block;
                        font-weight: bold;
                        margin-top: 14px;
                    }}
                    input, textarea {{
                        width: 100%;
                        padding: 10px;
                        margin-top: 5px;
                        box-sizing: border-box;
                    }}
                    button {{
                        margin-top: 18px;
                        padding: 10px 14px;
                        background: #1f5eff;
                        color: white;
                        border: none;
                        border-radius: 6px;
                        font-weight: bold;
                        cursor: pointer;
                    }}
                    a {{
                        color: #1f5eff;
                    }}
                    .message {{
                        background: #ecfdf5;
                        border-left: 5px solid #047857;
                        padding: 12px;
                        margin-bottom: 18px;
                    }}
                    .muted {{
                        color: #666;
                        font-size: 13px;
                        line-height: 1.4;
                    }}
                </style>
            </head>
            <body>
                <div class="card">
                    <p><a href="/dashboard">Back to Dashboard</a></p>
                    <h2>Create Workspace</h2>

                    {f'<div class="message">{message}</div>' if message else ''}

                    <form method="post" action="/admin/workspaces">
                        <label>Workspace Name</label>
                        <input type="text" name="name" required placeholder="Example: Evan Burns Pilot">

                        <label>Sender Email</label>
                        <input type="email" name="sender_email" required placeholder="evan.burns@mail.evolutioncrm.us">

                        <p class="muted">
                            This is the visible From address used by AWS SES for this workspace.
                            Replies will automatically route to the matching @evolutioncrm.us address when using @mail.evolutioncrm.us.
                        </p>

                        <label>Notes</label>
                        <textarea name="notes" rows="4" placeholder="Optional notes about this pilot/client"></textarea>

                        <button type="submit">Create Workspace</button>
                    </form>
                </div>
            </body>
        </html>
        """,
        status_code=200,
    )


@app.post("/admin/workspaces")
def create_workspace(
    request: Request,
    name: str = Form(...),
    sender_email: str = Form(...),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    require_admin_login(request)

    workspace = Organization(
        name=name.strip(),
        sender_email=sender_email.strip().lower(),
        notes=notes.strip() or None,
    )

    session.add(workspace)
    session.commit()
    session.refresh(workspace)

    reply_to_email = get_reply_to_email_for_sender(workspace.sender_email)

    return redirect_with_message(
        "/admin/workspaces/new",
        f"Workspace created: {workspace.name}. Sender: {workspace.sender_email}. Reply-To: {reply_to_email}.",
    )


@app.get("/admin/pilot-users/new", response_class=HTMLResponse)
def new_pilot_user_page(
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
):
    require_admin_login(request)

    organizations = session.exec(select(Organization)).all()

    options_html = ""

    for organization in organizations:
        sender_display = organization.sender_email or "No sender set"
        options_html += (
            f'<option value="{organization.id}">'
            f'{organization.name} - {sender_display}'
            f'</option>'
        )

    if not options_html:
        options_html = '<option value="">Create a workspace first</option>'

    return HTMLResponse(
        content=f"""
        <html>
            <head>
                <title>Create Pilot User</title>
                <style>
                    body {{
                        font-family: Arial, sans-serif;
                        background: #f6f7f9;
                        padding: 40px;
                    }}
                    .card {{
                        background: white;
                        max-width: 600px;
                        padding: 28px;
                        border-radius: 12px;
                        box-shadow: 0 1px 6px rgba(0,0,0,0.10);
                    }}
                    label {{
                        display: block;
                        font-weight: bold;
                        margin-top: 14px;
                    }}
                    input, select {{
                        width: 100%;
                        padding: 10px;
                        margin-top: 5px;
                        box-sizing: border-box;
                    }}
                    button {{
                        margin-top: 18px;
                        padding: 10px 14px;
                        background: #1f5eff;
                        color: white;
                        border: none;
                        border-radius: 6px;
                        font-weight: bold;
                        cursor: pointer;
                    }}
                    a {{
                        color: #1f5eff;
                    }}
                    .message {{
                        background: #ecfdf5;
                        border-left: 5px solid #047857;
                        padding: 12px;
                        margin-bottom: 18px;
                    }}
                </style>
            </head>
            <body>
                <div class="card">
                    <p><a href="/dashboard">Back to Dashboard</a></p>
                    <p><a href="/admin/workspaces/new">Create Workspace</a></p>

                    <h2>Create Pilot User</h2>

                    {f'<div class="message">{message}</div>' if message else ''}

                    <form method="post" action="/admin/pilot-users">
                        <label>Workspace</label>
                        <select name="organization_id" required>
                            {options_html}
                        </select>

                        <label>Name</label>
                        <input type="text" name="name" placeholder="Pilot User">

                        <label>Email</label>
                        <input type="email" name="email" required placeholder="pilot@example.com">

                        <label>Password</label>
                        <input type="text" name="password" required placeholder="temporary-password">

                        <button type="submit">Create Pilot User</button>
                    </form>
                </div>
            </body>
        </html>
        """,
        status_code=200,
    )


@app.post("/admin/pilot-users")
def create_pilot_user(
    request: Request,
    organization_id: int = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form("Pilot User"),
    session: Session = Depends(get_session),
):
    require_admin_login(request)

    organization = session.get(Organization, organization_id)

    if not organization:
        return redirect_with_message(
            "/admin/pilot-users/new",
            "Workspace not found. Create a workspace first.",
        )

    email_clean = email.strip().lower()

    existing = session.exec(
        select(AppUser).where(AppUser.email == email_clean)
    ).first()

    if existing:
        return redirect_with_message(
            "/admin/pilot-users/new",
            "Pilot user already exists.",
        )

    user = AppUser(
        organization_id=organization.id,
        email=email_clean,
        password_hash=pwd_context.hash(password),
        name=name.strip() or "Pilot User",
        role="pilot",
        is_active=True,
    )

    session.add(user)
    session.commit()

    return redirect_with_message(
        "/admin/pilot-users/new",
        f"Pilot user created for {email_clean} in workspace {organization.name}.",
    )


@app.get("/")
def home(request: Request):
    dashboard_link = "/dashboard" if is_logged_in(request) else "/login"

    return {
        "message": "AI Emailer MVP is running",
        "dashboard": dashboard_link,
        "demo_mode": DEMO_MODE,
        "logged_in_as": current_user_email(request),
    }


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "demo_mode": DEMO_MODE,
    }


@app.get("/debug/aws-env")
def debug_aws_env(request: Request):
    require_admin_login(request)

    access_key = os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    region = os.getenv("AWS_REGION")
    sender = os.getenv("SES_FROM_EMAIL")
    cron_secret = os.getenv("CRON_SECRET")
    ses_config_set = os.getenv("SES_CONFIGURATION_SET")
    ses_webhook_secret = os.getenv("SES_EVENT_WEBHOOK_SECRET")

    return {
        "AWS_ACCESS_KEY_ID_present": bool(access_key),
        "AWS_ACCESS_KEY_ID_starts_with": access_key[:4] if access_key else None,
        "AWS_SECRET_ACCESS_KEY_present": bool(secret_key),
        "AWS_SECRET_ACCESS_KEY_length": len(secret_key) if secret_key else 0,
        "AWS_REGION": region,
        "SES_FROM_EMAIL": sender,
        "CRON_SECRET_present": bool(cron_secret),
        "SES_CONFIGURATION_SET": ses_config_set,
        "SES_EVENT_WEBHOOK_SECRET_present": bool(ses_webhook_secret),
    }


@app.post("/webhooks/ses-events")
async def ses_events_webhook(
    request: Request,
    secret: str = Query(""),
    session: Session = Depends(get_session),
):
    if SES_EVENT_WEBHOOK_SECRET:
        if not hmac.compare_digest(secret, SES_EVENT_WEBHOOK_SECRET):
            raise HTTPException(status_code=403, detail="Invalid SES webhook secret.")

    payload = await request.json()

    sns_message_type = request.headers.get("x-amz-sns-message-type") or payload.get("Type")

    print(f"SES/SNS webhook received type: {sns_message_type}")

    if sns_message_type == "SubscriptionConfirmation":
        subscribe_url = payload.get("SubscribeURL")

        if not subscribe_url:
            raise HTTPException(status_code=400, detail="Missing SubscribeURL.")

        try:
            response = requests.get(subscribe_url, timeout=10)
            print(f"SNS subscription confirmation status: {response.status_code}")
        except Exception as e:
            print(f"SNS subscription confirmation failed: {repr(e)}")
            raise HTTPException(status_code=500, detail="Could not confirm SNS subscription.")

        return {
            "status": "subscription_confirmed",
        }

    if sns_message_type != "Notification":
        return {
            "status": "ignored",
            "message_type": sns_message_type,
        }

    message_raw = payload.get("Message")

    if not message_raw:
        return {
            "status": "ignored",
            "reason": "Missing Message.",
        }

    try:
        ses_event = json.loads(message_raw)
    except Exception:
        print(f"Could not parse SNS Message JSON: {message_raw[:500]}")
        return {
            "status": "ignored",
            "reason": "Message was not valid JSON.",
        }

    event_type = ses_event.get("eventType") or ses_event.get("notificationType")

    if not event_type:
        return {
            "status": "ignored",
            "reason": "Missing SES event type.",
        }

    mail = ses_event.get("mail", {})
    message_id = mail.get("messageId")
    tags = mail.get("tags", {}) or {}

    campaign_id = get_message_tag(tags, "campaign_id")
    contact_id = get_message_tag(tags, "contact_id")
    draft_id = get_message_tag(tags, "draft_id")
    organization_id = get_message_tag(tags, "organization_id")

    recipients = mail.get("destination") or []
    recipient_email = recipients[0].lower() if recipients else None

    event_time = parse_ses_event_time(mail.get("timestamp"))

    bounce_type = None
    complaint_feedback_type = None
    link_url = None

    if event_type == "Delivery":
        delivery = ses_event.get("delivery", {})
        event_time = parse_ses_event_time(delivery.get("timestamp") or mail.get("timestamp"))

    elif event_type == "Bounce":
        bounce = ses_event.get("bounce", {})
        bounce_type = bounce.get("bounceType")
        event_time = parse_ses_event_time(bounce.get("timestamp") or mail.get("timestamp"))

        bounced_recipients = bounce.get("bouncedRecipients") or []

        if bounced_recipients:
            recipient_email = bounced_recipients[0].get("emailAddress", recipient_email)
            if recipient_email:
                recipient_email = recipient_email.lower()

    elif event_type == "Complaint":
        complaint = ses_event.get("complaint", {})
        complaint_feedback_type = complaint.get("complaintFeedbackType")
        event_time = parse_ses_event_time(complaint.get("timestamp") or mail.get("timestamp"))

        complained_recipients = complaint.get("complainedRecipients") or []

        if complained_recipients:
            recipient_email = complained_recipients[0].get("emailAddress", recipient_email)
            if recipient_email:
                recipient_email = recipient_email.lower()

    elif event_type == "Open":
        open_event = ses_event.get("open", {})
        event_time = parse_ses_event_time(open_event.get("timestamp") or mail.get("timestamp"))

    elif event_type == "Click":
        click = ses_event.get("click", {})
        link_url = click.get("link")
        event_time = parse_ses_event_time(click.get("timestamp") or mail.get("timestamp"))

    elif event_type == "Reject":
        reject = ses_event.get("reject", {})
        event_time = parse_ses_event_time(reject.get("timestamp") or mail.get("timestamp"))

    elif event_type == "DeliveryDelay":
        delay = ses_event.get("deliveryDelay", {})
        event_time = parse_ses_event_time(delay.get("timestamp") or mail.get("timestamp"))

    saved_event = get_or_create_email_event(
        session=session,
        event_type=event_type,
        message_id=message_id,
        recipient_email=recipient_email,
        campaign_id=campaign_id,
        contact_id=contact_id,
        draft_id=draft_id,
        organization_id=organization_id,
        event_time=event_time,
        raw_event=ses_event,
        bounce_type=bounce_type,
        complaint_feedback_type=complaint_feedback_type,
        link_url=link_url,
    )

    if event_type == "Bounce":
        suppress_contact_from_event(
            session=session,
            contact_id=contact_id,
            organization_id=organization_id,
            recipient_email=recipient_email,
            reason=f"SES bounce: {bounce_type or 'unknown'}",
        )

    if event_type == "Complaint":
        suppress_contact_from_event(
            session=session,
            contact_id=contact_id,
            organization_id=organization_id,
            recipient_email=recipient_email,
            reason=f"SES complaint: {complaint_feedback_type or 'unknown'}",
        )

    return {
        "status": "saved",
        "event_id": saved_event.id,
        "event_type": event_type,
        "campaign_id": campaign_id,
        "contact_id": contact_id,
        "draft_id": draft_id,
        "recipient_email": recipient_email,
    }


@app.get("/unsubscribe/{contact_id}/{token}", response_class=HTMLResponse)
def unsubscribe_via_link(
    contact_id: int,
    token: str,
    session: Session = Depends(get_session),
):
    contact = session.get(Contact, contact_id)

    if not contact:
        return HTMLResponse(
            content="""
            <html>
                <body style="font-family: Arial; padding: 40px;">
                    <h2>Unsubscribe link not found</h2>
                    <p>We could not find this contact record.</p>
                </body>
            </html>
            """,
            status_code=404,
        )

    if not verify_unsubscribe_token(contact.id, contact.email, token):
        return HTMLResponse(
            content="""
            <html>
                <body style="font-family: Arial; padding: 40px;">
                    <h2>Invalid unsubscribe link</h2>
                    <p>This unsubscribe link is not valid.</p>
                </body>
            </html>
            """,
            status_code=400,
        )

    contact.unsubscribed = True
    contact.suppressed = True

    existing = session.exec(
        select(Suppression).where(
            Suppression.email == contact.email,
            Suppression.organization_id == contact.organization_id,
        )
    ).first()

    if not existing:
        suppression = Suppression(
            organization_id=contact.organization_id,
            email=contact.email,
            reason="unsubscribe link",
        )
        session.add(suppression)

    session.add(contact)
    session.commit()

    safe_update_hubspot_dnc(contact.email)

    return HTMLResponse(
        content=f"""
        <html>
            <body style="font-family: Arial; padding: 40px; background: #f6f7f9;">
                <div style="background: white; padding: 30px; border-radius: 10px; max-width: 600px;">
                    <h2>You have been unsubscribed</h2>
                    <p>{contact.email} has been removed from future outreach.</p>
                    <p>You can close this page.</p>
                </div>
            </body>
        </html>
        """,
        status_code=200,
    )


def safe_send_email(
    to_email: str,
    subject: str,
    body: str,
    from_email: Optional[str] = None,
    reply_to_email: Optional[str] = None,
    campaign_id: Optional[int] = None,
    contact_id: Optional[int] = None,
    draft_id: Optional[int] = None,
    organization_id: Optional[int] = None,
) -> dict:
    final_sender = from_email or DEFAULT_SES_FROM_EMAIL
    final_reply_to = reply_to_email or final_sender

    if not final_sender:
        raise ValueError("Missing sender email. Set workspace sender_email or SES_FROM_EMAIL.")

    if DEMO_MODE:
        print("\nDEMO MODE - Real email blocked")
        print(f"From: {final_sender}")
        print(f"Reply-To: {final_reply_to}")
        print(f"To: {to_email}")
        print(f"Subject: {subject}")
        print(f"Campaign ID: {campaign_id}")
        print(f"Contact ID: {contact_id}")
        print(f"Draft ID: {draft_id}")
        print(body)
        print("-" * 50)

        return {
            "demo_mode": True,
            "message": "Email blocked because DEMO_MODE=true",
        }

    response = send_email_via_ses(
        to_email=to_email,
        subject=subject,
        body=body,
        from_email=final_sender,
        reply_to_email=final_reply_to,
        campaign_id=campaign_id,
        contact_id=contact_id,
        draft_id=draft_id,
        organization_id=organization_id,
    )

    return {
        "demo_mode": False,
        "response": response,
    }


def build_campaign_context(
    campaign_id: int,
    request: Request,
    session: Session,
):
    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

    contacts = session.exec(
        select(Contact).where(Contact.campaign_id == campaign_id)
    ).all()

    steps = session.exec(
        select(CadenceStep).where(CadenceStep.campaign_id == campaign_id)
    ).all()

    drafts = session.exec(
        select(EmailDraft).where(EmailDraft.campaign_id == campaign_id)
    ).all()

    steps = sorted(steps, key=lambda s: (s.step_number, s.send_day))

    draft_rows = []

    for draft in drafts:
        contact = session.get(Contact, draft.contact_id)
        step = session.get(CadenceStep, draft.cadence_step_id) if draft.cadence_step_id else None

        draft_rows.append({
            "id": draft.id,
            "campaign_id": draft.campaign_id,
            "step_name": step.name if step else "",
            "step_number": draft.step_number,
            "send_day": draft.send_day,
            "to": contact.email if contact else "",
            "contact_name": f"{contact.first_name} {contact.last_name or ''}".strip() if contact else "",
            "company": contact.company if contact else "",
            "subject": draft.subject,
            "body": draft.body,
            "approved": draft.approved,
            "sent": draft.sent,
            "sent_at": draft.sent_at,
        })

    draft_rows = sorted(
        draft_rows,
        key=lambda x: (
            x["step_number"] or 0,
            x["contact_name"] or "",
        ),
    )

    stats = {
        "contacts": len(contacts),
        "active_contacts": len([c for c in contacts if not c.suppressed and not c.unsubscribed]),
        "suppressed_contacts": len([c for c in contacts if c.suppressed or c.unsubscribed]),
        "steps": len(steps),
        "drafts": len(drafts),
        "approved": len([d for d in drafts if d.approved]),
        "sent": len([d for d in drafts if d.sent]),
        "unapproved": len([d for d in drafts if not d.approved and not d.sent]),
        "automation_enabled": campaign.automation_enabled,
        "daily_send_limit": campaign.daily_send_limit,
        "automation_start_date": campaign.automation_start_date,
        "last_automation_run_at": campaign.last_automation_run_at,
    }

    return campaign, contacts, steps, draft_rows, stats


@app.get("/dashboard")
def dashboard(
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    org_id = get_current_organization_id(request, session)

    if is_admin(request):
        campaigns = session.exec(select(Campaign)).all()
        contacts = session.exec(select(Contact)).all()
        steps = session.exec(select(CadenceStep)).all()
        drafts = session.exec(select(EmailDraft)).all()
        organizations = session.exec(select(Organization)).all()
        current_organization = None
    else:
        campaigns = session.exec(
            select(Campaign).where(Campaign.organization_id == org_id)
        ).all()

        contacts = session.exec(
            select(Contact).where(Contact.organization_id == org_id)
        ).all()

        steps = session.exec(
            select(CadenceStep).where(CadenceStep.organization_id == org_id)
        ).all()

        drafts = session.exec(
            select(EmailDraft).where(EmailDraft.organization_id == org_id)
        ).all()

        organizations = []
        current_organization = session.get(Organization, org_id)

    campaign_rows = []

    for campaign in campaigns:
        campaign_contacts = [c for c in contacts if c.campaign_id == campaign.id]
        campaign_steps = [s for s in steps if s.campaign_id == campaign.id]
        campaign_drafts = [d for d in drafts if d.campaign_id == campaign.id]

        organization = (
            session.get(Organization, campaign.organization_id)
            if campaign.organization_id
            else None
        )

        sender_email = organization.sender_email if organization else ""
        reply_to_email = get_reply_to_email_for_sender(sender_email) if sender_email else ""

        campaign_rows.append({
            "id": campaign.id,
            "name": campaign.name,
            "workspace": organization.name if organization else "No workspace",
            "sender_email": sender_email,
            "reply_to_email": reply_to_email,
            "audience": campaign.audience,
            "offer": campaign.offer,
            "contacts": len(campaign_contacts),
            "steps": len(campaign_steps),
            "drafts": len(campaign_drafts),
            "approved": len([d for d in campaign_drafts if d.approved]),
            "sent": len([d for d in campaign_drafts if d.sent]),
            "unapproved": len([d for d in campaign_drafts if not d.approved and not d.sent]),
            "automation_enabled": campaign.automation_enabled,
            "daily_send_limit": campaign.daily_send_limit,
            "automation_start_date": campaign.automation_start_date,
        })

    analytics = {
        "total_campaigns": len(campaigns),
        "total_contacts": len(contacts),
        "total_steps": len(steps),
        "total_drafts": len(drafts),
        "approved_drafts": len([d for d in drafts if d.approved]),
        "sent_drafts": len([d for d in drafts if d.sent]),
    }

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "message": message,
            "demo_mode": DEMO_MODE,
            "campaigns": campaign_rows,
            "analytics": analytics,
            "organizations": organizations,
            "current_organization": current_organization,
            "current_user": current_user_email(request),
            "is_admin": is_admin(request),
        },
    )


@app.post("/dashboard/campaigns")
def dashboard_create_campaign(
    request: Request,
    name: str = Form(...),
    offer: str = Form(...),
    audience: str = Form("small businesses"),
    organization_id: Optional[int] = Form(None),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    if is_admin(request):
        final_organization_id = organization_id
    else:
        final_organization_id = get_current_organization_id(request, session)

    if not final_organization_id:
        return redirect_with_message(
            "/dashboard",
            "Create or select a workspace before creating a campaign.",
        )

    organization = session.get(Organization, final_organization_id)

    if not organization:
        return redirect_with_message(
            "/dashboard",
            "Workspace not found.",
        )

    campaign = Campaign(
        organization_id=final_organization_id,
        name=name,
        offer=offer,
        audience=audience or "small businesses",
        automation_enabled=False,
        daily_send_limit=5,
    )

    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign.id}",
        "Campaign created. Add an email step next.",
    )


@app.get("/dashboard/campaigns/{campaign_id}")
def campaign_detail(
    campaign_id: int,
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign, contacts, steps, draft_rows, stats = build_campaign_context(
        campaign_id,
        request,
        session,
    )

    organization = (
        session.get(Organization, campaign.organization_id)
        if campaign.organization_id
        else None
    )

    sender_email = (
        organization.sender_email
        if organization and organization.sender_email
        else DEFAULT_SES_FROM_EMAIL
    )

    reply_to_email = get_reply_to_email_for_sender(sender_email) if sender_email else None

    style_examples = session.exec(
        select(StyleExample)
        .where(StyleExample.organization_id == campaign.organization_id)
        .order_by(StyleExample.created_at.desc())
    ).all()

    return templates.TemplateResponse(
        request=request,
        name="campaign_detail.html",
        context={
            "message": message,
            "demo_mode": DEMO_MODE,
            "campaign": campaign,
            "organization": organization,
            "sender_email": sender_email,
            "reply_to_email": reply_to_email,
            "active_page": "overview",
            "contacts": contacts,
            "steps": steps,
            "drafts": draft_rows,
            "stats": stats,
            "style_examples": style_examples,
            "current_user": current_user_email(request),
            "is_admin": is_admin(request),
        },
    )

@app.get("/dashboard/campaigns/{campaign_id}/steps")
def campaign_steps_page(
    campaign_id: int,
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign, contacts, steps, draft_rows, stats = build_campaign_context(
        campaign_id,
        request,
        session,
    )

    organization = (
        session.get(Organization, campaign.organization_id)
        if campaign.organization_id
        else None
    )

    sender_email = (
        organization.sender_email
        if organization and organization.sender_email
        else DEFAULT_SES_FROM_EMAIL
    )

    reply_to_email = get_reply_to_email_for_sender(sender_email) if sender_email else None

    return templates.TemplateResponse(
        request=request,
        name="campaign_steps.html",
        context={
            "message": message,
            "demo_mode": DEMO_MODE,
            "campaign": campaign,
            "organization": organization,
            "sender_email": sender_email,
            "reply_to_email": reply_to_email,
            "contacts": contacts,
            "steps": steps,
            "drafts": draft_rows,
            "stats": stats,
            "active_page": "steps",
            "current_user": current_user_email(request),
            "is_admin": is_admin(request),
        },
    )
@app.get("/dashboard/campaigns/{campaign_id}/analytics")
def campaign_analytics(
    campaign_id: int,
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

    organization = (
        session.get(Organization, campaign.organization_id)
        if campaign.organization_id
        else None
    )

    sender_email = (
        organization.sender_email
        if organization and organization.sender_email
        else DEFAULT_SES_FROM_EMAIL
    )

    reply_to_email = get_reply_to_email_for_sender(sender_email) if sender_email else None

    email_performance = calculate_campaign_email_performance(
        campaign_id=campaign.id,
        session=session,
    )

    return templates.TemplateResponse(
        request=request,
        name="campaign_analytics.html",
        context={
            "message": message,
            "demo_mode": DEMO_MODE,
            "campaign": campaign,
            "organization": organization,
            "sender_email": sender_email,
            "active_page": "analytics",
            "reply_to_email": reply_to_email,
            "email_performance": email_performance,
            "current_user": current_user_email(request),
            "is_admin": is_admin(request),
        },
    )


@app.post("/dashboard/campaigns/{campaign_id}/edit")
def edit_campaign(
    campaign_id: int,
    request: Request,
    name: str = Form(...),
    audience: str = Form("small businesses"),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

    campaign.name = name
    campaign.audience = audience or "small businesses"

    session.add(campaign)
    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        "Campaign settings updated.",
    )


@app.post("/dashboard/campaigns/{campaign_id}/style-profile")
def update_workspace_style_profile(
    campaign_id: int,
    request: Request,
    brand_voice: str = Form(""),
    avoid_phrases: str = Form(""),
    preferred_cta: str = Form(""),
    signature_name: str = Form(""),
    signature_title: str = Form(""),
    signature_company: str = Form(""),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

    if not campaign.organization_id:
        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            "This campaign does not have a workspace.",
        )

    organization = session.get(Organization, campaign.organization_id)

    if not organization:
        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            "Workspace not found.",
        )

    organization.brand_voice = brand_voice.strip() or None
    organization.avoid_phrases = avoid_phrases.strip() or None
    organization.preferred_cta = preferred_cta.strip() or None
    organization.signature_name = signature_name.strip() or None
    organization.signature_title = signature_title.strip() or None
    organization.signature_company = signature_company.strip() or None

    session.add(organization)
    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        "Workspace voice profile saved. Future generated drafts will use this voice.",
    )

@app.get("/dashboard/campaigns/{campaign_id}/drafts")
def campaign_drafts_page(
    campaign_id: int,
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign, contacts, steps, draft_rows, stats = build_campaign_context(
        campaign_id,
        request,
        session,
    )

    organization = (
        session.get(Organization, campaign.organization_id)
        if campaign.organization_id
        else None
    )

    sender_email = (
        organization.sender_email
        if organization and organization.sender_email
        else DEFAULT_SES_FROM_EMAIL
    )

    reply_to_email = get_reply_to_email_for_sender(sender_email) if sender_email else None

    return templates.TemplateResponse(
        request=request,
        name="campaign_drafts.html",
        context={
            "message": message,
            "demo_mode": DEMO_MODE,
            "campaign": campaign,
            "organization": organization,
            "sender_email": sender_email,
            "reply_to_email": reply_to_email,
            "contacts": contacts,
            "steps": steps,
            "drafts": draft_rows,
            "stats": stats,
            "active_page": "drafts",
            "current_user": current_user_email(request),
            "is_admin": is_admin(request),
        },
    )

@app.post("/dashboard/campaigns/{campaign_id}/steps")
def add_campaign_step(
    campaign_id: int,
    request: Request,
    step_number: int = Form(...),
    send_day: int = Form(...),
    name: str = Form(...),
    purpose: str = Form(...),
    tone: str = Form("friendly, consultative, concise"),
    call_to_action: str = Form("Would you be open to a quick conversation?"),
    template_subject: str = Form("Quick question for {{ company }}"),
    offer: str = Form(""),
    template_body: str = Form(...),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

    if offer.strip():
        campaign.offer = offer.strip()
        session.add(campaign)

    step = CadenceStep(
        organization_id=campaign.organization_id,
        campaign_id=campaign.id,
        step_number=step_number,
        send_day=send_day,
        name=name,
        purpose=purpose,
        tone=tone,
        call_to_action=call_to_action,
        template_subject=template_subject,
        template_body=template_body,
    )

    session.add(step)
    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}/steps",
        "Email step added and campaign offer saved.",
    )
@app.post("/dashboard/steps/{step_id}/edit")
def edit_campaign_step(
    step_id: int,
    request: Request,
    step_number: int = Form(...),
    send_day: int = Form(...),
    name: str = Form(...),
    purpose: str = Form(...),
    tone: str = Form("friendly, consultative, concise"),
    call_to_action: str = Form("Would you be open to a quick conversation?"),
    template_subject: str = Form("Quick question for {{ company }}"),
    template_body: str = Form(...),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    step = session.get(CadenceStep, step_id)

    if not step:
        raise HTTPException(status_code=404, detail="Email step not found.")

    require_step_access(step, request, session)

    step.step_number = step_number
    step.send_day = send_day
    step.name = name
    step.purpose = purpose
    step.tone = tone
    step.call_to_action = call_to_action
    step.template_subject = template_subject
    step.template_body = template_body

    session.add(step)
    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{step.campaign_id}",
        "Email step updated. Existing drafts are not changed automatically.",
    )


@app.post("/dashboard/steps/{step_id}/delete")
def delete_campaign_step(
    step_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    step = session.get(CadenceStep, step_id)

    if not step:
        raise HTTPException(status_code=404, detail="Email step not found.")

    require_step_access(step, request, session)

    campaign_id = step.campaign_id

    existing_drafts = session.exec(
        select(EmailDraft).where(EmailDraft.cadence_step_id == step_id)
    ).all()

    if existing_drafts:
        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            "Cannot delete this step because drafts already exist for it. Delete the unsent drafts for this step first.",
        )

    session.delete(step)
    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        "Email step deleted.",
    )


@app.post("/dashboard/campaigns/{campaign_id}/contacts/upload")
async def upload_campaign_contacts(
    campaign_id: int,
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a CSV file.")

    df = pd.read_csv(file.file)

    required_columns = {"first_name", "email"}
    missing = required_columns - set(df.columns)

    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required columns: {missing}",
        )

    imported = 0
    skipped = 0

    for _, row in df.iterrows():
        email = str(row.get("email", "")).strip().lower()

        if not email or "@" not in email:
            skipped += 1
            continue

        existing = session.exec(
            select(Contact).where(
                Contact.email == email,
                Contact.campaign_id == campaign_id,
            )
        ).first()

        if existing:
            skipped += 1
            continue

        contact = Contact(
            organization_id=campaign.organization_id,
            campaign_id=campaign_id,
            first_name=str(row.get("first_name", "")).strip() or "there",
            last_name=str(row.get("last_name", "")).strip() if "last_name" in df.columns else None,
            email=email,
            company=str(row.get("company", "")).strip() if "company" in df.columns else None,
            industry=str(row.get("industry", "")).strip() if "industry" in df.columns else None,
            role=str(row.get("role", "")).strip() if "role" in df.columns else None,
            website=str(row.get("website", "")).strip() if "website" in df.columns else None,
            sequence_started_at=datetime.utcnow(),
        )

        session.add(contact)
        imported += 1

    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        f"Imported {imported} contacts into {campaign.name}. Skipped {skipped}.",
    )


@app.post("/dashboard/contacts/{contact_id}/unsubscribe")
def dashboard_unsubscribe_contact(
    request: Request,
    contact_id: int,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    contact = session.get(Contact, contact_id)

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found.")

    require_contact_access(contact, request, session)

    campaign_id = contact.campaign_id

    contact.unsubscribed = True
    contact.suppressed = True

    existing = session.exec(
        select(Suppression).where(
            Suppression.email == contact.email,
            Suppression.organization_id == contact.organization_id,
        )
    ).first()

    if not existing:
        suppression = Suppression(
            organization_id=contact.organization_id,
            email=contact.email,
            reason="manual unsubscribe",
        )
        session.add(suppression)

    session.add(contact)
    session.commit()

    safe_update_hubspot_dnc(contact.email)

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        "Contact unsubscribed, suppressed, and HubSpot DNC update attempted.",
    )


@app.post("/dashboard/contacts/{contact_id}/delete")
def delete_contact(
    contact_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    contact = session.get(Contact, contact_id)

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found.")

    require_contact_access(contact, request, session)

    campaign_id = contact.campaign_id
    contact_email = contact.email

    drafts = session.exec(
        select(EmailDraft).where(EmailDraft.contact_id == contact_id)
    ).all()

    draft_count = len(drafts)

    for draft in drafts:
        session.delete(draft)

    session.delete(contact)
    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        f"Deleted contact {contact_email} and {draft_count} related drafts.",
    )


@app.post("/dashboard/campaigns/{campaign_id}/hubspot/import")
def import_hubspot_to_campaign(
    campaign_id: int,
    request: Request,
    limit: int = Form(100),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

    try:
        hubspot_data = get_hubspot_contacts(limit=limit)
    except Exception as e:
        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            f"HubSpot import failed: {repr(e)}",
        )

    imported = 0
    skipped = 0

    for item in hubspot_data.get("results", []):
        props = item.get("properties", {})
        email = (props.get("email") or "").strip().lower()

        if not email or "@" not in email:
            skipped += 1
            continue

        existing = session.exec(
            select(Contact).where(
                Contact.email == email,
                Contact.campaign_id == campaign_id,
            )
        ).first()

        if existing:
            skipped += 1
            continue

        contact = Contact(
            organization_id=campaign.organization_id,
            campaign_id=campaign_id,
            first_name=(props.get("firstname") or "").strip() or "there",
            last_name=(props.get("lastname") or "").strip() or None,
            email=email,
            company=(props.get("company") or "").strip() or None,
            industry="HubSpot Import",
            role=(props.get("jobtitle") or "").strip() or None,
            website=(props.get("website") or "").strip() or None,
            sequence_started_at=datetime.utcnow(),
        )

        session.add(contact)
        imported += 1

    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        f"Imported {imported} HubSpot contacts into {campaign.name}. Skipped {skipped}.",
    )


@app.post("/dashboard/campaigns/{campaign_id}/hubspot/export")
def export_campaign_to_hubspot(
    campaign_id: int,
    request: Request,
    limit: int = Form(100),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    get_campaign_or_404_for_user(campaign_id, request, session)

    contacts = session.exec(
        select(Contact).where(
            Contact.campaign_id == campaign_id,
            Contact.unsubscribed == False,
            Contact.suppressed == False,
        )
    ).all()

    contacts = contacts[:limit]

    created = 0
    updated = 0
    skipped = 0
    failed = 0

    for contact in contacts:
        if not contact.email or "@" not in contact.email:
            skipped += 1
            continue

        try:
            result = export_contact_to_hubspot(
                email=contact.email,
                first_name=contact.first_name or "",
                last_name=contact.last_name or "",
                company=contact.company or "",
                jobtitle=contact.role or "",
                website=contact.website or "",
            )

            if result["status"] == "created":
                created += 1
            elif result["status"] == "updated":
                updated += 1
            else:
                failed += 1
                print(f"HubSpot export failed for {contact.email}: {result}")

        except Exception as e:
            failed += 1
            print(f"HubSpot export exception for {contact.email}: {repr(e)}")

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        f"HubSpot export complete. Created {created}, updated {updated}, skipped {skipped}, failed {failed}.",
    )


@app.post("/dashboard/drafts/{draft_id}/delete")
def delete_single_draft(
    draft_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    draft = session.get(EmailDraft, draft_id)

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")

    require_draft_access(draft, request, session)

    campaign_id = draft.campaign_id

    if draft.sent:
        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            "Sent drafts cannot be deleted because they are part of the send history.",
        )

    session.delete(draft)
    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        "Draft deleted. You can now regenerate it if needed.",
    )


@app.post("/dashboard/campaigns/{campaign_id}/drafts/delete-step")
def delete_unsent_drafts_for_step(
    campaign_id: int,
    request: Request,
    cadence_step_id: int = Form(...),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    get_campaign_or_404_for_user(campaign_id, request, session)

    step = session.get(CadenceStep, cadence_step_id)

    if not step or step.campaign_id != campaign_id:
        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            "Email step not found for this campaign.",
        )

    require_step_access(step, request, session)

    drafts = session.exec(
        select(EmailDraft).where(
            EmailDraft.campaign_id == campaign_id,
            EmailDraft.cadence_step_id == cadence_step_id,
            EmailDraft.sent == False,
        )
    ).all()

    deleted_count = len(drafts)

    for draft in drafts:
        session.delete(draft)

    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        f"Deleted {deleted_count} unsent drafts for Step {step.step_number} - {step.name}. You can now regenerate drafts for that step.",
    )


@app.post("/dashboard/drafts/{draft_id}/save-style-example")
def save_draft_as_style_example(
    draft_id: int,
    request: Request,
    label: str = Form("Edited draft example"),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    draft = session.get(EmailDraft, draft_id)

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")

    require_draft_access(draft, request, session)

    if not draft.organization_id:
        return redirect_with_message(
            f"/dashboard/campaigns/{draft.campaign_id}",
            "Draft does not have a workspace, so it cannot be saved as a style example.",
        )

    existing = session.exec(
        select(StyleExample).where(
            StyleExample.organization_id == draft.organization_id,
            StyleExample.draft_id == draft.id,
        )
    ).first()

    if existing:
        return redirect_with_message(
            f"/dashboard/campaigns/{draft.campaign_id}",
            "This draft is already saved as a style example.",
        )

    example = StyleExample(
        organization_id=draft.organization_id,
        campaign_id=draft.campaign_id,
        draft_id=draft.id,
        label=label.strip() or "Edited draft example",
        subject=draft.subject,
        body=draft.body,
    )

    session.add(example)
    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{draft.campaign_id}",
        "Draft saved as a style example. Future generated drafts can use this voice.",
    )


@app.post("/dashboard/style-examples/{example_id}/delete")
def delete_style_example(
    example_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    example = session.get(StyleExample, example_id)

    if not example:
        raise HTTPException(status_code=404, detail="Style example not found.")

    if not is_admin(request):
        org_id = get_current_organization_id(request, session)

        if example.organization_id != org_id:
            raise HTTPException(status_code=403, detail="Style example access denied.")

    campaign_id = example.campaign_id

    session.delete(example)
    session.commit()

    if campaign_id:
        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            "Style example deleted.",
        )

    return redirect_with_message(
        "/dashboard",
        "Style example deleted.",
    )
@app.get("/dashboard/campaigns/{campaign_id}/contacts")
def campaign_contacts_page(
    campaign_id: int,
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign, contacts, steps, draft_rows, stats = build_campaign_context(
        campaign_id,
        request,
        session,
    )

    organization = (
        session.get(Organization, campaign.organization_id)
        if campaign.organization_id
        else None
    )

    sender_email = (
        organization.sender_email
        if organization and organization.sender_email
        else DEFAULT_SES_FROM_EMAIL
    )

    reply_to_email = get_reply_to_email_for_sender(sender_email) if sender_email else None

    return templates.TemplateResponse(
        request=request,
        name="campaign_contacts.html",
        context={
            "message": message,
            "demo_mode": DEMO_MODE,
            "campaign": campaign,
            "organization": organization,
            "sender_email": sender_email,
            "reply_to_email": reply_to_email,
            "contacts": contacts,
            "steps": steps,
            "drafts": draft_rows,
            "stats": stats,
            "active_page": "contacts",
            "current_user": current_user_email(request),
            "is_admin": is_admin(request),
        },
    )

@app.post("/dashboard/campaigns/{campaign_id}/drafts/generate")
def generate_campaign_drafts(
    campaign_id: int,
    request: Request,
    cadence_step_id: str = Form("all"),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

    contacts = session.exec(
        select(Contact).where(
            Contact.campaign_id == campaign_id,
            Contact.unsubscribed == False,
            Contact.suppressed == False,
        )
    ).all()

    if cadence_step_id == "all":
        steps = session.exec(
            select(CadenceStep).where(CadenceStep.campaign_id == campaign_id)
        ).all()
    else:
        try:
            selected_step_id = int(cadence_step_id)
        except ValueError:
            return redirect_with_message(
                f"/dashboard/campaigns/{campaign_id}",
                "Invalid email step selected.",
            )

        selected_step = session.get(CadenceStep, selected_step_id)

        if not selected_step or selected_step.campaign_id != campaign_id:
            return redirect_with_message(
                f"/dashboard/campaigns/{campaign_id}",
                "Selected email step not found for this campaign.",
            )

        require_step_access(selected_step, request, session)
        steps = [selected_step]

    steps = sorted(steps, key=lambda step: step.step_number)

    if not contacts:
        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            "No contacts found. Upload contacts to this campaign first.",
        )

    if not steps:
        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            "No email steps found. Add an email step first.",
        )

    organization = (
        session.get(Organization, campaign.organization_id)
        if campaign.organization_id
        else None
    )

    style_examples = get_style_examples_for_organization(
        campaign.organization_id,
        session,
        limit=5,
    )

    created = 0
    skipped = 0

    for contact in contacts:
        for step in steps:
            existing = session.exec(
                select(EmailDraft).where(
                    EmailDraft.contact_id == contact.id,
                    EmailDraft.campaign_id == campaign_id,
                    EmailDraft.cadence_step_id == step.id,
                )
            ).first()

            if existing:
                skipped += 1
                continue

            unsubscribe_url = build_unsubscribe_url(contact)

            ai_email = render_template_email(
                template_subject=step.template_subject or "Quick question for {{ company }}",
                template_body=step.template_body or "",
                first_name=contact.first_name,
                company=contact.company or "",
                industry=contact.industry or "",
                role=contact.role or "",
                website=contact.website or "",
                email=contact.email or "",
                offer=campaign.offer,
                audience=campaign.audience or "small businesses",
                tone=step.tone or "friendly, consultative, concise",
                call_to_action=step.call_to_action or "Would you be open to a quick conversation?",
                unsubscribe_url=unsubscribe_url,
                cadence_step_name=step.name,
                cadence_step_purpose=step.purpose,
                step_number=step.step_number,
                brand_voice=organization.brand_voice if organization else None,
                avoid_phrases=organization.avoid_phrases if organization else None,
                preferred_cta=organization.preferred_cta if organization else None,
                signature_name=organization.signature_name if organization else None,
                signature_title=organization.signature_title if organization else None,
                signature_company=organization.signature_company if organization else None,
                style_examples=style_examples,
            )

            unsubscribe_line = f"\n\nIf this is not relevant, you can stop future emails here: {unsubscribe_url}"

            draft = EmailDraft(
                organization_id=campaign.organization_id,
                contact_id=contact.id,
                campaign_id=campaign_id,
                cadence_step_id=step.id,
                step_number=step.step_number,
                send_day=step.send_day,
                subject=ai_email["subject"],
                body=ai_email["body"] + unsubscribe_line,
                approved=False,
                sent=False,
            )

            session.add(draft)
            created += 1

    session.commit()

    selected_label = "all email steps" if cadence_step_id == "all" else "selected email step only"

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        f"Created {created} drafts for {selected_label}. Skipped {skipped} existing drafts.",
    )


@app.post("/dashboard/campaigns/{campaign_id}/drafts/approve-all")
def approve_all_campaign_drafts(
    campaign_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

    drafts = session.exec(
        select(EmailDraft).where(
            EmailDraft.campaign_id == campaign_id,
            EmailDraft.sent == False,
        )
    ).all()

    approved_count = 0

    for draft in drafts:
        draft.approved = True
        session.add(draft)
        approved_count += 1

    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        f"Approved {approved_count} drafts for {campaign.name}.",
    )


@app.post("/dashboard/campaigns/{campaign_id}/drafts/approve-day")
def approve_campaign_day(
    campaign_id: int,
    request: Request,
    send_day: int = Form(...),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    get_campaign_or_404_for_user(campaign_id, request, session)

    drafts = session.exec(
        select(EmailDraft).where(
            EmailDraft.campaign_id == campaign_id,
            EmailDraft.send_day == send_day,
            EmailDraft.sent == False,
        )
    ).all()

    approved_count = 0

    for draft in drafts:
        draft.approved = True
        session.add(draft)
        approved_count += 1

    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        f"Approved {approved_count} drafts for Day {send_day}.",
    )


@app.post("/dashboard/drafts/{draft_id}/approve")
def approve_single_draft(
    request: Request,
    draft_id: int,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    draft = session.get(EmailDraft, draft_id)

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")

    require_draft_access(draft, request, session)

    if draft.sent:
        raise HTTPException(status_code=400, detail="Cannot approve a sent draft.")

    draft.approved = True

    session.add(draft)
    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{draft.campaign_id}",
        "Draft approved.",
    )


@app.get("/dashboard/drafts/{draft_id}/edit")
def dashboard_edit_draft_page(
    draft_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    draft = session.get(EmailDraft, draft_id)

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")

    require_draft_access(draft, request, session)

    contact = session.get(Contact, draft.contact_id)
    campaign = session.get(Campaign, draft.campaign_id)
    step = session.get(CadenceStep, draft.cadence_step_id) if draft.cadence_step_id else None

    sender_email = get_sender_email_for_organization(draft.organization_id, session)
    reply_to_email = get_reply_to_email_for_sender(sender_email) if sender_email else None

    return templates.TemplateResponse(
        request=request,
        name="edit_draft.html",
        context={
            "draft": draft,
            "contact": contact,
            "campaign": campaign,
            "step": step,
            "sender_email": sender_email,
            "reply_to_email": reply_to_email,
            "demo_mode": DEMO_MODE,
            "current_user": current_user_email(request),
            "is_admin": is_admin(request),
        },
    )


@app.post("/dashboard/drafts/{draft_id}/edit")
def dashboard_save_draft_edit(
    request: Request,
    draft_id: int,
    subject: str = Form(...),
    body: str = Form(...),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    draft = session.get(EmailDraft, draft_id)

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")

    require_draft_access(draft, request, session)

    if draft.sent:
        raise HTTPException(status_code=400, detail="Cannot edit a sent draft.")

    draft.subject = subject
    draft.body = body
    draft.approved = False

    session.add(draft)
    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{draft.campaign_id}",
        "Draft saved. Re-approval required. Use 'Save as Style Example' if this draft reflects your preferred voice.",
    )


@app.post("/dashboard/drafts/{draft_id}/send")
def send_single_draft(
    request: Request,
    draft_id: int,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    draft = session.get(EmailDraft, draft_id)

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")

    require_draft_access(draft, request, session)

    campaign_id = draft.campaign_id

    if not draft.approved:
        raise HTTPException(status_code=400, detail="Draft must be approved first.")

    if draft.sent:
        raise HTTPException(status_code=400, detail="Draft already sent.")

    contact = session.get(Contact, draft.contact_id)

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found.")

    require_contact_access(contact, request, session)

    if contact.unsubscribed or contact.suppressed:
        raise HTTPException(status_code=400, detail="Contact is unsubscribed or suppressed.")

    suppression = session.exec(
        select(Suppression).where(
            Suppression.email == contact.email,
            Suppression.organization_id == contact.organization_id,
        )
    ).first()

    if suppression:
        raise HTTPException(status_code=400, detail="Email is suppressed.")

    sender_email = get_sender_email_for_organization(draft.organization_id, session)

    if not sender_email:
        raise HTTPException(
            status_code=400,
            detail="Missing sender email. Add sender_email to the workspace or set SES_FROM_EMAIL.",
        )

    reply_to_email = get_reply_to_email_for_sender(sender_email)

    try:
        safe_send_email(
            to_email=contact.email,
            subject=draft.subject,
            body=draft.body,
            from_email=sender_email,
            reply_to_email=reply_to_email,
            campaign_id=draft.campaign_id,
            contact_id=draft.contact_id,
            draft_id=draft.id,
            organization_id=draft.organization_id,
        )
    except Exception as e:
        technical_error = f"{contact.email}: {repr(e)}"
        print(f"SES SINGLE SEND ERROR: {technical_error}")

        if is_admin(request):
            user_message = (
                f"Send failed from {sender_email}. Reply-To {reply_to_email}. "
                f"{technical_error}"
            )
        else:
            user_message = "Email could not be sent. Please contact the administrator."

        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            user_message,
        )

    if DEMO_MODE:
        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            f"Demo mode is on. Email was previewed from {sender_email} with Reply-To {reply_to_email}, but not sent.",
        )

    draft.sent = True
    draft.sent_at = datetime.utcnow()

    session.add(draft)
    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        f"Email sent from {sender_email}. Reply-To {reply_to_email}.",
    )


@app.post("/dashboard/campaigns/{campaign_id}/drafts/send-day")
def send_campaign_day(
    campaign_id: int,
    request: Request,
    send_day: int = Form(...),
    max_send: int = Form(10),
    dry_run: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)
    sender_email = get_sender_email_for_organization(campaign.organization_id, session)

    if not sender_email:
        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            "Missing sender email. Add sender_email to the workspace or set SES_FROM_EMAIL.",
        )

    reply_to_email = get_reply_to_email_for_sender(sender_email)

    drafts = session.exec(
        select(EmailDraft).where(
            EmailDraft.campaign_id == campaign_id,
            EmailDraft.send_day == send_day,
            EmailDraft.approved == True,
            EmailDraft.sent == False,
        )
    ).all()

    drafts = drafts[:max_send]

    previewed_count = 0
    sent_count = 0
    skipped_count = 0
    errors = []

    for draft in drafts:
        contact = session.get(Contact, draft.contact_id)

        if not contact:
            skipped_count += 1
            continue

        if contact.unsubscribed or contact.suppressed:
            skipped_count += 1
            continue

        suppression = session.exec(
            select(Suppression).where(
                Suppression.email == contact.email,
                Suppression.organization_id == contact.organization_id,
            )
        ).first()

        if suppression:
            skipped_count += 1
            continue

        try:
            if dry_run or DEMO_MODE:
                print("\nDRY RUN / DEMO MODE - Email not sent")
                print(f"From: {sender_email}")
                print(f"Reply-To: {reply_to_email}")
                print(f"To: {contact.email}")
                print(f"Subject: {draft.subject}")
                print(draft.body)
                print("-" * 50)
                previewed_count += 1
            else:
                safe_send_email(
                    to_email=contact.email,
                    subject=draft.subject,
                    body=draft.body,
                    from_email=sender_email,
                    reply_to_email=reply_to_email,
                    campaign_id=draft.campaign_id,
                    contact_id=draft.contact_id,
                    draft_id=draft.id,
                    organization_id=draft.organization_id,
                )

                draft.sent = True
                draft.sent_at = datetime.utcnow()
                session.add(draft)

                sent_count += 1

        except Exception as e:
            technical_error = f"{contact.email}: {repr(e)}"
            print(f"SES SEND ERROR: {technical_error}")

            if is_admin(request):
                errors.append(technical_error)
            else:
                errors.append(f"{contact.email}: Email could not be sent. Please contact the administrator.")

    session.commit()

    if DEMO_MODE:
        message = f"Demo mode is on. Previewed {previewed_count} emails from {sender_email} with Reply-To {reply_to_email} for Day {send_day}. Nothing was sent."
    elif dry_run:
        message = f"Dry run complete. Previewed {previewed_count} emails from {sender_email} with Reply-To {reply_to_email} for Day {send_day}. Nothing was sent."
    else:
        message = f"Sent {sent_count} emails from {sender_email} with Reply-To {reply_to_email} for Day {send_day}. Skipped {skipped_count}."

    if errors:
        error_preview = " | ".join(errors[:2])
        message += f" Errors: {len(errors)}. {error_preview}"

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        message,
    )


def draft_is_due_today(contact: Contact, draft: EmailDraft, campaign: Campaign) -> bool:
    if not draft.send_day:
        return False

    if campaign.automation_start_date:
        sequence_start_date = campaign.automation_start_date
    else:
        sequence_start = contact.sequence_started_at or contact.created_at

        if not sequence_start:
            return False

        sequence_start_date = sequence_start.date()

    due_date = sequence_start_date + timedelta(days=max(draft.send_day - 1, 0))
    today = datetime.utcnow().date()

    return due_date <= today


@app.post("/dashboard/campaigns/{campaign_id}/automation")
def update_campaign_automation(
    campaign_id: int,
    request: Request,
    automation_enabled: Optional[str] = Form(None),
    daily_send_limit: int = Form(5),
    automation_start_date: str = Form(""),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

    campaign.automation_enabled = automation_enabled == "true"
    campaign.daily_send_limit = max(1, min(daily_send_limit, 100))

    if automation_start_date.strip():
        try:
            campaign.automation_start_date = datetime.strptime(
                automation_start_date.strip(),
                "%Y-%m-%d",
            ).date()
        except ValueError:
            return redirect_with_message(
                f"/dashboard/campaigns/{campaign_id}",
                "Invalid automation start date. Please select a valid date.",
            )
    else:
        campaign.automation_start_date = None

    session.add(campaign)
    session.commit()

    status = "enabled" if campaign.automation_enabled else "disabled"

    if campaign.automation_start_date:
        date_message = f" Automation start date set to {campaign.automation_start_date}."
    else:
        date_message = " No start date selected, so contacts will use their own upload/start date."

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        f"Automation {status}. Daily send limit set to {campaign.daily_send_limit}.{date_message}",
    )


@app.post("/dashboard/campaigns/{campaign_id}/delete")
def delete_campaign(
    campaign_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

    drafts = session.exec(
        select(EmailDraft).where(EmailDraft.campaign_id == campaign_id)
    ).all()

    steps = session.exec(
        select(CadenceStep).where(CadenceStep.campaign_id == campaign_id)
    ).all()

    contacts = session.exec(
        select(Contact).where(Contact.campaign_id == campaign_id)
    ).all()

    style_examples = session.exec(
        select(StyleExample).where(StyleExample.campaign_id == campaign_id)
    ).all()

    email_events = session.exec(
        select(EmailEvent).where(EmailEvent.campaign_id == campaign_id)
    ).all()

    draft_count = len(drafts)
    step_count = len(steps)
    contact_count = len(contacts)
    event_count = len(email_events)

    for event in email_events:
        session.delete(event)

    for example in style_examples:
        session.delete(example)

    for draft in drafts:
        session.delete(draft)

    for step in steps:
        session.delete(step)

    for contact in contacts:
        session.delete(contact)

    campaign_name = campaign.name
    session.delete(campaign)
    session.commit()

    return redirect_with_message(
        "/dashboard",
        f"Deleted campaign '{campaign_name}' with {contact_count} contacts, {step_count} steps, {draft_count} drafts, and {event_count} email events.",
    )
@app.get("/dashboard/campaigns/{campaign_id}/voice")
def campaign_voice_page(
    campaign_id: int,
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign, contacts, steps, draft_rows, stats = build_campaign_context(
        campaign_id,
        request,
        session,
    )

    organization = (
        session.get(Organization, campaign.organization_id)
        if campaign.organization_id
        else None
    )

    sender_email = (
        organization.sender_email
        if organization and organization.sender_email
        else DEFAULT_SES_FROM_EMAIL
    )

    reply_to_email = get_reply_to_email_for_sender(sender_email) if sender_email else None

    style_examples = session.exec(
        select(StyleExample)
        .where(
            StyleExample.organization_id == campaign.organization_id,
        )
        .order_by(StyleExample.created_at.desc())
    ).all()

    return templates.TemplateResponse(
        request=request,
        name="campaign_voice.html",
        context={
            "message": message,
            "demo_mode": DEMO_MODE,
            "campaign": campaign,
            "organization": organization,
            "sender_email": sender_email,
            "reply_to_email": reply_to_email,
            "contacts": contacts,
            "steps": steps,
            "drafts": draft_rows,
            "stats": stats,
            "style_examples": style_examples,
            "active_page": "voice",
            "current_user": current_user_email(request),
            "is_admin": is_admin(request),
        },
    )
@app.get("/dashboard/campaigns/{campaign_id}/automation")
def campaign_automation_page(
    campaign_id: int,
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign, contacts, steps, draft_rows, stats = build_campaign_context(
        campaign_id,
        request,
        session,
    )

    organization = (
        session.get(Organization, campaign.organization_id)
        if campaign.organization_id
        else None
    )

    sender_email = (
        organization.sender_email
        if organization and organization.sender_email
        else DEFAULT_SES_FROM_EMAIL
    )

    reply_to_email = get_reply_to_email_for_sender(sender_email) if sender_email else None

    return templates.TemplateResponse(
        request=request,
        name="campaign_automation.html",
        context={
            "message": message,
            "demo_mode": DEMO_MODE,
            "campaign": campaign,
            "organization": organization,
            "sender_email": sender_email,
            "reply_to_email": reply_to_email,
            "contacts": contacts,
            "steps": steps,
            "drafts": draft_rows,
            "stats": stats,
            "active_page": "automation",
            "current_user": current_user_email(request),
            "is_admin": is_admin(request),
        },
    )
@app.get("/dashboard/campaigns/{campaign_id}/settings")
def campaign_settings_page(
    campaign_id: int,
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign, contacts, steps, draft_rows, stats = build_campaign_context(
        campaign_id,
        request,
        session,
    )

    organization = (
        session.get(Organization, campaign.organization_id)
        if campaign.organization_id
        else None
    )

    sender_email = (
        organization.sender_email
        if organization and organization.sender_email
        else DEFAULT_SES_FROM_EMAIL
    )

    reply_to_email = get_reply_to_email_for_sender(sender_email) if sender_email else None

    return templates.TemplateResponse(
        request=request,
        name="campaign_settings.html",
        context={
            "message": message,
            "demo_mode": DEMO_MODE,
            "campaign": campaign,
            "organization": organization,
            "sender_email": sender_email,
            "reply_to_email": reply_to_email,
            "contacts": contacts,
            "steps": steps,
            "drafts": draft_rows,
            "stats": stats,
            "active_page": "settings",
            "current_user": current_user_email(request),
            "is_admin": is_admin(request),
        },
    )

@app.post("/cron/send-due-emails")
def cron_send_due_emails(
    secret: str = Query(""),
    session: Session = Depends(get_session),
):
    if not CRON_SECRET:
        raise HTTPException(status_code=500, detail="CRON_SECRET is not configured.")

    if not hmac.compare_digest(secret, CRON_SECRET):
        raise HTTPException(status_code=403, detail="Invalid cron secret.")

    if DEMO_MODE:
        print("CRON SEND SKIPPED: DEMO_MODE=true")
        return {
            "status": "skipped",
            "reason": "DEMO_MODE=true",
            "sent": 0,
            "errors": [],
        }

    campaigns = session.exec(
        select(Campaign).where(Campaign.automation_enabled == True)
    ).all()

    total_sent = 0
    total_skipped = 0
    errors = []
    campaign_results = []

    for campaign in campaigns:
        sender_email = get_sender_email_for_organization(campaign.organization_id, session)

        if not sender_email:
            errors.append(f"Campaign {campaign.id} missing sender email.")
            continue

        reply_to_email = get_reply_to_email_for_sender(sender_email)
        daily_limit = campaign.daily_send_limit or 5

        drafts = session.exec(
            select(EmailDraft).where(
                EmailDraft.campaign_id == campaign.id,
                EmailDraft.approved == True,
                EmailDraft.sent == False,
            )
        ).all()

        drafts = sorted(
            drafts,
            key=lambda d: (
                d.send_day or 999,
                d.created_at,
                d.id or 0,
            ),
        )

        campaign_sent = 0
        campaign_skipped = 0

        for draft in drafts:
            if campaign_sent >= daily_limit:
                break

            contact = session.get(Contact, draft.contact_id)

            if not contact:
                campaign_skipped += 1
                total_skipped += 1
                continue

            if contact.unsubscribed or contact.suppressed:
                campaign_skipped += 1
                total_skipped += 1
                continue

            suppression = session.exec(
                select(Suppression).where(
                    Suppression.email == contact.email,
                    Suppression.organization_id == contact.organization_id,
                )
            ).first()

            if suppression:
                campaign_skipped += 1
                total_skipped += 1
                continue

            if not draft_is_due_today(contact, draft, campaign):
                continue

            try:
                safe_send_email(
                    to_email=contact.email,
                    subject=draft.subject,
                    body=draft.body,
                    from_email=sender_email,
                    reply_to_email=reply_to_email,
                    campaign_id=draft.campaign_id,
                    contact_id=draft.contact_id,
                    draft_id=draft.id,
                    organization_id=draft.organization_id,
                )

                draft.sent = True
                draft.sent_at = datetime.utcnow()

                session.add(draft)

                campaign_sent += 1
                total_sent += 1

            except Exception as e:
                technical_error = f"Campaign {campaign.id}, draft {draft.id}, {contact.email}: {repr(e)}"
                print(f"CRON SES SEND ERROR: {technical_error}")
                errors.append(technical_error)

        campaign.last_automation_run_at = datetime.utcnow()
        session.add(campaign)

        campaign_results.append({
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "sent": campaign_sent,
            "skipped": campaign_skipped,
            "daily_limit": daily_limit,
            "automation_start_date": str(campaign.automation_start_date) if campaign.automation_start_date else None,
            "sender_email": sender_email,
            "reply_to_email": reply_to_email,
        })

    session.commit()

    return {
        "status": "complete",
        "sent": total_sent,
        "skipped": total_skipped,
        "errors": errors,
        "campaigns": campaign_results,
    }


@app.post("/campaigns")
def create_campaign(
    campaign: Campaign,
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    if not is_admin(request):
        org_id = get_current_organization_id(request, session)
        campaign.organization_id = org_id

    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return campaign


@app.get("/campaigns", response_model=List[Campaign])
def list_campaigns(
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    if is_admin(request):
        return session.exec(select(Campaign)).all()

    org_id = get_current_organization_id(request, session)

    return session.exec(
        select(Campaign).where(Campaign.organization_id == org_id)
    ).all()


@app.get("/contacts", response_model=List[Contact])
def list_contacts(
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    if is_admin(request):
        return session.exec(select(Contact)).all()

    org_id = get_current_organization_id(request, session)

    return session.exec(
        select(Contact).where(Contact.organization_id == org_id)
    ).all()


@app.get("/cadence-steps", response_model=List[CadenceStep])
def list_cadence_steps(
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    if is_admin(request):
        return session.exec(select(CadenceStep)).all()

    org_id = get_current_organization_id(request, session)

    return session.exec(
        select(CadenceStep).where(CadenceStep.organization_id == org_id)
    ).all()


@app.post("/suppressions")
def add_suppression(
    suppression: Suppression,
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    if not is_admin(request):
        suppression.organization_id = get_current_organization_id(request, session)

    suppression.email = suppression.email.strip().lower()

    existing = session.exec(
        select(Suppression).where(
            Suppression.email == suppression.email,
            Suppression.organization_id == suppression.organization_id,
        )
    ).first()

    if existing:
        return existing

    contact = session.exec(
        select(Contact).where(
            Contact.email == suppression.email,
            Contact.organization_id == suppression.organization_id,
        )
    ).first()

    if contact:
        contact.suppressed = True
        session.add(contact)

    session.add(suppression)
    session.commit()
    session.refresh(suppression)

    safe_update_hubspot_dnc(suppression.email)

    return suppression


@app.get("/suppressions", response_model=List[Suppression])
def list_suppressions(
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    if is_admin(request):
        return session.exec(select(Suppression)).all()

    org_id = get_current_organization_id(request, session)

    return session.exec(
        select(Suppression).where(Suppression.organization_id == org_id)
    ).all()


@app.get("/organizations", response_model=List[Organization])
def list_organizations(
    request: Request,
    session: Session = Depends(get_session),
):
    require_admin_login(request)
    return session.exec(select(Organization)).all()