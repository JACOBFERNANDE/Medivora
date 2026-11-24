# API Docs (JSON, session-cookie auth)
| Method | Path | Role | Body / Notes |
|---|---|---|---|
| POST | /api/register | public | name, email, password (creates patient) |
| POST | /api/login, /api/logout | any | email, password |
| GET | /api/doctors?specialisation= | public | search doctors |
| GET | /api/doctors/{id}/slots?date=YYYY-MM-DD | public | free slots (empty on leave day) |
| POST | /api/appointments/hold | patient | doctor_id, date, start -> 5 min hold (409 if taken) |
| POST | /api/appointments/{id}/confirm | patient | symptoms -> LLM pre-visit summary, emails, calendar |
| POST | /api/appointments/{id}/reschedule | patient | date, start |
| POST | /api/appointments/{id}/cancel | patient/doctor | frees slot, emails, deletes calendar events |
| GET | /api/my/appointments | patient/doctor | list with summaries |
| POST | /api/appointments/{id}/visit | doctor | notes, prescription[{name,times[],days}] -> post-visit summary + reminders |
| POST | /api/admin/doctors | admin | name,email,password,specialisation,work_start,work_end,slot_minutes |
| POST | /api/admin/doctors/{id}/leave | admin | day -> cancels and notifies affected patients |
| GET | /api/google/connect | any | returns Google consent URL |
| GET | /api/google/callback | any | OAuth redirect URI, stores token |

# Google Calendar setup
1. console.cloud.google.com > create project > APIs & Services > Library > enable **Google Calendar API**.
2. OAuth consent screen: External, add yourself (patient/doctor emails) as **Test users**.
3. Credentials > Create OAuth client ID > **Web application**.
4. Authorised redirect URIs: `http://localhost:5000/api/google/callback` and `https://<your-app>.onrender.com/api/google/callback`.
5. Put client ID/secret and GOOGLE_REDIRECT_URI in `.env` (or Render env vars). Each user clicks "Connect Google Calendar" once.

# LLM prompts
- Pre-visit: "Analyse these symptoms and return ONLY JSON with keys urgency (Low/Medium/High), chief_complaint, questions (exactly 3). Triage guidelines: <RAG context> Symptoms: <symptoms>"
- Post-visit: "Convert these clinical notes into a patient-friendly summary with medication schedule and follow-up steps. Notes: <notes> Prescription: <json>"

# Extra endpoints
| Method | Path | Role | Notes |
|---|---|---|---|
| PUT | /api/admin/doctors/{id} | admin | edit specialisation, work_start, work_end, slot_minutes, name |
| DELETE | /api/admin/doctors/{id} | admin | soft-deactivate (409 if upcoming appointments exist) |
| DELETE | /api/admin/doctors/{id}/leave/{YYYY-MM-DD} | admin | remove a leave day |

# Background jobs (APScheduler)
- every 30s: send queued emails (max 5 attempts)
- every 60s: turn due medication reminders into emails
- every 5 min: email patient and doctor 24h before each confirmed appointment
All times use the TIMEZONE env var (default Asia/Kolkata).
