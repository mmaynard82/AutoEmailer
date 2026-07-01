from datetime import datetime
from typing import List

import pandas as pd
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import create_db_and_tables, get_session
from app.models import Contact, Campaign, CadenceStep, EmailDraft, Suppression
from app.ai_writer import generate_sales_email
from app.ses_sender import send_email_via_ses


app = FastAPI(title="AI Emailer MVP")

templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


@app.get("/")
def home():
    return {
        "message": "AI Emailer MVP is running",
        "next_steps": [
            "Go to /dashboard",
            "Create campaign",
            "Add cadence steps",
            "Upload contacts",
            "Generate cadence drafts",
            "Edit drafts",
            "Approve drafts",
            "Send approved drafts",
            "Use dry run before real sending",
        ],
    }


# ------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------

def create_default_cadence_steps(campaign_id: int, session: Session):
    existing_steps = session.exec(
        select(CadenceStep).where(CadenceStep.campaign_id == campaign_id)
    ).all()

    if existing_steps:
        return

    default_steps = [
        CadenceStep(
            campaign_id=campaign_id,
            step_number=1,
            send_day=1,
            name="Intro Email",
            purpose="Introduce the offer and ask for a brief conversation.",
        ),
        CadenceStep(
            campaign_id=campaign_id,
            step_number=2,
            send_day=3,
            name="Follow-Up",
            purpose="Politely follow up and restate the problem being solved.",
        ),
        CadenceStep(
            campaign_id=campaign_id,
            step_number=3,
            send_day=7,
            name="Value Email",
            purpose="Share a helpful CRM insight or checklist-style value point.",
        ),
        CadenceStep(
            campaign_id=campaign_id,
            step_number=4,
            send_day=14,
            name="Close Loop",
            purpose="Ask whether to close the loop or reconnect later.",
        ),
    ]

    for step in default_steps:
        session.add(step)

    session.commit()


def get_available_send_days(cadence_steps: List[CadenceStep]):
    days = sorted(list({step.send_day for step in cadence_steps}))

    if not days:
        days = [1, 3, 7, 14, 21]

    return days


# ------------------------------------------------------------
# Dashboard Pages
# ------------------------------------------------------------

@app.get("/dashboard")
def dashboard(
    request: Request,
    message: str = "",
    session: Session = Depends(get_session),
):
    campaigns = session.exec(select(Campaign)).all()
    contacts = session.exec(select(Contact)).all()
    cadence_steps = session.exec(select(CadenceStep)).all()
    drafts = session.exec(select(EmailDraft)).all()
    suppressions = session.exec(select(Suppression)).all()

    available_send_days = get_available_send_days(cadence_steps)

    draft_rows = []

    for draft in drafts:
        contact = session.get(Contact, draft.contact_id)
        campaign = session.get(Campaign, draft.campaign_id)
        step = session.get(CadenceStep, draft.cadence_step_id) if draft.cadence_step_id else None

        draft_rows.append({
            "id": draft.id,
            "campaign": campaign.name if campaign else "",
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
            x["campaign"] or "",
            x["contact_name"] or "",
            x["step_number"] or 0,
        )
    )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "message": message,
            "campaigns": campaigns,
            "contacts": contacts,
            "cadence_steps": cadence_steps,
            "drafts": draft_rows,
            "suppressions": suppressions,
            "available_send_days": available_send_days,
        },
    )


@app.get("/dashboard/drafts/{draft_id}/edit")
def dashboard_edit_draft_page(
    draft_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
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
        },
    )


# ------------------------------------------------------------
# Dashboard Actions
# ------------------------------------------------------------

@app.post("/dashboard/campaigns")
def dashboard_create_campaign(
    name: str = Form(...),
    offer: str = Form(...),
    audience: str = Form(...),
    tone: str = Form("friendly, consultative, concise"),
    call_to_action: str = Form("Would you be open to a quick conversation?"),
    session: Session = Depends(get_session),
):
    campaign = Campaign(
        name=name,
        offer=offer,
        audience=audience,
        tone=tone,
        call_to_action=call_to_action,
    )

    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    create_default_cadence_steps(campaign.id, session)

    return RedirectResponse(
        url="/dashboard?message=Campaign created with default cadence steps.",
        status_code=303,
    )


@app.post("/dashboard/cadence-steps")
def dashboard_create_cadence_step(
    campaign_id: int = Form(...),
    step_number: int = Form(...),
    send_day: int = Form(...),
    name: str = Form(...),
    purpose: str = Form(...),
    session: Session = Depends(get_session),
):
    campaign = session.get(Campaign, campaign_id)

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    step = CadenceStep(
        campaign_id=campaign_id,
        step_number=step_number,
        send_day=send_day,
        name=name,
        purpose=purpose,
    )

    session.add(step)
    session.commit()

    return RedirectResponse(
        url="/dashboard?message=Custom cadence step added.",
        status_code=303,
    )


@app.post("/dashboard/contacts/upload")
async def dashboard_upload_contacts(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
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
            select(Contact).where(Contact.email == email)
        ).first()

        if existing:
            skipped += 1
            continue

        contact = Contact(
            first_name=str(row.get("first_name", "")).strip(),
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
        url=f"/dashboard?message=Imported {imported} contacts. Skipped {skipped}.",
        status_code=303,
    )


@app.post("/dashboard/drafts/generate")
def dashboard_generate_drafts(
    campaign_id: int = Form(...),
    session: Session = Depends(get_session),
):
    campaign = session.get(Campaign, campaign_id)

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    create_default_cadence_steps(campaign.id, session)

    contacts = session.exec(
        select(Contact).where(
            Contact.unsubscribed == False,
            Contact.suppressed == False,
        )
    ).all()

    cadence_steps = session.exec(
        select(CadenceStep).where(CadenceStep.campaign_id == campaign.id)
    ).all()

    cadence_steps = sorted(cadence_steps, key=lambda step: step.step_number)

    created = 0
    skipped = 0

    for contact in contacts:
        for step in cadence_steps:
            existing = session.exec(
                select(EmailDraft).where(
                    EmailDraft.contact_id == contact.id,
                    EmailDraft.campaign_id == campaign.id,
                    EmailDraft.cadence_step_id == step.id,
                )
            ).first()

            if existing:
                skipped += 1
                continue

            ai_email = generate_sales_email(
                first_name=contact.first_name,
                company=contact.company or "",
                industry=contact.industry or "",
                role=contact.role or "",
                offer=campaign.offer,
                audience=campaign.audience,
                tone=campaign.tone,
                call_to_action=campaign.call_to_action,
                cadence_step_name=step.name,
                cadence_step_purpose=step.purpose,
                step_number=step.step_number,
            )

            unsubscribe_line = "\n\nIf this is not relevant, reply 'unsubscribe' and I will not follow up."

            draft = EmailDraft(
                contact_id=contact.id,
                campaign_id=campaign.id,
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

    return RedirectResponse(
        url=f"/dashboard?message=Created {created} drafts. Skipped {skipped} existing drafts.",
        status_code=303,
    )


@app.post("/dashboard/drafts/{draft_id}/edit")
def dashboard_save_draft_edit(
    draft_id: int,
    subject: str = Form(...),
    body: str = Form(...),
    session: Session = Depends(get_session),
):
    draft = session.get(EmailDraft, draft_id)

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")

    if draft.sent:
        raise HTTPException(status_code=400, detail="Cannot edit a sent draft.")

    draft.subject = subject
    draft.body = body

    # Require re-approval after editing
    draft.approved = False

    session.add(draft)
    session.commit()

    return RedirectResponse(
        url="/dashboard?message=Draft saved. Re-approval required.",
        status_code=303,
    )


@app.post("/dashboard/drafts/{draft_id}/approve")
def dashboard_approve_draft(
    draft_id: int,
    session: Session = Depends(get_session),
):
    draft = session.get(EmailDraft, draft_id)

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")

    if draft.sent:
        raise HTTPException(status_code=400, detail="Cannot approve a sent draft.")

    draft.approved = True

    session.add(draft)
    session.commit()

    return RedirectResponse(
        url="/dashboard?message=Draft approved.",
        status_code=303,
    )


@app.post("/dashboard/drafts/approve-day")
def dashboard_approve_day(
    campaign_id: int = Form(...),
    send_day: int = Form(...),
    session: Session = Depends(get_session),
):
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
        url=f"/dashboard?message=Approved {approved_count} drafts for Day {send_day}.",
        status_code=303,
    )


@app.post("/dashboard/drafts/{draft_id}/send")
def dashboard_send_draft(
    draft_id: int,
    session: Session = Depends(get_session),
):
    draft = session.get(EmailDraft, draft_id)

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")

    if not draft.approved:
        raise HTTPException(status_code=400, detail="Draft must be approved first.")

    if draft.sent:
        raise HTTPException(status_code=400, detail="Draft already sent.")

    contact = session.get(Contact, draft.contact_id)

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found.")

    if contact.unsubscribed or contact.suppressed:
        raise HTTPException(
            status_code=400,
            detail="Contact is unsubscribed or suppressed.",
        )

    suppression = session.exec(
        select(Suppression).where(Suppression.email == contact.email)
    ).first()

    if suppression:
        raise HTTPException(status_code=400, detail="Email is suppressed.")

    send_email_via_ses(
        to_email=contact.email,
        subject=draft.subject,
        body=draft.body,
    )

    draft.sent = True
    draft.sent_at = datetime.utcnow()

    session.add(draft)
    session.commit()

    return RedirectResponse(
        url="/dashboard?message=Email sent.",
        status_code=303,
    )


@app.post("/dashboard/drafts/send-day")
def dashboard_send_day(
    campaign_id: int = Form(...),
    send_day: int = Form(...),
    max_send: int = Form(10),
    dry_run: str = Form(None),
    session: Session = Depends(get_session),
):
    drafts = session.exec(
        select(EmailDraft).where(
            EmailDraft.campaign_id == campaign_id,
            EmailDraft.send_day == send_day,
            EmailDraft.approved == True,
            EmailDraft.sent == False,
        )
    ).all()

    drafts = drafts[:max_send]

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
            if dry_run:
                print("\nDRY RUN - Email not sent")
                print(f"To: {contact.email}")
                print(f"Subject: {draft.subject}")
                print(draft.body)
                print("-" * 50)
            else:
                send_email_via_ses(
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

    if dry_run:
        message = f"Dry run complete. {sent_count} emails previewed for Day {send_day}. Skipped {skipped_count}."
    else:
        message = f"Sent {sent_count} emails for Day {send_day}. Skipped {skipped_count}."

    if errors:
        message += f" Errors: {len(errors)}. Check PowerShell logs."

    return RedirectResponse(
        url=f"/dashboard?message={message}",
        status_code=303,
    )


@app.post("/dashboard/contacts/{contact_id}/unsubscribe")
def dashboard_unsubscribe_contact(
    contact_id: int,
    session: Session = Depends(get_session),
):
    contact = session.get(Contact, contact_id)

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found.")

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
        url="/dashboard?message=Contact unsubscribed and suppressed.",
        status_code=303,
    )


# ------------------------------------------------------------
# API Endpoints
# ------------------------------------------------------------

@app.post("/campaigns")
def create_campaign(campaign: Campaign, session: Session = Depends(get_session)):
    session.add(campaign)
    session.commit()
    session.refresh(campaign)

    create_default_cadence_steps(campaign.id, session)

    return campaign


@app.get("/campaigns", response_model=List[Campaign])
def list_campaigns(session: Session = Depends(get_session)):
    return session.exec(select(Campaign)).all()


@app.get("/cadence-steps", response_model=List[CadenceStep])
def list_cadence_steps(session: Session = Depends(get_session)):
    return session.exec(select(CadenceStep)).all()


@app.post("/contacts/upload")
async def upload_contacts(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
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
            select(Contact).where(Contact.email == email)
        ).first()

        if existing:
            skipped += 1
            continue

        contact = Contact(
            first_name=str(row.get("first_name", "")).strip(),
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

    return {
        "imported": imported,
        "skipped": skipped,
    }


@app.get("/contacts", response_model=List[Contact])
def list_contacts(session: Session = Depends(get_session)):
    return session.exec(select(Contact)).all()


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


@app.post("/drafts/generate/{campaign_id}")
def generate_drafts(
    campaign_id: int,
    session: Session = Depends(get_session),
):
    campaign = session.get(Campaign, campaign_id)

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    create_default_cadence_steps(campaign.id, session)

    contacts = session.exec(
        select(Contact).where(
            Contact.unsubscribed == False,
            Contact.suppressed == False,
        )
    ).all()

    cadence_steps = session.exec(
        select(CadenceStep).where(CadenceStep.campaign_id == campaign.id)
    ).all()

    cadence_steps = sorted(cadence_steps, key=lambda step: step.step_number)

    created = 0
    skipped = 0

    for contact in contacts:
        for step in cadence_steps:
            existing = session.exec(
                select(EmailDraft).where(
                    EmailDraft.contact_id == contact.id,
                    EmailDraft.campaign_id == campaign.id,
                    EmailDraft.cadence_step_id == step.id,
                )
            ).first()

            if existing:
                skipped += 1
                continue

            ai_email = generate_sales_email(
                first_name=contact.first_name,
                company=contact.company or "",
                industry=contact.industry or "",
                role=contact.role or "",
                offer=campaign.offer,
                audience=campaign.audience,
                tone=campaign.tone,
                call_to_action=campaign.call_to_action,
                cadence_step_name=step.name,
                cadence_step_purpose=step.purpose,
                step_number=step.step_number,
            )

            unsubscribe_line = "\n\nIf this is not relevant, reply 'unsubscribe' and I will not follow up."

            draft = EmailDraft(
                contact_id=contact.id,
                campaign_id=campaign.id,
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

    return {
        "created": created,
        "skipped": skipped,
    }


@app.get("/drafts")
def list_drafts(session: Session = Depends(get_session)):
    drafts = session.exec(select(EmailDraft)).all()

    results = []

    for draft in drafts:
        contact = session.get(Contact, draft.contact_id)
        campaign = session.get(Campaign, draft.campaign_id)
        step = session.get(CadenceStep, draft.cadence_step_id) if draft.cadence_step_id else None

        results.append({
            "draft_id": draft.id,
            "campaign": campaign.name if campaign else None,
            "step": step.name if step else None,
            "step_number": draft.step_number,
            "send_day": draft.send_day,
            "to": contact.email if contact else None,
            "contact": f"{contact.first_name} {contact.last_name or ''}".strip() if contact else None,
            "company": contact.company if contact else None,
            "subject": draft.subject,
            "body": draft.body,
            "approved": draft.approved,
            "sent": draft.sent,
            "sent_at": draft.sent_at,
        })

    return results


@app.post("/drafts/{draft_id}/approve")
def approve_draft(
    draft_id: int,
    session: Session = Depends(get_session),
):
    draft = session.get(EmailDraft, draft_id)

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")

    if draft.sent:
        raise HTTPException(status_code=400, detail="Cannot approve a sent draft.")

    draft.approved = True

    session.add(draft)
    session.commit()
    session.refresh(draft)

    return draft


@app.post("/drafts/{draft_id}/send")
def send_draft(
    draft_id: int,
    session: Session = Depends(get_session),
):
    draft = session.get(EmailDraft, draft_id)

    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")

    if not draft.approved:
        raise HTTPException(
            status_code=400,
            detail="Draft must be approved before sending.",
        )

    if draft.sent:
        raise HTTPException(
            status_code=400,
            detail="Draft has already been sent.",
        )

    contact = session.get(Contact, draft.contact_id)

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found.")

    if contact.unsubscribed or contact.suppressed:
        raise HTTPException(
            status_code=400,
            detail="Contact is unsubscribed or suppressed.",
        )

    suppression = session.exec(
        select(Suppression).where(Suppression.email == contact.email)
    ).first()

    if suppression:
        raise HTTPException(
            status_code=400,
            detail="Email is on suppression list.",
        )

    response = send_email_via_ses(
        to_email=contact.email,
        subject=draft.subject,
        body=draft.body,
    )

    draft.sent = True
    draft.sent_at = datetime.utcnow()

    session.add(draft)
    session.commit()
    session.refresh(draft)

    return {
        "message": "Email sent",
        "draft_id": draft.id,
        "ses_message_id": response.get("MessageId"),
    }


@app.post("/contacts/{contact_id}/unsubscribe")
def unsubscribe_contact(
    contact_id: int,
    session: Session = Depends(get_session),
):
    contact = session.get(Contact, contact_id)

    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found.")

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

    return {
        "message": "Contact unsubscribed",
        "email": contact.email,
    }