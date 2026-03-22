"""
MINI AGENT — Holon Swarm Worker
================================
Zasada pomocniczości: rozwiązuj lokalnie (Ollama free), eskaluj tylko gdy potrzeba (Haiku).
Koszt: $0 przez Ollama, $0.0002 przez Haiku — logarytmicznie → 0 gdy heal_memory rośnie.

Capabilities:
  - Code analysis & generation
  - Supabase operations
  - Coolify API calls  
  - Web crawling (via httpx)
  - File operations
  - Health checks
  - Knowledge extraction (feeds evolution engine)
"""

import os, json, asyncio, logging, time, httpx
from fastapi import FastAPI, BackgroundTasks, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone
import anthropic
from supabase import create_client

logging.basicConfig(level=logging.INFO, format='%(asctime)s [MINI] %(message)s')
log = logging.getLogger("mini-agent")

# ── Config ─────────────────────────────────────────────────────────────────────
AGENT_ID      = os.environ.get("AGENT_ID", f"mini-{os.getpid()}")
SWARM_ROUTER  = os.environ.get("SWARM_ROUTER_URL", "https://agent-swarm-router.ofshore-workers.dev")
SWARM_SECRET  = os.environ.get("SWARM_SECRET", "holon-swarm-2026")
OLLAMA_URL    = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL  = os.environ.get("OLLAMA_MODEL", "qwen2.5:0.5b")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
COOLIFY_TOKEN = os.environ.get("COOLIFY_TOKEN", "")
COOLIFY_URL   = os.environ.get("COOLIFY_URL", "https://coolify.ofshore.dev")
PORT          = int(os.environ.get("PORT", "3000"))

# Cost tracking
HAIKU_COST_PER_1K = 0.00025
total_cost = 0.0
tasks_done = 0
tasks_failed = 0

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

app = FastAPI(title=f"Mini Agent {AGENT_ID}", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Ollama (free tier) ─────────────────────────────────────────────────────────
async def ollama_complete(prompt: str, max_tokens: int = 512) -> tuple[str, float]:
    """Returns (response, cost). Cost=0 for Ollama."""
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(f"{OLLAMA_URL}/api/generate", json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": max_tokens, "temperature": 0.3}
            })
            if r.status_code == 200:
                return r.json().get("response", ""), 0.0
    except Exception as e:
        log.warning(f"Ollama failed: {e}")
    return "", -1.0  # -1 = failed


async def haiku_complete(prompt: str, max_tokens: int = 1024, system: str = "") -> tuple[str, float]:
    """Returns (response, cost_usd). Falls back to Haiku when Ollama insufficient."""
    if not claude:
        return "", 0.0
    try:
        msgs = [{"role": "user", "content": prompt}]
        kwargs = {"model": "claude-haiku-4-5", "max_tokens": max_tokens, "messages": msgs}
        if system:
            kwargs["system"] = system
        resp = claude.messages.create(**kwargs)
        text = resp.content[0].text if resp.content else ""
        cost = (resp.usage.input_tokens + resp.usage.output_tokens) / 1000 * HAIKU_COST_PER_1K
        return text, cost
    except Exception as e:
        log.error(f"Haiku failed: {e}")
        return "", 0.0


async def smart_complete(prompt: str, complexity: float = 0.3, system: str = "") -> tuple[str, float]:
    """
    Subsidiarity AI routing:
    - complexity < 0.65 → Ollama (free)
    - complexity >= 0.65 → Haiku
    
    Checks heal_memory first (zero cost if pattern found).
    """
    global total_cost

    # 1. Check Supabase heal_memory for similar pattern (zero cost)
    if supabase:
        try:
            cached = supabase.table("heal_memory").select("fix_applied").ilike("issue_summary", f"%{prompt[:50]}%").limit(1).execute()
            if cached.data:
                log.info("✨ Zero-cost: pattern found in heal_memory")
                return cached.data[0]["fix_applied"], 0.0
        except:
            pass

    # 2. Try Ollama first (free)
    if complexity < 0.65:
        resp, cost = await ollama_complete(prompt)
        if cost >= 0 and len(resp) > 10:
            return resp, cost

    # 3. Escalate to Haiku
    resp, cost = await haiku_complete(prompt, system=system)
    total_cost += cost
    return resp, cost


# ── Task handlers by type ──────────────────────────────────────────────────────
SYSTEM_HOLON = """Jesteś autonomicznym agentem w systemie Holon (ofshore.dev).
Zasady: subsidiarity (rozwiązuj lokalnie), Pleroma (zostaw lepiej), Kairos (właściwy moment).
Odpowiadaj konkretnie i zwięźle. Jeśli potrzebne API calls — podaj je w JSON."""

async def handle_code_analysis(payload: dict) -> dict:
    code = payload.get("code", "")
    question = payload.get("question", "Analyze this code")
    prompt = f"Code:\n```\n{code[:3000]}\n```\n\nTask: {question}"
    complexity = 0.5 if len(code) < 500 else 0.75
    resp, cost = await smart_complete(prompt, complexity, SYSTEM_HOLON)
    return {"analysis": resp, "cost": cost}


async def handle_web_crawl(payload: dict) -> dict:
    url = payload.get("url", "")
    extract = payload.get("extract", "main content")
    if not url:
        return {"error": "no url"}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
            r = await http.get(url, headers={"User-Agent": "HolonBot/1.0 (+https://ofshore.dev)"})
            text = r.text[:5000]
        prompt = f"URL: {url}\nContent:\n{text}\n\nExtract: {extract}"
        resp, cost = await smart_complete(prompt, 0.4, "Extract the requested information concisely.")
        return {"url": url, "extracted": resp, "cost": cost, "status": r.status_code}
    except Exception as e:
        return {"error": str(e)}


async def handle_supabase_query(payload: dict) -> dict:
    if not supabase:
        return {"error": "Supabase not configured"}
    sql = payload.get("sql", "")
    if not sql:
        return {"error": "no sql"}
    # Safety: only SELECT allowed from mini-agent
    if not sql.strip().upper().startswith("SELECT"):
        return {"error": "Only SELECT queries allowed"}
    try:
        result = supabase.rpc("exec_sql", {"sql": sql}).execute()
        return {"data": result.data, "count": len(result.data or [])}
    except Exception as e:
        return {"error": str(e)}


async def handle_health_check(payload: dict) -> dict:
    urls = payload.get("urls", [])
    results = []
    async with httpx.AsyncClient(timeout=10) as http:
        for url in urls[:10]:
            try:
                r = await http.get(url)
                results.append({"url": url, "status": r.status_code, "ok": r.status_code < 400})
            except Exception as e:
                results.append({"url": url, "error": str(e), "ok": False})
    return {"checks": results, "healthy": sum(1 for r in results if r.get("ok"))}


async def handle_coolify_op(payload: dict) -> dict:
    op = payload.get("op", "status")
    app_uuid = payload.get("app_uuid", "")
    if not COOLIFY_TOKEN:
        return {"error": "no coolify token"}
    async with httpx.AsyncClient(timeout=20) as http:
        if op == "restart":
            r = await http.get(f"{COOLIFY_URL}/api/v1/applications/{app_uuid}/restart",
                             headers={"Authorization": f"Bearer {COOLIFY_TOKEN}"})
        elif op == "status":
            r = await http.get(f"{COOLIFY_URL}/api/v1/applications/{app_uuid}",
                             headers={"Authorization": f"Bearer {COOLIFY_TOKEN}"})
        else:
            return {"error": f"unknown op: {op}"}
        return r.json()


async def handle_knowledge_extract(payload: dict) -> dict:
    """Extract knowledge and feed evolution engine."""
    text = payload.get("text", "")
    source = payload.get("source", "mini-agent")
    if len(text) < 50:
        return {"extracted": 0}
    
    prompt = f"""Extract knowledge from this text for Holon knowledge base.
Return JSON only: {{"skills": [], "patterns": [], "holon_insights": [], "domain": "infrastructure|autonomous_agents|...", "quality": 0.0-1.0}}

Text: {text[:3000]}"""
    
    resp, cost = await smart_complete(prompt, 0.5)
    try:
        data = json.loads(resp)
        if supabase and data.get("quality", 0) > 0.3:
            supabase.rpc("contribute_knowledge", {
                "p_domain": data.get("domain", "infrastructure"),
                "p_source": source,
                "p_skills": data.get("skills", []),
                "p_patterns": data.get("patterns", []),
                "p_insights": data.get("holon_insights", [])
            }).execute()
        return {"extracted": len(data.get("skills", [])), "domain": data.get("domain"), "cost": cost}
    except:
        return {"extracted": 0, "cost": cost}


async def handle_general(payload: dict) -> dict:
    prompt = payload.get("prompt", payload.get("message", str(payload)))
    complexity = payload.get("complexity", 0.4)
    resp, cost = await smart_complete(prompt, float(complexity), SYSTEM_HOLON)
    return {"response": resp, "cost": cost}


TASK_HANDLERS = {
    "code_analysis":      handle_code_analysis,
    "web_crawl":          handle_web_crawl,
    "supabase_query":     handle_supabase_query,
    "health_check":       handle_health_check,
    "coolify_op":         handle_coolify_op,
    "knowledge_extract":  handle_knowledge_extract,
    "general":            handle_general,
}

# ── HTTP routes ────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok", "agent_id": AGENT_ID,
        "tasks_done": tasks_done, "tasks_failed": tasks_failed,
        "total_cost_usd": round(total_cost, 6),
        "cost_per_task": round(total_cost / max(1, tasks_done), 6),
        "ts": datetime.now(timezone.utc).isoformat()
    }


@app.post("/execute")
async def execute_task(background_tasks: BackgroundTasks, x_swarm_secret: str = Header(None)):
    # Auth
    if x_swarm_secret != SWARM_SECRET:
        raise HTTPException(401, "Unauthorized")
    
    from fastapi import Request
    # We need the body but FastAPI doesn't pass it here - workaround
    raise HTTPException(422, "Use /execute with body")


@app.post("/execute")  
async def execute(request, background_tasks: BackgroundTasks, x_swarm_secret: str = Header(None)):
    if x_swarm_secret != SWARM_SECRET:
        raise HTTPException(401)
    body = await request.json()
    task_id = body.get("task_id", "")
    task_type = body.get("task_type", "general")
    payload = body.get("payload", {})
    
    handler = TASK_HANDLERS.get(task_type, handle_general)
    
    global tasks_done, tasks_failed
    start = time.time()
    try:
        result = await handler(payload)
        tasks_done += 1
        ms = (time.time() - start) * 1000
        
        # Save to heal_memory if successful
        if supabase and result and not result.get("error"):
            try:
                supabase.table("autoheal_log").insert({
                    "task_type": task_type, "result": json.dumps(result),
                    "source": AGENT_ID, "cost_usd": result.get("cost", 0)
                }).execute()
            except: pass
        
        return {"task_id": task_id, "status": "done", "result": result, "ms": round(ms)}
    except Exception as e:
        tasks_failed += 1
        log.error(f"Task {task_id} failed: {e}")
        return {"task_id": task_id, "status": "failed", "error": str(e)}


@app.get("/capabilities")
async def capabilities():
    return {
        "agent_id": AGENT_ID,
        "task_types": list(TASK_HANDLERS.keys()),
        "llm_local": OLLAMA_MODEL,
        "llm_fallback": "claude-haiku-4-5",
        "cost_model": "ollama=free, haiku=$0.0002/1k",
        "subsidiarity": "ollama<0.65, haiku>=0.65"
    }


# ── Heartbeat to swarm router ──────────────────────────────────────────────────
async def heartbeat_loop():
    """Report status to swarm router every 30s."""
    await asyncio.sleep(10)  # Wait for startup
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                await http.post(f"{SWARM_ROUTER}/agent/heartbeat",
                    headers={"x-swarm-secret": SWARM_SECRET},
                    json={
                        "agent_id": AGENT_ID,
                        "status": "busy" if tasks_done % 3 == 0 else "idle",
                        "current_tasks": 0,
                        "avg_ms": 0,
                        "success_rate": tasks_done / max(1, tasks_done + tasks_failed)
                    }
                )
        except: pass
        await asyncio.sleep(30)


@app.on_event("startup")
async def startup():
    asyncio.create_task(heartbeat_loop())
    log.info(f"Mini Agent {AGENT_ID} started on :{PORT}")
    log.info(f"Ollama: {OLLAMA_URL} | Model: {OLLAMA_MODEL}")
    log.info(f"Swarm router: {SWARM_ROUTER}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
