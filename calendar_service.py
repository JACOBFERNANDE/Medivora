"""Google Calendar via OAuth 2.0. All functions may raise; callers wrap them (best effort)."""
import os, json
from datetime import datetime, timedelta
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
if (os.getenv("GOOGLE_REDIRECT_URI") or "").startswith("http://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")   # local dev only

def _flow():
    cfg = {"web": {"client_id": os.getenv("GOOGLE_CLIENT_ID"), "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}
    return Flow.from_client_config(cfg, scopes=SCOPES, redirect_uri=os.getenv("GOOGLE_REDIRECT_URI"),
                                   autogenerate_code_verifier=False)

def auth_url():
    return _flow().authorization_url(access_type="offline", prompt="consent")[0]

def exchange(code):
    f = _flow(); f.fetch_token(code=code); return f.credentials.to_json()

def _svc(tok):
    return build("calendar", "v3", credentials=Credentials.from_authorized_user_info(json.loads(tok), SCOPES), cache_discovery=False)

def _body(summary, desc, day, start, mins):
    s = datetime.fromisoformat(f"{day}T{start}:00"); tz = os.getenv("TIMEZONE", "Asia/Kolkata")
    return {"summary": summary, "description": desc,
            "start": {"dateTime": s.isoformat(), "timeZone": tz},
            "end": {"dateTime": (s + timedelta(minutes=mins)).isoformat(), "timeZone": tz}}

def create(tok, summary, desc, day, start, mins):
    return _svc(tok).events().insert(calendarId="primary", body=_body(summary, desc, day, start, mins)).execute()["id"]

def update(tok, eid, summary, desc, day, start, mins):
    _svc(tok).events().update(calendarId="primary", eventId=eid, body=_body(summary, desc, day, start, mins)).execute()

def delete(tok, eid):
    _svc(tok).events().delete(calendarId="primary", eventId=eid).execute()
