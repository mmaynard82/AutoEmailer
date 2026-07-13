import os
import hmac
import hashlib
from datetime import datetime
from typing import List

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import create_db_and_tables, get_session
from app.models import Contact, Campaign, CadenceStep, EmailDraft, Suppression
from app.ai_writer import render_template_email
from app.ses_sender import send_email_via_ses
from app.hubspot_client import get_hubspot_contacts, export_contact_to_hubspot


load_dotenv()

app = FastAPI(title="AI Emailer MVP")

templates = Jinja2Templates(directory="app/templates")

DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
SECRET_KEY = os.getenv("SECRET_KEY", "local-dev-secret")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


# ------------------------------------------------------------
# Auth Helpers
# ------------------------------------------------------------

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


def require_dashboard_login(request: Request):
    if not is_logged_in(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


# ------------------------------------------------------------
# Unsubscribe Helpers
# ------------------------------------------------------------

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


# ------------------------------------------------------------
# Login / Logout
# ------------------------------------------------------------

@app.get("/login")
def login_page(request: Request):
    if is_logged_in(request):
        return RedirectResponse(url="/dashboard", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": ""},
    )


@app.post("/login")
def login_submit(
    request: Request,
    password: str = Form(...),
):
    if password != ADMIN_PASSWORD:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Incorrect password."},
        )

    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        key="ai_emailer_auth",
        value=make_auth_token(),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 8,
    )

    return response


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("ai_emailer_auth")
    return response


# ------------------------------------------------------------
# Public Routes
# ------------------------------------------------------------

@app.get("/")
def home(request: Request):
    dashboard_link = "/dashboard" if is_logged_in(request) else "/login"

    return {
        "message": "AI Emailer MVP is running",
        "dashboard": dashboard_link,
        "demo_mode": DEMO_MODE,
        "next_steps": [
            "Login",
            "Create campaign",
            "Open campaign workspace",
            "Add/edit email steps",
            "Upload contacts to a campaign",
            "Generate drafts for all steps or one selected step",
            "Approve drafts",
            "Preview before sending",
        ],
    }


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "demo_mode": DEMO_MODE,
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
        select(Suppression).where(Suppression.email == contact.email)
    ).first()

    if not existing:
        suppression = Suppression(
            email=contact.email,
            reason="unsubscribe link",
        )
        session.add(suppression)

    session.add(contact)
    session.commit()

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


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def safe_send_email(to_email: str, subject: str, body: str) -> dict:
    if DEMO_MODE:
        print("\nDEMO MODE - Real email blocked")
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
    )

    return {
        "demo_mode": False,
        "response": response,
    }


def get_campaign_or_404(campaign_id: int, session: Session) -> Campaign:
    campaign = session.get(Campaign, campaign_id)

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    return campaign


def build_campaign_context(campaign_id: int, session: Session):
    campaign = get_campaign_or_404(campaign_id, session)

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


# ------------------------------------------------------------
# Dashboard: Campaign List
# ------------------------------------------------------------

@app.get("/dashboard")
def dashboard(
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaigns = session.exec(select(Campaign)).all()
    contacts = session.exec(select(Contact)).all()
    steps = session.exec(select(CadenceStep)).all()
    drafts = session.exec(select(EmailDraft)).all()

    campaign_rows = []

    for campaign in campaigns:
        campaign_contacts = [c for c in contacts if c.campaign_id == campaign.id]
        campaign_steps = [s for s in steps if s.campaign_id == campaign.id]
        campaign_drafts = [d for d in drafts if d.campaign_id == campaign.id]

        campaign_rows.append({
            "id": campaign.id,
            "name": campaign.name,
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
        },
    )


# ------------------------------------------------------------
# Campaign Routes
# ------------------------------------------------------------

@app.post("/dashboard/campaigns")
def dashboard_create_campaign(
    request: Request,
    name: str = Form(...),
    offer: str = Form(...),
    audience: str = Form("small businesses"),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = Campaign(
        name=name,
        offer=offer,
        audience=audience or "small businesses",
    )

    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    return RedirectResponse(
        url=f"/dashboard/campaigns/{campaign.id}?message=Campaign created. Add an email step next.",
        status_code=303,
    )


@app.get("/dashboard/campaigns/{campaign_id}")
def campaign_detail(
    campaign_id: int,
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign, contacts, steps, draft_rows, stats = build_campaign_context(campaign_id, session)

    return templates.TemplateResponse(
        request=request,
        name="campaign_detail.html",
        context={
            "message": message,
            "demo_mode": DEMO_MODE,
            "campaign": campaign,
            "contacts": contacts,
            "steps": steps,
            "drafts": draft_rows,
            "stats": stats,
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

    campaign = get_campaign_or_404(campaign_id, session)

    campaign.name = name
    campaign.audience = audience or "small businesses"
    campaign.offer = offer

    session.add(campaign)
    session.commit()

    return RedirectResponse(
        url=f"/dashboard/campaigns/{campaign_id}?message=Campaign settings updated.",
        status_code=303,
    )


# ------------------------------------------------------------
# Email Step Routes
# ------------------------------------------------------------

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

    campaign = get_campaign_or_404(campaign_id, session)

    step = CadenceStep(
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

    return RedirectResponse(
        url=f"/dashboard/campaigns/{campaign_id}?message=Email step added.",
        status_code=303,
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

    return RedirectResponse(
        url=f"/dashboard/campaigns/{step.campaign_id}?message=Email step updated. Existing drafts are not changed automatically.",
        status_code=303,
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

    campaign_id = step.campaign_id

    existing_drafts = session.exec(
        select(EmailDraft).where(EmailDraft.cadence_step_id == step_id)
    ).all()

    if existing_drafts:
        return RedirectResponse(
            url=f"/dashboard/campaigns/{campaign_id}?message=Cannot delete this step because drafts already exist for it.",
            status_code=303,
        )

    session.delete(step)
    session.commit()

    return RedirectResponse(
        url=f"/dashboard/campaigns/{campaign_id}?message=Email step deleted.",
        status_code=303,
    )


# ------------------------------------------------------------
# Contact Routes
# ------------------------------------------------------------

@app.post("/dashboard/campaigns/{campaign_id}/contacts/upload")
async def upload_campaign_contacts(
    campaign_id: int,
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404(campaign_id, session)

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

    return RedirectResponse(
        url=f"/dashboard/campaigns/{campaign_id}?message=Imported {imported} contacts into {campaign.name}. Skipped {skipped}.",
        status_code=303,
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

    campaign_id = contact.campaign_id

    contact.unsubscribed = True
    contact.suppressed = True

    existing = session.exec(
        select(Suppression).where(Suppression.email == contact.email)
    ).first()

    if not existing:
        suppression = Suppression(
            email=contact.email,
            reason="manual unsubscribe",
        )
        session.add(suppression)

    session.add(contact)
    session.commit()

    return RedirectResponse(
        url=f"/dashboard/campaigns/{campaign_id}?message=Contact unsubscribed and suppressed.",
        status_code=303,
    )


# ------------------------------------------------------------
# HubSpot Routes
# ------------------------------------------------------------

@app.post("/dashboard/campaigns/{campaign_id}/hubspot/import")
def import_hubspot_to_campaign(
    campaign_id: int,
    request: Request,
    limit: int = Form(100),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404(campaign_id, session)

    try:
        hubspot_data = get_hubspot_contacts(limit=limit)
    except Exception as e:
        return RedirectResponse(
            url=f"/dashboard/campaigns/{campaign_id}?message=HubSpot import failed: {str(e)}",
            status_code=303,
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

    return RedirectResponse(
        url=f"/dashboard/campaigns/{campaign_id}?message=Imported {imported} HubSpot contacts into {campaign.name}. Skipped {skipped}.",
        status_code=303,
    )


@app.post("/dashboard/campaigns/{campaign_id}/hubspot/export")
def export_campaign_to_hubspot(
    campaign_id: int,
    request: Request,
    limit: int = Form(100),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404(campaign_id, session)

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
            print(f"HubSpot export exception for {contact.email}: {e}")

    return RedirectResponse(
        url=(
            f"/dashboard/campaigns/{campaign_id}?message="
            f"HubSpot export complete. Created {created}, updated {updated}, skipped {skipped}, failed {failed}."
        ),
        status_code=303,
    )


# ------------------------------------------------------------
# Draft Routes
# ------------------------------------------------------------

@app.post("/dashboard/campaigns/{campaign_id}/drafts/generate")
def generate_campaign_drafts(
    campaign_id: int,
    request: Request,
    cadence_step_id: str = Form("all"),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404(campaign_id, session)

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
            return RedirectResponse(
                url=f"/dashboard/campaigns/{campaign_id}?message=Invalid email step selected.",
                status_code=303,
            )

        selected_step = session.get(CadenceStep, selected_step_id)

        if not selected_step or selected_step.campaign_id != campaign_id:
            return RedirectResponse(
                url=f"/dashboard/campaigns/{campaign_id}?message=Selected email step not found for this campaign.",
                status_code=303,
            )

        steps = [selected_step]

    steps = sorted(steps, key=lambda step: step.step_number)

    if not contacts:
        return RedirectResponse(
            url=f"/dashboard/campaigns/{campaign_id}?message=No contacts found. Upload contacts to this campaign first.",
            status_code=303,
        )

    if not steps:
        return RedirectResponse(
            url=f"/dashboard/campaigns/{campaign_id}?message=No email steps found. Add an email step first.",
            status_code=303,
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

            unsubscribe_line = f"\n\nIf this is not relevant, you can unsubscribe here: {unsubscribe_url}"

            draft = EmailDraft(
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

    if cadence_step_id == "all":
        selected_label = "all email steps"
    else:
        selected_label = "selected email step only"

    return RedirectResponse(
        url=f"/dashboard/campaigns/{campaign_id}?message=Created {created} drafts for {selected_label}. Skipped {skipped} existing drafts.",
        status_code=303,
    )


@app.post("/dashboard/campaigns/{campaign_id}/drafts/approve-all")
def approve_all_campaign_drafts(
    campaign_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    campaign = get_campaign_or_404(campaign_id, session)

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

    return RedirectResponse(
        url=f"/dashboard/campaigns/{campaign_id}?message=Approved {approved_count} drafts for {campaign.name}.",
        status_code=303,
    )


@app.post("/dashboard/campaigns/{campaign_id}/drafts/approve-day")
def approve_campaign_day(
    campaign_id: int,
    request: Request,
    send_day: int = Form(...),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    get_campaign_or_404(campaign_id, session)

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

    return RedirectResponse(
        url=f"/dashboard/campaigns/{campaign_id}?message=Approved {approved_count} drafts for Day {send_day}.",
        status_code=303,
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

    if draft.sent:
        raise HTTPException(status_code=400, detail="Cannot approve a sent draft.")

    draft.approved = True

    session.add(draft)
    session.commit()

    return RedirectResponse(
        url=f"/dashboard/campaigns/{draft.campaign_id}?message=Draft approved.",
        status_code=303,
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

    contact = session.get(Contact, draft.contact_id)
    campaign = session.get(Campaign, draft.campaign_id)
    step = session.get(CadenceStep, draft.cadence_step_id) if draft.cadence_step_id else None

    return templates.TemplateResponse(
        request=request,
        name="edit_draft.html",
        context={
            "draft": draft,
            "contact": contact,
            "campaign": campaign,
            "step": step,
            "demo_mode": DEMO_MODE,
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

    if draft.sent:
        raise HTTPException(status_code=400, detail="Cannot edit a sent draft.")

    draft.subject = subject
    draft.body = body
    draft.approved = False

    session.add(draft)
    session.commit()

    return RedirectResponse(
        url=f"/dashboard/campaigns/{draft.campaign_id}?message=Draft saved. Re-approval required.",
        status_code=303,
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

    campaign_id = draft.campaign_id

    if not draft.approved:
        raise HTTPException(status_code=400, detail="Draft must be approved first.")

    if draft.sent:
        raise HTTPException(status_code=400, detail="Draft already sent.")

    contact = session.get(Contact, draft.contact_id)

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found.")

    if contact.unsubscribed or contact.suppressed:
        raise HTTPException(status_code=400, detail="Contact is unsubscribed or suppressed.")

    suppression = session.exec(
        select(Suppression).where(Suppression.email == contact.email)
    ).first()

    if suppression:
        raise HTTPException(status_code=400, detail="Email is suppressed.")

    safe_send_email(
        to_email=contact.email,
        subject=draft.subject,
        body=draft.body,
    )

    if DEMO_MODE:
        return RedirectResponse(
            url=f"/dashboard/campaigns/{campaign_id}?message=Demo mode is on. Email was previewed in logs but not sent.",
            status_code=303,
        )

    draft.sent = True
    draft.sent_at = datetime.utcnow()

    session.add(draft)
    session.commit()

    return RedirectResponse(
        url=f"/dashboard/campaigns/{campaign_id}?message=Email sent.",
        status_code=303,
    )


@app.post("/dashboard/campaigns/{campaign_id}/drafts/send-day")
def send_campaign_day(
    campaign_id: int,
    request: Request,
    send_day: int = Form(...),
    max_send: int = Form(10),
    dry_run: str = Form(None),
    session: Session = Depends(get_session),
):
    require_dashboard_login(request)

    get_campaign_or_404(campaign_id, session)

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
            select(Suppression).where(Suppression.email == contact.email)
        ).first()

        if suppression:
            skipped_count += 1
            continue

        try:
            if dry_run or DEMO_MODE:
                print("\nDRY RUN / DEMO MODE - Email not sent")
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
                )

                draft.sent = True
                draft.sent_at = datetime.utcnow()
                session.add(draft)

                sent_count += 1

        except Exception as e:
            errors.append(f"{contact.email}: {str(e)}")

    session.commit()

    if DEMO_MODE:
        message = f"Demo mode is on. Previewed {previewed_count} emails for Day {send_day}. Nothing was sent."
    elif dry_run:
        message = f"Dry run complete. Previewed {previewed_count} emails for Day {send_day}. Nothing was sent."
    else:
        message = f"Sent {sent_count} emails for Day {send_day}. Skipped {skipped_count}."

    if errors:
        message += f" Errors: {len(errors)}. Check logs."

    return RedirectResponse(
        url=f"/dashboard/campaigns/{campaign_id}?message={message}",
        status_code=303,
    )


# ------------------------------------------------------------
# Basic API Endpoints
# ------------------------------------------------------------

@app.post("/campaigns")
def create_campaign(campaign: Campaign, session: Session = Depends(get_session)):
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return campaign


@app.get("/campaigns", response_model=List[Campaign])
def list_campaigns(session: Session = Depends(get_session)):
    return session.exec(select(Campaign)).all()


@app.get("/contacts", response_model=List[Contact])
def list_contacts(session: Session = Depends(get_session)):
    return session.exec(select(Contact)).all()


@app.get("/cadence-steps", response_model=List[CadenceStep])
def list_cadence_steps(session: Session = Depends(get_session)):
    return session.exec(select(CadenceStep)).all()


@app.post("/suppressions")
def add_suppression(
    suppression: Suppression,
    session: Session = Depends(get_session),
):
    suppression.email = suppression.email.strip().lower()

    existing = session.exec(
        select(Suppression).where(Suppression.email == suppression.email)
    ).first()

    if existing:
        return existing

    contact = session.exec(
        select(Contact).where(Contact.email == suppression.email)
    ).first()

    if contact:
        contact.suppressed = True
        session.add(contact)

    session.add(suppression)
    session.commit()
    session.refresh(suppression)

    return suppression


@app.get("/suppressions", response_model=List[Suppression])
def list_suppressions(session: Session = Depends(get_session)):
    return session.exec(select(Suppression)).all()