import json
import os
import re
import requests
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, Flow
from openai import OpenAI

load_dotenv()

# Google API Config
CLIENT_ID = os.getenv("GOOGLE_API_OAUTH_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_API_OAUTH_CLIENT_SECRET")
client_config = {
    "installed": {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

SCOPES = ["https://www.googleapis.com/auth/business.manage"]

IDENTITY_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

def get_google_login_url(redirect_uri: str) -> str:
    flow = Flow.from_client_config(client_config, scopes=IDENTITY_SCOPES, redirect_uri=redirect_uri)
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="select_account")
    return auth_url

def get_google_user_info(code: str, redirect_uri: str) -> dict:
    """Exchange OAuth code for user email and name."""
    flow = Flow.from_client_config(client_config, scopes=IDENTITY_SCOPES, redirect_uri=redirect_uri)
    flow.fetch_token(code=code)
    resp = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {flow.credentials.token}"},
    )
    return resp.json()  # {email, name, picture, ...}

# OpenAI Config
openai_client = OpenAI(api_key=os.getenv("CONFIG__OPENAI__KEY"))

def get_reviews(account_id: str, location_id: str):
    token_json = os.getenv("GOOGLE_TOKEN_JSON")
    if token_json:
        # Streamlit Cloud: use pre-generated token stored in secrets
        token_data = json.loads(token_json)
        creds = Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=token_data.get("scopes"),
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
    else:
        # Local development: use browser-based OAuth flow
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        creds = flow.run_local_server(port=0)
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }
    
    all_reviews = []
    next_page_token = None
    
    while True:
        url = f"https://mybusiness.googleapis.com/v4/accounts/{account_id}/locations/{location_id}/reviews"
        if next_page_token:
            url += f"?pageToken={next_page_token}"
            
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            reviews = data.get("reviews", [])
            all_reviews.extend(reviews)
            
            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break
        else:
            print(f"Error {response.status_code}: {response.text}")
            break
            
    return all_reviews

def get_original_text(review_text: str) -> str:
    original_text_match = re.search(r'\\(Original\\)\\s*(.*)', review_text, re.DOTALL | re.IGNORECASE)
    if original_text_match:
        return original_text_match.group(1).strip()
    return re.sub(r'\\(Translated by Google\\)\\s*', '', review_text).strip()

def analyze_review_and_suggest_response(review_text: str, rating: str, reviewer: str, examples: list, hotel_context: str) -> dict:
    if not review_text or str(review_text).strip() == "":
        return {
            "good_points": "Brak", 
            "bad_points": "Brak", 
            "suggested_response": "Dziękujemy za pozytywną ocenę! Zapraszamy ponownie." if rating in ["FOUR", "FIVE"] else "Dziękujemy za opinię."
        }
        
    review_text = get_original_text(review_text)

    examples_text = "\\n\\n".join([f"Opinia Gościa: {ex['comment']}\\nTwoja Odpowiedź: {ex['our_response']}" for ex in examples])

    prompt = f"""Jesteś właścicielem obiektu. Analizujesz opinię gościa i przygotowujesz szkic odpowiedzi.

Oto kontekst Twojego obiektu:
{hotel_context}

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
            response_format={ "type": "json_object" },
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\\s*|```$", "", text, flags=re.MULTILINE).strip()
            
        return json.loads(text)
    except Exception as e:
        return {"good_points": "Analysis Error", "bad_points": "Analysis Error", "suggested_response": f"Generation Error: {str(e)}"}

def generate_analytics_dashboard(all_answered_reviews: list, hotel_context: str) -> dict:
    # Compile a big list of text from reviews
    texts = []
    for r in all_answered_reviews:
        text = get_original_text(r.get("comment", ""))
        rating = r.get("rating", "")
        if text:
            texts.append(f"[{rating} STARS]: {text}")
            
    # Take up to last 100 for token limits
    combined_text = "\\n---\\n".join(texts[:100])
    
    prompt = f"""Jesteś analitykiem gościnności. Poniżej znajduje się lista historycznych opinii gości.
Twoim zadaniem jest znalezienie najczęściej powtarzających się wzorców i podsumowanie ich.

Kontekst obiektu:
{hotel_context}

Zadanie:
1. Stwórz krótkie, przekrojowe podsumowanie (Executive Summary).
2. Wypisz 3-5 elementów, za które goście chwalą pensjonat najbardziej (najlepiej z określeniem np. "chwalone w ponad 60% opinii").
3. Wypisz wszystkie najczęstsze skargi, braki lub obszary do poprawy, aby zarząd wiedział, co naprawić.

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
            response_format={ "type": "json_object" },
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\\s*|```$", "", text, flags=re.MULTILINE).strip()
            
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
