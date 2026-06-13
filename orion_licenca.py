# -*- coding: utf-8 -*-
"""
SISTEMA DE LICENCA DO ORION - um so algoritmo, usado por TODOS:
  - app desktop (jarvis_servidor.py)
  - app de nuvem (orion_cloud.py)
  - gerador de chaves do desenvolvedor (orion_keygen.py / OrionKeys.exe)

As chaves sao UNICAS e ASSINADAS: nao da pra forjar sem o segredo, e cada uma e diferente.
Uma chave gerada aqui vale em qualquer um (desktop, nuvem, exe).

>>> Se mudar LIC_SECRET, as chaves antigas param de valer. Mantenha esse valor fixo. <<<
"""
import os, hmac, hashlib, base64

LIC_SECRET = "Orion-LIC-2026-Gurroffe-Ads-Kx7p9Q2w"
_PB = {"pro": 1, "business": 2}
_BP = {1: "pro", 2: "business"}

def _mac(msg):
    return hmac.new(LIC_SECRET.encode(), msg, hashlib.sha256).digest()[:5]

def lic_make_det(plano, serial_bytes):
    plano = (plano or "").lower()
    if plano not in _PB: return None
    msg = bytes([_PB[plano]]) + serial_bytes[:5].ljust(5, b"\0")   # 6 bytes
    raw = msg + _mac(msg)                                          # +5 = 11 bytes
    s = base64.b32encode(raw).decode().rstrip("=")                 # 18 chars
    return f"{plano[:3].upper()}-{s[:6]}-{s[6:12]}-{s[12:]}"

def lic_make(plano):
    """Gera uma chave UNICA e valida pro plano. Ex: PRO-7K3Q2A-9XME4T-PL2VZ9"""
    return lic_make_det(plano, os.urandom(5))

def lic_master(plano):
    """Chave mestra fixa do plano (sempre valida). Util pra teste e pro painel Admin."""
    return lic_make_det(plano, b"MASTR")

def lic_check(chave):
    """Devolve o plano ('pro'/'business') se a chave for valida e assinada, senao None."""
    try:
        partes = (chave or "").strip().upper().split("-", 1)
        raw32 = (partes[1] if len(partes) > 1 else partes[0]).replace("-", "").replace(" ", "")
        pad = "=" * ((8 - len(raw32) % 8) % 8)
        data = base64.b32decode(raw32 + pad)
        if len(data) != 11: return None
        msg, mac = data[:6], data[6:11]
        if hmac.compare_digest(mac, _mac(msg)):
            return _BP.get(msg[0])
    except Exception:
        pass
    return None

if __name__ == "__main__":
    # teste rapido
    for p in ("pro", "business"):
        k = lic_make(p)
        print(p, "->", k, "| valida:", lic_check(k), "| mestra:", lic_master(p), lic_check(lic_master(p)))
    print("chave invalida ->", lic_check("XXX-1111-2222-3333"))
