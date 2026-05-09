from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import fitz
import base64, tempfile, os, json, sqlite3, time
from groq import Groq
import httpx
from datetime import datetime

import os
from dotenv import load_dotenv
load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
DB_PATH            = "aica.db"

groq_client = Groq(api_key=GROQ_API_KEY)

# ═══ DATABASE SETUP ═══
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            reg_no TEXT,
            company_type TEXT,
            industry TEXT,
            financial_year TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            filename TEXT,
            doc_type TEXT,
            extracted_text TEXT,
            analysis TEXT,
            uploaded_at TEXT,
            FOREIGN KEY(company_id) REFERENCES companies(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS company_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER UNIQUE,
            revenue TEXT,
            gross_profit TEXT,
            net_profit TEXT,
            total_assets TEXT,
            total_liabilities TEXT,
            equity TEXT,
            current_assets TEXT,
            current_liabilities TEXT,
            notes TEXT,
            updated_at TEXT,
            FOREIGN KEY(company_id) REFERENCES companies(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            role TEXT,
            message TEXT,
            created_at TEXT,
            FOREIGN KEY(company_id) REFERENCES companies(id)
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ═══ AI FALLBACK ═══
def call_ai(system: str, messages: list) -> tuple[str, str]:
    try:
        print("Trying Groq...")
        r = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system}] + messages,
            max_tokens=2048, temperature=0.3
        )
        return r.choices[0].message.content, "Groq (Llama 3.3 70B)"
    except Exception as e:
        print(f"Groq failed: {str(e)[:80]} — trying OpenRouter...")

    for model in ["mistralai/mistral-7b-instruct:free", "meta-llama/llama-3.1-8b-instruct:free"]:
        try:
            print(f"Trying OpenRouter {model}...")
            r = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "http://localhost",
                    "X-Title": "AI CA Sri Lanka"
                },
                json={
                    "model": model,
                    "messages": [{"role": "system", "content": system}] + messages,
                    "max_tokens": 2048
                },
                timeout=60.0
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"], f"OpenRouter ({model})"
        except Exception as e2:
            print(f"OpenRouter {model} failed: {str(e2)[:60]}")

    raise Exception("All AI providers failed.")

def get_company_context(company_id: int) -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT * FROM companies WHERE id=?", (company_id,))
    comp = c.fetchone()
    if not comp:
        conn.close()
        return ""

    c.execute("SELECT * FROM company_data WHERE company_id=?", (company_id,))
    data = c.fetchone()

    c.execute("SELECT filename, doc_type, analysis FROM documents WHERE company_id=? ORDER BY uploaded_at DESC LIMIT 5", (company_id,))
    docs = c.fetchall()
    conn.close()

    ctx = f"""COMPANY: {comp[1]}
Registration No: {comp[2] or 'N/A'}
Type: {comp[3] or 'N/A'}
Industry: {comp[4] or 'N/A'}
Financial Year: {comp[5] or 'N/A'}
"""
    if data:
        ctx += f"""
FINANCIAL DATA:
Revenue: {data[2] or 'N/A'}
Gross Profit: {data[3] or 'N/A'}
Net Profit: {data[4] or 'N/A'}
Total Assets: {data[5] or 'N/A'}
Total Liabilities: {data[6] or 'N/A'}
Equity: {data[7] or 'N/A'}
Current Assets: {data[8] or 'N/A'}
Current Liabilities: {data[9] or 'N/A'}
Additional Notes: {data[10] or 'N/A'}
"""
    if docs:
        ctx += "\nUPLOADED DOCUMENTS & ANALYSIS:\n"
        for doc in docs:
            ctx += f"\n--- {doc[0]} ({doc[1]}) ---\n{doc[2] or 'No analysis yet'}\n"

    return ctx

# ═══ APP ═══
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

CA_SYSTEM = """You are an expert Chartered Accountant (CA) specialising in Sri Lanka accounting standards (SLFRS/LKAS), IRD tax regulations, and Companies Act. 
Be specific with numbers, cite relevant Sri Lanka regulations, and give practical advice. 
If the user writes in Sinhala, reply in Sinhala."""

# ─── MODELS ───
class CompanyCreate(BaseModel):
    name: str
    reg_no: Optional[str] = ""
    company_type: Optional[str] = ""
    industry: Optional[str] = ""
    financial_year: Optional[str] = ""

class FinancialData(BaseModel):
    company_id: int
    revenue: Optional[str] = ""
    gross_profit: Optional[str] = ""
    net_profit: Optional[str] = ""
    total_assets: Optional[str] = ""
    total_liabilities: Optional[str] = ""
    equity: Optional[str] = ""
    current_assets: Optional[str] = ""
    current_liabilities: Optional[str] = ""
    notes: Optional[str] = ""

class ChatRequest(BaseModel):
    company_id: int
    message: str

class PDFRequest(BaseModel):
    company_id: int
    filename: str
    doc_type: Optional[str] = "Financial Statement"
    b64data: str

# ─── COMPANY ROUTES ───
@app.get("/")
def root():
    return {"status": "AI CA running"}

@app.get("/api/companies")
def get_companies():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, reg_no, company_type, industry, financial_year, created_at FROM companies ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return [{"id":r[0],"name":r[1],"reg_no":r[2],"company_type":r[3],"industry":r[4],"financial_year":r[5],"created_at":r[6]} for r in rows]

@app.post("/api/companies")
def create_company(req: CompanyCreate):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM companies")
    count = c.fetchone()[0]
    if count >= 3:
        conn.close()
        raise HTTPException(status_code=400, detail="Maximum 3 companies allowed. Delete one to add new.")
    c.execute("INSERT INTO companies (name,reg_no,company_type,industry,financial_year,created_at) VALUES (?,?,?,?,?,?)",
              (req.name, req.reg_no, req.company_type, req.industry, req.financial_year, datetime.now().isoformat()))
    conn.commit()
    company_id = c.lastrowid
    conn.close()
    return {"id": company_id, "message": f"Company '{req.name}' created successfully"}

@app.delete("/api/companies/{company_id}")
def delete_company(company_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM documents WHERE company_id=?", (company_id,))
    c.execute("DELETE FROM company_data WHERE company_id=?", (company_id,))
    c.execute("DELETE FROM chat_memory WHERE company_id=?", (company_id,))
    c.execute("DELETE FROM companies WHERE id=?", (company_id,))
    conn.commit()
    conn.close()
    return {"message": "Company deleted"}

@app.get("/api/companies/{company_id}")
def get_company(company_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM companies WHERE id=?", (company_id,))
    comp = c.fetchone()
    c.execute("SELECT * FROM company_data WHERE company_id=?", (company_id,))
    data = c.fetchone()
    c.execute("SELECT id,filename,doc_type,analysis,uploaded_at FROM documents WHERE company_id=? ORDER BY uploaded_at DESC", (company_id,))
    docs = c.fetchall()
    conn.close()
    if not comp:
        raise HTTPException(status_code=404, detail="Company not found")
    return {
        "company": {"id":comp[0],"name":comp[1],"reg_no":comp[2],"company_type":comp[3],"industry":comp[4],"financial_year":comp[5]},
        "financial_data": {"revenue":data[2],"gross_profit":data[3],"net_profit":data[4],"total_assets":data[5],"total_liabilities":data[6],"equity":data[7],"current_assets":data[8],"current_liabilities":data[9],"notes":data[10]} if data else {},
        "documents": [{"id":d[0],"filename":d[1],"doc_type":d[2],"analysis":d[3],"uploaded_at":d[4]} for d in docs]
    }

# ─── FINANCIAL DATA ───
@app.post("/api/financial-data")
def save_financial_data(req: FinancialData):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM company_data WHERE company_id=?", (req.company_id,))
    existing = c.fetchone()
    now = datetime.now().isoformat()
    if existing:
        c.execute("""UPDATE company_data SET revenue=?,gross_profit=?,net_profit=?,total_assets=?,
                  total_liabilities=?,equity=?,current_assets=?,current_liabilities=?,notes=?,updated_at=?
                  WHERE company_id=?""",
                  (req.revenue,req.gross_profit,req.net_profit,req.total_assets,req.total_liabilities,
                   req.equity,req.current_assets,req.current_liabilities,req.notes,now,req.company_id))
    else:
        c.execute("""INSERT INTO company_data (company_id,revenue,gross_profit,net_profit,total_assets,
                  total_liabilities,equity,current_assets,current_liabilities,notes,updated_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                  (req.company_id,req.revenue,req.gross_profit,req.net_profit,req.total_assets,
                   req.total_liabilities,req.equity,req.current_assets,req.current_liabilities,req.notes,now))
    conn.commit()
    conn.close()
    return {"message": "Financial data saved"}

# ─── PDF UPLOAD & ANALYSE ───
@app.post("/api/analyze-pdf")
async def analyze_pdf(req: PDFRequest):
    tmp_path = None
    try:
        b64 = req.b64data.split(",")[1] if "," in req.b64data else req.b64data
        pdf_bytes = base64.b64decode(b64)
        print(f"PDF: {req.filename} | {len(pdf_bytes)/1024/1024:.2f} MB")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        doc = fitz.open(tmp_path)
        text_parts = []
        for i, page in enumerate(doc):
            t = page.get_text()
            if t.strip():
                text_parts.append(f"[Page {i+1}]\n{t}")
        doc.close()

        extracted = "\n\n".join(text_parts)
        if len(extracted) < 50:
            return {"reply": "Error: PDF text extract කරන්න බැරි වුණා. Scanned PDF නම් text-based PDF use කරන්න.", "provider": "none"}

        if len(extracted) > 12000:
            extracted = extracted[:12000] + "\n[...truncated...]"

        comp_ctx = get_company_context(req.company_id)

        prompt = f"""Analyse this financial document for the company below.

{comp_ctx}

DOCUMENT: {req.filename} ({req.doc_type})

EXTRACTED TEXT:
{extracted}

Provide structured analysis:
1. **Document Type & Period**
2. **Key Financial Figures** (exact numbers)
3. **Financial Ratios** (Current Ratio, Gross/Net Margin, ROE, D/E)
4. **Sri Lanka Compliance** (SLFRS/IRD/Companies Act observations)
5. **Audit Flags** (red flags or concerns)
6. **Top 3 Recommendations**

Bold headings. Exact numbers only."""

        reply, provider = call_ai(CA_SYSTEM, [{"role": "user", "content": prompt}])

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""INSERT INTO documents (company_id,filename,doc_type,extracted_text,analysis,uploaded_at)
                  VALUES (?,?,?,?,?,?)""",
                  (req.company_id, req.filename, req.doc_type, extracted[:5000], reply, datetime.now().isoformat()))
        conn.commit()
        conn.close()

        print(f"PDF analysed by: {provider}")
        return {"reply": f"*Analysed by: {provider}*\n\n{reply}", "provider": provider}

    except Exception as e:
        print(f"Error: {e}")
        return {"reply": f"Error: {e}", "provider": "none"}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

# ─── CHATBOT WITH MEMORY ───
@app.post("/api/chat")
async def chat(req: ChatRequest):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT role, message FROM chat_memory WHERE company_id=? ORDER BY created_at DESC LIMIT 10", (req.company_id,))
        history_raw = c.fetchall()
        conn.close()

        history = [{"role": r[0], "content": r[1]} for r in reversed(history_raw)]
        history.append({"role": "user", "content": req.message})

        comp_ctx = get_company_context(req.company_id)
        system = f"""{CA_SYSTEM}

You have full access to this company's data and documents:
{comp_ctx}

Use the company data above to answer questions accurately. 
If data is missing, say so and suggest what information would help.
Remember previous conversation context."""

        reply, provider = call_ai(system, history)

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute("INSERT INTO chat_memory (company_id,role,message,created_at) VALUES (?,?,?,?)",
                  (req.company_id, "user", req.message, now))
        c.execute("INSERT INTO chat_memory (company_id,role,message,created_at) VALUES (?,?,?,?)",
                  (req.company_id, "assistant", reply, now))
        c.execute("""DELETE FROM chat_memory WHERE company_id=? AND id NOT IN 
                  (SELECT id FROM chat_memory WHERE company_id=? ORDER BY created_at DESC LIMIT 40)""",
                  (req.company_id, req.company_id))
        conn.commit()
        conn.close()

        return {"reply": reply, "provider": provider}
    except Exception as e:
        print(f"Chat error: {e}")
        return {"reply": f"Error: {e}", "provider": "none"}

@app.delete("/api/chat/{company_id}")
def clear_chat(company_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM chat_memory WHERE company_id=?", (company_id,))
    conn.commit()
    conn.close()
    return {"message": "Chat history cleared"}

@app.get("/api/chat/{company_id}/history")
def get_chat_history(company_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role,message,created_at FROM chat_memory WHERE company_id=? ORDER BY created_at ASC LIMIT 40", (company_id,))
    rows = c.fetchall()
    conn.close()
    return [{"role":r[0],"message":r[1],"time":r[2]} for r in rows]

# ─── AI ANALYSIS (dashboard panels) ───
class AnalysisRequest(BaseModel):
    company_id: int
    analysis_type: str

@app.post("/api/analyse")
async def run_analysis(req: AnalysisRequest):
    try:
        ctx = get_company_context(req.company_id)
        if not ctx:
            return {"reply": "Company data not found.", "provider": "none"}

        prompts = {
            "dashboard": f"Based on this company data, provide 5 key financial insights, main strengths, risks, and 2 immediate action items:\n{ctx}",
            "pl": f"Analyse the P&L performance of this company. Comment on revenue quality, margin trends, cost structure, and tax efficiency:\n{ctx}",
            "balance": f"Analyse the Balance Sheet. Comment on asset quality, working capital, capital structure, and Companies Act compliance:\n{ctx}",
            "ratios": f"Calculate and comment on all available financial ratios. Compare to Sri Lanka industry benchmarks where possible:\n{ctx}",
            "cashflow": f"Analyse cash flow sustainability, CAPEX strategy, and liquidity risk based on available data:\n{ctx}",
            "audit": f"Identify all audit risks and compliance issues. Provide specific remediation steps for each, referencing Sri Lanka law:\n{ctx}",
            "tax": f"Provide Sri Lanka tax planning advice, applicable IRD deductions, and VAT compliance tips based on company data:\n{ctx}"
        }

        prompt = prompts.get(req.analysis_type, f"Provide financial analysis for: {req.analysis_type}\n{ctx}")
        reply, provider = call_ai(CA_SYSTEM, [{"role": "user", "content": prompt}])
        return {"reply": reply, "provider": provider}
    except Exception as e:
        return {"reply": f"Error: {e}", "provider": "none"}