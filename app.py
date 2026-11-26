import os, json, smtplib, threading
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from email.message import EmailMessage
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
import llm
import calendar_service as cal
from flask import render_template, redirect

app = Flask(__name__)
url = os.getenv("DATABASE_URL", "sqlite:///clinic.db")
if url.startswith("postgres"):   # Render gives postgres:// or postgresql://; force the psycopg2 driver explicitly
    url = "postgresql+psycopg2://" + url.split("://", 1)[1]
app.config.update(SQLALCHEMY_DATABASE_URI=url, SECRET_KEY=os.getenv("SECRET_KEY", "dev"))
db = SQLAlchemy(app)
lm = LoginManager(app)
HOLD_MIN = 5

def now_local():
    """Naive datetime in the clinic time zone (TIMEZONE env, default Asia/Kolkata)."""
    return datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))).replace(tzinfo=None)

# ---------------- Models ----------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100)); email = db.Column(db.String(120), unique=True)
    password_hash = db.Column(db.String(255)); role = db.Column(db.String(10))  # patient/doctor/admin

class Doctor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    specialisation = db.Column(db.String(80), index=True)
    work_start = db.Column(db.String(5), default="09:00"); work_end = db.Column(db.String(5), default="17:00")
    slot_minutes = db.Column(db.Integer, default=30)
    active = db.Column(db.Boolean, default=True)
    emergency_slots = db.Column(db.Integer, default=2)  # last N slots/day reserved for urgent cases
    user = db.relationship("User")

class Leave(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctor.id")); day = db.Column(db.Date)
    __table_args__ = (db.UniqueConstraint("doctor_id", "day"),)

class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctor.id"))
    day = db.Column(db.Date); start = db.Column(db.String(5))
    status = db.Column(db.String(12))  # held/confirmed/cancelled/expired/completed
    slot_key = db.Column(db.String(60), unique=True, nullable=True)  # DB-level double-booking guard
    hold_expires = db.Column(db.DateTime)
    symptoms = db.Column(db.Text); pre_summary = db.Column(db.Text)
    notes = db.Column(db.Text); post_summary = db.Column(db.Text)
    reminded = db.Column(db.Boolean, default=False)
    is_emergency = db.Column(db.Boolean, default=False)
    p_event = db.Column(db.String(100)); d_event = db.Column(db.String(100))  # Google Calendar event ids
    doctor = db.relationship("Doctor")

class GoogleToken(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    token = db.Column(db.Text)

class Email(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    to = db.Column(db.String(120)); subject = db.Column(db.String(200)); body = db.Column(db.Text)
    status = db.Column(db.String(10), default="pending"); attempts = db.Column(db.Integer, default=0)
    priority = db.Column(db.Integer, default=0)

class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    send_at = db.Column(db.DateTime); text = db.Column(db.String(300)); sent = db.Column(db.Boolean, default=False)
    taken = db.Column(db.Boolean, default=False); missed_alerted = db.Column(db.Boolean, default=False)

@lm.user_loader
def load_user(uid): return db.session.get(User, int(uid))
@lm.unauthorized_handler
def unauth(): return jsonify(error="login required"), 401

def role_required(*roles):
    def deco(f):
        @wraps(f)
        @login_required
        def w(*a, **k):
            if current_user.role not in roles: return jsonify(error="forbidden"), 403
            return f(*a, **k)
        return w
    return deco

def queue_email(to, subject, body, priority=0):
    db.session.add(Email(to=to, subject=subject, body=body, priority=priority))

# ---------------- Slots ----------------
def all_slots(doc):
    t = datetime.strptime(doc.work_start, "%H:%M"); end = datetime.strptime(doc.work_end, "%H:%M")
    out = []
    while t + timedelta(minutes=doc.slot_minutes) <= end:
        out.append(t.strftime("%H:%M")); t += timedelta(minutes=doc.slot_minutes)
    return out

def free_slots(doc, day, emergency=False):
    if not doc.active or Leave.query.filter_by(doctor_id=doc.id, day=day).first(): return []
    now = datetime.utcnow(); taken = set()
    for a in Appointment.query.filter(Appointment.doctor_id == doc.id, Appointment.day == day, Appointment.slot_key.isnot(None)):
        if a.status == "held" and a.hold_expires < now: continue
        taken.add(a.start)
    today = now_local().date()
    cutoff = now_local().strftime("%H:%M") if day == today else ("" if day > today else "99:99")
    slots = all_slots(doc)
    if not emergency and doc.emergency_slots: slots = slots[:max(0, len(slots) - doc.emergency_slots)]
    return [s for s in slots if s not in taken and s > cutoff]

def cal_sync(a, action):
    """Best-effort Google Calendar sync for patient and doctor. Never raises."""
    for user, attr in ((db.session.get(User, a.patient_id), "p_event"), (a.doctor.user, "d_event")):
        t = db.session.get(GoogleToken, user.id)
        if not t: continue
        try:
            eid = getattr(a, attr)
            args = (f"Clinic appointment: Dr. {a.doctor.user.name}", f"Specialisation: {a.doctor.specialisation}",
                    str(a.day), a.start, a.doctor.slot_minutes)
            if action == "delete":
                if eid: cal.delete(t.token, eid); setattr(a, attr, None)
            elif eid: cal.update(t.token, eid, *args)
            else: setattr(a, attr, cal.create(t.token, *args))
        except Exception as ex:
            print("calendar sync failed:", ex)

# ---------------- Auth ----------------
@app.post("/api/register")
def register():
    d = request.json or {}
    if not all(str(d.get(k, "")).strip() for k in ("name", "email", "password")): return jsonify(error="name, email and password are required"), 400
    if User.query.filter_by(email=d["email"]).first(): return jsonify(error="email exists"), 409
    u = User(name=d["name"], email=d["email"], password_hash=generate_password_hash(d["password"]), role="patient")
    db.session.add(u); db.session.commit(); return jsonify(id=u.id), 201

@app.post("/api/login")
def login():
    d = request.json; u = User.query.filter_by(email=d["email"]).first()
    if not u or not check_password_hash(u.password_hash, d["password"]): return jsonify(error="bad credentials"), 401
    login_user(u); return jsonify(role=u.role, name=u.name)

@app.post("/api/logout")
@login_required
def logout(): logout_user(); return jsonify(ok=True)

@app.get("/api/me")
def me():
    if not current_user.is_authenticated: return jsonify(error="not logged in"), 401
    return jsonify(role=current_user.role, name=current_user.name)

# ---------------- Admin ----------------
@app.post("/api/admin/doctors")
@role_required("admin")
def add_doctor():
    d = request.json or {}
    if not all(str(d.get(k, "")).strip() for k in ("name", "email", "password", "specialisation")): return jsonify(error="name, email, password and specialisation are required"), 400
    if User.query.filter_by(email=d["email"]).first(): return jsonify(error="email already exists"), 409
    u = User(name=d["name"], email=d["email"], password_hash=generate_password_hash(d["password"]), role="doctor")
    db.session.add(u); db.session.flush()
    doc = Doctor(user_id=u.id, specialisation=d["specialisation"], work_start=d.get("work_start", "09:00"),
                 work_end=d.get("work_end", "17:00"), slot_minutes=d.get("slot_minutes", 30),
                 emergency_slots=d.get("emergency_slots", 2))
    db.session.add(doc); db.session.commit(); return jsonify(id=doc.id), 201

@app.post("/api/admin/doctors/<int:did>/leave")
@role_required("admin")
def add_leave(did):
    day = date.fromisoformat(request.json["day"])
    db.session.add(Leave(doctor_id=did, day=day))
    affected = Appointment.query.filter(Appointment.doctor_id == did, Appointment.day == day,
                                        Appointment.status.in_(["held", "confirmed"])).all()
    for a in affected:
        a.status = "cancelled"; a.slot_key = None
        cal_sync(a, "delete")
        p = db.session.get(User, a.patient_id)
        queue_email(p.email, "Appointment cancelled - doctor on leave",
                    f"Your appointment on {a.day} at {a.start} is cancelled as the doctor is on leave. Please rebook.")
    try: db.session.commit()
    except IntegrityError: db.session.rollback(); return jsonify(error="leave exists"), 409
    return jsonify(notified=len(affected))

@app.put("/api/admin/doctors/<int:did>")
@role_required("admin")
def edit_doctor(did):
    doc = Doctor.query.get_or_404(did); d = request.json or {}
    for k in ("specialisation", "work_start", "work_end", "slot_minutes", "emergency_slots"):
        if k in d: setattr(doc, k, d[k])
    if d.get("name"): doc.user.name = d["name"]
    db.session.commit(); return jsonify(ok=True)

@app.delete("/api/admin/doctors/<int:did>")
@role_required("admin")
def remove_doctor(did):
    doc = Doctor.query.get_or_404(did)
    n = Appointment.query.filter(Appointment.doctor_id == did, Appointment.status.in_(["held", "confirmed"]),
                                 Appointment.day >= now_local().date()).count()
    if n: return jsonify(error=f"{n} upcoming appointment(s) exist; mark leave or cancel them first"), 409
    doc.active = False; db.session.commit(); return jsonify(ok=True)

@app.delete("/api/admin/doctors/<int:did>/leave/<day>")
@role_required("admin")
def remove_leave(did, day):
    Leave.query.filter_by(doctor_id=did, day=date.fromisoformat(day)).delete()
    db.session.commit(); return jsonify(ok=True)

# ---------------- Patient ----------------
@app.get("/api/doctors")
def doctors():
    q = Doctor.query.filter_by(active=True)
    if request.args.get("specialisation"): q = q.filter(Doctor.specialisation.ilike(f"%{request.args['specialisation']}%"))
    return jsonify([dict(id=d.id, name=d.user.name, specialisation=d.specialisation) for d in q])

@app.get("/api/doctors/<int:did>/slots")
def slots(did):
    doc = Doctor.query.get_or_404(did)
    return jsonify(free_slots(doc, date.fromisoformat(request.args["date"])))

@app.post("/api/appointments/hold")
@role_required("patient")
def hold():
    d = request.json; doc = Doctor.query.get_or_404(d["doctor_id"])
    day = date.fromisoformat(d["date"]); start = d["start"]
    if start not in free_slots(doc, day): return jsonify(error="slot unavailable"), 409
    key = f"{doc.id}-{day}-{start}"
    old = Appointment.query.filter_by(slot_key=key).first()
    if old:
        if old.status == "held" and old.hold_expires < datetime.utcnow(): old.slot_key = None; old.status = "expired"; db.session.flush()
        else: return jsonify(error="slot just taken"), 409
    a = Appointment(patient_id=current_user.id, doctor_id=doc.id, day=day, start=start, status="held",
                    slot_key=key, hold_expires=datetime.utcnow() + timedelta(minutes=HOLD_MIN))
    db.session.add(a)
    try: db.session.commit()
    except IntegrityError: db.session.rollback(); return jsonify(error="slot just taken"), 409
    return jsonify(appointment_id=a.id, hold_minutes=HOLD_MIN), 201

@app.post("/api/appointments/<int:aid>/confirm")
@role_required("patient")
def confirm(aid):
    symptoms = ((request.json or {}).get("symptoms") or "").strip()
    if not symptoms: return jsonify(error="please describe your symptoms"), 400
    # atomic claim: a double-click or parallel request can only win once
    n = Appointment.query.filter(Appointment.id == aid, Appointment.patient_id == current_user.id, Appointment.status == "held",
                                 Appointment.hold_expires >= datetime.utcnow()).update({"status": "confirming"})
    db.session.commit()
    if not n: return jsonify(error="hold invalid or expired"), 409
    a = db.session.get(Appointment, aid)
    a.symptoms = symptoms
    ps = llm.pre_visit_summary(a.symptoms)   # never raises
    a.pre_summary = json.dumps(ps); a.is_emergency = bool(ps.get("red_flags"))
    a.status = "confirmed"; a.hold_expires = None
    cal_sync(a, "upsert")
    queue_email(current_user.email, "Booking confirmed", f"Appointment with Dr. {a.doctor.user.name} on {a.day} at {a.start}.")
    queue_email(a.doctor.user.email, "New appointment", f"Patient {current_user.name} on {a.day} at {a.start}.\nPre-visit summary: {a.pre_summary}", 1 if a.is_emergency else 0)
    db.session.commit(); return jsonify(status="confirmed", pre_summary=json.loads(a.pre_summary))

@app.post("/api/appointments/<int:aid>/cancel")
@login_required
def cancel(aid):
    a = Appointment.query.get_or_404(aid)
    if current_user.id not in (a.patient_id, a.doctor.user_id): return jsonify(error="forbidden"), 403
    a.status = "cancelled"; a.slot_key = None
    cal_sync(a, "delete")
    for u in (db.session.get(User, a.patient_id), a.doctor.user):
        queue_email(u.email, "Appointment cancelled", f"Appointment on {a.day} at {a.start} was cancelled.")
    db.session.commit(); return jsonify(status="cancelled")

@app.get("/api/my/appointments")
@login_required
def mine():
    if current_user.role == "patient": q = Appointment.query.filter_by(patient_id=current_user.id)
    else:
        doc = Doctor.query.filter_by(user_id=current_user.id).first_or_404()
        q = Appointment.query.filter_by(doctor_id=doc.id)
    return jsonify([dict(id=a.id, day=str(a.day), start=a.start, status=a.status, doctor_id=a.doctor_id, is_emergency=bool(a.is_emergency),
                         doctor="Dr. " + a.doctor.user.name, specialisation=a.doctor.specialisation,
                         patient=db.session.get(User, a.patient_id).name,
                         pre_summary=json.loads(a.pre_summary) if a.pre_summary else None,
                         post_summary=a.post_summary) for a in q.order_by(Appointment.day)])

@app.post("/api/appointments/<int:aid>/reschedule")
@role_required("patient")
def reschedule(aid):
    a = Appointment.query.get_or_404(aid); d = request.json
    if a.patient_id != current_user.id or a.status != "confirmed": return jsonify(error="cannot reschedule"), 409
    day = date.fromisoformat(d["date"]); start = d["start"]
    if start not in free_slots(a.doctor, day): return jsonify(error="slot unavailable"), 409
    key = f"{a.doctor_id}-{day}-{start}"
    stale = Appointment.query.filter_by(slot_key=key).first()
    if stale:
        if stale.status == "held" and stale.hold_expires < datetime.utcnow(): stale.slot_key = None; stale.status = "expired"; db.session.flush()
        else: return jsonify(error="slot just taken"), 409
    a.day, a.start, a.slot_key = day, start, key
    try: db.session.commit()
    except IntegrityError: db.session.rollback(); return jsonify(error="slot just taken"), 409
    cal_sync(a, "upsert")
    for u in (current_user, a.doctor.user):
        queue_email(u.email, "Appointment rescheduled", f"New time: {day} at {start}.")
    db.session.commit(); return jsonify(status="rescheduled", day=str(day), start=start)

# ---------------- Medication tracker ----------------
@app.get("/api/my/doses")
@role_required("patient")
def my_doses():
    start = datetime.combine(now_local().date() - timedelta(days=2), datetime.min.time()); now = now_local()
    rows = Reminder.query.filter(Reminder.patient_id == current_user.id, Reminder.send_at >= start).order_by(Reminder.send_at).limit(120).all()
    return jsonify([dict(id=r.id, text=r.text, at=r.send_at.strftime("%Y-%m-%d %H:%M"), taken=bool(r.taken),
                         missed=(not r.taken and r.send_at < now - timedelta(hours=2))) for r in rows])

@app.post("/api/doses/<int:rid>/taken")
@role_required("patient")
def dose_taken(rid):
    r = Reminder.query.get_or_404(rid)
    if r.patient_id != current_user.id: return jsonify(error="forbidden"), 403
    r.taken = True; db.session.commit(); return jsonify(ok=True)

# ---------------- Admin analytics and email log ----------------
@app.get("/api/admin/stats")
@role_required("admin")
def admin_stats():
    appts = Appointment.query.filter(Appointment.status.in_(["confirmed", "completed", "cancelled"])).all()
    by, hours, per = {}, {}, {}
    for a in appts:
        by[a.status] = by.get(a.status, 0) + 1
        if a.status != "cancelled":
            hours[a.start[:2]] = hours.get(a.start[:2], 0) + 1
            n = "Dr. " + a.doctor.user.name; per[n] = per.get(n, 0) + 1
    total = len(appts)
    return jsonify(total=total, by_status=by, cancel_rate=round(100 * by.get("cancelled", 0) / total) if total else 0,
                   urgent=sum(1 for a in appts if a.is_emergency), hours=dict(sorted(hours.items())), per_doctor=per,
                   emails={s: Email.query.filter_by(status=s).count() for s in ("pending", "sent", "failed")})

@app.get("/api/admin/emails")
@role_required("admin")
def admin_emails():
    q = Email.query
    if request.args.get("status"): q = q.filter_by(status=request.args["status"])
    return jsonify([dict(id=e.id, to=e.to, subject=e.subject, status=e.status, attempts=e.attempts) for e in q.order_by(Email.id.desc()).limit(50)])

@app.post("/api/admin/emails/<int:eid>/retry")
@role_required("admin")
def retry_email(eid):
    e = Email.query.get_or_404(eid); e.status = "pending"; e.attempts = 0; db.session.commit(); return jsonify(ok=True)

# ---------------- Urgent booking ----------------
@app.post("/api/triage/check")
@login_required
def triage_check():
    f = llm.red_flags((request.json or {}).get("symptoms", ""))
    return jsonify(emergency=bool(f), red_flags=f)

@app.post("/api/appointments/urgent")
@role_required("patient")
def urgent():
    d = request.json or {}; symptoms = (d.get("symptoms") or "").strip()
    if not symptoms: return jsonify(error="please describe what is wrong"), 400
    spec = (d.get("specialisation") or "").strip()
    q = Doctor.query.filter_by(active=True)
    if spec: q = q.filter(Doctor.specialisation.ilike(f"%{spec}%"))
    today = now_local().date(); cands = []
    for doc in q:
        for s in free_slots(doc, today, emergency=True): cands.append((s, doc))   # includes urgent-only slots
    cands.sort(key=lambda c: c[0])
    for s, doc in cands:       # same unique slot_key guard as normal booking; on a race, try the next slot
        a = Appointment(patient_id=current_user.id, doctor_id=doc.id, day=today, start=s, status="confirming",
                        slot_key=f"{doc.id}-{today}-{s}", symptoms=symptoms, is_emergency=True)
        db.session.add(a)
        try: db.session.commit()
        except IntegrityError: db.session.rollback(); continue
        break
    else:
        return jsonify(error="No urgent slot is free today. If this is an emergency, call 112 or go to the nearest hospital."), 409
    ps = llm.pre_visit_summary(symptoms)
    a.pre_summary = json.dumps(ps); a.status = "confirmed"
    cal_sync(a, "upsert")
    queue_email(current_user.email, "Urgent appointment confirmed",
                f"Dr. {doc.user.name} today at {s}. If your condition gets worse, call 112 or go to the nearest hospital.", 1)
    queue_email(doc.user.email, "URGENT patient booked", f"{current_user.name} today at {s}.\nSymptoms: {symptoms}\nSummary: {a.pre_summary}", 1)
    db.session.commit()
    threading.Thread(target=send_emails, daemon=True).start()     # do not wait for the 30s job
    return jsonify(appointment_id=a.id, doctor="Dr. " + doc.user.name, day=str(today), start=s,
                   pre_summary=ps, emergency=bool(ps.get("red_flags"))), 201

# ---------------- Google Calendar OAuth ----------------
@app.get("/api/google/connect")
@login_required
def g_connect():
    try: return jsonify(url=cal.auth_url())
    except Exception as ex: return jsonify(error=f"Google OAuth not configured: {ex}"), 500

@app.get("/api/google/callback")
@login_required
def g_callback():
    try:
        tok = cal.exchange(request.args["code"])
        row = db.session.get(GoogleToken, current_user.id)
        if row: row.token = tok
        else: db.session.add(GoogleToken(user_id=current_user.id, token=tok))
        db.session.commit()
    except Exception as ex:
        print("oauth failed:", ex)
    return redirect("/")

# ---------------- Doctor ----------------
@app.post("/api/appointments/<int:aid>/visit")
@role_required("doctor")
def visit(aid):
    a = Appointment.query.get_or_404(aid); d = request.json
    if a.doctor.user_id != current_user.id: return jsonify(error="forbidden"), 403
    rx = d.get("prescription", [])  # [{"name":"Paracetamol","times":["08:00","20:00"],"days":3}]
    a.notes = d["notes"]; a.status = "completed"
    a.post_summary = llm.post_visit_summary(a.notes, rx)
    for m in rx:
        for i in range(int(m.get("days", 1))):
            for t in m["times"]:
                at = datetime.combine(now_local().date() + timedelta(days=i), datetime.strptime(t, "%H:%M").time())
                db.session.add(Reminder(patient_id=a.patient_id, send_at=at, text=f"Time to take {m['name']}"))
    p = db.session.get(User, a.patient_id)
    queue_email(p.email, "Your visit summary", a.post_summary)
    db.session.commit(); return jsonify(post_summary=a.post_summary)

# ---------------- Background jobs ----------------
def _send_emails_impl():
    with app.app_context():
        for e in Email.query.filter(Email.status == "pending", Email.attempts < 5).order_by(Email.priority.desc(), Email.id).limit(20):
            e.attempts += 1
            try:
                host = os.getenv("SMTP_HOST"); user = os.getenv("SMTP_USER")
                if host and user:
                    m = EmailMessage(); m["From"] = user; m["To"] = e.to; m["Subject"] = e.subject; m.set_content(e.body)
                    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", 587)), timeout=15) as s:
                        s.starttls(); s.login(user, os.getenv("SMTP_PASS", "")); s.send_message(m)
                else:
                    print(f"[EMAIL -> {e.to}] {e.subject}\n{e.body}\n")
                e.status = "sent"
            except Exception as ex:
                print("email failed, will retry:", ex)
                if e.attempts >= 5: e.status = "failed"
        db.session.commit()

_mail_lock = threading.Lock()

def send_emails():
    with _mail_lock: _send_emails_impl()    # scheduler and urgent-booking thread never send twice

def send_reminders():
    with app.app_context():
        for r in Reminder.query.filter(Reminder.sent == False, Reminder.send_at <= now_local()):  # noqa
            queue_email(db.session.get(User, r.patient_id).email, "Medication reminder", r.text); r.sent = True
        db.session.commit()

def send_appt_reminders():
    """Email patient and doctor when a confirmed appointment is within the next 24 hours."""
    with app.app_context():
        now = now_local()
        for a in Appointment.query.filter(Appointment.status == "confirmed", Appointment.reminded == False,  # noqa
                                          Appointment.day <= now.date() + timedelta(days=1)):
            when = datetime.combine(a.day, datetime.strptime(a.start, "%H:%M").time())
            if timedelta(0) < when - now <= timedelta(hours=24):
                txt = f"Reminder: appointment on {a.day} at {a.start} with Dr. {a.doctor.user.name}."
                queue_email(db.session.get(User, a.patient_id).email, "Appointment reminder", txt)
                queue_email(a.doctor.user.email, "Appointment reminder", txt)
                a.reminded = True
        db.session.commit()

def send_missed_alerts():
    """Email a patient when a dose reminder was sent more than 2 hours ago and is not marked taken."""
    with app.app_context():
        now = now_local()
        for r in Reminder.query.filter(Reminder.sent == True, Reminder.taken == False, Reminder.missed_alerted == False,  # noqa
                                       Reminder.send_at < now - timedelta(hours=2), Reminder.send_at > now - timedelta(hours=24)):
            queue_email(db.session.get(User, r.patient_id).email, "Missed dose?",
                        f"{r.text} was due at {r.send_at:%H:%M} and is not marked as taken. If you missed it, follow your doctor's advice or ask your pharmacist.")
            r.missed_alerted = True
        db.session.commit()

def init():
    with app.app_context():
        db.create_all()
        em = os.getenv("ADMIN_EMAIL", "admin@clinic.com")
        if not User.query.filter_by(email=em).first():
            db.session.add(User(name="Admin", email=em, role="admin",
                                password_hash=generate_password_hash(os.getenv("ADMIN_PASSWORD", "admin123"))))
            db.session.commit()
    sch = BackgroundScheduler(daemon=True)
    sch.add_job(send_emails, "interval", seconds=30); sch.add_job(send_reminders, "interval", seconds=60); sch.add_job(send_appt_reminders, "interval", minutes=5); sch.add_job(send_missed_alerts, "interval", minutes=10)
    sch.start()

init()
@app.get("/")
def home(): return render_template("index.html")

if __name__ == "__main__":
    app.run(debug=False, port=5000)
