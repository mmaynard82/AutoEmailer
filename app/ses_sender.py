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
    reply_to_email: str | None = None,
) -> dict:
    """
    Sends email through AWS SES.

    Visible sender:
    - Uses from_email if provided.
    - Falls back to SES_FROM_EMAIL from .env / Render env.

    Reply-To:
    - Uses reply_to_email if provided.
    - If not provided, replies go to the visible sender.
    """

    sender = from_email or DEFAULT_SES_FROM_EMAIL

    if not sender:
        raise ValueError(
            "Missing sender email. Set workspace sender_email or SES_FROM_EMAIL."
        )

    email_payload = {
        "Source": sender,
        "Destination": {
            "ToAddresses": [to_email],
        },
        "Message": {
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
    }

    if reply_to_email:
        email_payload["ReplyToAddresses"] = [reply_to_email]

    response = ses_client.send_email(**email_payload)

    return response