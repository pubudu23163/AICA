from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import fitz
import base64, tempfile, os, sqlite3
from groq import Groq
import httpx
from datetime import datetime, date

GROQ_API_KEY       = "YOUR_GROQ_API_KEY_HERE"
OPENROUTER_API_KEY = "YOUR_OPENROUTER_API_KEY_HERE"
DB_PATH            = "aica.db"
groq_client        = Groq(api_key=GROQ_API_KEY)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, reg_no TEXT, company_type TEXT,
        industry TEXT, financial_year TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER, filename TEXT, doc_type TEXT,
        extracted_text TEXT, analysis TEXT, uploaded_at TEXT);
    CREATE TABLE IF NOT EXISTS company_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER UNIQUE,
        revenue TEXT, gross_profit TEXT, net_profit TEXT,
        total_assets TEXT, total_liabilities TEXT, equity TEXT,
        current_assets TEXT, current_liabilities TEXT,
        prev_revenue TEXT, prev_net_profit TEXT, prev_total_assets TEXT,
        epf_rate TEXT DEFAULT '8', etf_rate TEXT DEFAULT '3',
        total_employees TEXT, monthly_payroll TEXT,
        income_tax_rate TEXT, vat_registered TEXT DEFAULT 'Yes',
        notes TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS chat_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER, role TEXT, message TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS kpi_goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER, kpi_name TEXT, target_value TEXT,
        current_value TEXT, unit TEXT, deadline TEXT, created_at TEXT);
    """)
    conn.commit(); conn.close()

init_db()

def call_ai(system, messages):
    try:
        r = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":system}]+messages,
            max_tokens=2048, temperature=0.3)
        return r.choices[0].message.content, "Groq"
    except Exception as e:
        print(f"Groq: {str(e)[:80]}")
    for model in ["mistralai/mistral-7b-instruct:free","meta-llama/llama-3.1-8b-instruct:free"]:
        try:
            r = httpx.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization":f"Bearer {OPENROUTER_API_KEY}","Content-Type":"application/json",
                         "HTTP-Referer":"http://localhost","X-Title":"AI CA SL"},
                json={"model":model,"messages":[{"role":"system","content":system}]+messages,"max_tokens":2048},
                timeout=60.0)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"], f"OpenRouter"
        except Exception as e2:
            print(f"OR {model}: {str(e2)[:50]}")
    raise Exception("All providers failed.")

def get_ctx(cid):
    conn = sqlite3.connect(DB_PATH)
    comp = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
    if not comp: conn.close(); return ""
    d = conn.execute("SELECT * FROM company_data WHERE company_id=?", (cid,)).fetchone()
    docs = conn.execute("SELECT filename,doc_type,analysis FROM documents WHERE company_id=? ORDER BY uploaded_at DESC LIMIT 5", (cid,)).fetchall()
    kpis = conn.execute("SELECT kpi_name,target_value,current_value,unit,deadline FROM kpi_goals WHERE company_id=?", (cid,)).fetchall()
    conn.close()
    ctx = f"COMPANY: {comp[1]} | Reg: {comp[2] or 'N/A'} | Type: {comp[3] or 'N/A'} | Industry: {comp[4] or 'N/A'} | FY: {comp[5] or 'N/A'}\n"
    if d:
        ctx += f"""FINANCIALS: Revenue={d[2] or 'N/A'} | Gross Profit={d[3] or 'N/A'} | Net Profit={d[4] or 'N/A'}
Assets={d[5] or 'N/A'} | Liabilities={d[6] or 'N/A'} | Equity={d[7] or 'N/A'}
Current Assets={d[8] or 'N/A'} | Current Liabilities={d[9] or 'N/A'}
PREV YEAR: Revenue={d[10] or 'N/A'} | Net Profit={d[11] or 'N/A'} | Assets={d[12] or 'N/A'}
PAYROLL: Staff={d[15] or 'N/A'} | Monthly={d[16] or 'N/A'} | EPF={d[13] or '8'}% | ETF={d[14] or '3'}%
TAX: Rate={d[17] or 'N/A'} | VAT={d[18] or 'N/A'} | Notes={d[19] or 'N/A'}\n"""
    if docs:
        ctx += "DOCUMENTS:\n"
        for doc in docs: ctx += f"--- {doc[0]} ({doc[1]}) ---\n{doc[2] or 'No analysis'}\n"
    if kpis:
        ctx += "KPI GOALS:\n"
        for k in kpis: ctx += f"- {k[0]}: Target={k[1]}{k[3]}, Current={k[2]}{k[3]}, Deadline={k[4]}\n"
    return ctx

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

CA = """You are an expert Chartered Accountant (CA) in Sri Lanka. You specialise in:
SLFRS/LKAS standards, IRD income tax/VAT/WHT/PAYE/SSCL, Companies Act, EPF/ETF Act, BOI regulations.
Be specific with numbers, cite Sri Lanka laws. If user writes Sinhala, reply Sinhala.
Today: """ + date.today().isoformat()

class Co(BaseModel):
    name:str; reg_no:Optional[str]=""; company_type:Optional[str]=""
    industry:Optional[str]=""; financial_year:Optional[str]=""

class FD(BaseModel):
    company_id:int; revenue:Optional[str]=""; gross_profit:Optional[str]=""; net_profit:Optional[str]=""
    total_assets:Optional[str]=""; total_liabilities:Optional[str]=""; equity:Optional[str]=""
    current_assets:Optional[str]=""; current_liabilities:Optional[str]=""
    prev_revenue:Optional[str]=""; prev_net_profit:Optional[str]=""; prev_total_assets:Optional[str]=""
    epf_rate:Optional[str]="8"; etf_rate:Optional[str]="3"
    total_employees:Optional[str]=""; monthly_payroll:Optional[str]=""
    income_tax_rate:Optional[str]=""; vat_registered:Optional[str]="Yes"; notes:Optional[str]=""

class Chat(BaseModel):
    company_id:int; message:str

class PDF(BaseModel):
    company_id:int; filename:str; doc_type:Optional[str]="Financial Statement"; b64data:str

class Analysis(BaseModel):
    company_id:int; analysis_type:str

class KPI(BaseModel):
    company_id:int; kpi_name:str; target_value:str; current_value:str; unit:str; deadline:str

@app.get("/")
def root(): return {"status":"running"}

@app.get("/api/companies")
def get_companies():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id,name,reg_no,company_type,industry,financial_year,created_at FROM companies ORDER BY created_at DESC").fetchall()
    conn.close()
    return [{"id":r[0],"name":r[1],"reg_no":r[2],"company_type":r[3],"industry":r[4],"financial_year":r[5],"created_at":r[6]} for r in rows]

@app.post("/api/companies")
def create_company(req: Co):
    conn = sqlite3.connect(DB_PATH)
    if conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] >= 3:
        conn.close(); raise HTTPException(400,"Maximum 3 companies allowed.")
    conn.execute("INSERT INTO companies (name,reg_no,company_type,industry,financial_year,created_at) VALUES (?,?,?,?,?,?)",
                 (req.name,req.reg_no,req.company_type,req.industry,req.financial_year,datetime.now().isoformat()))
    conn.commit()
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return {"id":cid,"message":f"'{req.name}' created"}

@app.delete("/api/companies/{cid}")
def delete_company(cid:int):
    conn = sqlite3.connect(DB_PATH)
    for t in ["documents","company_data","chat_memory","kpi_goals"]:
        conn.execute(f"DELETE FROM {t} WHERE company_id=?", (cid,))
    conn.execute("DELETE FROM companies WHERE id=?", (cid,))
    conn.commit(); conn.close()
    return {"message":"Deleted"}

@app.get("/api/companies/{cid}")
def get_company(cid:int):
    conn = sqlite3.connect(DB_PATH)
    comp = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
    if not comp: conn.close(); raise HTTPException(404,"Not found")
    d = conn.execute("SELECT * FROM company_data WHERE company_id=?", (cid,)).fetchone()
    docs = conn.execute("SELECT id,filename,doc_type,analysis,uploaded_at FROM documents WHERE company_id=? ORDER BY uploaded_at DESC", (cid,)).fetchall()
    kpis = conn.execute("SELECT * FROM kpi_goals WHERE company_id=?", (cid,)).fetchall()
    conn.close()
    keys=["id","company_id","revenue","gross_profit","net_profit","total_assets","total_liabilities",
          "equity","current_assets","current_liabilities","prev_revenue","prev_net_profit","prev_total_assets",
          "epf_rate","etf_rate","total_employees","monthly_payroll","income_tax_rate","vat_registered","notes","updated_at"]
    return {
        "company":{"id":comp[0],"name":comp[1],"reg_no":comp[2],"company_type":comp[3],"industry":comp[4],"financial_year":comp[5]},
        "financial_data": dict(zip(keys,d)) if d else {},
        "documents":[{"id":x[0],"filename":x[1],"doc_type":x[2],"analysis":x[3],"uploaded_at":x[4]} for x in docs],
        "kpi_goals":[{"id":k[0],"kpi_name":k[2],"target_value":k[3],"current_value":k[4],"unit":k[5],"deadline":k[6]} for k in kpis]
    }

@app.post("/api/financial-data")
def save_fd(req:FD):
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now().isoformat()
    v=(req.revenue,req.gross_profit,req.net_profit,req.total_assets,req.total_liabilities,req.equity,
       req.current_assets,req.current_liabilities,req.prev_revenue,req.prev_net_profit,req.prev_total_assets,
       req.epf_rate,req.etf_rate,req.total_employees,req.monthly_payroll,req.income_tax_rate,req.vat_registered,req.notes,now)
    if conn.execute("SELECT id FROM company_data WHERE company_id=?", (req.company_id,)).fetchone():
        conn.execute("""UPDATE company_data SET revenue=?,gross_profit=?,net_profit=?,total_assets=?,
            total_liabilities=?,equity=?,current_assets=?,current_liabilities=?,prev_revenue=?,prev_net_profit=?,
            prev_total_assets=?,epf_rate=?,etf_rate=?,total_employees=?,monthly_payroll=?,income_tax_rate=?,
            vat_registered=?,notes=?,updated_at=? WHERE company_id=?""", v+(req.company_id,))
    else:
        conn.execute("""INSERT INTO company_data (revenue,gross_profit,net_profit,total_assets,total_liabilities,
            equity,current_assets,current_liabilities,prev_revenue,prev_net_profit,prev_total_assets,epf_rate,etf_rate,
            total_employees,monthly_payroll,income_tax_rate,vat_registered,notes,updated_at,company_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", v+(req.company_id,))
    conn.commit(); conn.close()
    return {"message":"Saved"}

@app.post("/api/analyze-pdf")
async def analyze_pdf(req:PDF):
    tmp = None
    try:
        b64 = req.b64data.split(",")[1] if "," in req.b64data else req.b64data
        pdf_bytes = base64.b64decode(b64)
        print(f"PDF: {req.filename} {len(pdf_bytes)/1024/1024:.1f}MB")
        with tempfile.NamedTemporaryFile(delete=False,suffix=".pdf") as f:
            f.write(pdf_bytes); tmp=f.name
        doc = fitz.open(tmp)
        pages = [f"[Page {i+1}]\n{p.get_text()}" for i,p in enumerate(doc) if p.get_text().strip()]
        doc.close()
        text = "\n\n".join(pages)
        if len(text)<50: return {"reply":"Error: Scanned PDF — text extract කරන්න බැරි වුණා.","provider":"none"}
        if len(text)>12000: text=text[:12000]+"\n[truncated]"
        prompt = f"""{get_ctx(req.company_id)}
DOCUMENT: {req.filename} ({req.doc_type})
TEXT:\n{text}

Analyse and provide:
1. **Document Type & Period**
2. **Key Financial Figures** (exact numbers)
3. **Financial Ratios** (Current Ratio, Margins, ROE, D/E)
4. **Year-on-Year Changes** (if previous year data available)
5. **Sri Lanka Compliance** (SLFRS/IRD/Companies Act)
6. **Audit Flags** (risks and red flags)
7. **Top 3 Recommendations**"""
        reply, provider = call_ai(CA, [{"role":"user","content":prompt}])
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO documents (company_id,filename,doc_type,extracted_text,analysis,uploaded_at) VALUES (?,?,?,?,?,?)",
                     (req.company_id,req.filename,req.doc_type,text[:5000],reply,datetime.now().isoformat()))
        conn.commit(); conn.close()
        return {"reply":f"*{provider}*\n\n{reply}","provider":provider}
    except Exception as e:
        return {"reply":f"Error: {e}","provider":"none"}
    finally:
        if tmp and os.path.exists(tmp): os.remove(tmp)

@app.post("/api/chat")
async def chat_ep(req:Chat):
    try:
        conn = sqlite3.connect(DB_PATH)
        hist = conn.execute("SELECT role,message FROM chat_memory WHERE company_id=? ORDER BY created_at DESC LIMIT 10", (req.company_id,)).fetchall()
        conn.close()
        msgs = [{"role":r[0],"content":r[1]} for r in reversed(hist)]
        msgs.append({"role":"user","content":req.message})
        sys = f"""{CA}
COMPANY DATA:\n{get_ctx(req.company_id)}
Use real numbers above. Be proactive — suggest actions owner should take.
If missing data, ask clarifying questions."""
        reply, provider = call_ai(sys, msgs)
        conn = sqlite3.connect(DB_PATH)
        now = datetime.now().isoformat()
        conn.execute("INSERT INTO chat_memory (company_id,role,message,created_at) VALUES (?,?,?,?)", (req.company_id,"user",req.message,now))
        conn.execute("INSERT INTO chat_memory (company_id,role,message,created_at) VALUES (?,?,?,?)", (req.company_id,"assistant",reply,now))
        conn.execute("DELETE FROM chat_memory WHERE company_id=? AND id NOT IN (SELECT id FROM chat_memory WHERE company_id=? ORDER BY created_at DESC LIMIT 40)", (req.company_id,req.company_id))
        conn.commit(); conn.close()
        return {"reply":reply,"provider":provider}
    except Exception as e:
        return {"reply":f"Error: {e}","provider":"none"}

@app.delete("/api/chat/{cid}")
def clear_chat(cid:int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM chat_memory WHERE company_id=?", (cid,))
    conn.commit(); conn.close()
    return {"message":"Cleared"}

@app.get("/api/chat/{cid}/history")
def chat_history(cid:int):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT role,message,created_at FROM chat_memory WHERE company_id=? ORDER BY created_at ASC LIMIT 40", (cid,)).fetchall()
    conn.close()
    return [{"role":r[0],"message":r[1],"time":r[2]} for r in rows]

@app.post("/api/analyse")
async def analyse(req:Analysis):
    ctx = get_ctx(req.company_id)
    if not ctx: return {"reply":"Company data not found.","provider":"none"}
    prompts = {
        "dashboard": f"Give 6 key executive insights about this company's financial health. Include strengths, risks, and 2 urgent actions.\n{ctx}",
        "pl": f"Detailed P&L analysis. Revenue quality, margin trends, cost structure, tax efficiency, YoY comparison.\n{ctx}",
        "balance": f"Balance sheet analysis. Asset quality, working capital, capital structure, Companies Act compliance.\n{ctx}",
        "ratios": f"Calculate ALL ratios possible from data. Compare to Sri Lanka industry benchmarks. Flag any ratios needing attention.\n{ctx}",
        "cashflow": f"Cash flow analysis. Sustainability, liquidity risk, working capital cycle, recommendations.\n{ctx}",
        "audit": f"Full audit risk assessment. For each risk: severity, Sri Lanka law reference, specific remediation steps.\n{ctx}",
        "tax": f"Sri Lanka tax planning advice. Income tax optimisation, VAT compliance, PAYE, WHT, SSCL calculations and tips.\n{ctx}",
        "payroll": f"""Calculate payroll statutory obligations for Sri Lanka:
EPF (Employee 8%, Employer 12%), ETF (Employer 3%), PAYE tax.
Data: {ctx}
Show: monthly EPF/ETF amounts, PAYE brackets, total cost to company, net pay estimate, filing deadlines.""",
        "forecast": f"""Based on financial data and trends, forecast next year:
1. Revenue forecast (with growth assumptions)
2. Profit forecast
3. Cash flow projection
4. Key risks to forecast
5. Assumptions made
Data: {ctx}""",
        "health": f"""Calculate a Financial Health Score (0-100) for this Sri Lanka company.
Score each area: Liquidity (20pts), Profitability (20pts), Solvency (20pts), Efficiency (20pts), Compliance (20pts).
Give: total score, grade (A/B/C/D/F), breakdown, and top 3 improvement areas.
Data: {ctx}""",
        "deadlines": f"""List ALL upcoming Sri Lanka statutory deadlines for this company:
- IRD income tax filing
- VAT returns (monthly/quarterly)  
- WHT payments
- PAYE monthly
- SSCL
- EPF/ETF payments (due 15th of following month)
- Companies Act annual return
- BOI reporting (if applicable)
Company: {ctx}
Format as a clear deadline calendar with dates and penalties for late filing.""",
        "kpi": f"""Analyse KPI goals and provide:
1. Progress assessment for each KPI
2. Whether targets are achievable
3. Specific actions to hit each target
4. Warning flags for any off-track KPIs
Data: {ctx}"""
    }
    prompt = prompts.get(req.analysis_type, f"Provide analysis for: {req.analysis_type}\n{ctx}")
    try:
        reply, provider = call_ai(CA, [{"role":"user","content":prompt}])
        return {"reply":reply,"provider":provider}
    except Exception as e:
        return {"reply":f"Error: {e}","provider":"none"}

@app.post("/api/kpi")
def add_kpi(req:KPI):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO kpi_goals (company_id,kpi_name,target_value,current_value,unit,deadline,created_at) VALUES (?,?,?,?,?,?,?)",
                 (req.company_id,req.kpi_name,req.target_value,req.current_value,req.unit,req.deadline,datetime.now().isoformat()))
    conn.commit(); conn.close()
    return {"message":"KPI added"}

@app.delete("/api/kpi/{kid}")
def delete_kpi(kid:int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM kpi_goals WHERE id=?", (kid,))
    conn.commit(); conn.close()
    return {"message":"Deleted"}