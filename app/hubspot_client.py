import os
import requests
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")

BASE_URL = "https://api.hubapi.com"


def hubspot_headers():
    if not HUBSPOT_ACCESS_TOKEN:
        raise ValueError("HUBSPOT_ACCESS_TOKEN is missing in .env")

    return {
        "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def get_hubspot_contacts(limit: int = 50):
    url = f"{BASE_URL}/crm/v3/objects/contacts"

    params = {
        "limit": limit,
        "properties": "firstname,lastname,email,company,jobtitle,website",
    }

    response = requests.get(
        url,
        headers=hubspot_headers(),
        params=params,
        timeout=20,
    )

    response.raise_for_status()
    return response.json()


def create_or_update_hubspot_contact(
    email: str,
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    jobtitle: str = "",
    website: str = "",
):
    """
    Simple create endpoint.
    Later we can make this smarter with search/update if contact exists.
    """

    url = f"{BASE_URL}/crm/v3/objects/contacts"

    payload = {
        "properties": {
            "email": email,
            "firstname": first_name,
            "lastname": last_name,
            "company": company,
            "jobtitle": jobtitle,
            "website": website,
        }
    }

    response = requests.post(
        url,
        headers=hubspot_headers(),
        json=payload,
        timeout=20,
    )

    response.raise_for_status()
    return response.json()
def create_hubspot_contact(
    email: str,
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    jobtitle: str = "",
    website: str = "",
):
    url = f"{BASE_URL}/crm/v3/objects/contacts"

    payload = {
        "properties": {
            "email": email,
            "firstname": first_name,
            "lastname": last_name,
            "company": company,
            "jobtitle": jobtitle,
            "website": website,
        }
    }

    response = requests.post(
        url,
        headers=hubspot_headers(),
        json=payload,
        timeout=20,
    )

    return response


def update_hubspot_contact_by_email(
    email: str,
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    jobtitle: str = "",
    website: str = "",
):
    url = f"{BASE_URL}/crm/v3/objects/contacts/{email}"

    params = {
        "idProperty": "email",
    }

    payload = {
        "properties": {
            "firstname": first_name,
            "lastname": last_name,
            "company": company,
            "jobtitle": jobtitle,
            "website": website,
        }
    }

    response = requests.patch(
        url,
        headers=hubspot_headers(),
        params=params,
        json=payload,
        timeout=20,
    )

    return response


def export_contact_to_hubspot(
    email: str,
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    jobtitle: str = "",
    website: str = "",
):
    """
    Creates a contact in HubSpot.
    If HubSpot reports that the contact already exists, tries to update by email.
    """

    create_response = create_hubspot_contact(
        email=email,
        first_name=first_name,
        last_name=last_name,
        company=company,
        jobtitle=jobtitle,
        website=website,
    )

    if create_response.status_code in [200, 201]:
        return {
            "status": "created",
            "response": create_response.json(),
        }

    # HubSpot usually returns 409 for duplicate existing records.
    if create_response.status_code == 409:
        update_response = update_hubspot_contact_by_email(
            email=email,
            first_name=first_name,
            last_name=last_name,
            company=company,
            jobtitle=jobtitle,
            website=website,
        )

        if update_response.status_code in [200, 201]:
            return {
                "status": "updated",
                "response": update_response.json(),
            }

        return {
            "status": "failed",
            "error": update_response.text,
            "status_code": update_response.status_code,
        }

    return {
        "status": "failed",
        "error": create_response.text,
        "status_code": create_response.status_code,
    }