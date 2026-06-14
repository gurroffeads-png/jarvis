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
import urllib.request, urllib.parse, urllib.error
import re, html as _html
import xml.etree.ElementTree as ET
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

# ======================= BANCO (SQLite local OU Postgres na nuvem) =======================
# Se existir DATABASE_URL (ex: Neon), usa Postgres (dados PERSISTEM entre deploys).
# Senao, SQLite num arquivo (local/desktop). As duas falam a mesma API que o resto do codigo usa.
_lock = threading.Lock()
_DBURL = os.environ.get("DATABASE_URL", "").strip()
_PG = _DBURL.startswith("postgres")
INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
if _PG:
    import ssl as _ssl
    import pg8000.dbapi as _pg
    INTEGRITY_ERRORS = (sqlite3.IntegrityError, _pg.IntegrityError)
    _pu = urllib.parse.urlparse(_DBURL)
    _PG_ARGS = dict(user=urllib.parse.unquote(_pu.username or ""),
                    password=urllib.parse.unquote(_pu.password or ""),
                    host=_pu.hostname, port=_pu.port or 5432,
                    database=(_pu.path or "/").lstrip("/") or "postgres")

class _PgCur:
    """Faz o cursor do pg8000 devolver linhas estilo dict (row["coluna"]), como o sqlite3.Row.
    OBS: o pg8000 devolve UMA linha tambem como lista, entao fetchone e fetchall sao tratados
    separadamente (nao da pra distinguir pelo tipo)."""
    def __init__(self, cur): self._c = cur; self.lastrowid = None
    def _cols(self): return [d[0] for d in (self._c.description or [])]
    def fetchone(self):
        row = self._c.fetchone()
        return dict(zip(self._cols(), row)) if row is not None else None
    def fetchall(self):
        cols = self._cols()
        return [dict(zip(cols, r)) for r in self._c.fetchall()]
    def __iter__(self): return iter(self.fetchall())

class _PgConn:
    """Embrulha a conexao pg8000 pra aceitar placeholders '?' e os SQLs que o codigo ja usa."""
    def __init__(self, conn): self._c = conn
    def execute(self, sql, params=()):
        ins_user = sql.lstrip().upper().startswith("INSERT INTO USERS")
        if sql.lstrip().upper().startswith("INSERT OR REPLACE INTO KV"):
            sql = "INSERT INTO kv(user_id,k,v) VALUES(?,?,?) ON CONFLICT (user_id,k) DO UPDATE SET v=EXCLUDED.v"
        sql = sql.replace("?", "%s")
        if ins_user: sql += " RETURNING id"
        cur = self._c.cursor(); cur.execute(sql, tuple(params))
        pc = _PgCur(cur)
        if ins_user:
            try:
                row = cur.fetchone(); pc.lastrowid = (row[0] if row else None)
            except Exception: pass
        return pc
    def commit(self): self._c.commit()
    def __enter__(self): return self
    def __exit__(self, et, ev, tb):
        try: self._c.commit() if et is None else self._c.rollback()
        except Exception: pass
        try: self._c.close()
        except Exception: pass
        return False

def _db():
    if _PG:
        conn = _pg.connect(ssl_context=_ssl.create_default_context(), **_PG_ARGS)
        try: conn.autocommit = True   # evita conflito de transacao/portal (ex: COUNT + INSERT no mesmo bloco)
        except Exception: pass
        return _PgConn(conn)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    pk = "BIGSERIAL PRIMARY KEY" if _PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    uid_t = "BIGINT" if _PG else "INTEGER"
    with _db() as c:
        c.execute(f"""CREATE TABLE IF NOT EXISTS users(
            id {pk}, nome TEXT, email TEXT UNIQUE,
            senha_hash TEXT, nome_real TEXT, tratamento TEXT, foto TEXT,
            plano TEXT DEFAULT 'free', socio INTEGER DEFAULT 0, criador INTEGER DEFAULT 0,
            origem TEXT DEFAULT 'local', criado TEXT)""")
        c.execute(f"""CREATE TABLE IF NOT EXISTS kv(
            user_id {uid_t}, k TEXT, v TEXT, PRIMARY KEY(user_id,k))""")
        c.execute(f"""CREATE TABLE IF NOT EXISTS licencas(
            chave TEXT PRIMARY KEY, plano TEXT, usada INTEGER DEFAULT 0,
            user_id {uid_t}, criada TEXT, usada_em TEXT)""")
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
def google_upsert(email, nome, foto):
    """Acha o usuario por e-mail ou cria um novo (origem google). Devolve o id."""
    with _db() as c:
        r = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if r:
            if foto and not (r["foto"] or ""):
                c.execute("UPDATE users SET foto=? WHERE id=?", (foto, r["id"])); c.commit()
            return r["id"]
        n = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        cur = c.execute("INSERT INTO users(nome,email,senha_hash,nome_real,tratamento,foto,plano,criador,origem,criado) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (nome.title(), email, None, nome, "", foto, "free", 1 if n==0 else 0, "google",
             datetime.datetime.now().strftime("%d/%m/%Y %H:%M")))
        c.commit(); return cur.lastrowid
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
            headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0"})  # Cloudflare da Groq bloqueia o UA padrao do urllib (403/1010)
        r = json.loads(urllib.request.urlopen(req, timeout=40).read().decode())
        return (r["choices"][0]["message"]["content"] or "").strip()
    except urllib.error.HTTPError as e:
        corpo = e.read()[:300].decode("utf-8", "ignore")
        print("[llm]", e.code, corpo)
        return f"Tive um problema pra pensar agora ({e.code}). Tente de novo, senhor."
    except Exception as e:
        print("[llm]", e)
        return f"Tive um problema pra pensar agora ({e}). Tente de novo, senhor."

# ======================= BUSCA WEB + NOTICIAS =======================
_UA_WEB = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
def _ddg_link(href):
    try:
        if "uddg=" in href: return urllib.parse.unquote(href.split("uddg=",1)[1].split("&",1)[0])
    except Exception: pass
    return href
def web_search(q):
    """Pesquisa na web. Devolve (resumo, fontes). Usa DuckDuckGo (sem chave)."""
    fontes=[]
    try:
        u="https://api.duckduckgo.com/?"+urllib.parse.urlencode({"q":q,"format":"json","no_html":1,"skip_disambig":1})
        d=json.loads(urllib.request.urlopen(urllib.request.Request(u,headers=_UA_WEB),timeout=8).read().decode())
        if d.get("AbstractURL"): fontes.append({"titulo":d.get("Heading") or "DuckDuckGo","url":d["AbstractURL"]})
        partes=[d.get("Answer") or "", d.get("AbstractText") or ""]
        for t in d.get("RelatedTopics",[])[:6]:
            if isinstance(t,dict) and t.get("Text"): partes.append(t["Text"])
        txt=" ".join(p for p in partes if p).strip()
        if len(txt)>60: return txt[:1700], fontes
    except Exception: pass
    try:
        data=urllib.parse.urlencode({"q":q}).encode()
        page=urllib.request.urlopen(urllib.request.Request("https://lite.duckduckgo.com/lite/",data=data,headers=_UA_WEB),timeout=10).read().decode("utf-8","ignore")
        for href,title in re.findall(r"href=['\"]([^'\"]+)['\"][^>]*class=['\"]result-link['\"][^>]*>(.*?)</a>", page, re.S)[:6]:
            fontes.append({"titulo":_html.unescape(re.sub(r'<[^>]+>','',title)).strip()[:90],"url":_ddg_link(href)})
        snips=re.findall(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td>", page, re.S)
        clean=[re.sub(r'\s+',' ',_html.unescape(re.sub(r'<[^>]+>',' ',s)).strip()) for s in snips]
        txt=" | ".join([c for c in clean if c][:6])[:1700].strip()
        return (txt or None), fontes
    except Exception: return None, fontes

def cloud_chat_web(system, messages, max_tokens=700):
    """Chat com ferramenta de busca: o modelo pesquisa na web quando precisa. Cai pro cloud_chat se algo falhar."""
    if not LLM_KEY: return cloud_chat(system, messages, max_tokens)
    tools=[{"type":"function","function":{"name":"buscar_web",
        "description":"Pesquisa na internet. Use SEMPRE que a pergunta for sobre fatos atuais, noticias, precos, eventos recentes, datas ou qualquer coisa que voce nao saiba com certeza.",
        "parameters":{"type":"object","properties":{"consulta":{"type":"string","description":"o termo de busca"}},"required":["consulta"]}}}]
    msgs=[{"role":"system","content":system}]+list(messages)
    def _call(body):
        req=urllib.request.Request(LLM_BASE.rstrip("/")+"/chat/completions",data=json.dumps(body).encode(),
            headers={"Authorization":f"Bearer {LLM_KEY}","Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
        return json.loads(urllib.request.urlopen(req,timeout=40).read().decode())
    try:
        for _ in range(2):
            r=_call({"model":LLM_MODEL,"max_tokens":max_tokens,"temperature":0.5,"messages":msgs,"tools":tools,"tool_choice":"auto"})
            msg=r["choices"][0]["message"]; tcs=msg.get("tool_calls") or []
            if not tcs: return (msg.get("content") or "").strip()
            msgs.append({"role":"assistant","content":msg.get("content") or "","tool_calls":tcs})
            for tc in tcs:
                try: args=json.loads(tc["function"].get("arguments") or "{}")
                except Exception: args={}
                q=args.get("consulta") or args.get("query") or ""
                resumo,fontes=web_search(q) if q else (None,[])
                cont=(resumo or "Nada encontrado.")
                if fontes: cont+=" || Fontes: "+"; ".join(f.get("url","") for f in fontes[:3])
                msgs.append({"role":"tool","tool_call_id":tc.get("id"),"name":"buscar_web","content":cont[:1800]})
        r=_call({"model":LLM_MODEL,"max_tokens":max_tokens,"temperature":0.5,"messages":msgs})  # resposta final sem ferramenta
        return (r["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        print("[llm-web]", e); return cloud_chat(system, messages, max_tokens)

_FEEDS=[("MERCADO","https://www.infomoney.com.br/feed/"),
        ("BRASIL","https://g1.globo.com/rss/g1/economia/"),
        ("MUNDO","https://g1.globo.com/rss/g1/mundo/"),
        ("MERCADO","https://news.google.com/rss/search?q=mercado+financeiro&hl=pt-BR&gl=BR&ceid=BR:pt-BR")]
_NOTICIAS=[]
def _atualiza_noticias():
    global _NOTICIAS
    out=[]; vistos=set()
    for cat,url in _FEEDS:
        try:
            xml=urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"}),timeout=10).read()
            n=0
            for item in ET.fromstring(xml).iter("item"):
                t=item.find("title")
                if t is not None and t.text:
                    titulo=t.text.rsplit(" - ",1)[0].strip()
                    if titulo and titulo not in vistos:
                        vistos.add(titulo); out.append([cat,titulo]); n+=1
                if n>=4: break
        except Exception: continue
    if out: _NOTICIAS=out
def loop_noticias():
    while True:
        try: _atualiza_noticias()
        except Exception: pass
        time.sleep(600)

# ======================= PUSH ("Orion te chama") =======================
VAPID_PUBLIC = os.environ.get("VAPID_PUBLIC", "BDbH8ACxeYEOyAaF8IqWqBSWu_dKg_LwBCRWxG8rtwE6oVwfMNhgG5BtutYhTDObRTFpeZ8mMkzPqwPJ2hmBV0Y")
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE", "").replace("\\n", "\n")
VAPID_EMAIL = os.environ.get("VAPID_EMAIL", "mailto:gurroffeads@gmail.com")
_PUSH = False; _vapid = None
if VAPID_PRIVATE:
    try:
        from pywebpush import webpush, WebPushException
        from py_vapid import Vapid01
        _vapid = Vapid01.from_pem(VAPID_PRIVATE.encode())
        _PUSH = True
    except Exception as e:
        print("[push] desativado:", e)

def push_subs_get(uid): return get_blob(uid, "push_subs", [])
def push_subs_add(uid, sub):
    if not (sub and sub.get("endpoint")): return
    subs = push_subs_get(uid)
    if sub["endpoint"] not in {s.get("endpoint") for s in subs}:
        subs.append(sub); set_blob(uid, "push_subs", subs)
def notificar(uid, titulo, corpo, url="/"):
    if not _PUSH: return
    subs = push_subs_get(uid); vivos = []
    for s in subs:
        try:
            webpush(subscription_info=s, data=json.dumps({"title":titulo,"body":corpo,"url":url}),
                    vapid_private_key=_vapid, vapid_claims={"sub":VAPID_EMAIL}); vivos.append(s)
        except WebPushException as e:
            code = getattr(getattr(e,"response",None),"status_code",0)
            if code not in (404,410): vivos.append(s); print("[push]", code)
        except Exception as e:
            vivos.append(s); print("[push]", e)
    if len(vivos) != len(subs): set_blob(uid, "push_subs", vivos)
def marcar_ativo(uid):
    try: set_blob(uid, "ativo_em", time.time())
    except Exception: pass
def loop_reengajar():
    while True:
        time.sleep(3600)
        if not _PUSH: continue
        try:
            with _db() as c:
                ids = [r["id"] for r in c.execute("SELECT id FROM users").fetchall()]
            agora = time.time()
            for uid in ids:
                at = get_blob(uid, "ativo_em", 0); rg = get_blob(uid, "reeng_em", 0)
                if at and (agora-at > 2*86400) and (agora-rg > 3*86400):
                    set_blob(uid, "reeng_em", agora)
                    notificar(uid, "Senti sua falta, senhor", "Voltei pra te ajudar. Tem algo que eu possa adiantar pra voce?", "/")
        except Exception as e: print("[reeng]", e)

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
def _is_master(chave):
    return chave in (lic_master("pro"), lic_master("business"))
def lic_registrar(plano):
    """Gera UMA chave de venda e registra no banco como disponivel (uso unico)."""
    chave = lic_make(plano)
    try:
        with _db() as c:
            c.execute("INSERT INTO licencas(chave,plano,usada,criada) VALUES(?,?,0,?)",
                (chave, plano, datetime.datetime.now().strftime("%d/%m/%Y %H:%M"))); c.commit()
    except Exception as e:
        print("[lic_registrar]", e)
    return chave
def lic_ativar(u, chave):
    chave = (chave or "").strip().upper().replace(" ", "")
    pl = lic_check(chave)
    if not pl: return {"ok":False,"erro":"Chave invalida, senhor. Confira se copiou ela inteira."}
    if _is_master(chave):                      # chave master do dono: ilimitada
        if _set_plano(u["id"], pl): return {"ok":True,"plano":pl,"user":_pub(get_user(u["id"]))}
        return {"ok":False,"erro":"Nao consegui ativar agora."}
    with _db() as c:                            # chave de venda: uso unico
        row = c.execute("SELECT * FROM licencas WHERE chave=?", (chave,)).fetchone()
        if not row: return {"ok":False,"erro":"Chave nao reconhecida. Gere pelo painel, senhor."}
        if row["usada"]: return {"ok":False,"erro":"Essa chave ja foi usada, senhor."}
        c.execute("UPDATE licencas SET usada=1, user_id=?, usada_em=? WHERE chave=?",
            (u["id"], datetime.datetime.now().strftime("%d/%m/%Y %H:%M"), chave)); c.commit()
    if _set_plano(u["id"], pl): return {"ok":True,"plano":pl,"user":_pub(get_user(u["id"]))}
    return {"ok":False,"erro":"Nao consegui ativar agora."}

# ======================= PAGAMENTO (server-side) =======================
def base_url(handler):
    host = handler.headers.get("Host","localhost")
    proto = handler.headers.get("X-Forwarded-Proto","http")
    return f"{proto}://{host}"
def mp_token(): return os.environ.get("MP_ACCESS_TOKEN","")
def pp_creds(): return (os.environ.get("PAYPAL_CLIENT_ID",""), os.environ.get("PAYPAL_SECRET",""), os.environ.get("PAYPAL_MODE","sandbox"))
def google_creds(): return (os.environ.get("GOOGLE_CLIENT_ID",""), os.environ.get("GOOGLE_CLIENT_SECRET",""))
def checkout(u, plano, provedor, burl):
    plano = (plano or "pro").lower()
    if plano not in PLANOS_PRECO or plano == "free": return {"ok":False,"msg":"Plano invalido."}
    if not provedor: provedor = "mp" if mp_token() else ("paypal" if pp_creds()[0] else None)
    if provedor == "mp" and mp_token():
        try:
            body = {"items":[{"title":PLANOS_NOME[plano],"quantity":1,"unit_price":PLANOS_PRECO[plano],"currency_id":"BRL"}],
                    "external_reference":f"{plano}:{u['id']}",
                    "back_urls":{"success":f"{burl}/?pago={plano}","failure":f"{burl}/?pago=falhou"}}
            if burl.startswith("https"): body["auto_return"]="approved"; body["notification_url"]=f"{burl}/pagamento/webhook"  # MP: auto_return + webhook so com https
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

def _set_plano(uid, plano):
    if plano not in ("pro","business"): return False
    try:
        with _db() as c: c.execute("UPDATE users SET plano=? WHERE id=?", (plano, int(uid))); c.commit()
        return True
    except Exception as e:
        print("[set_plano]", e); return False

def _pp_token(cid, sec, mode):
    base = "https://api-m.paypal.com" if mode=="live" else "https://api-m.sandbox.paypal.com"
    auth = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    tok = json.loads(urllib.request.urlopen(urllib.request.Request(base+"/v1/oauth2/token",
        data=b"grant_type=client_credentials",
        headers={"Authorization":f"Basic {auth}","Content-Type":"application/x-www-form-urlencoded","User-Agent":"Mozilla/5.0"}),timeout=20).read())["access_token"]
    return base, tok

def confirmar_pagamento(pid, provedor):
    """Verifica o pagamento de VERDADE e sobe o plano do COMPRADOR (o uid vem do proprio pagamento, nao do cliente)."""
    if not pid: return {"ok":False,"erro":"Sem identificador de pagamento."}
    try:
        if provedor == "paypal":
            cid,sec,mode = pp_creds()
            if not cid: return {"ok":False,"erro":"PayPal nao configurado."}
            base,tok = _pp_token(cid,sec,mode)
            r = json.loads(urllib.request.urlopen(urllib.request.Request(base+f"/v2/checkout/orders/{pid}/capture",
                data=b"{}", headers={"Authorization":f"Bearer {tok}","Content-Type":"application/json","User-Agent":"Mozilla/5.0"}),timeout=25).read())
            cust = ""
            try:
                pu = (r.get("purchase_units") or [{}])[0]
                cust = pu.get("custom_id") or pu.get("payments",{}).get("captures",[{}])[0].get("custom_id","")
            except Exception: cust = ""
            if r.get("status") == "COMPLETED" and ":" in cust:
                plano,uid = cust.split(":",1)
                if _set_plano(uid, plano): return {"ok":True,"plano":plano,"user":_pub(get_user(int(uid)))}
            return {"ok":False,"erro":f"Pagamento PayPal nao confirmado ({r.get('status')})."}
        else:  # mercado pago
            if not mp_token(): return {"ok":False,"erro":"Mercado Pago nao configurado."}
            r = json.loads(urllib.request.urlopen(urllib.request.Request("https://api.mercadopago.com/v1/payments/"+str(pid),
                headers={"Authorization":f"Bearer {mp_token()}","User-Agent":"Mozilla/5.0"}),timeout=20).read())
            ext = r.get("external_reference") or ""
            if r.get("status") == "approved" and ":" in ext:
                plano,uid = ext.split(":",1)
                if _set_plano(uid, plano): return {"ok":True,"plano":plano,"user":_pub(get_user(int(uid)))}
            return {"ok":False,"erro":f"Pagamento ainda nao aprovado ({r.get('status')})."}
    except urllib.error.HTTPError as e:
        print("[confirmar]", e.code); return {"ok":False,"erro":f"Erro ao confirmar ({e.code})."}
    except Exception as e:
        print("[confirmar]", e); return {"ok":False,"erro":f"Erro ao confirmar: {e}"}

def mp_webhook(pid):
    """Backup: o MP notifica e a gente sobe o plano (idempotente)."""
    try:
        if pid and mp_token():
            r = json.loads(urllib.request.urlopen(urllib.request.Request("https://api.mercadopago.com/v1/payments/"+str(pid),
                headers={"Authorization":f"Bearer {mp_token()}","User-Agent":"Mozilla/5.0"}),timeout=15).read())
            ext = r.get("external_reference") or ""
            if r.get("status") == "approved" and ":" in ext:
                plano,uid = ext.split(":",1); _set_plano(uid, plano)
    except Exception as e: print("[mp-webhook]", e)

# ======================= VISAO (Groq, le grafico/foto) =======================
def cloud_vision(b64, instr, max_tokens=600):
    if not LLM_KEY: return ""
    model = os.environ.get("LLM_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    body = {"model":model,"max_tokens":max_tokens,"temperature":0.3,"messages":[{"role":"user","content":[
        {"type":"text","text":instr},{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,"+b64}}]}]}
    try:
        req = urllib.request.Request(LLM_BASE.rstrip("/")+"/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization":f"Bearer {LLM_KEY}","Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
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

    def _redir(self, location, cookie=None):
        self.send_response(302)
        if cookie: self.send_header("Set-Cookie", f"orion_sess={cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
        self.send_header("Location", location); self.end_headers()

    def _google_start(self):
        cid, _ = google_creds()
        if not cid: return self._redir("/?login=google_off")
        params = urllib.parse.urlencode({
            "client_id": cid, "redirect_uri": base_url(self)+"/auth/google/callback",
            "response_type": "code", "scope": "openid email profile",
            "access_type": "online", "prompt": "select_account"})
        self._redir("https://accounts.google.com/o/oauth2/v2/auth?"+params)

    def _google_callback(self):
        cid, sec = google_creds()
        qs = urllib.parse.parse_qs(self.path.split("?",1)[1]) if "?" in self.path else {}
        code = (qs.get("code") or [""])[0]
        if not (code and cid and sec): return self._redir("/?login=google_err")
        try:
            data = urllib.parse.urlencode({"code":code,"client_id":cid,"client_secret":sec,
                "redirect_uri":base_url(self)+"/auth/google/callback","grant_type":"authorization_code"}).encode()
            tok = json.loads(urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token",
                data=data, headers={"Content-Type":"application/x-www-form-urlencoded","User-Agent":"Mozilla/5.0"}),timeout=20).read().decode())
            info = json.loads(urllib.request.urlopen(urllib.request.Request("https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization":"Bearer "+tok.get("access_token",""),"User-Agent":"Mozilla/5.0"}),timeout=20).read().decode())
            email = (info.get("email") or "").strip().lower()
            if not email: return self._redir("/?login=google_err")
            uid = google_upsert(email, info.get("name") or email.split("@")[0], info.get("picture") or "")
            self._redir("/?login=ok", cookie=make_session(uid))
        except Exception as e:
            print("[google]", e); self._redir("/?login=google_err")

    def do_OPTIONS(self): self._send(b"",204)

    def do_GET(self):
        try: self._get()
        except Exception as e:
            print("[GET]", self.path, repr(e))
            try: self._send({"erro":f"erro interno: {e}"}, 500)
            except Exception: pass
    def _get(self):
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
            self._send(
                "self.addEventListener('install',e=>self.skipWaiting());"
                "self.addEventListener('activate',e=>self.clients.claim());"
                "self.addEventListener('push',function(e){var d={};try{d=e.data.json()}catch(_){d={title:'Orion',body:(e.data&&e.data.text())||''}}"
                "e.waitUntil(self.registration.showNotification(d.title||'Orion',{body:d.body||'',icon:'/icon',badge:'/icon',data:{url:d.url||'/'}}));});"
                "self.addEventListener('notificationclick',function(e){e.notification.close();"
                "e.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(function(cl){for(var i=0;i<cl.length;i++){if('focus' in cl[i])return cl[i].focus();}if(clients.openWindow)return clients.openWindow((e.notification.data&&e.notification.data.url)||'/');}));});",
                200,"application/javascript")
        elif path == "/.well-known/assetlinks.json":
            # necessario pro APK (TWA) verificar que o site e seu. Cole o JSON do PWABuilder na env ASSETLINKS_JSON.
            self._send(os.environ.get("ASSETLINKS_JSON", "[]"), 200, "application/json")
        elif path == "/auth/google": self._google_start()
        elif path == "/auth/google/callback": self._google_callback()
        elif path == "/pagamento/webhook":
            qs = urllib.parse.parse_qs(self.path.split("?",1)[1]) if "?" in self.path else {}
            mp_webhook((qs.get("data.id") or qs.get("id") or [None])[0]); self._send({"ok":True})
        elif path == "/push/key": self._send({"key":VAPID_PUBLIC, "on":_PUSH})
        elif path == "/estado":
            self._send({"status":"ocioso","cloud":True,"user":(_pub(u) if u else None),"noticias":_NOTICIAS})
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
        try: self._post()
        except Exception as e:
            print("[POST]", self.path, repr(e))
            try: self._send({"ok":False,"erro":f"erro interno: {e}"}, 500)
            except Exception: pass
    def _post(self):
        path = self.path
        d = self._body()
        u = self._user()
        # ---- contas / sessao ----
        if path == "/usuario/criar":
            nome = (d.get("nome") or "").strip()
            if not nome: self._send({"ok":False,"erro":"Diga seu nome."}); return
            if not d.get("aceitou_termos"): self._send({"ok":False,"erro":"Voce precisa aceitar os Termos de Uso e a Privacidade pra criar a conta."}); return
            if len(d.get("senha") or "") < 8: self._send({"ok":False,"erro":"A senha precisa de pelo menos 8 caracteres, senhor."}); return
            try:
                with _db() as c:
                    n_users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
                    cur = c.execute("INSERT INTO users(nome,email,senha_hash,nome_real,tratamento,foto,plano,criador,origem,criado) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (nome.title(), (d.get("email") or "").strip().lower() or None, _hash(d.get("senha")),
                         (d.get("nome_real") or "").strip(), (d.get("tratamento") or "").strip(), d.get("foto") or "",
                         "free", 1 if n_users==0 else 0, "local", datetime.datetime.now().strftime("%d/%m/%Y %H:%M")))
                    c.commit(); uid = cur.lastrowid
                self._send({"ok":True,"user":_pub(get_user(uid))}, cookie=make_session(uid))
            except INTEGRITY_ERRORS:
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
            uso_reg(u, "msg"); marcar_ativo(u["id"])
            trat = (u["tratamento"] or "").strip()
            hoje = datetime.date.today().strftime("%d/%m/%Y")
            fl = frase.lower(); extra = ""
            if _NOTICIAS and any(w in fl for w in ("noticia","notícia","mercado","acontec","manchete","jornal","economia","hoje")):
                extra = (" MANCHETES REAIS DE HOJE (resuma a partir DESTAS, NAO diga que nao achou): "
                         + " || ".join(f"[{c}] {t}" for c,t in _NOTICIAS[:10]) + ".")
            sysp = (PERSONA + f" Hoje e {hoje}." + (f" Trate o usuario como '{trat}'." if trat else "") + extra
                    + " Seja capaz, util e direto. Para fatos atuais, precos, eventos ou o que nao souber, USE a ferramenta buscar_web e cite a fonte. Nunca invente numeros. Se houver manchetes acima, resuma a partir delas.")
            hist = get_blob(u["id"], "hist", [])[-8:]
            reply = cloud_chat_web(sysp, hist + [{"role":"user","content":frase}])
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
                self._send({"ok":True,"plano":d["plano"],"chave":lic_registrar(d["plano"])})
            else: self._send({"ok":False,"erro":"so o dono gera chaves"})
        elif path == "/pagamento/checkout":
            self._send(checkout(u, d.get("plano","pro"), d.get("provedor"), base_url(self)) if u else {"ok":False,"msg":"faca login"})
        elif path == "/pagamento/confirmar":
            self._send(confirmar_pagamento(d.get("payment_id"), d.get("provedor")) if u else {"ok":False,"erro":"faca login"})
        elif path == "/pagamento/webhook":
            qs = urllib.parse.parse_qs(self.path.split("?",1)[1]) if "?" in self.path else {}
            pid = (d.get("data") or {}).get("id") or (qs.get("data.id") or qs.get("id") or [None])[0]
            mp_webhook(pid); self._send({"ok":True})
        elif path == "/push/subscribe":
            if u: push_subs_add(u["id"], d.get("sub") or {}); self._send({"ok":True})
            else: self._send({"ok":False,"erro":"faca login"})
        elif path == "/push/teste":
            if u: notificar(u["id"], "Orion", "Funcionou, senhor. Vou te chamar quando precisar."); self._send({"ok":True,"on":_PUSH})
            else: self._send({"ok":False})
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
    threading.Thread(target=loop_noticias, daemon=True).start()
    threading.Thread(target=loop_reengajar, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()

if __name__ == "__main__":
    main()
