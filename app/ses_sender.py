import os
import boto3
from dotenv import load_dotenv

load_dotenv()
load_dotenv("/etc/secrets/.env")

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")

AWS_SECRET_ACCESS_KEY = (
    os.getenv("AWS_SECRET_ACCESS_KEY")
    or os.getenv("AWS_SECRET_KEY_FOR_SES")
    or os.getenv("SES_SECRET_KEY")
)

DEFAULT_SES_FROM_EMAIL = os.getenv("SES_FROM_EMAIL")


def get_ses_client():
    missing = []

    if not AWS_ACCESS_KEY_ID:
        missing.append("AWS_ACCESS_KEY_ID")

    if not AWS_SECRET_ACCESS_KEY:
        missing.append("AWS_SECRET_ACCESS_KEY, AWS_SECRET_KEY_FOR_SES, or SES_SECRET_KEY")

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


def send_email_via_ses(
    to_email: str,
    subject: str,
    body: str,
    from_email: str | None = None,
    reply_to_email: str | None = None,
) -> dict:
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

    ses_client = get_ses_client()
    response = ses_client.send_email(**email_payload)

    return response