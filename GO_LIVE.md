# 🚀 GO-LIVE — Orion + Clínica+ (passo a passo pra colocar no ar)

Tudo roda num único serviço (`nuvem/`) no Render. Abaixo, o que ligar pra funcionar 100%.

## 1. Deploy no Render
1. Suba a pasta `nuvem/` (já tem tudo: `orion_cloud.py`, os HTMLs, `requirements.txt`, `render.yaml`).
2. Render → New → Web Service → conecte o repo/pasta.
3. Start command: `python orion_cloud.py` (a porta vem do env `PORT`, já tratado).
4. Banco: configure `DATABASE_URL` (Postgres do Render/Neon) pra não perder dados. Sem ele, usa SQLite local (some a cada deploy).

## 2. Variáveis de ambiente (Render → Environment)
| Variável | Pra quê | Obrigatória? |
|---|---|---|
| `DATABASE_URL` | Banco Postgres (dados persistentes) | **Sim** (senão perde dados) |
| `LLM_API_KEY` | Cérebro de IA (Gemini grátis: aistudio.google.com). Cola só a chave (`AIza...`) | **Sim** (chat/atendente) |
| `MP_ACCESS_TOKEN` | Seu Mercado Pago — recebe planos do Orion E mensalidades da Clínica+ | Pra cobrar |
| `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_ID` | WhatsApp Cloud API (global, opcional) | Pra push/atendente global |
| `WHATSAPP_VERIFY_TOKEN` | Token de verificação do webhook (invente um, ex: `orion2026`) | Pra atendente WhatsApp |
| `VAPID_PRIVATE` / `VAPID_PUBLIC` / `VAPID_EMAIL` | Notificações push (briefing matinal, lembretes) | Opcional |
| `CLINICA_PRECO` | Preço mensal da Clínica+ (padrão 79) | Opcional |
| `SECRET` | Segredo das sessões (invente um forte) | **Recomendada** |

> A chave de IA também dá pra colar pelo app: **Conta → Admin → Cérebro** (sem redeploy). O detector acha o provedor pela cara da chave (Gemini/Groq/GLM/OpenAI).

## 3. Integrações (depois de no ar)
- **Mercado Pago:** Admin → Integrações → cole o Access Token. Webhook no painel do MP: `https://SEUSITE/webhook/mp`.
- **WhatsApp (atendente da clínica):** cada clínica conecta o número dela em **Clínica+ → Atendente → Conectar WhatsApp** (token + phone_id). No painel da Meta, webhook = `https://SEUSITE/webhook/whatsapp`, verify token = o `WHATSAPP_VERIFY_TOKEN`, inscrever no campo **messages**.
- **PIX da clínica:** cada clínica põe a própria **chave PIX** em Clínica+ → Serviços (sem token, qualquer banco).

## 4. Endereços (rotas)
- Orion (app): `https://SEUSITE/`  · site de vendas: `/site`
- Clínica+ (painel da dona): `/clinica`  · vendas: `/clinica/sobre`  · agendamento do cliente: `/agendar?c=<id>`
- Webhooks: WhatsApp `/webhook/whatsapp` · Mercado Pago `/webhook/mp`
- OrionAdmin.exe (seu controle, roda no PC): aponta pra `https://SEUSITE`

## 5. Checklist final
- [ ] Deploy no ar, abre `/`
- [ ] `DATABASE_URL` setado (dados persistem após redeploy)
- [ ] Chave de IA respondendo (mande um "oi" no chat)
- [ ] Mercado Pago + webhook `/webhook/mp`
- [ ] Cria conta, vira ADM (primeira conta = dono)
- [ ] Clínica+: cria conta de teste, onboarding, agenda uma consulta
- [ ] OrionAdmin.exe loga e mostra usuários/clínicas
- [ ] (Opcional) VAPID pra push, WhatsApp pra atendente

Pronto: Orion no ar pra você usar/vender, e Clínica+ pra vender pras clínicas. 💜
