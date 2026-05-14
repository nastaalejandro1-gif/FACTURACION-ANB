"""
Verifica que el primer cliente se puede leer correctamente desde Sheets.
Uso: py scripts/test_client.py
"""
import os
import sys
from pathlib import Path


def load_env():
    for line in Path(__file__).parent.parent.joinpath(".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


load_env()
sys.path.insert(0, str(Path(__file__).parent.parent))

from sheets_client import get_client_by_canal_id

alejandro_chat_id = os.environ["ALEJANDRO_CHAT_ID"]

print(f"Buscando cliente con canal_id={alejandro_chat_id}...")
profile = get_client_by_canal_id("telegram", alejandro_chat_id)

if not profile:
    print("ERROR: No se encontro el cliente.")
    print("Verifica que la columna 'canal_id' tenga exactamente:", alejandro_chat_id)
    sys.exit(1)

print("\nCliente encontrado:")
print(f"  Nombre comercial : {profile.nombre_comercial}")
print(f"  Razon social     : {profile.razon_social}")
print(f"  RFC              : {profile.rfc}")
print(f"  Regimen fiscal   : {profile.regimen_fiscal}")
print(f"  Tipo persona     : {profile.tipo_persona}")
print(f"  CP fiscal        : {profile.cp_fiscal}")
print(f"  IVA aplica       : {profile.iva_aplica}")
print(f"  Retencion IVA    : {profile.retencion_iva}%")
print(f"  Retencion ISR    : {profile.retencion_isr}%")
print(f"  Clave prod/serv  : {profile.clave_prod_serv_default}")
print(f"  Requiere revision: {'SI' if profile.requiere_revision else 'NO'}")
print(f"  Notas fiscales   : {profile.notas_fiscales or '(ninguna)'}")
print(f"  FacturAPI key    : {'OK' if profile.facturapi_key else 'VACIA - falta llenar'}")

if not profile.facturapi_key:
    print("\nAVISO: La columna Facturapi_key esta vacia. El timbrado no funcionara.")
    sys.exit(1)

print("\nPerfil listo. El bot puede identificar a este cliente.")
