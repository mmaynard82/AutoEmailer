from typing import Optional
from datetime import datetime, date
from sqlmodel import SQLModel, Field


class Organization(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    name: str = Field(index=True)
    notes: Optional[str] = None
    sender_email: Optional[str] = None

    brand_voice: Optional[str] = (
        "Warm, direct, brief, and consultative. Sound like a real person, not a sales script. "
        "Keep paragraphs short. Focus on practical CRM improvement, better follow-up, clearer workflows, "
        "and helping the business stay organized without adding complexity."
    )

    avoid_phrases: Optional[str] = (
        "Avoid: I hope this email finds you well, revolutionize, game-changer, cutting-edge, "
        "unlock your potential, transform your business, just checking in, circling back, "
        "fake compliments, long intros, exaggerated claims, and overly formal corporate language."
    )

    preferred_cta: Optional[str] = "Would it be worth a quick conversation?"
    signature_name: Optional[str] = None
    signature_title: Optional[str] = "CRM Consultant"
    signature_company: Optional[str] = "Evolution CRM"

    created_at: datetime = Field(default_factory=datetime.utcnow)


class AppUser(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    organization_id: Optional[int] = Field(default=None, index=True)

    email: str = Field(index=True, unique=True)
    password_hash: str

    name: Optional[str] = None
    role: str = "pilot"
    is_active: bool = True

    created_at: datetime = Field(default_factory=datetime.utcnow)


class Contact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    organization_id: Optional[int] = Field(default=None, index=True)
    campaign_id: Optional[int] = Field(default=None, index=True)

    first_name: str
    last_name: Optional[str] = None
    email: str = Field(index=True)

    company: Optional[str] = None
    industry: Optional[str] = None
    role: Optional[str] = None
    website: Optional[str] = None

    unsubscribed: bool = False
    suppressed: bool = False

    created_at: datetime = Field(default_factory=datetime.utcnow)
    sequence_started_at: datetime = Field(default_factory=datetime.utcnow)


class Campaign(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    organization_id: Optional[int] = Field(default=None, index=True)

    name: str
    offer: str
    audience: str = "small businesses"

    created_at: datetime = Field(default_factory=datetime.utcnow)

    automation_enabled: bool = False
    daily_send_limit: int = 5
    automation_start_date: Optional[date] = None
    last_automation_run_at: Optional[datetime] = None


class CadenceStep(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    organization_id: Optional[int] = Field(default=None, index=True)
    campaign_id: int = Field(index=True)

    step_number: int
    send_day: int
    name: str
    purpose: str

    tone: str = "friendly, consultative, concise"
    call_to_action: str = "Would you be open to a quick conversation?"

    template_subject: Optional[str] = "Quick question for {{ company }}"
    template_body: Optional[str] = """Hi {{ first_name }},

{{ intro_para }}

I’m reaching out because we help {{ audience }} improve CRM follow-up, sales visibility, and client communication.

{{ offer }}

{{ call_to_action }}

Best,
Evolution CRM"""

    created_at: datetime = Field(default_factory=datetime.utcnow)


class EmailDraft(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    organization_id: Optional[int] = Field(default=None, index=True)

    contact_id: int = Field(index=True)
    campaign_id: int = Field(index=True)
    cadence_step_id: Optional[int] = Field(default=None, index=True)

    step_number: Optional[int] = None
    send_day: Optional[int] = None

    subject: str
    body: str

    approved: bool = False
    sent: bool = False
    sent_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)


class EmailEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    organization_id: Optional[int] = Field(default=None, index=True)
    campaign_id: Optional[int] = Field(default=None, index=True)
    contact_id: Optional[int] = Field(default=None, index=True)
    draft_id: Optional[int] = Field(default=None, index=True)

    message_id: Optional[str] = Field(default=None, index=True)
    event_type: str = Field(index=True)

    recipient_email: Optional[str] = Field(default=None, index=True)

    bounce_type: Optional[str] = None
    complaint_feedback_type: Optional[str] = None
    link_url: Optional[str] = None

    raw_event: Optional[str] = None

    event_time: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)

class AutomationLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    organization_id: Optional[int] = Field(default=None, index=True)
    campaign_id: Optional[int] = Field(default=None, index=True)
    event_type: str = Field(index=True)
    message: str
    drafts_due: int = 0
    sent_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    details: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

class StyleExample(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    organization_id: Optional[int] = Field(default=None, index=True)
    campaign_id: Optional[int] = Field(default=None, index=True)
    draft_id: Optional[int] = Field(default=None, index=True)

    label: str = "Approved style example"
    subject: str
    body: str

    created_at: datetime = Field(default_factory=datetime.utcnow)

class AutomationLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    organization_id: Optional[int] = Field(default=None, index=True)
    campaign_id: Optional[int] = Field(default=None, index=True)
    event_type: str = Field(index=True)
    message: str
    drafts_due: int = 0
    sent_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    details: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

class Suppression(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    organization_id: Optional[int] = Field(default=None, index=True)

    email: str = Field(index=True)
    reason: str

    created_at: datetime = Field(default_factory=datetime.utcnow)