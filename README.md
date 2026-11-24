# Healthcare Appointment Manager (Flask + LangGraph + RAG + Gemini)

## Run locally
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # put your GOOGLE_API_KEY
python app.py           # http://localhost:5000

## Quick test (curl)
1. Login admin:  POST /api/login {"email":"admin@clinic.com","password":"admin123"}
2. Add doctor:   POST /api/admin/doctors {"name":"Dr A","email":"a@x.com","password":"pw","specialisation":"Cardiology"}
3. Register patient: POST /api/register, then /api/login
4. GET /api/doctors?specialisation=cardio ; GET /api/doctors/1/slots?date=2026-11-25
5. POST /api/appointments/hold {"doctor_id":1,"date":"2026-11-25","start":"09:00"}
6. POST /api/appointments/1/confirm {"symptoms":"fever and cough for 3 days"}
7. Doctor login -> POST /api/appointments/1/visit {"notes":"Viral fever","prescription":[{"name":"Paracetamol","times":["08:00","20:00"],"days":3}]}

## Deploy on Render
Push to GitHub -> Render > New Web Service -> select repo -> set env vars (GOOGLE_API_KEY, SECRET_KEY, DATABASE_URL from a Render PostgreSQL). Keep workers = 1 (scheduler runs inside app).

## More docs
API_DOCS.md (endpoints, Google Calendar setup, LLM prompts), SCHEMA.md (ER diagram), SYSTEM_DESIGN.md (write-up).
If you ran an earlier version, delete `instance/clinic.db` (new columns were added).

## Email (optional)
Without SMTP_* variables emails are printed in the console. For Gmail: enable 2-step verification, create an App Password,
then set SMTP_HOST=smtp.gmail.com, SMTP_PORT=587, SMTP_USER=<gmail>, SMTP_PASS=<app password>.

## Deploy on Render (step by step)
1. Push this folder to a GitHub repo (do NOT commit .env or instance/).
2. Render dashboard > New > Blueprint > select the repo. render.yaml creates the web service and a free PostgreSQL database.
3. Fill the prompted env vars: GOOGLE_API_KEY, ADMIN_PASSWORD, and (optional) Google OAuth + SMTP values.
4. After deploy, set GOOGLE_REDIRECT_URI=https://<your-app>.onrender.com/api/google/callback and add the same URL in Google Cloud Console.
5. Notes: free web services sleep after inactivity (jobs pause until the next request); Render's free PostgreSQL expires after about 30 days.
