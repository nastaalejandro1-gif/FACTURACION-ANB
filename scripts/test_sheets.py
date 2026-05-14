"""
Verifica que la conexion a Google Sheets funciona y que las 4 pestanas
tienen los encabezados esperados.

Uso: py scripts/test_sheets.py
"""
import os
import sys
from pathlib import Path


def load_env():
    env_path = Path(__file__).parent.parent / ".env"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


load_env()

EXPECTED_HEADERS = {
    "Clientes": [
        "despacho_id", "ID_cliente", "Nombre_comercial", "Razon_social",
        "RFC", "canal", "canal_id", "Email_factura", "Tipo_persona",
        "Regimen_fiscal", "CP_fiscal", "IVA_aplica", "Retencion_IVA",
        "Retencion_ISR", "Clave_prod_serv_default", "Requiere_revision",
        "Notas_fiscales", "Activo", "Facturapi_key",
    ],
    "Conversaciones": [
        "despacho_id", "canal", "canal_id", "historial", "ultima_actualizacion",
    ],
    "Pendientes": [
        "id", "despacho_id", "canal", "canal_id", "telegram_message_id",
        "invoice_json", "motivo_revision", "timestamp", "estado", "timestamp_respuesta",
    ],
    "Bitacora": [
        "id", "despacho_id", "canal_id", "rfc_emisor", "rfc_receptor",
        "monto", "total", "requirio_revision", "estado", "folio_fiscal",
        "timestamp", "error_detalle",
    ],
}


def main():
    try:
        import gspread
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        print(f"Falta dependencia: {e}\nCorre: pip install gspread google-auth")
        sys.exit(1)

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    sheets_id = os.environ.get("GOOGLE_SHEETS_ID", "")

    print("Conectando a Google Sheets...")
    try:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        creds.refresh(Request())
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(sheets_id)
        print(f"Conectado: '{spreadsheet.title}'\n")
    except Exception as e:
        print(f"ERROR al conectar: {e}")
        sys.exit(1)

    tabs = {ws.title: ws for ws in spreadsheet.worksheets()}
    all_ok = True

    for tab_name, expected in EXPECTED_HEADERS.items():
        print(f"Pestaña '{tab_name}':", end=" ")

        if tab_name not in tabs:
            print(f"NO ENCONTRADA")
            all_ok = False
            continue

        actual = tabs[tab_name].row_values(1)
        missing = [h for h in expected if h not in actual]
        extra = [h for h in actual if h and h not in expected]

        if not missing:
            print(f"OK ({len(actual)} columnas)")
        else:
            print(f"FALTAN columnas: {missing}")
            all_ok = False

        if extra:
            print(f"  Columnas extra (no afectan): {extra}")

    print()
    if all_ok:
        print("Todo correcto. Google Sheets listo para usar.")
    else:
        print("Corrige las pestanas con errores antes de continuar.")
        sys.exit(1)


if __name__ == "__main__":
    main()
