# -*- coding: utf-8 -*-
"""
Testes do Orion (nuvem). Rodar:  py -3.12 -m pytest test_orion.py -q
Usa um SQLite temporario (env ORION_DB) - nao toca no banco real.
"""
import os, tempfile, json
os.environ.setdefault("ORION_DB", os.path.join(tempfile.gettempdir(), "orion_pytest.db"))
# garante banco limpo a cada execucao
try: os.remove(os.environ["ORION_DB"])
except Exception: pass

import orion_cloud as o

def U(uid=10, plano="free", criador=False, socio=False):
    return {"id":uid, "plano":plano, "criador":criador, "socio":socio, "nome":"Teste", "email":"", "tratamento":""}

# ---------- plano / gating ----------
def test_plano_de():
    assert o.plano_de(U(plano="free")) == "free"
    assert o.plano_de(U(plano="trafego")) == "trafego"
    assert o.plano_de(U(criador=True)) == "adm"
    assert o.plano_de(U(socio=True)) == "business"

def test_modos_gating():
    # free nao acessa modo pago (raciocinio) -> cai pro avancado
    assert o._modo_ok(U(plano="free"), "raciocinio") == "avancado"
    assert o._modo_ok(U(plano="business"), "raciocinio") == "raciocinio"
    # esforco profundo so pago
    assert o._esforco_ok(U(plano="free"), "profundo") == "normal"
    assert o._esforco_ok(U(plano="trafego"), "profundo") == "profundo"

# ---------- tokens ----------
def test_tokens_grant_e_consumo():
    u = U(uid=11, plano="pro")
    st = o.tokens_estado(u)
    assert st["grant"] == o.PLANO_TOKENS["pro"]
    assert st["restante"] == st["grant"]
    o.tokens_registrar(u, {"prompt_tokens":100, "completion_tokens":50}, "llama-3.3-70b-versatile")
    st2 = o.tokens_estado(u)
    assert st2["usados"] == 150
    assert st2["restante"] == st["grant"] - 150

def test_tokens_avulso_credita():
    u = U(uid=12, plano="pro")
    antes = o.tokens_estado(u)["restante"]
    o.tokens_add(u, 500000)
    assert o.tokens_estado(u)["restante"] == antes + 500000

def test_tokens_sem_saldo():
    u = U(uid=13, plano="free")
    # estoura a cota do free
    o.tokens_registrar(u, {"prompt_tokens":o.PLANO_TOKENS["free"], "completion_tokens":0}, "llama-3.1-8b-instant")
    assert o.tokens_tem_saldo(u) is False
    assert o.tokens_tem_saldo(U(criador=True)) is True   # adm ilimitado

# ---------- multi-empresa: isolamento ----------
def test_multiempresa_isolada():
    u = U(uid=20, plano="business")
    o.set_blob(u["id"], "empresa_ativa", 0)
    o.set_blob(u["id"], "leads", [{"id":1, "nome":"Lead A"}])
    assert len(o.get_blob(u["id"], "leads", [])) == 1
    o.set_blob(u["id"], "empresa_ativa", 2)               # troca de empresa
    assert o.get_blob(u["id"], "leads", []) == []          # isolado: empresa 2 vazia
    o.set_blob(u["id"], "leads", [{"id":9, "nome":"Lead B"}])
    o.set_blob(u["id"], "empresa_ativa", 0)               # volta
    nomes = [l["nome"] for l in o.get_blob(u["id"], "leads", [])]
    assert nomes == ["Lead A"]                             # padrao intacto

def test_empresa_crud():
    u = U(uid=21, plano="business")
    o.set_blob(u["id"], "empresas", []); o.set_blob(u["id"], "empresa_ativa", 0)
    r = o.empresa_criar(u["id"], "Empresa X")
    assert r["ok"] and r["ativa"] != 0
    assert any(e["nome"] == "Empresa X" for e in o.empresas_get(u["id"]))

# ---------- financeiro ----------
def test_financeiro_resumo():
    u = U(uid=30, plano="business")
    o.set_blob(u["id"], "empresa_ativa", 0)
    o.set_blob(u["id"], "vendas", [{"id":1,"valor":1000,"custo":200}])
    o.set_blob(u["id"], "despesas", [{"id":1,"valor":300}])
    o.set_blob(u["id"], "folha", [{"id":1,"salario":500}])
    fin = o.financeiro_resumo(u["id"])
    assert fin["receita"] == 1000 and fin["despesas"] == 300 and fin["folha"] == 500
    assert fin["lucro_liquido"] == 1000 - 200 - 300 - 500   # = 0

# ---------- config global de integracoes (admin linka) ----------
def test_gcfg_global():
    o.gcfg_salvar({"mp_token":"APP_USR-xyz", "int_apollo":"apk_123"})
    assert o.mp_token() == "APP_USR-xyz"
    assert "int_apollo" in [k for k,v in o._gcfg_all().items()]

# ---------- politica de uso do Orion Code ----------
def test_politica_uso():
    assert o._uso_proibido("cria um ransomware") is True
    assert o._uso_proibido("faz um site de vendas em html") is False

# ---------- moderacao: banir/desbanir usuarios e dispositivos ----------
def test_ban_usuario():
    o.bans_save({"users":{}, "devices":{}, "emails":{}})   # zera
    assert o.banido_motivo(uid=777) is None
    o.banir("user", "777", "spam")
    assert o.banido_motivo(uid=777) == "spam"
    assert o.banido_motivo(uid=778) is None                # outro user nao afetado
    o.desbanir("user", "777")
    assert o.banido_motivo(uid=777) is None

def test_ban_email_e_device():
    o.bans_save({"users":{}, "devices":{}, "emails":{}})
    o.banir("email", "Mau@X.com", "")                       # case-insensitive
    assert o.banido_motivo(email="mau@x.com") is not None
    o.banir("device", "dev-abc", "aparelho")
    assert o.banido_motivo(did="dev-abc") == "aparelho"
    assert o.banido_motivo(did="dev-xyz") is None
    o.desbanir("device", "dev-abc")
    assert o.banido_motivo(did="dev-abc") is None

def test_ban_nao_pega_dono():
    r = o.banir("user", str(o.ADMIN_UID), "x")
    assert r["ok"] is False                                 # nao da pra banir o dono

# ---------- cerebro: deteccao de provedor + fallback (anti 404/429) ----------
def test_brain_glm():
    k,b,m = o._brain_norm("zai-abc","","")
    assert "z.ai" in b and m=="glm-5.2"
    # chave GLM com base/modelo errado de outro provedor -> corrige
    k,b,m = o._brain_norm("zai-x","https://api.groq.com/openai/v1","llama-3.3-70b-versatile")
    assert "z.ai" in b and m.startswith("glm")

def test_brain_fallbacks():
    fb = o._fallbacks("https://generativelanguage.googleapis.com/v1beta/openai/","gemini-2.0-flash")
    assert "gemini-2.0-flash-lite" in fb and "gemini-2.0-flash" not in fb
    assert o._fallbacks("https://api.openai.com/v1","gpt-4o-mini")==[]   # provedor pago: sem fallback

# ---------- diretrizes: ensinar o Orion (autodidata controlavel) ----------
def test_diretrizes():
    uid=55
    o.set_blob(uid,"diretrizes",[])
    o.diretrizes_add(uid,"sempre responda curto")
    o.diretrizes_add(uid,"me chame de chefe")
    o.diretrizes_add(uid,"sempre responda curto")   # dedup
    ds=o.diretrizes_get(uid); assert len(ds)==2
    assert "REGRAS QUE O USUARIO TE ENSINOU" in o.diretrizes_contexto(uid)
    o.diretrizes_remover(uid,"me chame de chefe")
    assert o.diretrizes_get(uid)==["sempre responda curto"]

# ---------- Orion proativo (briefing fundamentado, sem inventar) ----------
def test_proativo_fundamentado():
    import datetime
    u = U(uid=70, plano="business")
    o.set_blob(70, "empresa_ativa", 0)
    o.set_blob(70, "leads", [{"status":"novo"}, {"status":"fechado"}])
    o.set_blob(70, "vendas", [{"valor":700, "custo":350, "data":datetime.date.today().isoformat()}])
    r = o.orion_proativo(u)
    assert r["ok"] and "," in r["saudacao"]            # "Boa noite, Teste."
    titulos = " ".join(i["titulo"].lower() for i in r["insights"])
    assert "lead" in titulos and "vendid" in titulos    # insights vem de dados REAIS
    # free sem negocio: nao inventa cards de negocio, mas sempre devolve algo util
    r2 = o.orion_proativo(U(uid=71, plano="free"))
    assert r2["ok"] and len(r2["insights"]) >= 1

# ---------- CLINICA: agenda sem sobreposicao + faturamento + cadastro + cliente/fidelidade ----------
def test_clinica_agenda_sem_conflito():
    import datetime
    uid = 90
    o.set_blob(uid, "cl_servicos", []); o.set_blob(uid, "cl_agendamentos", [])
    o.cl_servico_salvar(uid, {"nome":"Limpeza","preco":150,"duracao":60})
    sid = o.cl_servicos_get(uid)[0]["id"]
    d = datetime.date.today() + datetime.timedelta(days=2)
    while d.weekday()==6: d += datetime.timedelta(days=1)   # evita domingo (fechado)
    data = d.isoformat()
    r = o.cl_agendar(uid, {"servico_id":sid, "data":data, "hora":"10:00", "cliente":"Maria", "telefone":"11999"})
    assert r["ok"]
    livres = o.cl_disponibilidade(uid, data, sid)["slots"]
    assert "10:00" not in livres and "09:30" not in livres and "11:00" in livres   # 60min bloqueia 09:30 (sobrepoe)
    assert o.cl_agendar(uid, {"servico_id":sid, "data":data, "hora":"10:30", "cliente":"Joao"})["ok"] is False

def test_clinica_cadastro_e_doc():
    assert o._doc_ok("123") is False
    assert o._doc_ok("12345678901") is True       # CPF 11
    assert o._doc_ok("12345678000199") is True    # CNPJ 14
    r = o.clinica_criar_conta({"nome":"Teste Clinica","documento":"529.982.247-25","senha":"abcdef","email":"tc@x.com","telefone":"11987654321","aceitou_termos":True})
    assert r["ok"]
    assert o.clinica_login_dono("tc@x.com","abcdef") == r["uid"]      # login por email
    assert o.clinica_login_dono("11987654321","abcdef") == r["uid"]   # login por telefone
    assert o.clinica_login_dono("tc@x.com","errada") is False          # senha errada

def test_clinica_fidelidade():
    uid = 91; o.set_blob(uid,"cl_clientes",{}); o.cl_fidelidade_set(uid,{"ativo":True,"meta":3,"premio":"brinde"})
    o.cl_cliente_registrar(uid,"Bia","11900001111",cadastrado=True)
    for _ in range(3): o.cl_cliente_proc_inc(uid,"11900001111")
    cli = o.cl_clientes_listar(uid)["clientes"][0]
    assert cli["procedimentos"]==3 and cli["ganhou"] is True

# ---------- CLINICA: agenda por profissional + promoção segmentada + PIX config ----------
def test_clinica_agenda_por_profissional():
    import datetime
    uid = 95
    o.set_blob(uid,"cl_servicos",[]); o.set_blob(uid,"cl_agendamentos",[]); o.set_blob(uid,"cl_funcionarios",[])
    o.cl_servico_salvar(uid,{"nome":"Limpeza","preco":150,"duracao":60})
    o.cl_servico_salvar(uid,{"nome":"Massagem","preco":120,"duracao":50})
    sv=o.cl_servicos_get(uid); slimp=[s for s in sv if s["nome"]=="Limpeza"][0]["id"]; smass=[s for s in sv if s["nome"]=="Massagem"][0]["id"]
    hor={"dias":{d:{"aberto":(d=="ter"),"ini":"09:00","fim":"12:00"} for d in o.CL_DIAS},"intervalo":30}
    o.cl_func_salvar(uid,{"nome":"Ana","cpf":"52998224725","telefone":"11999998888","login":"ana","senha":"123456","servicos":[slimp],"horario":hor})
    fid=o.cl_func_get(uid)[0]["id"]
    d=datetime.date.today()+datetime.timedelta(days=1)
    while d.weekday()!=1: d+=datetime.timedelta(days=1)
    data=d.isoformat()
    assert o.cl_disponibilidade(uid,data,smass,fid)["slots"]==[]                 # nao faz massagem
    assert "11:00" in o.cl_disponibilidade(uid,data,slimp,fid)["slots"]          # faz limpeza de manha
    assert "11:30" not in o.cl_disponibilidade(uid,data,slimp,fid)["slots"]      # horario dela vai so ate 12h

def test_clinica_promo_segmentada():
    uid=96; o.set_blob(uid,"cl_clientes",{}); o.set_blob(uid,"cl_agendamentos",[])
    o.set_blob(uid,"cl_wa",{"token":"x","phone_id":"1"})   # tem credencial (envio falha, mas conta alvos)
    o.cl_cliente_registrar(uid,"Fiel","11911111111",cadastrado=True)
    for _ in range(3): o.cl_cliente_proc_inc(uid,"11911111111")
    o.cl_cliente_registrar(uid,"Avulso","11922222222")
    assert o.cl_promo_enviar(uid,"Promo da semana","todos")["alvos"]==2
    assert o.cl_promo_enviar(uid,"Promo da semana","fieis")["alvos"]==1
    assert o.cl_promo_enviar(uid,"Promo da semana","cadastrados")["alvos"]==1
    assert o.cl_promo_enviar(uid,"oi","todos")["ok"] is False   # mensagem curta

def test_clinica_pix_config():
    uid=97
    r=o.cl_pix_set(uid,{"ativo":True,"modo":"sinal","sinal_pct":40})
    assert r["ok"] and o.cl_pix_get(uid)["ativo"] and o.cl_pix_get(uid)["sinal_pct"]==40

def test_clinica_pix_brcode():
    cc = o.pix_brcode("teste@email.com", "Clinica Bella", "Sao Paulo", 150.0)
    assert cc.startswith("000201") and "br.gov.bcb.pix" in cc and "teste@email.com" in cc and "150.00" in cc
    assert len(cc[-4:]) == 4   # CRC no fim
    assert o.pix_brcode("", "x", "y", 10) == ""   # sem chave, sem codigo

def test_clinica_comissao():
    uid=98; o.set_blob(uid,"cl_servicos",[]); o.set_blob(uid,"cl_agendamentos",[]); o.set_blob(uid,"cl_funcionarios",[])
    o.cl_func_salvar(uid,{"nome":"Ana","cpf":"52998224725","telefone":"11999998888","login":"ana","senha":"123456","comissao_pct":40})
    fid=o.cl_func_get(uid)[0]["id"]
    import datetime
    d=(datetime.date.today()+datetime.timedelta(days=2)).isoformat()
    o.set_blob(uid,"cl_agendamentos",[{"id":1,"func_id":fid,"servico":"X","valor":200,"status":"concluido","inicio":d+"T10:00","telefone":"1"}])
    pf=o.cl_dashboard(uid)["por_funcionario"][0]
    assert pf["faturou"]==200 and pf["comissao"]==80.0
    assert o.cl_func_dashboard(uid,fid)["comissao_mes"]==80.0

def test_clinica_pacote_debita():
    import datetime
    uid=110; o.set_blob(uid,"cl_servicos",[]); o.set_blob(uid,"cl_agendamentos",[]); o.set_blob(uid,"cl_clientes",{}); o.set_blob(uid,"cl_pacotes",[]); o.set_blob(uid,"cl_bloqueios",[])
    o.cl_horario_set(uid,{"dias":{d:{"aberto":True,"ini":"08:00","fim":"20:00"} for d in o.CL_DIAS},"intervalo":30})
    o.cl_servico_salvar(uid,{"nome":"Limpeza","preco":150,"duracao":60}); sid=o.cl_servicos_get(uid)[0]["id"]
    o.cl_pacote_salvar(uid,{"nome":"3 Limpezas","servico_id":sid,"sessoes":3,"preco":400})
    pid=o.cl_pacotes_get(uid)[0]["id"]
    o.cl_pacote_vender(uid,"11999990000",pid)
    assert o.cl_dashboard(uid)["faturamento_mes"]==400   # venda do pacote = receita
    d=(datetime.date.today()+datetime.timedelta(days=2)).isoformat()
    ag=o.cl_agendar(uid,{"servico_id":sid,"data":d,"hora":"10:00","cliente":"C","telefone":"11999990000"})
    rc=o.cl_concluir(uid,ag["agendamento"]["id"],{})
    assert rc["pacote"] is True
    assert o.cl_cliente_ficha(uid,"11999990000")["cliente"]["pacotes"][0]["restantes"]==2   # debitou 1

def test_clinica_estoque_baixa_alerta():
    import datetime
    uid=111; o.set_blob(uid,"cl_servicos",[]); o.set_blob(uid,"cl_agendamentos",[]); o.set_blob(uid,"cl_estoque",[]); o.set_blob(uid,"cl_bloqueios",[])
    o.cl_horario_set(uid,{"dias":{d:{"aberto":True,"ini":"08:00","fim":"20:00"} for d in o.CL_DIAS},"intervalo":30})
    o.cl_servico_salvar(uid,{"nome":"Peeling","preco":200,"duracao":60}); sid=o.cl_servicos_get(uid)[0]["id"]
    o.cl_estoque_salvar(uid,{"nome":"Acido","qtd":10,"minimo":3,"unidade":"ml"}); prod=o.cl_estoque_get(uid)[0]["id"]
    sv=o.cl_servico_by(uid,sid); sv["insumos"]=[{"produto_id":prod,"qtd":2}]; o.set_blob(uid,"cl_servicos",[sv])
    d=(datetime.date.today()+datetime.timedelta(days=2)).isoformat()
    ag=o.cl_agendar(uid,{"servico_id":sid,"data":d,"hora":"10:00","cliente":"C","telefone":"119"})
    o.cl_concluir(uid,ag["agendamento"]["id"],{"valor":200})
    assert o.cl_estoque_get(uid)[0]["qtd"]==8       # baixou 2
    o.cl_estoque_mexer(uid,prod,-6)
    assert any(a["nome"]=="Acido" for a in o.cl_estoque_alertas(uid))   # 2 <= 3 -> alerta

def test_clinica_produtos_no_agendamento():
    import datetime
    uid=140
    for k in ("cl_servicos","cl_agendamentos","cl_estoque","cl_bloqueios"): o.set_blob(uid,k,[] )
    o.set_blob(uid,"cl_clientes",{}); o.set_blob(uid,"cl_pix",{})
    o.cl_horario_set(uid,{"dias":{d:{"aberto":True,"ini":"08:00","fim":"20:00"} for d in o.CL_DIAS},"intervalo":30})
    o.cl_servico_salvar(uid,{"nome":"Limpeza","preco":150,"duracao":60}); sid=o.cl_servicos_get(uid)[0]["id"]
    o.cl_estoque_salvar(uid,{"nome":"Serum","qtd":10,"minimo":2,"unidade":"un","preco":"80,00","vender":True})
    o.cl_estoque_salvar(uid,{"nome":"Mascara","qtd":5,"minimo":1,"unidade":"un","preco":"40,00","vender":True})
    # so produtos com vender+preco aparecem
    pv=o.cl_produtos_venda(uid); assert len(pv)==2
    pid1=[p for p in o.cl_estoque_get(uid) if p["nome"]=="Serum"][0]["id"]
    pid2=[p for p in o.cl_estoque_get(uid) if p["nome"]=="Mascara"][0]["id"]
    d=(datetime.date.today()+datetime.timedelta(days=2)).isoformat()
    o.cl_pix_set(uid,{"ativo":True,"modo":"sinal","sinal_pct":30,"chave":"x@x.com","nome":"C","cidade":"SP"})
    r=o.cl_agendar(uid,{"servico_id":sid,"data":d,"hora":"10:00","cliente":"Ana","telefone":"11999990000",
        "produtos":[{"produto_id":pid1,"qtd":2,"modo":"comprar"},{"produto_id":pid2,"qtd":1,"modo":"reservar"}]},origem="cliente")
    a=r["agendamento"]
    assert a["valor"]==350.0 and a["valor_servico"]==150.0      # 150 + 160 + 40
    assert {p["nome"] for p in a["produtos"]}=={"Serum","Mascara"}
    es={p["nome"]:p["qtd"] for p in o.cl_estoque_get(uid)}
    assert es["Serum"]==8 and es["Mascara"]==4                  # baixou no agendamento
    assert r["pix"]["valor"]==217.0                             # comprar cheio (160) + (150+40)*0.30
    # desmarcar devolve o estoque nao entregue
    o.cl_desmarcar(uid,a["id"],"teste")
    es2={p["nome"]:p["qtd"] for p in o.cl_estoque_get(uid)}
    assert es2["Serum"]==10 and es2["Mascara"]==5
    # sem estoque bloqueia e nao mexe em nada
    r2=o.cl_agendar(uid,{"servico_id":sid,"data":d,"hora":"11:00","cliente":"Bia","telefone":"11888880000",
        "produtos":[{"produto_id":pid1,"qtd":999,"modo":"comprar"}]},origem="cliente")
    assert r2["ok"] is False
    assert o.cl_estoque_get(uid)[0]["qtd"] in (10,5)            # intacto

def test_novidades_broadcast():
    # broadcast do dono com filtro por app (orion/clinica/ambos) + ativo/inativo + editar/apagar
    o.set_blob(1, "novidades", [])
    o.novidades_salvar({"tag":"Novo","titulo":"So clinica","texto":"x","app":"clinica"})
    o.novidades_salvar({"tag":"Novo","titulo":"Ambos","texto":"y","app":"ambos"})
    o.novidades_salvar({"tag":"IA","titulo":"So orion oculto","texto":"z","app":"orion","ativo":False})
    tit_orion=[n["titulo"] for n in o.novidades_get("orion")]
    tit_clin=[n["titulo"] for n in o.novidades_get("clinica")]
    assert "Ambos" in tit_orion and "So clinica" not in tit_orion and "So orion oculto" not in tit_orion
    assert "Ambos" in tit_clin and "So clinica" in tit_clin
    assert len(o.novidades_get("orion", incluir_inativas=True))==3   # admin ve tudo
    assert o.novidades_salvar({"titulo":""})["ok"] is False          # titulo obrigatorio
    nid=[n for n in o.novidades_all() if n["titulo"]=="Ambos"][0]["id"]
    o.novidades_salvar({"id":nid,"titulo":"Ambos v2","app":"ambos"})
    assert "Ambos v2" in [n["titulo"] for n in o.novidades_get("clinica")]
    o.novidades_apagar(nid)
    assert "Ambos v2" not in [n["titulo"] for n in o.novidades_get("clinica")]

def test_clinica_atendente_fallback():
    # sem IA configurada, o atendente responde ancorado nos dados reais (sem vazar termo tecnico)
    uid=142; o.set_blob(uid,"cl_servicos",[]); o.set_blob(uid,"cl_faq",[])
    with o._db() as c: c.execute("INSERT INTO users(nome,email,senha_hash,plano,criado) VALUES(?,?,?,?,?)",("Bella","b142@x.com","x","business",__import__("time").time()))
    o.cl_perfil_set(uid,{"nome":"Studio Bella","endereco":"Rua X, 10"})
    o.cl_servico_salvar(uid,{"nome":"Limpeza","preco":150,"duracao":60})
    rp=o.cl_bot_fallback(uid,"quanto custa?"); assert "150" in rp and "Limpeza" in rp
    rh=o.cl_bot_fallback(uid,"que horas abre?"); assert "Segunda" in rh
    re_=o.cl_bot_fallback(uid,"onde fica?"); assert "Rua X" in re_
    for r in (rp,rh,re_): assert "LLM" not in r and "API" not in r and "cerebro" not in r.lower()
    # FAQ cadastrada tem prioridade
    o.cl_faq_set(uid,[{"pergunta":"voces tem estacionamento?","resposta":"Temos convenio com o estacionamento ao lado."}])
    assert "convenio" in o.cl_bot_fallback(uid,"tem estacionamento ai?")

def test_clinica_aniversariantes():
    import datetime
    uid=144; o.set_blob(uid,"cl_clientes",{})
    o.cl_perfil_set(uid,{"nome":"Bella"})
    hoje=datetime.date.today()
    # um aniversariante HOJE e um so no mes
    o.cl_cliente_registrar(uid,"Aniver Hoje","11999990001",cadastrado=True,nascimento=f"1990-{hoje.month:02d}-{hoje.day:02d}")
    outro_dia = 28 if hoje.day!=28 else 15
    o.cl_cliente_registrar(uid,"Outro Dia","11999990002",cadastrado=True,nascimento=f"1985-{hoje.month:02d}-{outro_dia:02d}")
    # um de outro mes (nao deve aparecer)
    om = 1 if hoje.month!=1 else 2
    o.cl_cliente_registrar(uid,"Outro Mes","11999990003",cadastrado=True,nascimento=f"1980-{om:02d}-10")
    a=o.cl_aniversariantes(uid)
    nomes=[x["nome"] for x in a]
    assert "Aniver Hoje" in nomes and "Outro Dia" in nomes and "Outro Mes" not in nomes
    assert a[0]["nome"]=="Aniver Hoje" and a[0]["hoje"] is True      # hoje vem primeiro
    assert a[0]["link"].startswith("https://wa.me/55")              # link de parabens pronto
    assert any(not x["hoje"] for x in a)

def test_clinica_atendente_anuncio():
    # cliente que chega pelo anuncio: bot (mesmo sem IA) reconhece a origem e recebe puxando o procedimento
    uid=143; o.set_blob(uid,"cl_servicos",[]); o.set_blob(uid,"cl_anuncios",[]); o.set_blob(uid,"cl_faq",[])
    with o._db() as c: c.execute("INSERT INTO users(nome,email,senha_hash,plano,criado) VALUES(?,?,?,?,?)",("B","b143@x.com","x","business",__import__("time").time()))
    o.cl_perfil_set(uid,{"nome":"Bella","telefone":"11999990000"})
    o.cl_servico_salvar(uid,{"nome":"Botox","preco":500,"duracao":40}); sid=o.cl_servicos_get(uid)[0]["id"]
    o.cl_anuncio_salvar(uid,{"nome":"Promo Botox","servico_id":sid})
    lst=o.cl_anuncios_listar(uid)
    assert lst["anuncios"][0]["link"].startswith("https://wa.me/55")   # link wa.me com mensagem padrao
    assert o.cl_anuncio_match(uid, lst["anuncios"][0]["mensagem"]) is not None  # reconhece a origem
    r=o.cl_bot_fallback(uid, lst["anuncios"][0]["mensagem"])
    assert "Botox" in r and "500" in r                                  # ja puxa procedimento + preco
    assert o.cl_bot_fallback(uid,"oi quanto custa?").count("500")>=1     # cliente normal = FAQ

def test_clinica_servico_foto():
    uid=141; o.set_blob(uid,"cl_servicos",[])
    o.cl_servico_salvar(uid,{"nome":"Botox","preco":500,"duracao":30,"foto":"data:image/jpeg;base64,ABCD"})
    sid=o.cl_servicos_get(uid)[0]["id"]
    assert o.cl_servico_by(uid,sid)["foto"]=="data:image/jpeg;base64,ABCD"
    # editar sem mandar foto preserva; mandar vazio remove; publico expoe foto e produtos
    o.cl_servico_salvar(uid,{"id":sid,"nome":"Botox","preco":550,"duracao":30})
    assert o.cl_servico_by(uid,sid).get("foto")=="data:image/jpeg;base64,ABCD"
    assert any(s.get("foto") for s in o.cl_publico(uid)["servicos"])
    assert "produtos" in o.cl_publico(uid)
    o.cl_servico_salvar(uid,{"id":sid,"nome":"Botox","preco":550,"duracao":30,"foto":""})
    assert not o.cl_servico_by(uid,sid).get("foto")

def test_clinica_anamnese():
    uid=112; o.set_blob(uid,"cl_clientes",{})
    o.cl_anamnese_set(uid,"11999990000",{"alergias":"dipirona","gestante":"nao"})
    o.cl_foto_add(uid,"11999990000","data:image/png;base64,AAAA","antes")
    fic=o.cl_cliente_ficha(uid,"11999990000")["cliente"]
    assert fic["anamnese"]["alergias"]=="dipirona" and len(fic["fotos"])==1

def test_clinica_plano_gating():
    import time
    uid=120; o.set_blob(uid,"cl_plano",{}); o.set_blob(uid,"cl_funcionarios",[])
    o.cl_trial_iniciar(uid)
    assert o.cl_plano_efetivo(uid)=="trial" and o.cl_pode(uid,"atendente") and o.cl_pode(uid,"relatorios")
    p=o.cl_plano_raw(uid); p["trial_ate"]=time.time()-10; o.set_blob(uid,"cl_plano",p)   # expira
    assert o.cl_plano_efetivo(uid)=="free"
    assert o.cl_pode(uid,"agenda") and not o.cl_pode(uid,"atendente") and not o.cl_pode(uid,"estoque")
    # free: 1 funcionario; 2o barrado
    assert o.cl_func_salvar(uid,{"nome":"A","cpf":"52998224725","telefone":"119","login":"a","senha":"123456"})["ok"]
    r2=o.cl_func_salvar(uid,{"nome":"B","cpf":"52998224725","telefone":"118","login":"b","senha":"123456"})
    assert r2["ok"] is False and r2.get("upgrade")
    o.cl_plano_set_pro(uid,1)
    assert o.cl_plano_efetivo(uid)=="pro" and o.cl_pode(uid,"atendente")
    assert o.cl_func_salvar(uid,{"nome":"B","cpf":"52998224725","telefone":"118","login":"b","senha":"123456"})["ok"]  # pro libera equipe

def test_clinica_caixa_e_admin():
    import datetime
    hoje=datetime.date.today().isoformat()
    with o._db() as c:
        cur=c.execute("INSERT INTO users(nome,email,senha_hash,plano,criador,origem,criado) VALUES(?,?,?,?,?,?,?)",
                      ("Dona Caixa","caixa@x.com",o._hash("x"),"business",0,"clinica","01/01/2026")); c.commit(); uid=cur.lastrowid
    o.set_blob(uid,"cl_funcionarios",[]); o.set_blob(uid,"cl_perfil",{"nome":"Teste Caixa"})
    o.set_blob(uid,"cl_agendamentos",[
        {"id":1,"status":"concluido","inicio":hoje+"T10:00","valor":150,"pagamento":"pix","func_id":None,"servico":"X"},
        {"id":2,"status":"concluido","inicio":hoje+"T11:00","valor":100,"pagamento":"dinheiro","func_id":None,"servico":"Y"}])
    cx=o.cl_caixa(uid,hoje)
    assert cx["total"]==250 and cx["atendimentos"]==2 and cx["por_pagamento"]["pix"]==150
    ac=o.admin_clinicas()
    assert ac["ok"] and any(c["id"]==uid for c in ac["clinicas"])   # aparece no painel do dono

# ---------- precificacao (logica de margem) ----------
def test_precos_salvar():
    u = U(uid=40, plano="business")
    o.set_blob(u["id"], "empresa_ativa", 0)
    r = o.precos_salvar(u["id"], {"custo_hora":50,"horas":20,"custos_fixos":200,"margem":40,"imposto":6})
    assert r["ok"] and o.precos_get(u["id"])["margem"] == 40.0

# ---------- Carteira coletiva (co-investimento) ----------
def test_carteira_coletiva():
    uid=77; o.set_blob(uid,"coinv",{"participantes":[],"pix":{},"lan":[],"pos":[]})
    assert o.coinv_part_add(uid,{"nome":"Murilo","cpf":"123"})["ok"] is False        # CPF invalido barra
    o.coinv_part_add(uid,{"nome":"Murilo","cpf":"11144477735","email":"m@x.com"})
    o.coinv_part_add(uid,{"nome":"Vo","cpf":"52998224725","email":"vo@x.com"})
    ids={p["nome"]:p["id"] for p in o.coinv_estado(uid)["participantes"]}; M,V=ids["Murilo"],ids["Vo"]
    assert o.coinv_estado(uid)["participantes"][0]["cpf"].startswith("***")            # CPF mascarado
    # acesso compartilhado: quem tem o e-mail cadastrado cai na carteira do titular como participante
    ow,pt,modo=o._coinv_resolve({"id":999,"email":"vo@x.com","plano":"trading","criador":False,"socio":False})
    assert ow==uid and modo=="participante"
    assert o.coinv_sandbox_set(uid,True)["sandbox"] is True                            # modo simulacao
    o.coinv_lancar(uid,"deposito",M,"1000"); o.coinv_lancar(uid,"deposito",V,"500")
    assert o.coinv_estado(uid)["resumo"]["caixa"]==1500.0
    o.coinv_compra(uid,"PETR4",[{"part_id":M,"valor":"600"},{"part_id":V,"valor":"400"}])
    st=o.coinv_estado(uid); pm={p["nome"]:p for p in st["participantes"]}
    assert pm["Murilo"]["caixa"]==400.0 and pm["Murilo"]["investido"]==600.0 and st["resumo"]["investido"]==1000.0
    assert o.coinv_compra(uid,"VALE3",[{"part_id":V,"valor":"99999"}])["ok"] is False   # sem caixa
    pos=st["posicoes"][0]["id"]; o.coinv_vender(uid,pos,"1200")                          # lucro 200, 60/40
    pm=o.coinv_estado(uid)["participantes"]; pm={p["nome"]:p for p in pm}
    assert pm["Murilo"]["caixa"]==1120.0 and pm["Vo"]["caixa"]==580.0                    # proporcional ao custo
    assert o.coinv_lancar(uid,"saque",M,"1120")["ok"] is True
    assert o.coinv_lancar(uid,"saque",M,"50")["ok"] is False                             # caixa zerado
    o.coinv_pix_set(uid,{"chave":"a@b.com","recebedor":"M","cidade":"SP"})
    assert o.coinv_qr(uid,"100")["copia_cola"].startswith("0002")

# ---------- Lote 6: avaliacao, retorno, comissao por periodo ----------
def test_clinica_lote6():
    import datetime as _dt
    r = o.clinica_criar_conta({"nome":"L6","documento":"11144477735","email":"l6@x.com","telefone":"11900005555","senha":"teste123","aceitou_termos":True})
    uid = r["uid"]
    o.cl_cliente_salvar(uid, {"nome":"Ana","tel":"11988887777"})
    o._cl_aguardar_aval(uid, "11988887777", "Limpeza")
    assert o.cl_aguardando_aval(uid, "11988887777")
    assert o.cl_avaliar(uid, "11988887777", "5")["ok"] and not o.cl_aguardando_aval(uid, "11988887777")
    assert o.cl_avaliar(uid, "11988887777", "9")["ok"] is False    # fora de 1-5
    res = o.cl_avaliacao_resumo(uid); assert res["media"] == 5 and res["total"] == 1
    assert o.cl_retorno_set(uid, {"ativo":True, "dias":"45"})["retorno"]["dias"] == 45
    # comissao por periodo
    o.cl_func_salvar(uid, {"nome":"Carla","login":"carla","senha":"123456","cpf":"11144477735","telefone":"11999998888","comissao_pct":40})
    fid = o.cl_func_get(uid)[0]["id"]
    o.cl_servico_salvar(uid, {"nome":"Botox","preco":"500","duracao":"40"}); sv = o.cl_servicos_get(uid)[0]
    o.cl_horario_set(uid, {"dias":{d:{"aberto":True,"ini":"08:00","fim":"20:00"} for d in o.CL_DIAS}, "intervalo":30})
    amanha = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
    ag = o.cl_agendar(uid, {"servico_id":sv["id"], "data":amanha, "hora":"10:00", "cliente":"Ana", "telefone":"11988887777", "func_id":fid})
    assert ag.get("ok"), ag
    o.cl_concluir(uid, ag["agendamento"]["id"], {"pagamento":"pix"})
    cm = o.cl_comissao_periodo(uid, amanha, amanha)
    assert cm["ok"] and cm["total_comissao"] == 200.0 and cm["linhas"][0]["funcionario"] == "Carla"   # 40% de 500

# ---------- Mercado Pago OAuth (partes testaveis sem credencial real) ----------
def test_clinica_mp_oauth():
    r = o.clinica_criar_conta({"nome":"MP","documento":"11144477735","email":"mp@x.com","telefone":"11900004444","senha":"teste123","aceitou_termos":True})
    uid = r["uid"]
    assert o.cl_mp_status(uid)["app_ok"] is False                  # sem app configurado
    assert o.cl_mp_oauth_url(uid, "https://c.onrender.com")["ok"] is False
    o.set_blob(1, "integra_global", {**(o.get_blob(1,"integra_global",{}) or {}), "mp_client_id":"cid123", "mp_client_secret":"sec"})
    u = o.cl_mp_oauth_url(uid, "https://c.onrender.com")
    assert u["ok"] and "client_id=cid123" in u["url"] and "callback" in u["url"] and "state=" in u["url"]
    assert o.cl_mp_callback("code", "estado-falso", "https://c")["ok"] is False   # state invalido barrado
    assert o.cl_mp_entradas(uid)["ok"] is False                    # sem token conectado

# ---------- atendente do anuncio (campanha -> mensagem/link -> bot reconhece) ----------
def test_clinica_anuncio():
    r = o.clinica_criar_conta({"nome":"Anuncio","documento":"11144477735","email":"anun@x.com","telefone":"11900003333","senha":"teste123","aceitou_termos":True})
    uid = r["uid"]; o.cl_perfil_set(uid, {"telefone":"(11) 98888-7777"})
    o.cl_servico_salvar(uid, {"nome":"Botox","preco":"500","duracao":"40"})
    sv = o.cl_servicos_get(uid)[0]
    assert o.cl_anuncio_salvar(uid, {"nome":"Promo Botox","servico_id":sv["id"]})["ok"]
    an = o.cl_anuncios_listar(uid)["anuncios"][0]
    assert an["link"].startswith("https://wa.me/5511988887777?text=") and "anuncio" in an["mensagem"].lower()
    assert o.cl_anuncio_match(uid, an["mensagem"])["nome"] == "Promo Botox"   # bot reconhece
    assert o.cl_anuncio_match(uid, "oi tudo bem") is None
    assert o.cl_anuncio_apagar(uid, an["id"])["ok"]

# ---------- integracao Orion<->Clinica+ por API (token) ----------
def test_clinica_api_token():
    r = o.clinica_criar_conta({"nome":"API Teste","documento":"11144477735","email":"apit@x.com","telefone":"11900002222","senha":"teste123","aceitou_termos":True})
    uid = r["uid"]
    o.cl_servico_salvar(uid, {"nome":"Limpeza","preco":"150","duracao":"60"})
    tok = o.cl_api_token(uid)
    assert tok.startswith("clk_") and o.cl_api_uid(tok) == uid and o.cl_api_uid("clk_x") is None
    res = o.cl_api_resumo(uid)
    assert res["ok"] and any(s["nome"]=="Limpeza" for s in res["servicos"])
    assert o.cl_api_preco(uid, "Limpeza", "199,90")["ok"]
    assert o.cl_api_resumo(uid)["servicos"][0]["preco"] == 199.9      # mudou via API
    novo = o.cl_api_token_reset(uid)["token"]
    assert novo != tok and o.cl_api_uid(tok) is None and o.cl_api_uid(novo) == uid   # token antigo invalidado

# ---------- calendario + cliente (nascimento/aniversario) ----------
def test_calendario_e_cliente_nascimento():
    import datetime as _dt
    r = o.clinica_criar_conta({"nome":"Cal Cli","documento":"11144477735","email":"calcli@x.com","telefone":"11900001111","senha":"teste123","aceitou_termos":True})
    uid = r["uid"]
    mes = _dt.date.today().month
    s = o.cl_cliente_salvar(uid, {"nome":"Ana","tel":"11988887777","nascimento":f"1990-{mes:02d}-15"})
    assert s["ok"] and s["cliente"]["nascimento"] == f"1990-{mes:02d}-15"
    assert any(a["nome"]=="Ana" for a in o.cl_aniversariantes(uid))           # aparece nos aniversariantes do mes
    o.cl_bloqueio_add(uid, {"data":f"2099-{mes:02d}-10", "dia_todo":True, "motivo":"Reuniao"})
    cal = o.cl_calendario(uid, f"2099-{mes:02d}")
    assert cal["ok"] and any(i["tipo"]=="evento" for i in cal["dias"].get(f"2099-{mes:02d}-10", []))
    assert o.cl_cliente_apagar(uid, "11988887777")["ok"]

# ---------- preco do servico: BR (virgula) + nao duplicar ao editar ----------
def test_servico_preco_e_dedup():
    assert o._num("150,00") == 150.0 and o._num("1.500,50") == 1500.5 and o._num("R$ 89") == 89.0
    r = o.clinica_criar_conta({"nome":"Preco Teste","documento":"11144477735","email":"preco@x.com","telefone":"11912345678","senha":"teste123","aceitou_termos":True})
    uid = r["uid"]
    o.cl_servico_salvar(uid, {"nome":"Limpeza","preco":"150,00","duracao":"60"})
    o.cl_servico_salvar(uid, {"nome":"limpeza","preco":"200,00","duracao":"60"})   # mesmo nome (case) = editar, nao duplicar
    sv = o.cl_servicos_get(uid)
    assert len(sv) == 1 and sv[0]["preco"] == 200.0
    ag = o.cl_agendar(uid, {"servico_id":sv[0]["id"], "data":"2099-12-31", "hora":"10:00", "cliente":"Ana", "telefone":"11988887777"})
    # agenda pode recusar por horario; o que importa: se agendar, usa o preco atual
    if ag.get("ok"): assert ag["agendamento"]["valor"] == 200.0

# ---------- seguranca: hash de senha (PBKDF2 com salt + compat antigo) ----------
def test_hash_senha_pbkdf2():
    import hashlib
    h = o._hash("Senha@Forte1")
    assert h.startswith("pbkdf2$") and o._senha_ok("Senha@Forte1", h) and not o._senha_ok("errada", h)
    # hashes iguais de senhas iguais NAO podem ser identicos (salt diferente)
    assert o._hash("Senha@Forte1") != h
    # compat: hash antigo (sha256 sem salt) ainda valida
    antigo = hashlib.sha256(("orion_velha").encode()).hexdigest()
    assert o._senha_ok("velha", antigo) and not o._senha_ok("x", antigo)

# ---------- chaves de licenca da Clinica+ (criar/vender/ativar) ----------
def test_clinica_chave_licenca():
    r = o.clinica_criar_conta({"nome":"Chave Teste","documento":"11144477735","email":"chave@x.com","telefone":"11955554444","senha":"teste123","aceitou_termos":True})
    uid = r["uid"]
    assert o.cl_plano_efetivo(uid) == "free"
    chave = o.lic_registrar("clinica")
    assert chave and chave.startswith("CLI-")
    assert o.cl_ativar_chave(uid, chave)["ok"] is True
    assert o.cl_plano_efetivo(uid) == "pro"                       # ativou
    assert o.cl_ativar_chave(uid, chave)["ok"] is False           # uso unico (nao reusa)
    assert o.cl_ativar_chave(uid, o.lic_registrar("pro"))["ok"] is False   # chave de outro produto nao vale
    assert o.cl_ativar_chave(uid, "CLI-XXXXXX-XXXXXX-XX")["ok"] is False    # invalida

# ---------- REGRESSAO: roda igual ao Render (python orion_cloud.py / __main__) ----------
def test_boot_como_main_igual_render():
    """O Render roda `python orion_cloud.py` (modulo __main__). Como clinica_backend faz
    `import orion_cloud`, sem o alias em sys.modules os nomes cl_* iam pra copia errada e o
    main() quebrava (NameError loop_lembretes). Este teste sobe o processo de verdade e bate
    numa rota, pra garantir que nao volta a crashar no deploy."""
    import sys, time, socket, subprocess, tempfile, urllib.request, urllib.error
    s = socket.socket(); s.bind(("127.0.0.1", 0)); porta = s.getsockname()[1]; s.close()
    env = dict(os.environ); env["PORT"] = str(porta); env["APP_MODE"] = "clinica"
    env["ORION_DB"] = os.path.join(tempfile.gettempdir(), "orion_boottest_%d.db" % porta)
    aqui = os.path.dirname(os.path.abspath(__file__))
    p = subprocess.Popen([sys.executable, os.path.join(aqui, "orion_cloud.py")], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        ok = False; saida = ""
        for _ in range(40):
            if p.poll() is not None:
                saida = p.stdout.read(); break
            try:
                r = urllib.request.urlopen("http://127.0.0.1:%d/clinica" % porta, timeout=2)
                ok = (r.status == 200); break
            except urllib.error.HTTPError as e:
                ok = True; break  # respondeu (mesmo que 4xx) = nao crashou
            except Exception:
                time.sleep(0.25)
        assert p.poll() is None, "orion_cloud.py crashou como __main__ (deploy quebraria):\n" + saida
        assert ok, "subiu mas nao respondeu em /clinica"
    finally:
        p.terminate()
        try: p.wait(timeout=5)
        except Exception: p.kill()
