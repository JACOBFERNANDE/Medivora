"""LLM layer: RAG (in-memory vector store) + LangGraph pipeline + safe fallbacks."""
import os, json, re
from typing import TypedDict
from langgraph.graph import StateGraph, END

KB = [
    "Chest pain, difficulty breathing, fainting or stroke signs (face droop, slurred speech) are HIGH urgency.",
    "Fever above 103F, persistent vomiting, severe dehydration or blood in stool/urine are HIGH urgency.",
    "Fever 100-103F for 2-3 days, persistent cough, moderate pain or infection signs are MEDIUM urgency.",
    "Mild cold, minor aches, mild rash or short-lived headache without red flags are LOW urgency.",
    "Useful doctor questions: onset and duration, severity (1-10), aggravating/relieving factors, past history, current medicines, allergies.",
]
_store = None

def _get_store():
    global _store
    if _store is None:
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        from langchain_core.vectorstores import InMemoryVectorStore
        emb = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001")
        _store = InMemoryVectorStore.from_texts(KB, emb)
    return _store

def _llm():
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"), temperature=0.2)

def _text(c):
    """Gemini may return a string or a list of content parts."""
    if isinstance(c, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return c if isinstance(c, str) else str(c)

def _json(text):
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0))

class S(TypedDict, total=False):
    symptoms: str; context: str; raw: str; result: dict

def retrieve(s: S):
    try:
        docs = _get_store().similarity_search(s["symptoms"], k=3)
        return {"context": "\n".join(d.page_content for d in docs)}
    except Exception as e:
        print("RAG ERROR:", e)
        return {"context": ""}          # RAG failure must not break flow

def generate(s: S):
    prompt = (
        "Analyse these symptoms and return ONLY JSON with keys: "
        '"urgency" (Low/Medium/High), "chief_complaint" (string), "questions" (list of exactly 3 strings).\n'
        f"Triage guidelines:\n{s.get('context','')}\n\nSymptoms: {s['symptoms']}"
    )
    try:
        return {"raw": _text(_llm().invoke(prompt).content)}
    except Exception as e:
        print("GEMINI ERROR:", e)
        return {"raw": ""}

def validate(s: S):
    try:
        r = _json(s["raw"])
        assert r["urgency"] in ("Low", "Medium", "High")
        return {"result": {"urgency": r["urgency"], "chief_complaint": r["chief_complaint"],
                           "questions": list(r["questions"])[:3], "source": "llm"}}
    except Exception as e:
        print("VALIDATE ERROR:", repr(e), "| RAW:", str(s.get("raw"))[:300])
        return {"result": {"urgency": "Unknown", "chief_complaint": s["symptoms"][:200],
                           "questions": [], "source": "fallback"}}

_g = StateGraph(S)
_g.add_node("retrieve", retrieve); _g.add_node("generate", generate); _g.add_node("validate", validate)
_g.set_entry_point("retrieve"); _g.add_edge("retrieve", "generate"); _g.add_edge("generate", "validate"); _g.add_edge("validate", END)
_graph = _g.compile()

def _pre_visit(symptoms: str) -> dict:
    try:
        return _graph.invoke({"symptoms": symptoms})["result"]
    except Exception as e:
        print("GRAPH ERROR:", repr(e))
        return {"urgency": "Unknown", "chief_complaint": symptoms[:200], "questions": [], "source": "fallback"}

def post_visit_summary(notes: str, prescription: list) -> str:
    prompt = ("Convert these clinical notes into a patient-friendly summary with medication schedule "
              f"and follow-up steps. Use simple language.\nNotes: {notes}\nPrescription: {json.dumps(prescription)}")
    try:
        return _text(_llm().invoke(prompt).content)
    except Exception as e:
        print("GEMINI ERROR:", e)
        return f"Visit notes: {notes}\nMedicines: {json.dumps(prescription)}\n(Auto-summary unavailable; please follow your doctor's instructions.)"


# --- red flags: fixed rules that work even when the AI is down ---
RED = [r"chest (pain|tight)", r"(can.?t|cannot|difficulty|trouble) breath", r"short(ness)? of breath", r"not breathing",
       r"stroke", r"slurred speech", r"face (is )?droop", r"seizure|convuls", r"unconscious|passed out|fainted|unresponsive",
       r"(heavy|severe|uncontrolled) bleeding|vomiting blood|coughing (up )?blood", r"choking", r"overdose|poison",
       r"suicid|kill myself", r"anaphyla|throat (is )?swelling", r"seene (me|mein) dard|saans (nahi|lene)|behosh"]

def red_flags(text):
    t = (text or "").lower()
    return [m.group(0) for p in RED if (m := re.search(p, t))]

def pre_visit_summary(symptoms: str) -> dict:
    r = _pre_visit(symptoms); f = red_flags(symptoms)
    if f: r = {**r, "urgency": "High", "red_flags": f}      # rules override the AI
    return r
