import os
import boto3
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
SES_FROM_EMAIL = os.getenv("SES_FROM_EMAIL")

ses_client = boto3.client("ses", region_name=AWS_REGION)


def send_email_via_ses(to_email: str, subject: str, body: str) -> dict:
    """
    Sends a plain-text email through Amazon SES.
    """

    if not SES_FROM_EMAIL:
        raise ValueError("SES_FROM_EMAIL is missing in .env")

    response = ses_client.send_email(
        Source=SES_FROM_EMAIL,
        Destination={
            "ToAddresses": [to_email]
        },
        Message={
            "Subject": {
                "Data": subject,
                "Charset": "UTF-8"
            },
            "Body": {
                "Text": {
                    "Data": body,
                    "Charset": "UTF-8"
                }
            }
        }
    )

    return response