# DB Schema
```mermaid
erDiagram
  USER ||--o| DOCTOR : "is"
  USER ||--o{ APPOINTMENT : books
  DOCTOR ||--o{ APPOINTMENT : receives
  DOCTOR ||--o{ LEAVE : takes
  USER ||--o| GOOGLE_TOKEN : has
  USER ||--o{ REMINDER : gets
  USER { int id PK; string name; string email UK; string password_hash; string role }
  DOCTOR { int id PK; int user_id FK; string specialisation; string work_start; string work_end; int slot_minutes }
  LEAVE { int id PK; int doctor_id FK; date day }
  APPOINTMENT { int id PK; int patient_id FK; int doctor_id FK; date day; string start; string status; string slot_key UK; datetime hold_expires; text symptoms; text pre_summary; text notes; text post_summary; string p_event; string d_event }
  EMAIL { int id PK; string to; string subject; text body; string status; int attempts }
  REMINDER { int id PK; int patient_id FK; datetime send_at; string text; bool sent }
  GOOGLE_TOKEN { int user_id PK; text token }
```
