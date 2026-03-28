import base64
import hashlib
import json
import os
import re
import requests
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from openai import OpenAI

load_dotenv()

# Google API Config — web application credentials
CLIENT_ID = os.getenv("GOOGLE_API_OAUTH_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_API_OAUTH_CLIENT_SECRET")
client_config = {
    "web": {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

# Single scope set: user identity + GMB access in one login
LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/business.manage",
]

# OpenAI Config
openai_client = OpenAI(api_key=os.getenv("CONFIG__OPENAI__KEY"))


def _pkce_pair() -> tuple[str, str]:
    """Generate a (code_verifier, code_challenge) pair for PKCE."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def get_google_login_url(redirect_uri: str) -> str:
    """Build the Google OAuth URL with PKCE. The code_verifier is carried
    in the OAuth state parameter so it survives the browser redirect."""
    verifier, challenge = _pkce_pair()
    # Encode verifier into the state param so we can retrieve it on callback
    state = base64.urlsafe_b64encode(
        json.dumps({"v": verifier}).encode()
    ).decode().rstrip("=")

    flow = Flow.from_client_config(client_config, scopes=LOGIN_SCOPES, redirect_uri=redirect_uri)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
        code_challenge=challenge,
        code_challenge_method="S256",
    )
    return auth_url


def complete_google_login(code: str, redirect_uri: str, state: str) -> dict:
    """Exchange OAuth code for user info + serialisable credential data."""
    # Recover code_verifier from the state parameter
    padding = 4 - len(state) % 4
    verifier = json.loads(
        base64.urlsafe_b64decode(state + "=" * padding).decode()
    ).get("v", "")

    flow = Flow.from_client_config(client_config, scopes=LOGIN_SCOPES, redirect_uri=redirect_uri)
    flow.fetch_token(code=code, code_verifier=verifier)
    creds = flow.credentials
    resp = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"},
    )
    user_info = resp.json()
    return {
        "email": user_info.get("email", ""),
        "name": user_info.get("name", ""),
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri or "https://oauth2.googleapis.com/token",
        "scopes": list(creds.scopes or LOGIN_SCOPES),
    }


def list_gmb_locations(creds: Credentials) -> list:
    """Return [{account_id, location_id, name}] for every GMB location the user manages."""
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}

    accounts_resp = requests.get("https://mybusiness.googleapis.com/v4/accounts", headers=headers)
    accounts = accounts_resp.json().get("accounts", [])

    locations = []
    for account in accounts:
        account_name = account["name"]          # e.g. "accounts/114674571764534564133"
        account_id = account_name.split("/")[-1]
        locs_resp = requests.get(
            f"https://mybusiness.googleapis.com/v4/{account_name}/locations",
            headers=headers,
        )
        for loc in locs_resp.json().get("locations", []):
            location_id = loc["name"].split("/")[-1]
            locations.append({
                "account_id": account_id,
                "location_id": location_id,
                "name": loc.get("locationName", loc["name"]),
            })
    return locations


def get_reviews(creds: Credentials, account_id: str, location_id: str) -> list:
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}

    all_reviews = []
    next_page_token = None

    while True:
        url = f"https://mybusiness.googleapis.com/v4/accounts/{account_id}/locations/{location_id}/reviews"
        if next_page_token:
            url += f"?pageToken={next_page_token}"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            all_reviews.extend(data.get("reviews", []))
            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break
        else:
            print(f"Error {response.status_code}: {response.text}")
            break

    return all_reviews


def get_original_text(review_text: str) -> str:
    original_text_match = re.search(r'\(Original\)\s*(.*)', review_text, re.DOTALL | re.IGNORECASE)
    if original_text_match:
        return original_text_match.group(1).strip()
    return re.sub(r'\(Translated by Google\)\s*', '', review_text).strip()


def analyze_review_and_suggest_response(review_text: str, rating: str, reviewer: str, examples: list, hotel_context: str) -> dict:
    if not review_text or str(review_text).strip() == "":
        return {
            "good_points": "Brak",
            "bad_points": "Brak",
            "suggested_response": "Dziękujemy za pozytywną ocenę! Zapraszamy ponownie." if rating in ["FOUR", "FIVE"] else "Dziękujemy za opinię."
        }

    review_text = get_original_text(review_text)

    examples_text = "\n\n".join([f"Opinia Gościa: {ex['comment']}\nTwoja Odpowiedź: {ex['our_response']}" for ex in examples])

    context_section = f"\nOto kontekst Twojego obiektu:\n{hotel_context}\n" if hotel_context.strip() else ""

    prompt = f"""Jesteś właścicielem obiektu. Analizujesz opinię gościa i przygotowujesz szkic odpowiedzi.
{context_section}
Oto przykłady Twoich wcześniejszych odpowiedzi (do naśladowania stylu):
{examples_text}

Zadanie:
1. Wypunktuj krótko co gość ocenił jako PLUSY i MINUSY w opinii (krótkie równoważniki zdań po polsku).
2. Przygotuj sugerowaną odpowiedź na opinię w języku polskim.
   - Odpowiedź musi być uprzejma, profesjonalna i ciepła.
   - Musi pasować stylem i tonem do Twoich wcześniejszych odpowiedzi.
   - Odnieś się uprzejmie do ewentualnych uwag (minusów).
   - Podziękuj za pochwały (plusy).
   - Zwróć się do recenzenta po imieniu, jeśli pasuje ({reviewer}).

Odpowiedz TYLKO i WYŁĄCZNIE obiektem JSON w tym dokładnym formacie (żadnego formatowania markdown, żadnych bloków kodu):
{{
  "good_points": "...",
  "bad_points": "...",
  "suggested_response": "..."
}}

Tekst opinii (wersja oryginalna):
{review_text}
Ocena (Rating): {rating}
"""

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|```$", "", text, flags=re.MULTILINE).strip()
        return json.loads(text)
    except Exception as e:
        return {"good_points": "Analysis Error", "bad_points": "Analysis Error", "suggested_response": f"Generation Error: {str(e)}"}


def generate_analytics_dashboard(all_answered_reviews: list, hotel_context: str) -> dict:
    texts = []
    for r in all_answered_reviews:
        text = get_original_text(r.get("comment", ""))
        rating = r.get("rating", "")
        if text:
            texts.append(f"[{rating} STARS]: {text}")

    combined_text = "\n---\n".join(texts[:100])

    context_section = f"\nKontekst obiektu:\n{hotel_context}\n" if hotel_context.strip() else ""

    prompt = f"""Jesteś analitykiem gościnności. Poniżej znajduje się lista historycznych opinii gości.
Twoim zadaniem jest znalezienie najczęściej powtarzających się wzorców i podsumowanie ich.
{context_section}
Zadanie:
1. Stwórz krótkie, przekrojowe podsumowanie (Executive Summary).
2. Wypisz 3-5 elementów, za które goście chwalą obiekt najbardziej (najlepiej z określeniem np. "chwalone w ponad 60% opinii").
3. Wypisz wszystkie najczęstsze skargi, braki lub obszary do poprawy.

Zwróć odpowiedź TYLKO w formie JSON:
{{
  "executive_summary": "...",
  "top_praises": ["...", "..."],
  "areas_to_improve": ["...", "..."]
}}

Opinie:
{combined_text}
"""

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|```$", "", text, flags=re.MULTILINE).strip()
        return json.loads(text)
    except Exception as e:
        return {"executive_summary": "Error", "top_praises": [], "areas_to_improve": [str(e)]}


def parse_reviews_to_lists(reviews):
    unanswered = []
    answered = []
    examples = []

    for r in reviews:
        comment = r.get("comment", "")
        create_time = r.get("createTime", "")
        try:
            dt = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d")
        except Exception:
            date_str = create_time

        rating = r.get("starRating", "")
        reviewer = (r.get("reviewer") or {}).get("displayName", "")

        reply_obj = r.get("reviewReply") or {}
        our_response = reply_obj.get("comment", "") if reply_obj else ""

        parsed_r = {
            "date": date_str,
            "reviewer": reviewer,
            "rating": rating,
            "comment": comment,
            "our_response": our_response
        }

        if our_response:
            answered.append(parsed_r)
            if comment and len(examples) < 10:
                examples.append({"comment": get_original_text(comment), "our_response": our_response})
        else:
            unanswered.append(parsed_r)

    return unanswered, answered, examples
