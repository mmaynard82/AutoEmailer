import os
import hmac
import hashlib
from datetime import datetime
from typing import List, Optional
from urllib.parse import quote

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
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

templates = Jinja2Templates(directory="app/templates")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
SECRET_KEY = os.getenv("SECRET_KEY", "local-dev-secret")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
DEFAULT_SES_FROM_EMAIL = os.getenv("SES_FROM_EMAIL")


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
                        <input type="email" name="sender_email" required placeholder="mmaynard@mail.evolutioncrm.us">

                        <p class="muted">
                            This is the visible From address used by AWS SES for this workspace.
                            It must be verified in SES, or the sender domain must be verified in SES.
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

    return redirect_with_message(
        "/admin/workspaces/new",
        f"Workspace created: {workspace.name}. Sender: {workspace.sender_email}.",
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
        "next_steps": [
            "Login",
            "Create workspace",
            "Create pilot user",
            "Create campaign",
            "Open campaign workspace",
            "Add/edit email steps",
            "Upload contacts to a campaign",
            "Generate drafts",
            "Approve drafts",
            "Preview/send",
        ],
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
    fallback_secret_key = os.getenv("AWS_SECRET_KEY_FOR_SES")
    ses_secret_key = os.getenv("SES_SECRET_KEY")
    test_render_secret = os.getenv("TEST_RENDER_SECRET")
    region = os.getenv("AWS_REGION")
    sender = os.getenv("SES_FROM_EMAIL")

    return {
        "AWS_ACCESS_KEY_ID_present": bool(access_key),
        "AWS_ACCESS_KEY_ID_starts_with": access_key[:4] if access_key else None,
        "AWS_SECRET_ACCESS_KEY_present": bool(secret_key),
        "AWS_SECRET_ACCESS_KEY_length": len(secret_key) if secret_key else 0,
        "AWS_SECRET_KEY_FOR_SES_present": bool(fallback_secret_key),
        "AWS_SECRET_KEY_FOR_SES_length": len(fallback_secret_key) if fallback_secret_key else 0,
        "SES_SECRET_KEY_present": bool(ses_secret_key),
        "SES_SECRET_KEY_length": len(ses_secret_key) if ses_secret_key else 0,
        "TEST_RENDER_SECRET_present": bool(test_render_secret),
        "TEST_RENDER_SECRET_value": test_render_secret,
        "AWS_REGION": region,
        "SES_FROM_EMAIL": sender,
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
) -> dict:
    final_sender = from_email or DEFAULT_SES_FROM_EMAIL

    if not final_sender:
        raise ValueError("Missing sender email. Set workspace sender_email or SES_FROM_EMAIL.")

    if DEMO_MODE:
        print("\nDEMO MODE - Real email blocked")
        print(f"From: {final_sender}")
        print(f"Reply-To: {reply_to_email or final_sender}")
        print(f"To: {to_email}")
        print(f"Subject: {subject}")
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
        reply_to_email=reply_to_email,
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

        campaign_rows.append({
            "id": campaign.id,
            "name": campaign.name,
            "workspace": organization.name if organization else "No workspace",
            "sender_email": organization.sender_email if organization else "",
            "audience": campaign.audience,
            "offer": campaign.offer,
            "contacts": len(campaign_contacts),
            "steps": len(campaign_steps),
            "drafts": len(campaign_drafts),
            "approved": len([d for d in campaign_drafts if d.approved]),
            "sent": len([d for d in campaign_drafts if d.sent]),
            "unapproved": len([d for d in campaign_drafts if not d.approved and not d.sent]),
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

    return templates.TemplateResponse(
        request=request,
        name="campaign_detail.html",
        context={
            "message": message,
            "demo_mode": DEMO_MODE,
            "campaign": campaign,
            "organization": organization,
            "sender_email": sender_email,
            "contacts": contacts,
            "steps": steps,
            "drafts": draft_rows,
            "stats": stats,
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
    offer: str = Form(...),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

    campaign.name = name
    campaign.audience = audience or "small businesses"
    campaign.offer = offer

    session.add(campaign)
    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        "Campaign settings updated.",
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
    template_body: str = Form(...),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

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
        f"/dashboard/campaigns/{campaign_id}",
        "Email step added.",
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
            "Cannot delete this step because drafts already exist for it.",
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

    campaign = get_campaign_or_404_for_user(campaign_id, request, session)

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
        f"HubSpot export complete for {campaign.name}. Created {created}, updated {updated}, skipped {skipped}, failed {failed}.",
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

    return templates.TemplateResponse(
        request=request,
        name="edit_draft.html",
        context={
            "draft": draft,
            "contact": contact,
            "campaign": campaign,
            "step": step,
            "sender_email": sender_email,
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
        "Draft saved. Re-approval required.",
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

    try:
        safe_send_email(
            to_email=contact.email,
            subject=draft.subject,
            body=draft.body,
            from_email=sender_email,
            reply_to_email=sender_email,
        )
    except Exception as e:
        error_message = f"{contact.email}: {repr(e)}"
        print(f"SES SINGLE SEND ERROR: {error_message}")

        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            f"Send failed from {sender_email}. {error_message}",
        )

    if DEMO_MODE:
        return redirect_with_message(
            f"/dashboard/campaigns/{campaign_id}",
            f"Demo mode is on. Email was previewed from {sender_email} but not sent.",
        )

    draft.sent = True
    draft.sent_at = datetime.utcnow()

    session.add(draft)
    session.commit()

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        f"Email sent from {sender_email}.",
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
                print(f"Reply-To: {sender_email}")
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
                    reply_to_email=sender_email,
                )

                draft.sent = True
                draft.sent_at = datetime.utcnow()
                session.add(draft)

                sent_count += 1

        except Exception as e:
            error_message = f"{contact.email}: {repr(e)}"
            print(f"SES SEND ERROR: {error_message}")
            errors.append(error_message)

    session.commit()

    if DEMO_MODE:
        message = f"Demo mode is on. Previewed {previewed_count} emails from {sender_email} for Day {send_day}. Nothing was sent."
    elif dry_run:
        message = f"Dry run complete. Previewed {previewed_count} emails from {sender_email} for Day {send_day}. Nothing was sent."
    else:
        message = f"Sent {sent_count} emails from {sender_email} for Day {send_day}. Skipped {skipped_count}."

    if errors:
        error_preview = " | ".join(errors[:2])
        message += f" Errors: {len(errors)}. {error_preview}"

    return redirect_with_message(
        f"/dashboard/campaigns/{campaign_id}",
        message,
    )


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