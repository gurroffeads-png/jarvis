# -*- coding: utf-8 -*-
"""Clinica+ : backend (modulo proprio).

Toda a logica `cl_*` da Clinica+ vive aqui. Foi separada do orion_cloud.py para
que o Orion e a Clinica+ evoluam sem se sobrescrever nos syncs (combinado do
INTEGRAR.md). O orion_cloud.py faz `import clinica_backend` no fim do load; este
modulo injeta os nomes `cl_*` de volta no orion_cloud (qualquer ordem de import).

Os helpers do orion (get_blob, set_blob, cloud_chat, _db, etc.) e a stdlib sao
reaproveitados via late-binding (_bind_orion_helpers), sem prefixo.
"""
import os, re, json, time, datetime, threading, hmac, hashlib, base64, urllib.request, urllib.parse
import orion_cloud as _oc

def _bind_orion_helpers():
    """Copia pra este modulo os nomes do orion_cloud (helpers + stdlib) que ainda
    nao existem aqui, pra que as funcoes cl_* usem `get_blob`, `cloud_chat`, etc.
    sem prefixo. Roda no fim, com o orion_cloud ja 100% carregado."""
    g = globals()
    for _n in dir(_oc):
        if not _n.startswith("__") and _n not in g:
            g[_n] = getattr(_oc, _n)


# ======================= CLINICA: gestao pra clinicas de estetica (produto a parte, usa o motor do Orion) =======================
# Dados por DONA da clinica (uid do dono = id da clinica). Funcionarios logam com email da clinica + login + senha.
CL_DIAS = ["seg","ter","qua","qui","sex","sab","dom"]
CL_DIAS_NOME = {"seg":"Segunda","ter":"Terca","qua":"Quarta","qui":"Quinta","sex":"Sexta","sab":"Sabado","dom":"Domingo"}
CL_FAQ_PADRAO = [
  {"pergunta":"Quais procedimentos vocês fazem?","resposta":"Trabalhamos com varios procedimentos esteticos. Me diga o que voce procura que eu te explico e ja posso agendar."},
  {"pergunta":"Qual o valor?","resposta":"Os valores variam por procedimento. Me diga qual te interessa que eu informo o preco atualizado."},
  {"pergunta":"Como faço pra agendar?","resposta":"Me diga o procedimento e um dia/horario de sua preferencia que eu verifico a disponibilidade e marco pra voce."},
  {"pergunta":"Onde fica a clínica?","resposta":"Posso te passar o endereco e como chegar. Quer que eu envie a localizacao?"},
  {"pergunta":"Precisa de preparo antes do procedimento?","resposta":"Depende do procedimento. Me diga qual voce vai fazer que eu te oriento sobre o preparo."},
  {"pergunta":"Posso remarcar ou cancelar?","resposta":"Claro. Me diga seu nome e o horario marcado que eu remarco ou cancelo pra voce."},
  {"pergunta":"Quais as formas de pagamento?","resposta":"Aceitamos cartao (maquininha), dinheiro e PIX. Posso confirmar os detalhes no agendamento."},
]
def _hm(s):
    try: h,m=str(s).split(":")[:2]; return int(h)*60+int(m)
    except Exception: return 0
def _hmstr(t): t=max(0,int(t)); return f"{t//60:02d}:{t%60:02d}"
def _dtmin(iso):
    try: return _hm(str(iso).split("T")[1][:5])
    except Exception: return 0
# ---- perfil do negocio (editavel: tudo que costuma mudar) ----
def cl_perfil_get(uid): return get_blob(uid, "cl_perfil", {}) or {}
def cl_perfil_set(uid, d):
    p = cl_perfil_get(uid)
    for k in ("nome","sobre","dona","telefone","endereco","instagram","boas_vindas"):
        if k in d: p[k] = str(d.get(k) or "")[:600]
    set_blob(uid, "cl_perfil", p); return {"ok":True, "perfil":p}
# ---- servicos: tabela de precos + duracao media (pra agenda nao atropelar) ----
def cl_servicos_get(uid): return get_blob(uid, "cl_servicos", []) or []
def cl_servico_by(uid, sid):
    try: sid=int(sid)
    except Exception: pass
    return next((s for s in cl_servicos_get(uid) if s.get("id")==sid), None)
def cl_servico_salvar(uid, d):
    sv = cl_servicos_get(uid); sid = d.get("id") or int(time.time()*1000)
    s = {"id":sid, "nome":(d.get("nome") or "").strip()[:80], "preco":float(d.get("preco") or 0), "duracao":max(5,int(d.get("duracao") or 60))}
    if not s["nome"]: return {"ok":False,"erro":"de um nome ao servico"}
    sv = [x for x in sv if x.get("id")!=sid]; sv.append(s); sv.sort(key=lambda x:x["nome"].lower())
    set_blob(uid, "cl_servicos", sv); return {"ok":True, "servicos":sv}
def cl_servico_apagar(uid, sid):
    try: sid=int(sid)
    except Exception: pass
    set_blob(uid, "cl_servicos", [x for x in cl_servicos_get(uid) if x.get("id")!=sid]); return {"ok":True}
# ---- horario de funcionamento (dias abertos/fechados + janela + intervalo entre encaixes) ----
def cl_horario_get(uid):
    h = get_blob(uid, "cl_horario", {}) or {}
    if not h.get("dias"):
        h = {"dias":{d:{"aberto":(d!="dom"), "ini":"09:00", "fim":"18:00"} for d in CL_DIAS}, "intervalo":30}
    return h
def cl_horario_set(uid, d):
    h = cl_horario_get(uid)
    if isinstance(d.get("dias"), dict): h["dias"] = d["dias"]
    if d.get("intervalo"): h["intervalo"] = max(5, int(d["intervalo"]))
    set_blob(uid, "cl_horario", h); return {"ok":True, "horario":h}
# ---- funcionarios (CRUD + login proprio) ----
def cl_func_get(uid): return get_blob(uid, "cl_funcionarios", []) or []
def cl_func_pub(f):
    return {"id":f["id"], "nome":f.get("nome",""), "login":f.get("login",""), "cargo":f.get("cargo",""),
            "admin":bool(f.get("admin")), "ativo":(f.get("ativo", True) is not False), "tem_senha":bool(f.get("senha_hash")),
            "email":f.get("email",""), "telefone":f.get("telefone",""), "nascimento":f.get("nascimento",""),
            "servicos":f.get("servicos", []), "comissao_pct":f.get("comissao_pct", 0),
            "cpf":("***."+f.get("cpf","")[3:6]+".***-**" if f.get("cpf") else "")}   # CPF mascarado na listagem
def cl_func_salvar(uid, d):
    fs = cl_func_get(uid); fid = d.get("id") or int(time.time()*1000)
    cur = next((x for x in fs if x.get("id")==fid), {})
    novo = not cur
    login = (d.get("login") or "").strip().lower()[:40]
    if any(x.get("login")==login and x.get("id")!=fid for x in fs): return {"ok":False,"erro":"ja existe funcionario com esse login"}
    nome = (d.get("nome") or "").strip()[:60]
    cpf = _digs(d.get("cpf")) or cur.get("cpf","")
    if novo and len([x for x in fs]) >= 1 and not cl_pode(uid, "equipe"):
        return {"ok":False, "upgrade":True, "erro":"O plano gratis permite 1 profissional. Assine o Profissional pra ter equipe."}
    if not nome or not login: return {"ok":False,"erro":"nome e login sao obrigatorios"}
    if not cpf or len(cpf)!=11: return {"ok":False,"erro":"informe o CPF do funcionario (11 digitos)"}
    if not _digs(d.get("telefone") or cur.get("telefone")): return {"ok":False,"erro":"informe o telefone do funcionario"}
    if novo and len(d.get("senha") or "")<4: return {"ok":False,"erro":"defina uma senha de acesso pro funcionario"}
    servs = d.get("servicos") if isinstance(d.get("servicos"), list) else cur.get("servicos", [])
    try: servs = [int(x) for x in (servs or [])]
    except Exception: servs = cur.get("servicos", [])
    f = {"id":fid, "nome":nome, "login":login, "cargo":(d.get("cargo") or "").strip()[:40], "admin":bool(d.get("admin")),
         "ativo": (bool(d.get("ativo")) if "ativo" in d else cur.get("ativo", True)),
         "cpf":cpf, "telefone":_digs(d.get("telefone")) or cur.get("telefone",""),
         "email":((d.get("email") or cur.get("email","")).strip().lower()[:120]),
         "nascimento":(d.get("nascimento") or cur.get("nascimento","")),
         "servicos": servs,
         "comissao_pct": (max(0, min(100, float(d.get("comissao_pct")))) if d.get("comissao_pct") not in (None,"") else cur.get("comissao_pct", 0)),
         "horario": (d.get("horario") if isinstance(d.get("horario"), dict) else cur.get("horario")),
         "senha_hash": (_hash(d["senha"]) if d.get("senha") else cur.get("senha_hash")),
         "criado": cur.get("criado", time.time())}
    fs = [x for x in fs if x.get("id")!=fid]; fs.append(f); set_blob(uid, "cl_funcionarios", fs)
    return {"ok":True, "funcionarios":[cl_func_pub(x) for x in fs]}
def cl_func_ativo(uid, fid, ativo):
    try: fid=int(fid)
    except Exception: pass
    fs = cl_func_get(uid)
    for f in fs:
        if f.get("id")==fid: f["ativo"] = bool(ativo)
    set_blob(uid, "cl_funcionarios", fs); return {"ok":True, "funcionarios":[cl_func_pub(x) for x in fs]}
def cl_func_apagar(uid, fid):
    try: fid=int(fid)
    except Exception: pass
    set_blob(uid, "cl_funcionarios", [x for x in cl_func_get(uid) if x.get("id")!=fid]); return {"ok":True}
def cl_func_login(clinica_email, login, senha):
    with _db() as c:
        r = c.execute("SELECT id FROM users WHERE lower(email)=lower(?)", ((clinica_email or "").strip(),)).fetchone()
    if not r: return None
    uid = r["id"]
    for f in cl_func_get(uid):
        if f.get("login")==(login or "").strip().lower() and f.get("senha_hash") and _senha_ok(senha, f["senha_hash"]):
            if f.get("ativo", True) is False: return "inativo"
            return {"uid":uid, "fid":f["id"], "nome":f.get("nome"), "admin":bool(f.get("admin"))}
    return None
# ---- agenda: disponibilidade (respeita horario + duracao + sem sobreposicao) ----
def cl_ags_get(uid): return get_blob(uid, "cl_agendamentos", []) or []
def cl_disponibilidade(uid, data_iso, servico_id, func_id=None):
    try: dt = datetime.date.fromisoformat(data_iso)
    except Exception: return {"ok":False,"erro":"data invalida"}
    try: fid = int(func_id) if func_id not in (None,"","null") else None
    except Exception: fid = None
    h = cl_horario_get(uid)
    f = _func_by(uid, fid) if fid else None
    try: sid_int = int(servico_id)
    except Exception: sid_int = None
    # agenda por profissional: usa servicos/horario do funcionario quando definidos
    if f:
        if f.get("ativo", True) is False: return {"ok":True, "slots":[], "motivo":"profissional indisponivel"}
        if f.get("servicos") and sid_int is not None and sid_int not in [int(x) for x in f["servicos"]]:
            return {"ok":True, "slots":[], "motivo":"esse profissional nao faz esse servico"}
        if isinstance(f.get("horario"), dict) and f["horario"].get("dias"): h = f["horario"]
    dd = h["dias"].get(CL_DIAS[dt.weekday()], {})
    if not dd.get("aberto"): return {"ok":True, "slots":[], "motivo":"fechado nesse dia"}
    sv = cl_servico_by(uid, servico_id); dur = int((sv or {}).get("duracao", 60))
    passo = int(h.get("intervalo", 30)) or 30
    ini = _hm(dd.get("ini","09:00")); fim = _hm(dd.get("fim","18:00"))
    ocup = [(_dtmin(a["inicio"]), _dtmin(a["inicio"])+int(a.get("duracao",60))) for a in cl_ags_get(uid)
            if a.get("status")=="marcado" and str(a.get("inicio","")).startswith(data_iso) and (fid is None or a.get("func_id")==fid)]
    for b in cl_bloqueios_get(uid):   # folgas/bloqueios avulsos contam como ocupado
        if str(b.get("data"))==data_iso: ocup.append((_hm(b.get("ini","00:00")), _hm(b.get("fim","23:59"))))
    agora_min = _hm(datetime.datetime.now().strftime("%H:%M")) if dt==datetime.date.today() else -1
    slots = []; t = ini
    while t + dur <= fim:
        if t > agora_min and not any(not (t+dur<=o0 or t>=o1) for o0,o1 in ocup):
            slots.append(_hmstr(t))
        t += passo
    return {"ok":True, "slots":slots, "duracao":dur}
def cl_agendar(uid, d, origem="painel"):
    sv = cl_servico_by(uid, d.get("servico_id"))
    if not sv: return {"ok":False, "erro":"servico nao encontrado"}
    data = (d.get("data") or "").strip(); hora = (d.get("hora") or "").strip()
    if not (data and hora): return {"ok":False, "erro":"informe data e hora"}
    try: fid = int(d.get("func_id")) if d.get("func_id") not in (None,"","null") else None
    except Exception: fid = None
    disp = cl_disponibilidade(uid, data, sv["id"], fid)
    if not disp.get("ok"): return disp
    if hora not in disp["slots"]: return {"ok":False, "erro":"esse horario nao esta disponivel", "slots":disp["slots"]}
    ags = cl_ags_get(uid); aid = int(time.time()*1000)
    a = {"id":aid, "cliente":(d.get("cliente") or "").strip()[:80], "telefone":(d.get("telefone") or "").strip()[:30],
         "servico_id":sv["id"], "servico":sv["nome"], "func_id":fid, "inicio":f"{data}T{hora}", "duracao":int(sv.get("duracao",60)),
         "valor":float(d.get("valor") if d.get("valor") is not None else sv.get("preco",0)),
         "status":"marcado", "pagamento":(d.get("pagamento") or ""), "origem":origem, "criado":time.time()}
    ags.append(a); set_blob(uid, "cl_agendamentos", ags)
    try:
        if a["telefone"]: cl_cliente_registrar(uid, a["cliente"], a["telefone"])
    except Exception: pass
    try: notificar(uid, "Nova consulta marcada", f"{a['cliente'] or 'Cliente'} - {a['servico']} em {data} as {hora}", "/clinica")
    except Exception: pass
    try:   # confirmacao automatica pro cliente (se WhatsApp conectado)
        if a["telefone"] and cl_lembrete_get(uid).get("confirmar", True):
            p = cl_perfil_get(uid)
            _wa_enviar(a["telefone"], f"{p.get('nome','Sua clinica')}: sua consulta de {a['servico']} esta confirmada para {data} as {hora}. Ate la!", uid)
    except Exception: pass
    resp = {"ok":True, "agendamento":a}
    try:   # PIX (sinal ou total) pra agendamento de cliente/atendente, se a clinica ativou
        px = cl_pix_get(uid)
        if px.get("ativo") and origem in ("cliente","atendente") and float(a.get("valor") or 0) > 0:
            valor = float(a["valor"]) * (px.get("sinal_pct",30)/100.0 if px.get("modo")=="sinal" else 1.0)
            cob = cl_pix_cobrar(uid, valor, f"{a['servico']} - {a['cliente'] or 'cliente'}")
            if cob.get("ok"):
                a["pix"] = {"id":cob.get("id"), "valor":cob.get("valor")}; set_blob(uid, "cl_agendamentos", ags)
                resp["pix"] = {"copia_cola":cob.get("copia_cola",""), "qr_base64":cob.get("qr_base64",""), "valor":cob.get("valor"), "tipo":px.get("modo")}
    except Exception as e: print("[agendar pix]", e)
    return resp
def cl_desmarcar(uid, aid, motivo=""):
    try: aid=int(aid)
    except Exception: pass
    ags = cl_ags_get(uid); ch = False; nome=""
    for a in ags:
        if a.get("id")==aid: a["status"]="desmarcado"; a["motivo"]=(motivo or "")[:200]; ch=True; nome=a.get("cliente","")
    set_blob(uid, "cl_agendamentos", ags)
    if ch:
        try: notificar(uid, "Consulta desmarcada", f"{nome or 'Cliente'} cancelou. Um horario foi liberado.", "/clinica")
        except Exception: pass
    return {"ok":ch}
def cl_concluir(uid, aid, d=None):
    try: aid=int(aid)
    except Exception: pass
    ags = cl_ags_get(uid); tel=""; svid=None; usou_pacote=False
    for a in ags:
        if a.get("id")==aid:
            a["status"]="concluido"
            if d and d.get("pagamento"): a["pagamento"]=d["pagamento"]
            if d and d.get("valor") is not None: a["valor"]=float(d["valor"])
            tel = a.get("telefone",""); svid = a.get("servico_id")
            # se o cliente tem pacote pra esse servico, debita 1 sessao (ja foi pago na compra do pacote)
            if tel and _cl_pacote_debitar(uid, tel, svid):
                a["valor"]=0; a["pagamento"]="pacote"; usou_pacote=True
    set_blob(uid, "cl_agendamentos", ags)
    if tel:
        try: cl_cliente_proc_inc(uid, tel)
        except Exception: pass
    try: _cl_estoque_baixa_servico(uid, svid)   # baixa automatica de insumos
    except Exception: pass
    return {"ok":True, "pacote":usou_pacote}
def cl_agenda(uid, data_iso=None, fid=None):
    ags = [a for a in cl_ags_get(uid) if a.get("status")=="marcado"]
    if data_iso: ags = [a for a in ags if str(a.get("inicio","")).startswith(data_iso)]
    if fid is not None: ags = [a for a in ags if a.get("func_id")==fid]
    fmap = {f["id"]:f.get("nome","") for f in cl_func_get(uid)}
    for a in ags: a["func_nome"] = fmap.get(a.get("func_id"), "")
    return sorted(ags, key=lambda x: x.get("inicio",""))
# ---- dashboard / faturamento (dono e admin) ----
def cl_dashboard(uid):
    ags = cl_ags_get(uid); hoje = datetime.date.today().isoformat(); mes = datetime.date.today().strftime("%Y-%m")
    marcados = [a for a in ags if a.get("status")=="marcado"]
    concl_mes = [a for a in ags if a.get("status")=="concluido" and str(a.get("inicio","")).startswith(mes)]
    fat = sum(float(a.get("valor") or 0) for a in concl_mes)
    por_pag = {}
    for a in concl_mes:
        k = a.get("pagamento") or "outro"; por_pag[k] = round(por_pag.get(k,0)+float(a.get("valor") or 0),2)
    fmap = {f["id"]:f for f in cl_func_get(uid)}; agg = {}
    for a in concl_mes: agg[a.get("func_id")] = agg.get(a.get("func_id"),0)+float(a.get("valor") or 0)
    por_func = sorted([{"funcionario":(fmap.get(k) or {}).get("nome","(sem funcionario)"), "faturou":round(v,2),
                        "comissao":round(v*((fmap.get(k) or {}).get("comissao_pct",0))/100.0, 2), "comissao_pct":(fmap.get(k) or {}).get("comissao_pct",0)}
                       for k,v in agg.items()], key=lambda x:-x["faturou"])
    fs = cl_func_get(uid)
    return {"ok":True, "agenda_hoje":cl_agenda(uid, hoje), "marcados_total":len(marcados),
            "concluidos_mes":len(concl_mes), "faturamento_mes":round(fat,2),
            "por_pagamento":por_pag, "por_funcionario":por_func,
            "func_total":len(fs), "func_ativos":sum(1 for f in fs if f.get("ativo", True) is not False)}
def cl_relatorios(uid):
    """Relatorios pro painel: faturamento por dia (30d), servicos mais vendidos, taxa de falta/cancelamento."""
    ags = cl_ags_get(uid); hoje = datetime.date.today()
    concl = [a for a in ags if a.get("status")=="concluido"]
    # faturamento por dia (ultimos 30 dias)
    por_dia = {}
    for a in concl:
        dia = str(a.get("inicio","")).split("T")[0]
        if dia: por_dia[dia] = round(por_dia.get(dia,0)+float(a.get("valor") or 0), 2)
    serie = [{"dia":(hoje-datetime.timedelta(days=i)).strftime("%d/%m"),
              "valor":por_dia.get((hoje-datetime.timedelta(days=i)).isoformat(), 0)} for i in range(29,-1,-1)]
    # servicos mais vendidos (qtd + receita)
    sv = {}
    for a in concl:
        s = a.get("servico","?"); d = sv.setdefault(s, {"servico":s, "qtd":0, "receita":0.0})
        d["qtd"] += 1; d["receita"] = round(d["receita"]+float(a.get("valor") or 0), 2)
    top = sorted(sv.values(), key=lambda x:-x["qtd"])[:10]
    # taxa de falta/cancelamento
    desm = sum(1 for a in ags if a.get("status")=="desmarcado")
    base = len(concl) + desm
    taxa = round(desm/base*100, 1) if base else 0
    return {"ok":True, "faturamento_dia":serie, "top_servicos":top,
            "concluidos":len(concl), "cancelados":desm, "taxa_falta":taxa,
            "total_periodo":round(sum(por_dia.values()),2)}
def cl_caixa(uid, data_iso=None):
    """Fechamento de caixa de um dia: entradas por forma de pagamento e por profissional."""
    data_iso = data_iso or datetime.date.today().isoformat()
    concl = [a for a in cl_ags_get(uid) if a.get("status")=="concluido" and str(a.get("inicio","")).startswith(data_iso)]
    por_pag = {}; por_func = {}; total = 0.0; fmap = {f["id"]:f.get("nome","") for f in cl_func_get(uid)}
    for a in concl:
        v = float(a.get("valor") or 0); total += v
        k = a.get("pagamento") or "outro"; por_pag[k] = round(por_pag.get(k,0)+v, 2)
        fn = fmap.get(a.get("func_id"), "(clínica)"); por_func[fn] = round(por_func.get(fn,0)+v, 2)
    return {"ok":True, "data":data_iso, "total":round(total,2), "atendimentos":len(concl),
            "por_pagamento":por_pag, "por_funcionario":sorted([{"funcionario":k,"valor":v} for k,v in por_func.items()], key=lambda x:-x["valor"])}
def cl_export_agendamentos(uid):
    rows = []
    fmap = {f["id"]:f.get("nome","") for f in cl_func_get(uid)}
    for a in sorted(cl_ags_get(uid), key=lambda x:x.get("inicio",""), reverse=True):
        ini = str(a.get("inicio","")).split("T")
        rows.append({"data":ini[0] if ini else "", "hora":ini[1] if len(ini)>1 else "", "cliente":a.get("cliente",""),
                     "telefone":a.get("telefone",""), "servico":a.get("servico",""), "profissional":fmap.get(a.get("func_id"),""),
                     "status":a.get("status",""), "valor":a.get("valor",0), "pagamento":a.get("pagamento",""), "origem":a.get("origem","")})
    return rows
def cl_func_dashboard(uid, fid):
    mes = datetime.date.today().strftime("%Y-%m")
    meus = [a for a in cl_ags_get(uid) if a.get("func_id")==fid]
    fat = sum(float(a.get("valor") or 0) for a in meus if a.get("status")=="concluido" and str(a.get("inicio","")).startswith(mes))
    marc = sorted([a for a in meus if a.get("status")=="marcado"], key=lambda x:x.get("inicio",""))
    pct = (_func_by(uid, fid) or {}).get("comissao_pct", 0)
    return {"ok":True, "agendamentos":marc, "faturamento_mes":round(fat,2), "comissao_pct":pct, "comissao_mes":round(fat*pct/100.0, 2),
            "atendimentos_mes":sum(1 for a in meus if a.get("status")=="concluido" and str(a.get("inicio","")).startswith(mes))}
# ---- FAQ + atendente virtual (bot do WhatsApp), 100% ancorado nos dados da clinica ----
def cl_faq_get(uid):
    f = get_blob(uid, "cl_faq", None); return f if isinstance(f, list) else CL_FAQ_PADRAO
def cl_faq_set(uid, lista):
    lista = [{"pergunta":str(x.get("pergunta",""))[:200], "resposta":str(x.get("resposta",""))[:600]} for x in (lista or []) if x.get("pergunta")]
    set_blob(uid, "cl_faq", lista); return {"ok":True, "faq":lista}
def cl_bot_contexto(uid):
    p = cl_perfil_get(uid); sv = cl_servicos_get(uid); h = cl_horario_get(uid); faq = cl_faq_get(uid)
    dias = ", ".join(f"{CL_DIAS_NOME[d]}: {(h['dias'][d]['ini']+'-'+h['dias'][d]['fim']) if h['dias'][d].get('aberto') else 'fechado'}" for d in CL_DIAS)
    tabela = "; ".join(f"{s['nome']} R$ {s['preco']:.0f} ({s['duracao']}min)" for s in sv) or "tabela ainda nao cadastrada"
    faqs = " | ".join(f"P: {q['pergunta']} R: {q['resposta']}" for q in faq[:12])
    return ("Voce e a atendente virtual da " + (p.get('nome') or 'clinica de estetica') + ". "
            "Fale como uma recepcionista simpatica, objetiva, em portugues do Brasil, sem travessao. "
            f"Sobre o negocio: {p.get('sobre','')}. Endereco: {p.get('endereco','sob consulta')}. "
            f"Horarios de atendimento: {dias}. Intervalo entre encaixes: {h.get('intervalo',30)} min. "
            f"TABELA DE PRECOS (use exatamente estes valores, nao invente): {tabela}. "
            f"Perguntas frequentes: {faqs}. "
            "REGRAS: responda SO com base nessas informacoes. Se nao souber, ofereca falar com a equipe. "
            "Quando o cliente quiser agendar, pergunte o procedimento e o dia/horario de preferencia e diga que vai verificar a disponibilidade.")
def cl_bot_responder(uid, mensagem, historico=None):
    u = get_user(uid)
    if not u: return "Atendimento indisponivel no momento."
    return cloud_chat(cl_bot_contexto(uid), (historico or [])[-8:] + [{"role":"user","content":mensagem}], 350, u)
# ---- estado / onboarding ----
def cl_onboarded(uid): return bool(get_blob(uid, "cl_onboarded", False))
def cl_onboarding_salvar(uid, d):
    cl_perfil_set(uid, {"nome":d.get("nome"), "dona":d.get("dona"), "sobre":d.get("sobre"),
                        "telefone":d.get("telefone"), "endereco":d.get("endereco")})
    for s in (d.get("servicos") or []):
        if s.get("nome"): cl_servico_salvar(uid, s)
    set_blob(uid, "cl_onboarded", True); return {"ok":True}
# ---- PLANO da clinica (SaaS): gratis x Profissional, com 14 dias de trial ----
CL_TRIAL_DIAS = 14
CL_PRO_RECURSOS = {"atendente","pix","relatorios","promo","pacotes","estoque","lembrete","equipe"}
def cl_plano_raw(uid): return get_blob(uid, "cl_plano", {}) or {}
def cl_plano_efetivo(uid):
    p = cl_plano_raw(uid)
    if p.get("plano")=="pro" and (not p.get("pro_ate") or p["pro_ate"] > time.time()): return "pro"
    if p.get("trial_ate") and p["trial_ate"] > time.time(): return "trial"
    return "free"
def cl_trial_iniciar(uid):
    p = cl_plano_raw(uid)
    if not p.get("trial_ate") and p.get("plano") != "pro":
        p["trial_ate"] = time.time() + CL_TRIAL_DIAS*86400; set_blob(uid, "cl_plano", p)
def cl_plano_set_pro(uid, meses=1):
    p = cl_plano_raw(uid); p["plano"]="pro"; p["pro_ate"] = max(p.get("pro_ate",0), time.time()) + int(meses)*31*86400
    set_blob(uid, "cl_plano", p); return {"ok":True, "plano":"pro"}
def cl_plano_set_free(uid):
    p = cl_plano_raw(uid); p["plano"]="free"; p.pop("pro_ate", None); p["trial_ate"] = 0
    set_blob(uid, "cl_plano", p); return {"ok":True, "plano":"free"}
def cl_ativar_chave(uid, chave):
    """Ativa a Clinica+ Pro com uma chave de licenca (que o dono da plataforma gera e vende).
    A chave tem plano "clinica". Master (do dono) e ilimitada; chave de venda e uso unico."""
    chave = (chave or "").strip().upper().replace(" ", "")
    pl = lic_check(chave)
    if not pl: return {"ok":False, "erro":"Chave invalida. Confira se copiou ela inteira."}
    if pl != "clinica": return {"ok":False, "erro":"Essa chave nao e da Clinica+."}
    if not _is_master(chave):                       # chave de venda: uso unico (consome no banco)
        try:
            with _db() as c:
                row = c.execute("SELECT * FROM licencas WHERE chave=?", (chave,)).fetchone()
                if not row: return {"ok":False, "erro":"Chave nao encontrada. Gere pelo painel."}
                if row["usada"]: return {"ok":False, "erro":"Essa chave ja foi usada."}
                c.execute("UPDATE licencas SET usada=1, user_id=?, usada_em=? WHERE chave=?",
                          (uid, datetime.datetime.now().strftime("%d/%m/%Y %H:%M"), chave)); c.commit()
        except Exception as e:
            print("[cl_ativar_chave]", repr(e)); return {"ok":False, "erro":"erro ao validar a chave, tente de novo"}
    cl_plano_set_pro(uid, 12)
    return {"ok":True, "plano":"pro", "via":"chave"}
def admin_clinicas():
    """Pro dono da plataforma: lista todas as clinicas, plano, faturamento e MRR da Clinica+."""
    out = []; total_pro = 0
    with _db() as c: rows = c.execute("SELECT * FROM users ORDER BY id").fetchall()
    for x in rows:
        if not (cl_onboarded(x["id"]) or (cl_perfil_get(x["id"]) or {}).get("nome") or cl_servicos_get(x["id"])): continue
        ef = cl_plano_efetivo(x["id"])
        if ef == "pro": total_pro += 1
        try: fat = cl_dashboard(x["id"]).get("faturamento_mes", 0)
        except Exception: fat = 0
        out.append({"id":x["id"], "nome":(cl_perfil_get(x["id"]) or {}).get("nome","(sem nome)"), "dona":x["nome"], "email":x["email"] or "",
                    "plano":ef, "trial_dias":cl_plano_info(x["id"])["trial_dias"], "faturamento_mes":fat, "criado":x["criado"] or ""})
    return {"ok":True, "clinicas":sorted(out, key=lambda c:-c["faturamento_mes"]), "total":len(out), "pro":total_pro, "mrr":round(total_pro*cl_preco(),2)}
def cl_pode(uid, recurso): return (recurso not in CL_PRO_RECURSOS) or cl_plano_efetivo(uid) in ("pro","trial")
def cl_preco():
    try: return float(gcfg("clinica_preco","CLINICA_PRECO") or 79)
    except Exception: return 79.0
def cl_assinar(uid, base):
    """Cria a assinatura mensal da Clinica+ no Mercado Pago do DONO da plataforma (voce recebe)."""
    tok = gcfg("mp_token","MP_ACCESS_TOKEN")
    if not tok: return {"ok":False,"erro":"o pagamento da plataforma ainda nao foi configurado pelo Orion"}
    u = get_user(uid); email = (u["email"] if u else "") or "cliente@clinica.com"
    body = {"reason":"Clinica+ Profissional", "external_reference":f"cl:{uid}", "payer_email":email,
            "back_url":base.rstrip("/")+"/clinica?assinado=1", "status":"pending",
            "auto_recurring":{"frequency":1, "frequency_type":"months", "transaction_amount":cl_preco(), "currency_id":"BRL"}}
    try:
        req = urllib.request.Request("https://api.mercadopago.com/preapproval", data=json.dumps(body).encode(),
            headers={"Authorization":f"Bearer {tok}","Content-Type":"application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        idx = get_blob(1,"cl_preapproval",{}) or {}; idx[str(r.get("id"))] = uid; set_blob(1,"cl_preapproval",idx)
        return {"ok":True, "url":(r.get("init_point") or r.get("sandbox_init_point"))}
    except urllib.error.HTTPError as e:
        print("[assinar]", e.code, e.read()[:200]); return {"ok":False,"erro":"nao consegui abrir a assinatura agora"}
    except Exception as e:
        print("[assinar]", e); return {"ok":False,"erro":"erro ao criar a assinatura"}
def cl_mp_webhook(d):
    try:
        tok = gcfg("mp_token","MP_ACCESS_TOKEN")
        tipo = str(d.get("type") or d.get("topic") or ""); pid = str((d.get("data") or {}).get("id") or d.get("id") or "")
        if "preapproval" in tipo and pid and tok:
            req = urllib.request.Request(f"https://api.mercadopago.com/preapproval/{pid}", headers={"Authorization":f"Bearer {tok}"})
            r = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
            if r.get("status") == "authorized":
                ext = r.get("external_reference",""); uid = int(ext[3:]) if ext.startswith("cl:") else (get_blob(1,"cl_preapproval",{}) or {}).get(pid)
                if uid: cl_plano_set_pro(int(uid), 1); print("[mp] Clinica+ PRO ativado pra", uid)
    except Exception as e: print("[mp webhook]", e)
def cl_plano_info(uid):
    ef = cl_plano_efetivo(uid); p = cl_plano_raw(uid)
    dias = max(0, int((p.get("trial_ate",0)-time.time())/86400)) if ef=="trial" else 0
    return {"plano":ef, "trial_dias":dias}
def cl_estado(uid):
    u = get_user(uid); ex = conta_extra_get(uid)
    cl_trial_iniciar(uid)
    pl = cl_plano_info(uid)
    return {"ok":True, "onboarded":cl_onboarded(uid), "perfil":cl_perfil_get(uid),
            "plano":pl["plano"], "trial_dias":pl["trial_dias"],
            "nome_dono":(u["nome"] if u else ""), "email":(u["email"] if u else ""),
            "tem_funcionarios":len(cl_func_get(uid))>0, "cerebro_ok":bool((get_blob(uid,"brain",{}) or {}).get("key") or llm_key()),
            "conta_completa":conta_completa(uid), "telefone":ex.get("telefone",""), "doc":bool(ex.get("documento")), "tel_validado":bool(ex.get("tel_validado")),
            "link_agendamento":f"/agendar?c={uid}"}

# ---- CONTA da dona: criar/salvar, CPF/CNPJ, telefone+email, validacao por codigo ----
def _digs(s): return re.sub(r"\D", "", str(s or ""))
def _doc_ok(doc): return len(_digs(doc)) in (11, 14)   # CPF=11, CNPJ=14
def conta_extra_get(uid): return get_blob(uid, "conta_extra", {}) or {}
def conta_extra_set(uid, patch):
    e = conta_extra_get(uid)
    for k, v in (patch or {}).items():
        if v is not None: e[k] = v
    set_blob(uid, "conta_extra", e); return e
def _tel_index(): return get_blob(1, "tel_index", {}) or {}
def _tel_index_set(tel, uid):
    tel = _digs(tel)
    if tel: idx = _tel_index(); idx[tel] = uid; set_blob(1, "tel_index", idx)
def conta_completa(uid):
    e = conta_extra_get(uid); u = get_user(uid)
    return bool(u and u["email"] and e.get("telefone") and e.get("documento") and e.get("tel_validado"))
def clinica_criar_conta(d):
    nome = (d.get("nome") or "").strip()
    if len(nome) < 2: return {"ok":False, "erro":"diga seu nome"}
    if len(d.get("senha") or "") < 6: return {"ok":False, "erro":"senha de no minimo 6 caracteres"}
    if not d.get("aceitou_termos"): return {"ok":False, "erro":"aceite os termos pra continuar"}
    if not _doc_ok(d.get("documento")): return {"ok":False, "erro":"informe um CPF ou CNPJ valido"}
    email = (d.get("email") or "").strip().lower() or None
    tel = _digs(d.get("telefone"))
    if not email and not tel: return {"ok":False, "erro":"informe um e-mail ou telefone pra acessar depois"}
    if email and ("@" not in email or "." not in email.split("@")[-1]): return {"ok":False, "erro":"e-mail invalido"}
    if tel and len(tel) < 10: return {"ok":False, "erro":"telefone invalido (com DDD)"}
    # checa duplicado ANTES de inserir (funciona igual em SQLite e Postgres; nao depende do tipo de erro do driver)
    try:
        with _db() as c:
            if email and c.execute("SELECT 1 FROM users WHERE lower(email)=lower(?)", (email,)).fetchone():
                return {"ok":False, "erro":"Esse e-mail ja tem conta. Use a aba Entrar pra acessar."}
        if tel and _tel_index().get(tel):
            return {"ok":False, "erro":"Esse telefone ja tem conta. Use a aba Entrar pra acessar."}
        with _db() as c:
            n = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            cur = c.execute("INSERT INTO users(nome,email,senha_hash,nome_real,tratamento,foto,plano,criador,origem,criado) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (nome.title(), email, _hash(d.get("senha")), nome, "", "", "free", 1 if n==0 else 0, "clinica", datetime.datetime.now().strftime("%d/%m/%Y %H:%M")))
            c.commit(); uid = cur.lastrowid
    except INTEGRITY_ERRORS:
        return {"ok":False, "erro":"Esse e-mail ja tem conta. Use a aba Entrar pra acessar."}
    except Exception as e:
        print("[clinica_criar_conta]", repr(e))
        # rede/corrida: se mesmo assim foi violacao de unicidade, avisa amigavelmente
        if "unique" in str(e).lower() or "duplicate" in str(e).lower() or "23505" in str(e):
            return {"ok":False, "erro":"Esse e-mail ja tem conta. Use a aba Entrar pra acessar."}
        return {"ok":False, "erro":"nao consegui criar a conta agora, tente de novo em instantes"}
    conta_extra_set(uid, {"telefone":tel, "documento":_digs(d.get("documento")), "tel_validado":False})
    if tel: _tel_index_set(tel, uid)
    return {"ok":True, "uid":uid}
def clinica_login_dono(ident, senha):
    """Login da dona por e-mail, telefone ou nome. Retorna uid, None (nao achou) ou False (senha errada)."""
    ident = (ident or "").strip(); r = None
    with _db() as c:
        if "@" in ident:
            r = c.execute("SELECT * FROM users WHERE lower(email)=lower(?)", (ident,)).fetchone()
        else:
            dig = _digs(ident)
            uid = _tel_index().get(dig) if len(dig) >= 8 else None
            r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else \
                c.execute("SELECT * FROM users WHERE lower(nome)=lower(?)", (ident,)).fetchone()
    if not r: return None
    if r["senha_hash"] and not _senha_ok(senha, r["senha_hash"]): return False
    return r["id"]
def _wa_creds(uid=None):
    """Credenciais do WhatsApp: da clinica (se ela conectou o proprio numero) ou as globais do admin."""
    if uid:
        w = get_blob(uid, "cl_wa", {}) or {}
        if w.get("token") and w.get("phone_id"): return w["token"], w["phone_id"]
    return gcfg("whatsapp_token","WHATSAPP_TOKEN"), gcfg("whatsapp_phone_id","WHATSAPP_PHONE_ID")
def _wa_enviar(tel, texto, uid=None):
    tok, pid = _wa_creds(uid)
    if not (tok and pid): return False
    try:
        body = {"messaging_product":"whatsapp","to":_digs(tel),"type":"text","text":{"body":texto}}
        req = urllib.request.Request(f"https://graph.facebook.com/v20.0/{pid}/messages", data=json.dumps(body).encode(),
            headers={"Authorization":f"Bearer {tok}","Content-Type":"application/json"})
        urllib.request.urlopen(req, timeout=12); return True
    except Exception as e: print("[wa]", e); return False
def tel_codigo_enviar(uid, telefone):
    tel = _digs(telefone)
    if len(tel) < 10: return {"ok":False, "erro":"telefone invalido (com DDD)"}
    code = f"{int.from_bytes(os.urandom(3),'big')%1000000:06d}"
    set_blob(uid, "tel_code", {"code":code, "tel":tel, "em":time.time()})
    conta_extra_set(uid, {"telefone":tel}); _tel_index_set(tel, uid)
    enviado = _wa_enviar(tel, f"Seu codigo de verificacao: {code}")
    return {"ok":True, "enviado":bool(enviado), "dev_code":(None if enviado else code)}
def tel_codigo_validar(uid, code):
    c = get_blob(uid, "tel_code", {}) or {}
    if c.get("code") and str(code).strip()==c["code"] and time.time()-c.get("em",0) < 900:
        conta_extra_set(uid, {"tel_validado":True}); set_blob(uid, "tel_code", {}); return {"ok":True}
    return {"ok":False, "erro":"codigo invalido ou expirado"}

# ---- CLIENTES + FIDELIDADE (perfis, beneficios escolhidos pela dona) ----
def cl_clientes_raw(uid): return get_blob(uid, "cl_clientes", {}) or {}
def cl_cliente_registrar(uid, nome, tel, cadastrado=False, email=""):
    tk = _digs(tel)
    if not tk: return None
    cs = cl_clientes_raw(uid); c = cs.get(tk, {"tel":tk, "procedimentos":0, "cadastrado":False, "criado":time.time()})
    if nome: c["nome"] = nome[:80]
    if email: c["email"] = email[:120]
    if cadastrado: c["cadastrado"] = True
    cs[tk] = c; set_blob(uid, "cl_clientes", cs); return c
def cl_cliente_proc_inc(uid, tel):
    tk = _digs(tel)
    if not tk: return
    cs = cl_clientes_raw(uid); c = cs.get(tk)
    if not c: c = {"tel":tk, "procedimentos":0, "cadastrado":False, "criado":time.time()}
    c["procedimentos"] = int(c.get("procedimentos",0)) + 1; cs[tk] = c; set_blob(uid, "cl_clientes", cs)
def cl_fidelidade_get(uid):
    f = get_blob(uid, "cl_fidelidade", None)
    return f if isinstance(f, dict) else {"ativo":False, "meta":10, "premio":"um mimo especial"}
def cl_fidelidade_set(uid, d):
    f = {"ativo":bool(d.get("ativo")), "meta":max(1, int(d.get("meta") or 10)), "premio":str(d.get("premio") or "")[:140]}
    set_blob(uid, "cl_fidelidade", f); return {"ok":True, "fidelidade":f}
def cl_clientes_listar(uid):
    f = cl_fidelidade_get(uid); meta = f.get("meta", 10) or 10; out = []
    for c in cl_clientes_raw(uid).values():
        p = int(c.get("procedimentos", 0))
        out.append({"nome":c.get("nome",""), "tel":c.get("tel",""), "email":c.get("email",""),
                    "procedimentos":p, "cadastrado":bool(c.get("cadastrado")),
                    "falta_premio":(meta - (p % meta) if (f.get("ativo") and p>0 and p % meta != 0) else 0),
                    "ganhou":bool(f.get("ativo") and p>0 and p % meta == 0)})
    return {"ok":True, "clientes":sorted(out, key=lambda x:-x["procedimentos"]), "fidelidade":f}
# ---- info publica da clinica (pra landing de agendamento do cliente) ----
def cl_publico(uid):
    p = cl_perfil_get(uid)
    if not p.get("nome") and not cl_servicos_get(uid): return {"ok":False, "erro":"clinica nao encontrada"}
    return {"ok":True, "nome":p.get("nome","Clinica"), "sobre":p.get("sobre",""), "endereco":p.get("endereco",""),
            "telefone":p.get("telefone",""), "boas_vindas":p.get("boas_vindas",""),
            "servicos":[{"id":s["id"], "nome":s["nome"], "preco":s["preco"], "duracao":s["duracao"]} for s in cl_servicos_get(uid)],
            "profissionais":[{"id":f["id"], "nome":f.get("nome",""), "servicos":f.get("servicos",[])} for f in cl_func_get(uid) if f.get("ativo", True) is not False],
            "fidelidade":cl_fidelidade_get(uid), "pix":cl_pix_get(uid)}

# ---- bloqueios/folgas avulsas (alem dos dias fechados fixos) ----
def cl_bloqueios_get(uid): return get_blob(uid, "cl_bloqueios", []) or []
def cl_bloqueio_add(uid, d):
    if not d.get("data"): return {"ok":False, "erro":"informe a data"}
    b = {"id":int(time.time()*1000), "data":d.get("data"), "dia_todo":bool(d.get("dia_todo")),
         "ini":(d.get("ini") or "00:00"), "fim":(d.get("fim") or "23:59"), "motivo":str(d.get("motivo") or "")[:80]}
    if b["dia_todo"]: b["ini"]="00:00"; b["fim"]="23:59"
    bs = cl_bloqueios_get(uid); bs.append(b); set_blob(uid, "cl_bloqueios", bs); return {"ok":True, "bloqueios":bs}
def cl_bloqueio_remover(uid, bid):
    try: bid=int(bid)
    except Exception: pass
    set_blob(uid, "cl_bloqueios", [x for x in cl_bloqueios_get(uid) if x.get("id")!=bid]); return {"ok":True}
# ---- remarcar (mover de horario, conferindo disponibilidade) ----
def cl_remarcar(uid, aid, data, hora):
    try: aid=int(aid)
    except Exception: pass
    ags = cl_ags_get(uid); a = next((x for x in ags if x.get("id")==aid), None)
    if not a: return {"ok":False, "erro":"agendamento nao encontrado"}
    disp = cl_disponibilidade(uid, data, a.get("servico_id"), a.get("func_id"))
    if hora not in disp.get("slots", []): return {"ok":False, "erro":"esse horario nao esta livre", "slots":disp.get("slots",[])}
    a["inicio"] = f"{data}T{hora}"; a["lembrete_enviado"] = False; set_blob(uid, "cl_agendamentos", ags)
    try:
        if a.get("telefone"): _wa_enviar(a["telefone"], f"{cl_perfil_get(uid).get('nome','Sua clinica')}: sua consulta foi remarcada para {data} as {hora}.", uid)
    except Exception: pass
    try: notificar(uid, "Consulta remarcada", f"{a.get('cliente','Cliente')} - {a.get('servico')} agora em {data} {hora}", "/clinica")
    except Exception: pass
    return {"ok":True, "agendamento":a}
# ---- ficha do cliente (historico de procedimentos + observacoes/anamnese) ----
def cl_cliente_ficha(uid, tel):
    tk = _digs(tel); c = cl_clientes_raw(uid).get(tk) or {"tel":tk}
    hist = sorted([a for a in cl_ags_get(uid) if _digs(a.get("telefone"))==tk], key=lambda x:x.get("inicio",""), reverse=True)
    f = cl_fidelidade_get(uid); meta = f.get("meta",10) or 10; p = int(c.get("procedimentos",0))
    return {"ok":True, "cliente":{"nome":c.get("nome",""), "tel":tk, "email":c.get("email",""), "obs":c.get("obs",""),
            "procedimentos":p, "cadastrado":bool(c.get("cadastrado")), "anamnese":c.get("anamnese",{}),
            "pacotes":[pk for pk in c.get("pacotes",[]) if pk.get("restantes",0)>0],
            "fotos":[{"id":x["id"], "data":x.get("data",""), "nota":x.get("nota","")} for x in c.get("fotos",[])],
            "ganhou":bool(f.get("ativo") and p>0 and p%meta==0), "falta_premio":(meta-(p%meta) if (f.get("ativo") and p>0 and p%meta!=0) else 0)},
            "historico":[{"servico":a.get("servico"), "data":a.get("inicio"), "status":a.get("status"), "valor":a.get("valor"), "pagamento":a.get("pagamento","")} for a in hist][:60]}
def cl_cliente_obs_set(uid, tel, obs):
    tk = _digs(tel); cs = cl_clientes_raw(uid); c = cs.get(tk) or {"tel":tk, "procedimentos":0, "cadastrado":False, "criado":time.time()}
    c["obs"] = str(obs or "")[:3000]; cs[tk] = c; set_blob(uid, "cl_clientes", cs); return {"ok":True}
# ---- ANAMNESE (ficha de saude estruturada) + fotos antes/depois ----
def cl_anamnese_set(uid, tel, d):
    tk = _digs(tel); cs = cl_clientes_raw(uid); c = cs.get(tk) or {"tel":tk, "procedimentos":0, "cadastrado":False, "criado":time.time()}
    a = c.get("anamnese", {})
    for k in ("alergias","medicacoes","condicoes","gestante","fumante","obs_saude"):
        if k in d: a[k] = str(d.get(k) or "")[:600]
    c["anamnese"] = a; cs[tk] = c; set_blob(uid, "cl_clientes", cs); return {"ok":True, "anamnese":a}
def cl_foto_add(uid, tel, data_b64, nota=""):
    if not data_b64 or len(data_b64) > 1_600_000: return {"ok":False, "erro":"imagem muito grande (max ~1MB)"}
    tk = _digs(tel); cs = cl_clientes_raw(uid); c = cs.get(tk) or {"tel":tk, "procedimentos":0, "cadastrado":False, "criado":time.time()}
    fotos = c.get("fotos", []); fotos.append({"id":int(time.time()*1000), "data":data_b64, "nota":str(nota or "")[:80], "em":time.time()})
    c["fotos"] = fotos[-30:]; cs[tk] = c; set_blob(uid, "cl_clientes", cs); return {"ok":True}
def cl_foto_apagar(uid, tel, fid):
    tk = _digs(tel); cs = cl_clientes_raw(uid); c = cs.get(tk)
    if c: c["fotos"] = [f for f in c.get("fotos",[]) if f.get("id")!=int(fid)]; cs[tk]=c; set_blob(uid, "cl_clientes", cs)
    return {"ok":True}
# ---- PACOTES DE SESSAO (vende N sessoes; debita a cada atendimento) ----
def cl_pacotes_get(uid): return get_blob(uid, "cl_pacotes", []) or []
def cl_pacote_salvar(uid, d):
    ps = cl_pacotes_get(uid); pid = d.get("id") or int(time.time()*1000)
    try: svid = int(d["servico_id"]) if d.get("servico_id") not in (None,"","null") else None
    except Exception: svid = None
    p = {"id":pid, "nome":(d.get("nome") or "").strip()[:80], "servico_id":svid, "sessoes":max(1,int(d.get("sessoes") or 1)), "preco":float(d.get("preco") or 0)}
    if not p["nome"]: return {"ok":False,"erro":"de um nome ao pacote"}
    ps = [x for x in ps if x.get("id")!=pid]; ps.append(p); set_blob(uid, "cl_pacotes", ps); return {"ok":True, "pacotes":ps}
def cl_pacote_apagar(uid, pid):
    set_blob(uid, "cl_pacotes", [x for x in cl_pacotes_get(uid) if x.get("id")!=int(pid)]); return {"ok":True}
def cl_pacote_vender(uid, tel, pacote_id, pagamento=""):
    pac = next((p for p in cl_pacotes_get(uid) if p.get("id")==int(pacote_id)), None)
    if not pac: return {"ok":False,"erro":"pacote nao encontrado"}
    tk = _digs(tel)
    if not tk: return {"ok":False,"erro":"informe o telefone do cliente"}
    cs = cl_clientes_raw(uid); c = cs.get(tk) or {"tel":tk, "procedimentos":0, "cadastrado":False, "criado":time.time()}
    c.setdefault("pacotes", []).append({"id":int(time.time()*1000), "nome":pac["nome"], "servico_id":pac["servico_id"],
                                        "restantes":pac["sessoes"], "total":pac["sessoes"], "comprado":time.time()})
    cs[tk] = c; set_blob(uid, "cl_clientes", cs)
    # registra a venda do pacote como receita (entra no faturamento na hora da venda)
    ags = cl_ags_get(uid)
    ags.append({"id":int(time.time()*1000)+1, "cliente":c.get("nome",""), "telefone":tk, "servico":"Pacote: "+pac["nome"],
                "func_id":None, "inicio":datetime.datetime.now().strftime("%Y-%m-%dT%H:%M"), "duracao":0, "valor":float(pac["preco"]),
                "status":"concluido", "pagamento":(pagamento or "pacote"), "origem":"pacote", "criado":time.time()})
    set_blob(uid, "cl_agendamentos", ags); return {"ok":True}
def _cl_pacote_debitar(uid, tel, servico_id):
    """Debita 1 sessao de um pacote do cliente pra esse servico. Retorna True se debitou."""
    tk = _digs(tel); cs = cl_clientes_raw(uid); c = cs.get(tk)
    if not c or not c.get("pacotes"): return False
    for pk in c["pacotes"]:
        if pk.get("restantes",0) > 0 and (pk.get("servico_id") in (None,) or str(pk.get("servico_id"))==str(servico_id)):
            pk["restantes"] -= 1; cs[tk] = c; set_blob(uid, "cl_clientes", cs); return True
    return False
# ---- ESTOQUE de produtos/insumos (baixa por procedimento + alerta) ----
def cl_estoque_get(uid): return get_blob(uid, "cl_estoque", []) or []
def cl_estoque_salvar(uid, d):
    es = cl_estoque_get(uid); pid = d.get("id") or int(time.time()*1000)
    cur = next((x for x in es if x.get("id")==pid), {})
    p = {"id":pid, "nome":(d.get("nome") or "").strip()[:60], "qtd":float(d.get("qtd") if d.get("qtd") is not None else cur.get("qtd",0)),
         "minimo":float(d.get("minimo") if d.get("minimo") is not None else cur.get("minimo",0)), "unidade":(d.get("unidade") or cur.get("unidade","un"))[:10]}
    if not p["nome"]: return {"ok":False,"erro":"nome do produto"}
    es = [x for x in es if x.get("id")!=pid]; es.append(p); es.sort(key=lambda x:x["nome"].lower())
    set_blob(uid, "cl_estoque", es); return {"ok":True, "estoque":es, "alertas":cl_estoque_alertas(uid)}
def cl_estoque_apagar(uid, pid):
    set_blob(uid, "cl_estoque", [x for x in cl_estoque_get(uid) if x.get("id")!=int(pid)]); return {"ok":True}
def cl_estoque_mexer(uid, pid, delta):
    es = cl_estoque_get(uid)
    for p in es:
        if p.get("id")==int(pid): p["qtd"] = round(float(p.get("qtd",0)) + float(delta), 2)
    set_blob(uid, "cl_estoque", es); return {"ok":True, "estoque":es, "alertas":cl_estoque_alertas(uid)}
def cl_estoque_alertas(uid):
    return [{"nome":p["nome"], "qtd":p.get("qtd",0), "unidade":p.get("unidade","un")} for p in cl_estoque_get(uid) if float(p.get("qtd",0)) <= float(p.get("minimo",0))]
def _cl_estoque_baixa_servico(uid, servico_id):
    """Baixa automatica dos insumos vinculados ao servico (se houver), ao concluir."""
    sv = cl_servico_by(uid, servico_id)
    if not sv or not sv.get("insumos"): return
    es = cl_estoque_get(uid); mp = {p["id"]:p for p in es}
    for it in sv["insumos"]:
        p = mp.get(it.get("produto_id"))
        if p: p["qtd"] = round(float(p.get("qtd",0)) - float(it.get("qtd",1)), 2)
    set_blob(uid, "cl_estoque", es)
# ---- config de lembrete/confirmacao ----
def cl_lembrete_get(uid):
    s = get_blob(uid, "cl_lembrete", None)
    return s if isinstance(s, dict) else {"ativo":True, "confirmar":True, "horas":3}
def cl_lembrete_set(uid, d):
    s = {"ativo":bool(d.get("ativo")), "confirmar":bool(d.get("confirmar")), "horas":max(1, min(72, int(d.get("horas") or 3)))}
    set_blob(uid, "cl_lembrete", s); return {"ok":True, "lembrete":s}
def loop_lembretes():
    """Manda lembrete pro cliente X horas antes da consulta (1x por consulta). Precisa do WhatsApp conectado."""
    while True:
        time.sleep(600)
        try:
            with _db() as c: ids = [r["id"] for r in c.execute("SELECT id FROM users").fetchall()]
            agora = datetime.datetime.now()
            for uid in ids:
                ags = cl_ags_get(uid)
                if not ags: continue
                st = cl_lembrete_get(uid)
                if not st.get("ativo"): continue
                horas = st.get("horas", 3); mudou = False
                for a in ags:
                    if a.get("status")!="marcado" or a.get("lembrete_enviado") or not a.get("telefone"): continue
                    try: ini = datetime.datetime.fromisoformat(a["inicio"])
                    except Exception: continue
                    delta = (ini - agora).total_seconds()/3600.0
                    if 0 < delta <= horas:
                        _wa_enviar(a["telefone"], f"{cl_perfil_get(uid).get('nome','Lembrete')}: voce tem {a['servico']} hoje as {a['inicio'].split('T')[1]}. Te esperamos!", uid)
                        a["lembrete_enviado"] = True; mudou = True
                if mudou: set_blob(uid, "cl_agendamentos", ags)
        except Exception as e: print("[lembrete]", e)
# ---- ATENDENTE QUE AGENDA SOZINHA (tool-calling com o cerebro do dono) ----
def cl_bot_agente(uid, mensagem, historico=None, cliente_tel="", cliente_nome=""):
    u = get_user(uid)
    key, base, model, fonte = _llm_resolve(u)
    if not key: return cl_bot_responder(uid, mensagem, historico)   # sem cerebro: cai pro modo simples
    servs = cl_servicos_get(uid)
    def _findserv(nome):
        nome = (nome or "").lower().strip()
        return (next((s for s in servs if s["nome"].lower()==nome), None)
                or next((s for s in servs if nome and (nome in s["nome"].lower() or s["nome"].lower() in nome)), None))
    hoje = datetime.date.today()
    # status do cliente (sessoes de pacote + fidelidade) pra atendente avisar
    stat = ""
    if cliente_tel:
        c = cl_clientes_raw(uid).get(_digs(cliente_tel)) or {}
        pks = [p for p in c.get("pacotes",[]) if p.get("restantes",0) > 0]
        if pks: stat += " O cliente TEM pacote(s) ativo(s): " + "; ".join(f"{p['nome']} ({p['restantes']} sessoes restantes)" for p in pks) + "."
        fdl = cl_fidelidade_get(uid)
        if fdl.get("ativo"):
            pr = int(c.get("procedimentos",0)); meta = fdl.get("meta",10) or 10
            if pr>0 and pr%meta==0: stat += f" O cliente JA atingiu a fidelidade: ofereça o premio ({fdl.get('premio','')})."
            elif pr>0: stat += f" Fidelidade: faltam {meta-(pr%meta)} procedimento(s) pro premio ({fdl.get('premio','')})."
    pacs = cl_pacotes_get(uid)
    sysp = (cl_bot_contexto(uid) + f" Hoje e {hoje.isoformat()} ({CL_DIAS_NOME[CL_DIAS[hoje.weekday()]]})." + stat +
            (" Pacotes a venda: " + "; ".join(f"{p['nome']} ({p['sessoes']} sessoes por R$ {p['preco']:.0f})" for p in pacs) + "." if pacs else "") +
            " Voce PODE agendar, cancelar e vender pacote usando as ferramentas. Antes de marcar ou vender, confirme com o cliente. "
            "Use consultar_disponibilidade pra oferecer horarios REAIS (nunca invente). Se o cliente tem pacote ativo do servico, lembre que a sessao ja esta paga. Datas no formato AAAA-MM-DD.")
    tools = [
      {"type":"function","function":{"name":"consultar_disponibilidade","description":"Lista os horarios livres de um servico numa data",
        "parameters":{"type":"object","properties":{"servico":{"type":"string"},"data":{"type":"string","description":"AAAA-MM-DD"}},"required":["servico","data"]}}},
      {"type":"function","function":{"name":"marcar_consulta","description":"Marca a consulta do cliente apos ele confirmar",
        "parameters":{"type":"object","properties":{"servico":{"type":"string"},"data":{"type":"string"},"hora":{"type":"string","description":"HH:MM"},"nome":{"type":"string"}},"required":["servico","data","hora"]}}},
      {"type":"function","function":{"name":"cancelar_consulta","description":"Cancela a proxima consulta marcada deste cliente","parameters":{"type":"object","properties":{},"required":[]}}},
      {"type":"function","function":{"name":"vender_pacote","description":"Vende um pacote de sessoes pro cliente (apos ele confirmar)","parameters":{"type":"object","properties":{"pacote":{"type":"string"}},"required":["pacote"]}}},
    ]
    msgs = [{"role":"system","content":sysp}] + (historico or [])[-8:] + [{"role":"user","content":mensagem}]
    try:
        for _ in range(5):
            r = _llm_post(base, key, {"model":model,"max_tokens":500,"temperature":0.3,"messages":msgs,"tools":tools,"tool_choice":"auto"})
            if u and fonte=="managed": tokens_registrar(u, r.get("usage") or {}, model)
            m = r["choices"][0]["message"]; tcs = m.get("tool_calls") or []
            if not tcs: return (m.get("content") or "").strip()
            msgs.append({"role":"assistant","content":m.get("content") or "","tool_calls":tcs})
            for tc in tcs:
                try: args = json.loads(tc["function"].get("arguments") or "{}")
                except Exception: args = {}
                fn = tc["function"]["name"]; res = {}
                if fn=="consultar_disponibilidade":
                    sv = _findserv(args.get("servico"))
                    res = cl_disponibilidade(uid, args.get("data"), sv["id"]) if sv else {"ok":False,"erro":"servico nao encontrado","servicos":[s["nome"] for s in servs]}
                elif fn=="marcar_consulta":
                    sv = _findserv(args.get("servico"))
                    if not sv: res = {"ok":False,"erro":"servico nao encontrado","servicos":[s["nome"] for s in servs]}
                    elif not cliente_tel: res = {"ok":False,"erro":"preciso do telefone do cliente pra marcar"}
                    else: res = cl_agendar(uid, {"servico_id":sv["id"],"data":args.get("data"),"hora":args.get("hora"),
                                                 "cliente":(args.get("nome") or cliente_nome),"telefone":cliente_tel}, origem="atendente")
                elif fn=="cancelar_consulta":
                    prox = sorted([a for a in cl_ags_get(uid) if a.get("status")=="marcado" and _digs(a.get("telefone"))==_digs(cliente_tel)], key=lambda x:x.get("inicio",""))
                    res = cl_desmarcar(uid, prox[0]["id"], "cancelado pelo cliente") if prox else {"ok":False,"erro":"nenhuma consulta encontrada nesse numero"}
                elif fn=="vender_pacote":
                    nm = (args.get("pacote") or "").lower()
                    pac = next((p for p in pacs if p["nome"].lower()==nm), None) or next((p for p in pacs if nm and nm in p["nome"].lower()), None)
                    res = (cl_pacote_vender(uid, cliente_tel, pac["id"]) if pac else {"ok":False,"erro":"pacote nao encontrado","pacotes":[p["nome"] for p in pacs]}) if cliente_tel else {"ok":False,"erro":"preciso do telefone"}
                msgs.append({"role":"tool","tool_call_id":tc.get("id"),"name":fn,"content":json.dumps(res, ensure_ascii=False)[:1500]})
        return "Pode confirmar os detalhes, por favor?"
    except Exception as e:
        print("[cl_bot_agente]", e); return cl_bot_responder(uid, mensagem, historico)
# ---- WhatsApp da clinica: conectar o numero proprio + webhook (atendente atende no numero real) ----
def cl_wa_get(uid):
    w = get_blob(uid, "cl_wa", {}) or {}
    return {"phone_id":w.get("phone_id",""), "tem_token":bool(w.get("token"))}
def cl_wa_set(uid, d):
    w = get_blob(uid, "cl_wa", {}) or {}
    if (d.get("token") or "").strip(): w["token"] = d["token"].strip()
    if "phone_id" in d: w["phone_id"] = _digs(d.get("phone_id"))
    if d.get("limpar"): w = {}
    set_blob(uid, "cl_wa", w)
    idx = get_blob(1, "wa_index", {}) or {}
    idx = {k:v for k,v in idx.items() if v != uid}   # remove mapeamentos antigos dessa clinica
    if w.get("phone_id"): idx[w["phone_id"]] = uid
    set_blob(1, "wa_index", idx)
    return {"ok":True, "wa":cl_wa_get(uid)}
def _wa_uid_por_phone(pid): return (get_blob(1, "wa_index", {}) or {}).get(_digs(pid))
def cl_promo_enviar(uid, mensagem, alvo="todos"):
    """Dispara uma promocao/mensagem pros clientes da clinica (todos, cadastrados, fieis ou inativos)."""
    msg = (mensagem or "").strip()
    if len(msg) < 3: return {"ok":False, "erro":"escreva a mensagem"}
    cs = cl_clientes_raw(uid); fmeta = cl_fidelidade_get(uid).get("meta", 10) or 10
    ult = {}
    for a in cl_ags_get(uid):
        t = _digs(a.get("telefone"));
        if t and a.get("inicio","") > ult.get(t, ""): ult[t] = a.get("inicio","")
    alvos = []
    for tel, c in cs.items():
        if alvo == "cadastrados" and not c.get("cadastrado"): continue
        if alvo == "fieis" and int(c.get("procedimentos", 0)) < 3: continue
        if alvo == "inativos":
            u = ult.get(tel, "")
            try:
                if u and (datetime.date.today() - datetime.date.fromisoformat(u.split("T")[0])).days < 45: continue
            except Exception: pass
        alvos.append(tel)
    if not _wa_creds(uid)[0]: return {"ok":False, "erro":"conecte o WhatsApp da clinica primeiro"}
    n = sum(1 for tel in alvos if _wa_enviar(tel, msg, uid))
    return {"ok":True, "enviados":n, "alvos":len(alvos)}
def _func_by(uid, fid):
    try: fid = int(fid)
    except Exception: return None
    return next((f for f in cl_func_get(uid) if f.get("id")==fid), None)
# ---- PIX (Mercado Pago): sinal/pagamento online no agendamento. Cartao/dinheiro = presencial. ----
def _ascii(s, n):
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii","ignore").decode().upper()
    return re.sub(r"[^A-Z0-9 ]", "", s)[:n].strip() or "BRASIL"
def _pix_tlv(i, v): return f"{i}{len(v):02d}{v}"
def _pix_crc16(s):
    crc = 0xFFFF
    for ch in s.encode():
        crc ^= ch << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1); crc &= 0xFFFF
    return f"{crc:04X}"
def pix_brcode(chave, nome, cidade, valor=None, txid="***"):
    """PIX 'copia e cola' ESTATICO (padrao BR Code do Banco Central). Funciona com QUALQUER banco, sem token/API."""
    chave = (chave or "").strip()
    if not chave: return ""
    mai = _pix_tlv("00","br.gov.bcb.pix") + _pix_tlv("01", chave)
    p = _pix_tlv("00","01") + _pix_tlv("26", mai) + _pix_tlv("52","0000") + _pix_tlv("53","986")
    if valor: p += _pix_tlv("54", f"{float(valor):.2f}")
    p += _pix_tlv("58","BR") + _pix_tlv("59", _ascii(nome,25)) + _pix_tlv("60", _ascii(cidade,15)) + _pix_tlv("62", _pix_tlv("05", txid[:25] or "***"))
    p += "6304"; return p + _pix_crc16(p)
def cl_pix_get(uid):
    p = get_blob(uid, "cl_pix", None) or {}
    ch = p.get("chave","")
    return {"ativo":bool(p.get("ativo")), "modo":("total" if p.get("modo")=="total" else "sinal"),
            "sinal_pct":int(p.get("sinal_pct", 30) or 30), "tem_chave":bool(ch),
            "chave_masc":(ch[:3]+"***"+ch[-3:] if len(ch) > 7 else ("***" if ch else "")),
            "tipo_chave":p.get("tipo_chave",""), "recebedor":p.get("recebedor",""), "cidade":p.get("cidade",""), "maquininha":p.get("maquininha","")}
def cl_pix_set(uid, d):
    p = get_blob(uid, "cl_pix", {}) or {}
    p["ativo"] = bool(d.get("ativo")); p["modo"] = ("total" if d.get("modo")=="total" else "sinal")
    p["sinal_pct"] = max(5, min(100, int(d.get("sinal_pct") or 30)))
    for k in ("tipo_chave","recebedor","cidade","maquininha"):
        if k in d: p[k] = str(d.get(k) or "")[:60]
    if (d.get("chave") or "").strip(): p["chave"] = d["chave"].strip()
    if d.get("limpar_chave"): p.pop("chave", None)
    set_blob(uid, "cl_pix", p); return {"ok":True, "pix":cl_pix_get(uid)}
def cl_pix_cobrar(uid, valor, descricao, email=""):
    """PIX copia-e-cola estatico com a CHAVE da clinica (ela recebe direto no banco dela)."""
    p = get_blob(uid, "cl_pix", {}) or {}
    if not p.get("chave"): return {"ok":False, "erro":"a clinica ainda nao cadastrou a chave PIX"}
    nome = p.get("recebedor") or cl_perfil_get(uid).get("nome") or "CLINICA"
    cidade = p.get("cidade") or "BRASIL"
    cc = pix_brcode(p["chave"], nome, cidade, round(float(valor),2))
    if not cc: return {"ok":False, "erro":"chave PIX invalida"}
    return {"ok":True, "copia_cola":cc, "valor":round(float(valor),2)}
def _cl_hist_get(uid, tel): return get_blob(uid, "cl_chat_"+_digs(tel), []) or []
def _cl_hist_add(uid, tel, role, content):
    h = _cl_hist_get(uid, tel); h.append({"role":role, "content":content[:1500]}); set_blob(uid, "cl_chat_"+_digs(tel), h[-12:])
_WA_VISTOS = set()
def cl_wa_receber(payload):
    """Processa mensagens recebidas no WhatsApp e responde com a atendente que agenda. Roda em thread."""
    try:
        entry = (payload.get("entry") or [{}])[0]; ch = (entry.get("changes") or [{}])[0]; val = ch.get("value") or {}
        msgs = val.get("messages") or []
        if not msgs: return
        pid = (val.get("metadata") or {}).get("phone_number_id")
        uid = _wa_uid_por_phone(pid)
        if not uid: print("[wa] numero nao vinculado a nenhuma clinica:", pid); return
        if not cl_pode(uid, "atendente"): return   # atendente automatica e do plano Profissional
        nome = ""
        try: nome = ((val.get("contacts") or [{}])[0].get("profile") or {}).get("name", "")
        except Exception: pass
        for m in msgs:
            mid = m.get("id")
            if not mid or mid in _WA_VISTOS: continue
            _WA_VISTOS.add(mid)
            if len(_WA_VISTOS) > 3000: _WA_VISTOS.clear()
            if m.get("type") != "text": continue
            tel = m.get("from"); txt = (m.get("text") or {}).get("body", "")
            if not (tel and txt): continue
            try:
                resp = cl_bot_agente(uid, txt, _cl_hist_get(uid, tel), tel, nome)
                _cl_hist_add(uid, tel, "user", txt); _cl_hist_add(uid, tel, "assistant", resp)
                _wa_enviar(tel, resp, uid)
            except Exception as e: print("[wa msg]", e)
    except Exception as e: print("[wa_receber]", e)

# ===================== exporta e injeta no orion_cloud =====================
_STDLIB = ("os","re","json","time","datetime","threading","hmac","hashlib","base64","urllib")
__all__ = [n for n in list(globals())
           if not n.startswith("__") and n not in (("_oc", "_bind_orion_helpers") + _STDLIB)]
_bind_orion_helpers()
for _n in __all__:  # injeta os nomes da Clinica+ no orion_cloud (funciona em qualquer ordem de import)
    setattr(_oc, _n, globals()[_n])
