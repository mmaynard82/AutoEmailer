import os
import boto3
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
DEFAULT_SES_FROM_EMAIL = os.getenv("SES_FROM_EMAIL")
SES_CONFIGURATION_SET = os.getenv("SES_CONFIGURATION_SET", "").strip()


def get_ses_client():
    missing = []

    if not AWS_ACCESS_KEY_ID:
        missing.append("AWS_ACCESS_KEY_ID")

    if not AWS_SECRET_ACCESS_KEY:
        missing.append("AWS_SECRET_ACCESS_KEY")

    if not AWS_REGION:
        missing.append("AWS_REGION")

    if missing:
        raise ValueError(
            f"Missing AWS environment variables in Render: {', '.join(missing)}"
        )

    return boto3.client(
        "ses",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )


def clean_tag_value(value) -> str:
    if value is None:
        return ""

    value = str(value).strip()

    # SES tag values should be simple strings.
    value = value.replace(" ", "_")
    value = value.replace("/", "_")
    value = value.replace("\\", "_")

    return value[:256]


def send_email_via_ses(
    to_email: str,
    subject: str,
    body: str,
    from_email: str | None = None,
    reply_to_email: str | None = None,
    campaign_id: int | None = None,
    contact_id: int | None = None,
    draft_id: int | None = None,
    organization_id: int | None = None,
) -> dict:
    sender = from_email or DEFAULT_SES_FROM_EMAIL

    if not sender:
        raise ValueError(
            "Missing sender email. Set workspace sender_email or SES_FROM_EMAIL."
        )

    email_payload = {
        "Source": sender,
        "Destination": {"ToAddresses": [to_email]},
        "Message": {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
        },
    }

    if reply_to_email:
        email_payload["ReplyToAddresses"] = [reply_to_email]

    if SES_CONFIGURATION_SET:
        email_payload["ConfigurationSetName"] = SES_CONFIGURATION_SET

        email_payload["Tags"] = [
            {"Name": "campaign_id", "Value": clean_tag_value(campaign_id)},
            {"Name": "contact_id", "Value": clean_tag_value(contact_id)},
            {"Name": "draft_id", "Value": clean_tag_value(draft_id)},
            {"Name": "organization_id", "Value": clean_tag_value(organization_id)},
        ]

    ses_client = get_ses_client()
    return ses_client.send_email(**email_payload)