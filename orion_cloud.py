# -*- coding: utf-8 -*-
"""
ORION CLOUD - backend multi-inquilino pra hospedar na nuvem.

Diferencas pro app desktop (jarvis_servidor.py):
  - Multi-usuario de verdade: cada requisicao identifica o usuario por uma SESSAO (cookie assinado).
  - Dados ISOLADOS por conta (SQLite): memoria, documentos, rotina e uso sao por usuario.
  - Cerebro na NUVEM (plugavel, API compativel com OpenAI: Groq, OpenAI, Together, etc.).
  - SEM dependencias de desktop (nada de microfone, alto-falante, abrir apps).
  - Serve a mesma interface (orion_app.html) em "modo nuvem".

Roda com Python puro (stdlib). Configuracao por variaveis de ambiente:
  PORT                 porta (default 8766; a nuvem injeta a dela)
  ORION_SECRET         segredo pra assinar sessoes e licencas (DEFINA na producao!)
  LLM_API_KEY          chave do cerebro de nuvem (ex: chave do Groq, gratis)
  LLM_BASE_URL         endpoint (default Groq: https://api.groq.com/openai/v1)
  LLM_MODEL            modelo (default llama-3.3-70b-versatile)
  MP_ACCESS_TOKEN      token do Mercado Pago (vendedor) - opcional
  PAYPAL_CLIENT_ID/PAYPAL_SECRET/PAYPAL_MODE - opcional
"""
import os, json, time, sqlite3, hmac, hashlib, base64, datetime, secrets, threading
import urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie

PASTA = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8766"))
SECRET = os.environ.get("ORION_SECRET", "orion-cloud-dev-troque-em-producao")
DB_PATH = os.environ.get("ORION_DB", os.path.join(PASTA, "orion_cloud.db"))

LLM_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")

HTML_FILE = os.path.join(PASTA, "orion_app.html")
SITE_FILE = os.path.join(PASTA, "orion_site.html")

# ======================= BANCO (SQLite, isolado por usuario) =======================
_lock = threading.Lock()
def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c
def init_db():
    with _db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, email TEXT UNIQUE,
            senha_hash TEXT, nome_real TEXT, tratamento TEXT, foto TEXT,
            plano TEXT DEFAULT 'free', socio INTEGER DEFAULT 0, criador INTEGER DEFAULT 0,
            origem TEXT DEFAULT 'local', criado TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS kv(
            user_id INTEGER, k TEXT, v TEXT, PRIMARY KEY(user_id,k))""")
        c.commit()
init_db()

def _hash(s):
    if not s: return None
    return hashlib.sha256(("orion_"+s).encode()).hexdigest()
def _pub(u):
    plano = "business" if (u["criador"] or u["socio"]) else (u["plano"] or "free")
    return {"id":u["id"],"nome":u["nome"],"nome_real":u["nome_real"] or "","tratamento":u["tratamento"] or "",
            "email":u["email"] or "","foto":u["foto"] or "","plano":plano,
            "socio":bool(u["socio"]),"criador":bool(u["criador"]),"origem":u["origem"] or "local","convidado":False}
def get_user(uid):
    with _db() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return r
def get_blob(uid, k, default):
    with _db() as c:
        r = c.execute("SELECT v FROM kv WHERE user_id=? AND k=?", (uid,k)).fetchone()
        return json.loads(r["v"]) if r else json.loads(json.dumps(default))
def set_blob(uid, k, obj):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO kv(user_id,k,v) VALUES(?,?,?)", (uid,k,json.dumps(obj,ensure_ascii=False)))
        c.commit()

# ======================= SESSAO (cookie assinado) =======================
def _sign(data):
    mac = hmac.new(SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{data}.{mac}"
def _verify(tok):
    try:
        data, mac = tok.rsplit(".", 1)
        if hmac.compare_digest(mac, hmac.new(SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()[:24]):
            return data
    except Exception: pass
    return None
def make_session(uid): return _sign(f"u{uid}")
def session_uid(tok):
    d = _verify(tok or "")
    if d and d.startswith("u"):
        try: return int(d[1:])
        except Exception: return None
    return None

# ======================= CEREBRO NA NUVEM (OpenAI-compativel) =======================
PERSONA = ("Voce e o Orion, um assistente pessoal de IA em portugues do Brasil. "
           "Educado, direto, prestativo, com leve tom de mordomo (trata por 'senhor' as vezes). "
           "Nunca use o caractere travessao. Respostas curtas e uteis. Seu nome e sempre Orion.")
def cloud_chat(system, messages, max_tokens=700):
    if not LLM_KEY:
        return "O cerebro de nuvem ainda nao foi configurado, senhor. Falta a variavel LLM_API_KEY (ex: chave gratis do Groq)."
    body = {"model": LLM_MODEL, "max_tokens": max_tokens, "temperature": 0.5,
            "messages": [{"role":"system","content":system}] + messages}
    try:
        req = urllib.request.Request(LLM_BASE.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=40).read().decode())
        return (r["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        print("[llm]", e)
        return f"Tive um problema pra pensar agora ({e}). Tente de novo, senhor."

# ======================= PLANOS / LIMITES =======================
PLANOS_PRECO = {"free":0.0, "pro":59.99, "business":109.99}
PLANOS_NOME = {"free":"Orion Free", "pro":"Orion Pro", "business":"Orion Business"}
LIMITES = {"free":{"msgs_dia":20,"docs_mes":3}, "pro":{"msgs_dia":0,"docs_mes":0}, "business":{"msgs_dia":0,"docs_mes":0}}
def plano_de(u): return "business" if (u["criador"] or u["socio"]) else (u["plano"] or "free")
def uso_pode(u, rec="msg"):
    lim = LIMITES.get(plano_de(u), LIMITES["free"]); uso = get_blob(u["id"], "uso", {})
    if rec == "msg":
        cap = lim["msgs_dia"]
        if cap <= 0: return {"ok":True,"cap":0}
        hoje = datetime.date.today().isoformat(); usados = uso.get("dia",{}).get(hoje,0)
        if usados >= cap: return {"ok":False,"cap":cap,"usados":usados,
            "motivo":f"O senhor ja usou as {cap} mensagens de hoje do plano gratuito. No Pro e ilimitado."}
        return {"ok":True,"cap":cap,"usados":usados}
    if rec == "doc":
        cap = lim["docs_mes"]
        if cap <= 0: return {"ok":True,"cap":0}
        mes = datetime.date.today().strftime("%Y-%m"); usados = uso.get("mes",{}).get(mes,{}).get("docs",0)
        if usados >= cap: return {"ok":False,"cap":cap,"motivo":f"O plano gratuito gera {cap} documentos por mes, e o senhor ja usou."}
        return {"ok":True,"cap":cap,"usados":usados}
    return {"ok":True}
def uso_reg(u, rec="msg"):
    uso = get_blob(u["id"], "uso", {}); hoje = datetime.date.today().isoformat(); mes = datetime.date.today().strftime("%Y-%m")
    if rec == "msg":
        d = uso.setdefault("dia",{}); d[hoje] = d.get(hoje,0)+1
        for kk in sorted(d.keys())[:-7]: d.pop(kk,None)
    elif rec == "doc":
        m = uso.setdefault("mes",{}).setdefault(mes,{}); m["docs"] = m.get("docs",0)+1
    set_blob(u["id"], "uso", uso)
def uso_resumo(u):
    lim = LIMITES.get(plano_de(u), LIMITES["free"]); uso = get_blob(u["id"], "uso", {})
    hoje = datetime.date.today().isoformat(); mes = datetime.date.today().strftime("%Y-%m")
    return {"plano":plano_de(u),"msgs_hoje":uso.get("dia",{}).get(hoje,0),"msgs_cap":lim["msgs_dia"],
            "ilimitado":lim["msgs_dia"]<=0,"docs_mes":uso.get("mes",{}).get(mes,{}).get("docs",0),
            "docs_cap":lim["docs_mes"],"docs_ilimitado":lim["docs_mes"]<=0}

# ======================= MEMORIA (grafo por usuario) =======================
_RAMOS = (("voce","Voce","Tudo sobre voce"),("diretrizes","Diretrizes","Como prefere que eu aja"),("mundo","Mundo","O que aprendi"))
def grafo(uid):
    g = get_blob(uid, "grafo", {"nodes":{}, "seq":0})
    n = g["nodes"]
    if "root" not in n: n["root"] = {"id":"root","name":"Orion","data":"","parent":None,"fixo":True}
    for fid,nome,desc in _RAMOS:
        if fid not in n: n[fid] = {"id":fid,"name":nome,"desc":desc,"data":"","parent":"root","fixo":True}
    return g
def grafo_tree(uid):
    g = grafo(uid); n = g["nodes"]
    def build(pid): return [{"id":x["id"],"name":x["name"],"desc":x.get("desc",""),"data":x.get("data",""),
        "fixo":x.get("fixo",False),"filhos":build(x["id"])} for x in n.values() if x.get("parent")==pid]
    return {"id":"root","name":"Orion","fixo":True,"filhos":build("root")}
def grafo_add(uid, data, parent="voce", name=None):
    g = grafo(uid); n = g["nodes"]; parent = parent if parent in n else "voce"
    g["seq"] += 1; nid = f"n{g['seq']}"
    n[nid] = {"id":nid,"name":name or str(data)[:42] or "nota","data":str(data),"parent":parent}
    set_blob(uid, "grafo", g); return nid
def grafo_acao(uid, d):
    g = grafo(uid); n = g["nodes"]; a = d.get("acao")
    if a == "add": grafo_add(uid, d.get("data",""), d.get("parent","voce"), d.get("name"))
    elif a == "edit" and d.get("id") in n:
        for k in ("name","data","desc"):
            if k in d: n[d["id"]][k] = d[k]
        set_blob(uid, "grafo", g)
    elif a == "del" and d.get("id") in n and not n[d["id"]].get("fixo"):
        pid = n[d["id"]].get("parent","root")
        for x in n.values():
            if x.get("parent")==d["id"]: x["parent"]=pid
        n.pop(d["id"],None); set_blob(uid, "grafo", g)
    else: return {"ok":False,"erro":"acao invalida"}
    return {"ok":True,"tree":grafo_tree(uid)}

# ======================= DOCUMENTOS =======================
DOC_TPL = {"email":"E-mail profissional","redacao":"Redacao escolar","curriculo":"Curriculo","contrato":"Contrato simples",
           "post":"Post de rede social","resumo":"Resumo","carta":"Carta formal","plano":"Plano de acao"}
DOC_INSTR = {"email":"Escreva um e-mail profissional, claro e educado.","redacao":"Escreva uma redacao dissertativa-argumentativa com introducao, desenvolvimento e conclusao.",
    "curriculo":"Monte um curriculo objetivo e profissional.","contrato":"Escreva um contrato simples com clausulas numeradas. Avise que nao substitui um advogado.",
    "post":"Escreva um post envolvente com chamada para acao.","resumo":"Resuma o conteudo em topicos.","carta":"Escreva uma carta formal.","plano":"Monte um plano com objetivo, etapas, prazos e recursos."}
def gerar_doc(u, tipo, ctx):
    pode = uso_pode(u, "doc")
    if not pode["ok"]: return {"ok":False,"erro":pode.get("motivo"),"upgrade":True}
    tipo = (tipo or "email").lower()
    sys = f"{PERSONA} {DOC_INSTR.get(tipo,'Escreva o documento pedido.')} Entregue pronto, sem comentarios extras."
    txt = cloud_chat(sys, [{"role":"user","content":ctx or DOC_TPL.get(tipo,'documento')}])
    if not txt: return {"ok":False,"erro":"Nao consegui gerar agora."}
    uso_reg(u, "doc")
    docs = get_blob(u["id"], "docs", [])
    item = {"id":int(time.time()*1000),"tipo":tipo,"titulo":DOC_TPL.get(tipo,"Documento"),"contexto":ctx,
            "texto":txt,"quando":datetime.datetime.now().strftime("%d/%m/%Y %H:%M")}
    docs.insert(0, item); set_blob(u["id"], "docs", docs[:50])
    return {"ok":True,"doc":item}

# ======================= ROTINA =======================
def rotina_estado(uid):
    r = get_blob(uid, "rotina", {"rotinas":[],"checkins":{}})
    hoje = datetime.date.today().isoformat(); ch = r["checkins"].get(hoje,{})
    total = sum(len(x.get("itens",[])) for x in r["rotinas"]); feitos = sum(len(v) for v in ch.values())
    hist = []
    for i in range(6,-1,-1):
        dia = (datetime.date.today()-datetime.timedelta(days=i)).isoformat()
        hist.append({"dia":dia,"feitos":sum(len(v) for v in r["checkins"].get(dia,{}).values())})
    streak = 0
    for i in range(0,60):
        dia = (datetime.date.today()-datetime.timedelta(days=i)).isoformat()
        if sum(len(v) for v in r["checkins"].get(dia,{}).values())>0: streak += 1
        elif i>0: break
    return {"rotinas":r["rotinas"],"hoje":ch,"stats":{"total":total,"feitos":feitos,"hist":hist,"streak":streak}}
def rotina_acao(u, d):
    uid = u["id"]; r = get_blob(uid, "rotina", {"rotinas":[],"checkins":{}}); a = d.get("acao")
    if a == "criar":
        r["rotinas"].append({"id":int(time.time()*1000),"nome":d.get("nome","Rotina"),"tipo":d.get("tipo","habitos"),
            "itens":[str(x).strip() for x in (d.get("itens") or []) if str(x).strip()]})
    elif a == "gerar":
        sys = f"{PERSONA} Monte uma rotina sustentavel pro objetivo. Responda APENAS com a lista, um item curto por linha, sem numeracao, no maximo 8 itens."
        out = cloud_chat(sys, [{"role":"user","content":d.get("objetivo","")}], 400)
        itens = [l.strip("-*0123456789. ").strip() for l in out.splitlines() if l.strip()][:8]
        if not itens: return {"ok":False,"erro":"Nao consegui montar agora."}
        r["rotinas"].append({"id":int(time.time()*1000),"nome":d.get("nome") or d.get("objetivo","Rotina")[:40],"tipo":"habitos","itens":itens})
    elif a == "del": r["rotinas"] = [x for x in r["rotinas"] if x.get("id")!=d.get("id")]
    elif a == "checkin":
        hoje = datetime.date.today().isoformat(); ch = r["checkins"].setdefault(hoje,{})
        lst = ch.setdefault(str(d.get("rotina_id")),[]); idx = d.get("idx")
        (lst.remove(idx) if idx in lst else lst.append(idx))
    else: return {"ok":False,"erro":"acao invalida"}
    set_blob(uid, "rotina", r); return {"ok":True,"estado":rotina_estado(uid)}

# ======================= LICENCA (mesmo modulo do desktop e do gerador) =======================
# licenca EMBUTIDA (mesmo algoritmo e segredo do desktop/keygen). Auto-suficiente: nao depende de import externo.
_LIC_SECRET = "Orion-LIC-2026-Gurroffe-Ads-Kx7p9Q2w"
_LIC_PB = {"pro": 1, "business": 2}; _LIC_BP = {1: "pro", 2: "business"}
def _lic_mac(msg): return hmac.new(_LIC_SECRET.encode(), msg, hashlib.sha256).digest()[:5]
def _lic_make_det(plano, serial):
    plano=(plano or "").lower()
    if plano not in _LIC_PB: return None
    msg=bytes([_LIC_PB[plano]])+serial[:5].ljust(5,b"\0"); raw=msg+_lic_mac(msg)
    s=base64.b32encode(raw).decode().rstrip("="); return f"{plano[:3].upper()}-{s[:6]}-{s[6:12]}-{s[12:]}"
def lic_make(plano): return _lic_make_det(plano, os.urandom(5))
def lic_master(plano): return _lic_make_det(plano, b"MASTR")
def lic_check(chave):
    try:
        p=(chave or "").strip().upper().split("-",1); raw=(p[1] if len(p)>1 else p[0]).replace("-","").replace(" ","")
        data=base64.b32decode(raw+"="*((8-len(raw)%8)%8))
        if len(data)!=11: return None
        if hmac.compare_digest(data[6:11], _lic_mac(data[:6])): return _LIC_BP.get(data[0])
    except Exception: pass
    return None
class LIC:  # alias pra nao mexer no resto do codigo
    lic_check=staticmethod(lic_check); lic_master=staticmethod(lic_master); lic_make=staticmethod(lic_make)
def lic_ativar(u, chave):
    pl = LIC.lic_check(chave)
    if pl:
        with _db() as c: c.execute("UPDATE users SET plano=? WHERE id=?", (pl, u["id"])); c.commit()
        return {"ok":True,"plano":pl,"user":_pub(get_user(u["id"]))}
    return {"ok":False,"erro":"Chave invalida, senhor."}

# ======================= PAGAMENTO (server-side) =======================
def base_url(handler):
    host = handler.headers.get("Host","localhost")
    proto = handler.headers.get("X-Forwarded-Proto","http")
    return f"{proto}://{host}"
def mp_token(): return os.environ.get("MP_ACCESS_TOKEN","")
def pp_creds(): return (os.environ.get("PAYPAL_CLIENT_ID",""), os.environ.get("PAYPAL_SECRET",""), os.environ.get("PAYPAL_MODE","sandbox"))
def checkout(u, plano, provedor, burl):
    plano = (plano or "pro").lower()
    if plano not in PLANOS_PRECO or plano == "free": return {"ok":False,"msg":"Plano invalido."}
    if not provedor: provedor = "mp" if mp_token() else ("paypal" if pp_creds()[0] else None)
    if provedor == "mp" and mp_token():
        try:
            body = {"items":[{"title":PLANOS_NOME[plano],"quantity":1,"unit_price":PLANOS_PRECO[plano],"currency_id":"BRL"}],
                    "external_reference":f"{plano}:{u['id']}",
                    "back_urls":{"success":f"{burl}/?pago={plano}","failure":f"{burl}/?pago=falhou"}}
            if burl.startswith("https"): body["auto_return"] = "approved"   # MP so aceita auto_return com https
            req = urllib.request.Request("https://api.mercadopago.com/checkout/preferences", data=json.dumps(body).encode(),
                headers={"Authorization":f"Bearer {mp_token()}","Content-Type":"application/json"})
            r = json.loads(urllib.request.urlopen(req,timeout=15).read().decode())
            url = r.get("init_point") or r.get("sandbox_init_point")
            if url: return {"ok":True,"url":url}
        except Exception as e: return {"ok":False,"msg":f"Erro MP: {e}"}
    if provedor == "paypal" and pp_creds()[0]:
        cid,sec,mode = pp_creds()
        try:
            base = "https://api-m.paypal.com" if mode=="live" else "https://api-m.sandbox.paypal.com"
            auth = base64.b64encode(f"{cid}:{sec}".encode()).decode()
            tok = json.loads(urllib.request.urlopen(urllib.request.Request(base+"/v1/oauth2/token",
                data=b"grant_type=client_credentials", headers={"Authorization":f"Basic {auth}","Content-Type":"application/x-www-form-urlencoded"}),timeout=15).read())["access_token"]
            body = {"intent":"CAPTURE","purchase_units":[{"amount":{"currency_code":"BRL","value":f"{PLANOS_PRECO[plano]:.2f}"},"custom_id":f"{plano}:{u['id']}"}],
                    "application_context":{"return_url":f"{burl}/?pago={plano}&prov=paypal","cancel_url":f"{burl}/?pago=falhou"}}
            r = json.loads(urllib.request.urlopen(urllib.request.Request(base+"/v2/checkout/orders",data=json.dumps(body).encode(),
                headers={"Authorization":f"Bearer {tok}","Content-Type":"application/json"}),timeout=15).read())
            link = next((l["href"] for l in r.get("links",[]) if l.get("rel")=="approve"),None)
            if link: return {"ok":True,"url":link}
        except Exception as e: return {"ok":False,"msg":f"Erro PayPal: {e}"}
    return {"ok":False,"msg":"Pagamento nao configurado no servidor. Ou use uma chave de ativacao em Planos."}

# ======================= VISAO (Groq, le grafico/foto) =======================
def cloud_vision(b64, instr, max_tokens=600):
    if not LLM_KEY: return ""
    model = os.environ.get("LLM_VISION_MODEL", "llama-3.2-90b-vision-preview")
    body = {"model":model,"max_tokens":max_tokens,"temperature":0.3,"messages":[{"role":"user","content":[
        {"type":"text","text":instr},{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,"+b64}}]}]}
    try:
        req = urllib.request.Request(LLM_BASE.rstrip("/")+"/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization":f"Bearer {LLM_KEY}","Content-Type":"application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=45).read().decode())
        return (r["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        print("[cloud vision]", e); return ""

# ======================= APRENDER (tutor) =======================
def cl_apr_estado(uid): return {"trilhas": get_blob(uid,"aprender",{"trilhas":[]})["trilhas"][:20]}
def cl_apr_trilha(uid, obj, nivel):
    obj=(obj or "").strip()
    if not obj: return {"ok":False,"erro":"diga o que quer aprender"}
    sysp=("Voce e o Orion, tutor que ensina qualquer pessoa (ate quem nunca programou, vibe coders). Monte uma TRILHA pro objetivo. "
          "Responda APENAS a lista, um passo por linha, sem numeracao, no maximo 8 passos curtos e praticos.")
    txt=cloud_chat(sysp,[{"role":"user","content":f"Objetivo: {obj}. Nivel: {nivel}."}],400)
    passos=[l.strip("-*0123456789.) ").strip() for l in txt.splitlines() if l.strip()][:8]
    if not passos: return {"ok":False,"erro":"nao consegui montar agora"}
    a=get_blob(uid,"aprender",{"trilhas":[]}); t={"id":int(time.time()*1000),"objetivo":obj,"nivel":nivel,"passos":passos,"feitos":[]}
    a["trilhas"].insert(0,t); a["trilhas"]=a["trilhas"][:20]; set_blob(uid,"aprender",a)
    return {"ok":True,"trilha":t}
def cl_apr_licao(obj,passo,nivel):
    sysp=("Voce e o Orion, tutor pra leigo (inclusive nao-dev / vibe coder). Explique este passo simples e pratico: o que e, por que importa, "
          "como fazer com exemplo, e termine com uma tarefinha. Curto, claro, sem travessao.")
    return {"ok":True,"licao":cloud_chat(sysp,[{"role":"user","content":f"Objetivo: {obj}. Nivel: {nivel}. Ensine: {passo}"}],500)}
def cl_apr_acao(uid,d):
    a=get_blob(uid,"aprender",{"trilhas":[]}); ac=d.get("acao")
    if ac=="checkin":
        t=next((x for x in a["trilhas"] if x["id"]==d.get("id")),None)
        if t: i=d.get("passo"); (t["feitos"].remove(i) if i in t["feitos"] else t["feitos"].append(i))
    elif ac=="del": a["trilhas"]=[x for x in a["trilhas"] if x["id"]!=d.get("id")]
    else: return {"ok":False}
    set_blob(uid,"aprender",a); return {"ok":True,"estado":cl_apr_estado(uid)}

# ======================= TRADING (cotacao + paper + analise + print) =======================
_CCOT={}
def cl_yh(t):
    t=(t or "").upper().strip(); return t if ("-USD" in t or "USDT" in t or "." in t) else t+".SA"
def cl_cotacao(ticker, ttl=20):
    k=(ticker or "").upper().strip(); now=time.time(); c=_CCOT.get(k)
    if c and now-c[0]<ttl: return c[1]
    try:
        url=f"https://query1.finance.yahoo.com/v8/finance/chart/{cl_yh(k)}?range=1d&interval=5m"
        r=json.loads(urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"}),timeout=10).read().decode())
        res=r["chart"]["result"][0]; m=res["meta"]; price=m.get("regularMarketPrice"); prev=m.get("chartPreviousClose") or price
        closes=[x for x in (res.get("indicators",{}).get("quote",[{}])[0].get("close") or []) if x is not None]
        out={"ticker":k,"price":price,"pct":((price-prev)/prev*100 if prev else 0),"hist":closes[-80:],"moeda":m.get("currency","BRL")}
    except Exception as e: out={"ticker":k,"price":None,"erro":str(e)}
    _CCOT[k]=(now,out); return out
def _tr(uid): return get_blob(uid,"trading",{"saldo":100000.0,"inicial":100000.0,"posicoes":{},"historico":[],"watchlist":["PETR4","VALE3","ITUB4","BTC-USD"]})
def cl_trading_estado(uid):
    T=_tr(uid); pos=[]; vp=0.0
    for t,p in list(T["posicoes"].items()):
        c=cl_cotacao(t); pr=c.get("price") or p["preco_medio"]; vp+=pr*p["qtd"]
        pos.append({"ticker":t,"qtd":p["qtd"],"preco_medio":p["preco_medio"],"preco_atual":pr,"valor":pr*p["qtd"],
                    "pl":(pr-p["preco_medio"])*p["qtd"],"plpct":((pr/p["preco_medio"]-1)*100 if p["preco_medio"] else 0)})
    pat=T["saldo"]+vp; watch=[{"ticker":t,"price":cl_cotacao(t).get("price"),"pct":cl_cotacao(t).get("pct",0),"moeda":cl_cotacao(t).get("moeda","BRL")} for t in T["watchlist"]]
    return {"saldo":T["saldo"],"inicial":T["inicial"],"patrimonio":pat,"lucro_total":pat-T["inicial"],
            "lucro_pct":(pat/T["inicial"]-1)*100 if T["inicial"] else 0,"posicoes":pos,"historico":T["historico"][:30],
            "watchlist":T["watchlist"],"watch":watch,"agente":False,"curva":[]}
def cl_trading_acao(uid,d):
    T=_tr(uid); a=d.get("acao")
    if a=="ordem":
        tk=(d.get("ticker") or "").upper(); side=d.get("tipo");
        try: qtd=int(d.get("qtd"))
        except Exception: return {"ok":False,"erro":"qtd invalida"}
        c=cl_cotacao(tk,5); pr=c.get("price")
        if not pr or qtd<=0: return {"ok":False,"erro":"sem cotacao/qtd"}
        if side=="comprar":
            if pr*qtd>T["saldo"]: return {"ok":False,"erro":"saldo insuficiente"}
            T["saldo"]-=pr*qtd; p=T["posicoes"].get(tk)
            if p: tot=p["qtd"]+qtd; p["preco_medio"]=(p["preco_medio"]*p["qtd"]+pr*qtd)/tot; p["qtd"]=tot
            else: T["posicoes"][tk]={"qtd":qtd,"preco_medio":pr}
            T["historico"].insert(0,{"t":time.time(),"acao":"compra","ticker":tk,"qtd":qtd,"preco":pr,"origem":"voce"})
        elif side=="vender":
            p=T["posicoes"].get(tk)
            if not p or p["qtd"]<qtd: return {"ok":False,"erro":"sem posicao"}
            T["saldo"]+=pr*qtd; p["qtd"]-=qtd
            if p["qtd"]<=0: T["posicoes"].pop(tk,None)
            T["historico"].insert(0,{"t":time.time(),"acao":"venda","ticker":tk,"qtd":qtd,"preco":pr,"origem":"voce"})
        else: return {"ok":False,"erro":"tipo invalido"}
    elif a=="watch_add":
        tk=(d.get("ticker") or "").upper().strip()
        if tk and tk not in T["watchlist"]: T["watchlist"].append(tk)
    elif a=="watch_del": T["watchlist"]=[x for x in T["watchlist"] if x!=(d.get("ticker") or "").upper()]
    elif a=="reset": T.update({"saldo":100000.0,"inicial":100000.0,"posicoes":{},"historico":[]})
    elif a=="agente": pass   # agente automatico fica so no desktop por enquanto
    else: return {"ok":False,"erro":"acao invalida"}
    T["historico"]=T["historico"][:100]; set_blob(uid,"trading",T)
    return {"ok":True,"estado":cl_trading_estado(uid)}
def cl_trading_analise(ticker):
    c=cl_cotacao(ticker)
    if not c.get("price"): return {"ok":False,"erro":c.get("erro","sem cotacao")}
    h=c.get("hist",[]); s=sum(h[-9:])/len(h[-9:]) if len(h)>=9 else c["price"]; l=sum(h[-21:])/len(h[-21:]) if len(h)>=21 else s
    tend="alta" if s>=l else "baixa"
    sysp=(PERSONA+" Analise tecnica curta (3-4 frases) e termine com vies COMPRA, VENDA ou NEUTRO. Deixe claro que e analise, nao recomendacao.")
    out=cloud_chat(sysp,[{"role":"user","content":f"{ticker.upper()}: preco {c['price']:.2f}, dia {c['pct']:.2f}%, media curta {s:.2f}, longa {l:.2f}, tendencia {tend}."}],400)
    return {"ok":True,"ticker":ticker.upper(),"cotacao":c,"tendencia":tend,"analise":out}
def cl_trading_analise_img(img):
    b64=img.split(",",1)[1] if (img or "").startswith("data:") else (img or "")
    if not b64: return {"ok":False,"erro":"sem imagem"}
    instr=("Analista tecnico. Leia ESTE GRAFICO e responda em portugues, sem travessao, neste formato (uma linha cada): "
           "ATIVO: / TENDENCIA: / SUPORTE: / RESISTENCIA: / ENTRADA: (COMPRA ou VENDA e preco) / STOP: / ALVO: / FUNDAMENTO: . "
           "Especifico nos numeros que ler. E analise, nao recomendacao garantida.")
    txt=cloud_vision(b64,instr,600)
    if not txt: return {"ok":False,"erro":"Configure o cerebro de nuvem (LLM_API_KEY do Groq) pra ler imagem."}
    campos={}
    for ln in txt.splitlines():
        u=ln.strip().upper()
        for ch in ("ATIVO","TENDENCIA","SUPORTE","RESISTENCIA","ENTRADA","STOP","ALVO","FUNDAMENTO"):
            if u.startswith(ch) and ":" in ln: campos[ch]=ln.split(":",1)[1].strip()
    return {"ok":True,"texto":txt,"campos":campos}

# ======================= HTTP =======================
ASSET_TYPES = {".png":"image/png",".ico":"image/x-icon",".svg":"image/svg+xml",".jpg":"image/jpeg"}
def _asset(*names):
    for n in names:
        p = os.path.join(PASTA, n)
        if os.path.exists(p): return p
    return None

class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _send(self, body, code=200, ctype="application/json", cookie=None):
        if isinstance(body,(dict,list)): body = json.dumps(body,ensure_ascii=False)
        if isinstance(body,str): body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype if "charset" in ctype else ctype+"; charset=utf-8")
        if cookie is not None:
            self.send_header("Set-Cookie", f"orion_sess={cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
        self.end_headers(); self.wfile.write(body)
    def _file(self, path, ctype):
        try: self._send(open(path,"rb").read(), 200, ctype)
        except Exception: self._send(b"", 404)
    def _body(self):
        try:
            n = int(self.headers.get("Content-Length",0) or 0)
            return json.loads(self.rfile.read(n).decode()) if n else {}
        except Exception: return {}
    def _user(self):
        ck = SimpleCookie(self.headers.get("Cookie",""))
        tok = ck["orion_sess"].value if "orion_sess" in ck else ""
        uid = session_uid(tok)
        return get_user(uid) if uid else None

    def do_OPTIONS(self): self._send(b"",204)

    def do_GET(self):
        path = self.path.split("?",1)[0]
        u = self._user()
        if path in ("/","/index.html"):
            try:
                html = open(HTML_FILE,encoding="utf-8").read()
                html = html.replace("<head>", "<head>\n<script>window.ORION_CLOUD=true;</script>",1)
                self._send(html,200,"text/html")
            except Exception: self._send(b"<h1>orion_app.html nao encontrado</h1>",200,"text/html")
        elif path == "/site": self._file(SITE_FILE,"text/html") if os.path.exists(SITE_FILE) else self._send(b"",404)
        elif path == "/logo": p=_asset("orion_logo.png","orion_icon.png"); self._file(p,"image/png") if p else self._send(b"",404)
        elif path == "/icon": p=_asset("orion_icon.png"); self._file(p,"image/png") if p else self._send(b"",404)
        elif path == "/favicon.ico": p=_asset("orion.ico"); self._file(p,"image/x-icon") if p else self._send(b"",404)
        elif path == "/manifest.webmanifest":
            self._send(json.dumps({"name":"Orion","short_name":"Orion","start_url":"/","display":"standalone",
                "background_color":"#0a0c11","theme_color":"#0a0c11","lang":"pt-BR",
                "icons":[{"src":"/icon","sizes":"512x512","type":"image/png","purpose":"any maskable"}]}),200,"application/manifest+json")
        elif path == "/sw.js":
            self._send("self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>self.clients.claim());",200,"application/javascript")
        elif path == "/.well-known/assetlinks.json":
            # necessario pro APK (TWA) verificar que o site e seu. Cole o JSON do PWABuilder na env ASSETLINKS_JSON.
            self._send(os.environ.get("ASSETLINKS_JSON", "[]"), 200, "application/json")
        elif path == "/estado":
            self._send({"status":"ocioso","cloud":True,"user":(_pub(u) if u else None),"noticias":[]})
        elif path == "/usuarios":
            with _db() as c:
                lst = [{"id":x["id"],"nome":x["nome"],"email":x["email"] or "","foto":x["foto"] or "",
                        "tem_senha":bool(x["senha_hash"]),"plano":("business" if (x["criador"] or x["socio"]) else x["plano"]),
                        "origem":x["origem"],"criador":bool(x["criador"])} for x in c.execute("SELECT * FROM users ORDER BY id").fetchall()]
            self._send({"ativo":(_pub(u) if u else None),"lista":lst})
        elif path == "/config":
            self._send({"cloud":True,"cerebro":"nuvem","modelos_ollama":[],"modelos_anthropic":[],
                        "vozes":["pt-BR-AntonioNeural"],"voz":"pt-BR-AntonioNeural","volume":90,"rate":0})
        elif path == "/uso": self._send(uso_resumo(u) if u else {"plano":"free","msgs_hoje":0,"msgs_cap":20,"ilimitado":False})
        elif path == "/documentos":
            self._send({"docs":(get_blob(u["id"],"docs",[]) if u else []),
                        "templates":[{"id":k,"nome":v} for k,v in DOC_TPL.items()]})
        elif path == "/rotina": self._send(rotina_estado(u["id"]) if u else {"rotinas":[],"hoje":{},"stats":{}})
        elif path == "/aprender": self._send(cl_apr_estado(u["id"]) if u else {"trilhas":[]})
        elif path == "/trading": self._send(cl_trading_estado(u["id"]) if u else {"posicoes":[],"watch":[],"historico":[]})
        elif path == "/memoria/grafo": self._send(grafo_tree(u["id"]) if u else {"id":"root","name":"Orion","filhos":[]})
        elif path == "/integra/status":
            self._send({"google":False,"clickup":False,"apollo":False,"meta":False,
                        "mercadopago":bool(mp_token()),"paypal":bool(pp_creds()[0]),"paypal_mode":pp_creds()[2]})
        elif path == "/admin/usuarios":
            if not (u and u["criador"]): self._send({"ok":False,"erro":"so o dono"}); return
            with _db() as c:
                lst = [{"id":x["id"],"nome":x["nome"],"email":x["email"] or "","foto":x["foto"] or "",
                        "plano":("business" if (x["criador"] or x["socio"]) else x["plano"]),"socio":bool(x["socio"]),"criador":bool(x["criador"])}
                       for x in c.execute("SELECT * FROM users ORDER BY id").fetchall()]
            self._send({"ok":True,"usuarios":lst})
        elif path == "/licenca/chaves":
            self._send({"ok":True,"pro":LIC.lic_master("pro"),"business":LIC.lic_master("business")} if (u and u["criador"]) else {"ok":False})
        elif path == "/mics": self._send({"mics":[],"atual":None})
        else: self._send({"erro":"nao encontrado","path":path},404)

    def do_POST(self):
        path = self.path
        d = self._body()
        u = self._user()
        # ---- contas / sessao ----
        if path == "/usuario/criar":
            nome = (d.get("nome") or "").strip()
            if not nome: self._send({"ok":False,"erro":"Diga seu nome."}); return
            try:
                with _db() as c:
                    n_users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
                    cur = c.execute("INSERT INTO users(nome,email,senha_hash,nome_real,tratamento,foto,plano,criador,origem,criado) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (nome.title(), (d.get("email") or "").strip().lower() or None, _hash(d.get("senha")),
                         (d.get("nome_real") or "").strip(), (d.get("tratamento") or "").strip(), d.get("foto") or "",
                         "free", 1 if n_users==0 else 0, "local", datetime.datetime.now().strftime("%d/%m/%Y %H:%M")))
                    c.commit(); uid = cur.lastrowid
                self._send({"ok":True,"user":_pub(get_user(uid))}, cookie=make_session(uid))
            except sqlite3.IntegrityError:
                self._send({"ok":False,"erro":"Ja existe conta com esse e-mail."})
        elif path == "/usuario/login":
            with _db() as c:
                r = c.execute("SELECT * FROM users WHERE lower(nome)=lower(?)", ((d.get("nome") or "").strip(),)).fetchone()
            if not r: self._send({"ok":False,"erro":"Nao achei esse perfil."}); return
            if r["senha_hash"]:
                if not d.get("senha"): self._send({"ok":False,"precisa_senha":True}); return
                if _hash(d.get("senha")) != r["senha_hash"]: self._send({"ok":False,"erro":"Senha errada, senhor."}); return
            self._send({"ok":True,"user":_pub(r)}, cookie=make_session(r["id"]))
        elif path == "/usuario/convidado":
            # convidado de nuvem: sessao efemera id negativo nao persiste dados
            self._send({"ok":True,"user":{"id":0,"nome":(d.get("nome") or "Convidado"),"convidado":True,"plano":"free","foto":"","tratamento":""}})
        elif path == "/usuario/logout":
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Set-Cookie","orion_sess=; Path=/; Max-Age=0"); self.end_headers()
            self.wfile.write(b'{"ok":true}')
        elif path == "/usuario/google_login":
            self._send({"ok":False,"erro":"No app de nuvem o login Google entra depois (OAuth hospedado). Por enquanto crie conta com e-mail, senhor."})
        elif path == "/usuario/reset_senha":
            if u and (u["criador"] or u["id"]==d.get("id")):
                with _db() as c: c.execute("UPDATE users SET senha_hash=NULL WHERE id=?", (d.get("id"),)); c.commit()
                self._send({"ok":True})
            else: self._send({"ok":False,"erro":"sem permissao"})
        elif path == "/usuario/atualizar":
            if not u: self._send({"ok":False,"erro":"faca login"}); return
            campos = {}
            for k in ("nome","nome_real","tratamento","email","foto"):
                if d.get(k) is not None: campos[k]=d[k]
            if d.get("senha"): campos["senha_hash"]=_hash(d["senha"])
            if campos:
                sets = ",".join(f"{k}=?" for k in campos)
                with _db() as c: c.execute(f"UPDATE users SET {sets} WHERE id=?", (*campos.values(), u["id"])); c.commit()
            self._send({"ok":True,"user":_pub(get_user(u["id"]))})
        elif path == "/usuario/apagar":
            if u and (u["criador"] or u["id"]==d.get("id")):
                with _db() as c:
                    c.execute("DELETE FROM users WHERE id=?", (d.get("id"),)); c.execute("DELETE FROM kv WHERE user_id=?", (d.get("id"),)); c.commit()
                self._send({"ok":True})
            else: self._send({"ok":False,"erro":"sem permissao"})
        # ---- chat (cerebro nuvem) ----
        elif path == "/chat":
            if not u: self._send({"ok":True,"reply":"Crie uma conta ou entre pra eu te responder, senhor."}); return
            frase = (d.get("frase") or "").strip()
            if not frase: self._send({"ok":False}); return
            pode = uso_pode(u, "msg")
            if not pode["ok"]: self._send({"ok":True,"reply":pode["motivo"]+" Abra os Planos pra liberar tudo."}); return
            uso_reg(u, "msg")
            trat = (u["tratamento"] or "").strip()
            sysp = PERSONA + (f" Trate o usuario como '{trat}'." if trat else "")
            hist = get_blob(u["id"], "hist", [])[-8:]
            reply = cloud_chat(sysp, hist + [{"role":"user","content":frase}])
            hist = (hist + [{"role":"user","content":frase},{"role":"assistant","content":reply}])[-12:]
            set_blob(u["id"], "hist", hist)
            # aprende fato simples
            self._send({"ok":True,"reply":reply})
        elif path in ("/falar","/poder","/mic","/visao","/acao","/automacao","/upload","/integra/cred","/integra/google_connect","/usuario/pedir_codigo","/usuario/verificar_codigo","/usuario/reconhecer"):
            self._send({"ok":True,"cloud":True})   # sem efeito na nuvem
        elif path == "/config":
            self._send({"ok":True,"config":{"cloud":True}})
        elif path == "/uso": self._send(uso_resumo(u) if u else {})
        # ---- modulos ----
        elif path == "/documento":
            self._send(gerar_doc(u, d.get("tipo"), d.get("contexto","")) if u else {"ok":False,"erro":"faca login"})
        elif path == "/documento/apagar":
            if u:
                docs = [x for x in get_blob(u["id"],"docs",[]) if x.get("id")!=d.get("id")]; set_blob(u["id"],"docs",docs)
            self._send({"ok":True})
        elif path == "/rotina":
            self._send(rotina_acao(u, d) if u else {"ok":False,"erro":"faca login"})
        elif path == "/aprender":
            self._send(cl_apr_acao(u["id"], d) if u else {"ok":False,"erro":"faca login"})
        elif path == "/aprender/trilha":
            self._send(cl_apr_trilha(u["id"], d.get("objetivo",""), d.get("nivel","iniciante")) if u else {"ok":False,"erro":"faca login"})
        elif path == "/aprender/licao":
            self._send(cl_apr_licao(d.get("objetivo",""), d.get("passo",""), d.get("nivel","iniciante")) if u else {"ok":False})
        elif path == "/trading":
            self._send(cl_trading_acao(u["id"], d) if u else {"ok":False,"erro":"faca login"})
        elif path == "/trading/analise":
            self._send(cl_trading_analise(d.get("ticker","")) if u else {"ok":False})
        elif path == "/trading/analise_imagem":
            self._send(cl_trading_analise_img(d.get("imagem","")) if u else {"ok":False})
        elif path == "/memoria/grafo":
            self._send(grafo_acao(u["id"], d) if u else {"ok":False,"erro":"faca login"})
        elif path == "/memoria":
            self._send({"ok":True})
        # ---- licenca / pagamento / admin ----
        elif path == "/licenca/ativar":
            self._send(lic_ativar(u, d.get("chave")) if u else {"ok":False,"erro":"faca login"})
        elif path == "/licenca/gerar":
            if u and u["criador"] and (d.get("plano") in ("pro","business")):
                self._send({"ok":True,"plano":d["plano"],"chave":LIC.lic_make(d["plano"])})
            else: self._send({"ok":False,"erro":"so o dono gera chaves"})
        elif path == "/pagamento/checkout":
            self._send(checkout(u, d.get("plano","pro"), d.get("provedor"), base_url(self)) if u else {"ok":False,"msg":"faca login"})
        elif path == "/pagamento/confirmar":
            self._send({"ok":False,"erro":"Confirmacao automatica via webhook (configure depois)."})
        elif path == "/pagamento/testar":
            self._send({"mercadopago":{"ok":bool(mp_token())},"paypal":{"ok":bool(pp_creds()[0]),"modo":pp_creds()[2]}} if (u and u["criador"]) else {"erro":"sem permissao"})
        elif path == "/admin/plano":
            if u and u["criador"] and d.get("plano") in ("free","pro","business"):
                with _db() as c: c.execute("UPDATE users SET plano=? WHERE id=?", (d["plano"], d.get("id"))); c.commit()
                self._send({"ok":True,"user":_pub(get_user(d.get("id")))})
            else: self._send({"ok":False,"erro":"sem permissao"})
        elif path == "/admin/socio":
            if u and u["criador"]:
                with _db() as c: c.execute("UPDATE users SET socio=?, plano=CASE WHEN ?=1 THEN 'business' ELSE plano END WHERE id=?",
                    (1 if d.get("socio") else 0, 1 if d.get("socio") else 0, d.get("id"))); c.commit()
                self._send({"ok":True,"user":_pub(get_user(d.get("id")))})
            else: self._send({"ok":False,"erro":"sem permissao"})
        else: self._send({"erro":"nao encontrado","path":path},404)

def main():
    print(f"[orion-cloud] porta {PORT} | cerebro: {'configurado ('+LLM_MODEL+')' if LLM_KEY else 'NAO configurado (defina LLM_API_KEY)'} | db: {DB_PATH}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()

if __name__ == "__main__":
    main()
