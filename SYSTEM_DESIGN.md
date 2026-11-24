# System Design

**Double-booking prevention.** Every active appointment (held or confirmed) carries `slot_key = doctor-date-time`, which has a UNIQUE constraint. Two simultaneous requests both pass the availability check, but only one INSERT succeeds; the other raises IntegrityError and receives HTTP 409. Safety lives in the database, not in application locks, so it holds across multiple workers. Cancelling or rescheduling sets `slot_key` to NULL, which frees the slot while keeping history.

**Slot hold mechanism.** Choosing a slot creates a `held` appointment with `hold_expires = now + 5 min`. The slot is reserved while the patient fills the symptom form. Confirming within the window converts it to `confirmed`. An expired hold is treated as free: availability queries ignore it, and the next booking attempt releases its key (status `expired`) inside the same transaction before inserting. No cleanup job is needed for correctness.

**Doctor leave conflicts.** Leave is stored per doctor per day (unique pair). Slot listing returns nothing on leave days. When admin marks leave, one transaction finds all held/confirmed appointments that day, cancels them, frees their slot keys, deletes calendar events, and queues a cancellation email to each patient. The response reports how many patients were notified, so the admin knows the impact.

**Notification failure handling.** Emails are never sent inline. They are written to an `email` outbox table in the same transaction as the business change, so a booking never succeeds without its notification being recorded. A background scheduler sends pending emails every 30 seconds, increments `attempts`, and retries up to 5 times before marking `failed`. An SMTP outage therefore cannot fail a request. Medication reminders use the same path: each dose is a `reminder` row with a `send_at` time, and a job converts due rows into queued emails.

**Calendar failures.** Google Calendar sync is best effort: errors are logged and never roll back a booking. Event IDs are stored on the appointment so later reschedules update, and cancellations delete, the same events.

**LLM failure handling.** The pre-visit summary runs in a LangGraph pipeline (retrieve, generate, validate). RAG retrieval failure yields empty context; model failure or malformed JSON yields a deterministic fallback (urgency "Unknown", symptoms as the chief complaint). The post-visit summary falls back to the raw notes and prescription. Results are stored in the DB, so each is generated once.

**Trade-offs.** The scheduler runs in-process, so deploy with a single worker (or move it to a dedicated worker or cron service at scale). SQLite is used locally and PostgreSQL on Render.
