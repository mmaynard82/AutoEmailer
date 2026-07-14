import os
import boto3
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
DEFAULT_SES_FROM_EMAIL = os.getenv("SES_FROM_EMAIL")

ses_client = boto3.client("ses", region_name=AWS_REGION)


def send_email_via_ses(
    to_email: str,
    subject: str,
    body: str,
    from_email: str | None = None,
) -> dict:
    """
    Sends email through AWS SES.

    Sender priority:
    1. Workspace sender_email passed in as from_email
    2. Fallback SES_FROM_EMAIL from .env / Render env
    """

    sender = from_email or DEFAULT_SES_FROM_EMAIL

    if not sender:
        raise ValueError(
            "Missing sender email. Set workspace sender_email or SES_FROM_EMAIL."
        )

    response = ses_client.send_email(
        Source=sender,
        Destination={
            "ToAddresses": [to_email],
        },
        Message={
            "Subject": {
                "Data": subject,
                "Charset": "UTF-8",
            },
            "Body": {
                "Text": {
                    "Data": body,
                    "Charset": "UTF-8",
                },
            },
        },
    )

    return response