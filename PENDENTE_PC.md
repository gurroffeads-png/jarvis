# ✅ O QUE FAZER NO PC (sua parte) — atualizado em 27/06/2026

Quando estiver no computador, faça estes passos. Tudo já está pronto e testado nos arquivos; falta só subir.

## 1. Subir os arquivos (GitHub) e dar redeploy
Suba estes arquivos da pasta `C:\Users\USER\J.A.R.V.I.S\` nos **DOIS** repositórios e dê redeploy em cada serviço no Render:
- `orion_cloud.py`  ← **mudou** (Carteira Coletiva técnica + produtos no agendamento da clínica)
- `orion_app.html`  ← **mudou** (aba Carteira Coletiva reescrita)
- `clinica_backend.py`  ← **mudou** (foto no serviço + produto vendável + produtos no agendamento c/ baixa de estoque)
- `clinica_app.html`  ← **mudou** (foto no serviço, preço/venda no estoque, carrinho de produtos na agenda)
- `clinica_cliente.html`  ← **mudou** (página pública mostra foto do serviço + oferece produtos no agendamento)
- `orion_widget.html`  ← **novo** (chat do atendente Orion pra embutir no site do parceiro)
- `.dockerignore`  ← **novo** (não deixa subir banco de teste `.db` pro deploy)
- (se ainda não subiu: `orion_site.html`, `clinica_site.html`)

Repositórios:
- **Orion** → `gurroffeads-png/jarvis` (serviço `orion-l89a`)
- **Clínica+** → `gurroffeads-png/Clinica-` (serviço `clinica-5vdp`)

> Os arquivos são idênticos nas pastas `nuvem/` e `nuvem_clinica/`; pode subir de qualquer uma.

## 2. Rebuild do OrionAdmin.exe
Mudou o `orion_admin.py` (plano "clinica" + botão "Gerar CLINICA"). Rebuild do `OrionAdmin.exe` pra ter isso no seu painel.

## 3. Mercado Pago (config no painel do MP, não é código)
O erro "app não está pronto" é uma destas duas coisas:
- **`MP_CLIENT_ID`** tem que ser o **número da aplicação** (só dígitos, ex `1234567890123456`), NÃO o Access Token (`APP_USR-...`) nem a Public Key.
- No app do Mercado Pago, em **Configurar OAuth / URLs de redirecionamento**, coloque EXATAMENTE (com https, sem barra no fim):
  `https://clinica-5vdp.onrender.com/clinica/mp/callback`
- Setar `MP_CLIENT_ID` e `MP_CLIENT_SECRET` no Render do serviço da Clínica+. Salvar, esperar 1 min, tentar de novo.

## 4. Conferir envs no Render (serviço da Clínica+)
- `APP_MODE=clinica`
- `DATABASE_URL` (Postgres, pra não perder dados)
- `LLM_API_KEY` (chave grátis do Gemini, pra atendente/chat responder)

## 5. Pôr o Orion atendente no site do parceiro (julourenco.app)
- Entre na Clínica+ → aba **Clientes** → card "✨ Atendente Orion no seu site" → **Copiar código**.
- O código é tipo: `<script src="https://clinica-5vdp.onrender.com/orion-widget.js" data-clinica="ID"></script>`
- Cole esse `<script>` antes de `</body>` no site do parceiro (no julourenco.app, onde hoje está o outro assistente de IA — troque por esse).
- Aparece uma bolha flutuante; o Orion responde dúvidas e marca consulta. Dá pra mudar cor (`data-cor="#b76e79"`) e lado (`data-lado="esquerda"`).
- Sem `LLM_API_KEY` o atendente já responde preços/horários/endereço (FAQ); com a chave, ele AGENDA sozinho.

---
Quando isso estiver feito, o sistema fica 100% no ar com tudo que construímos (agenda sem conflito, calendário, clientes, anúncios, integração Orion↔Clínica, Mercado Pago, avaliação/retorno/comissão).
