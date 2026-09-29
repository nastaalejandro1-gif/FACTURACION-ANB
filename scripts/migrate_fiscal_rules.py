"""
Migra los datos fiscales de `clientes` a las tablas nuevas
reglas_fiscales_cliente y catalogo_clave_prod_serv.

Requiere haber corrido antes scripts/schema_fiscal_engine.sql en el SQL
Editor de Supabase.

Idempotente: se puede volver a correr sin duplicar filas — si un cliente ya
tiene una regla vigente (vigente_hasta IS NULL) no se le crea otra, y las
claves se verifican contra el UNIQUE constraint antes de insertar.

SIN --apply solo imprime qué haría (dry-run, default seguro). Para escribir
de verdad en Supabase: python scripts/migrate_fiscal_rules.py --apply
"""
import argparse
from datetime import date

from supabase import create_client

from config import DESPACHO_ID, SUPABASE_SERVICE_KEY, SUPABASE_URL

CLAVE_FLETE_GLOBAL = "78101800"
CLAVE_FLETE_DESCRIPCION = "Transporte de carga"


def _to_float(value) -> float:
    try:
        return float(str(value).replace("%", "").strip() or 0)
    except (ValueError, TypeError):
        return 0.0


def migrar(apply: bool) -> None:
    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    modo = "APLICANDO CAMBIOS" if apply else "DRY-RUN (nada se escribe)"
    print(f"=== Migración de reglas fiscales — {modo} ===\n")

    clientes = sb.table("clientes").select("*").execute().data or []
    if not clientes:
        print("No hay clientes en la tabla `clientes`. Nada que migrar.")
        return

    hoy = date.today().isoformat()

    existe_flete = (
        sb.table("catalogo_clave_prod_serv")
        .select("id")
        .eq("despacho_id", DESPACHO_ID)
        .is_("id_cliente", "null")
        .eq("clave_prod_serv", CLAVE_FLETE_GLOBAL)
        .execute()
    )
    if not existe_flete.data:
        print(f"[flete global] insertar clave {CLAVE_FLETE_GLOBAL}")
        if apply:
            sb.table("catalogo_clave_prod_serv").insert({
                "despacho_id": DESPACHO_ID,
                "id_cliente": None,
                "clave_prod_serv": CLAVE_FLETE_GLOBAL,
                "descripcion_clave": CLAVE_FLETE_DESCRIPCION,
                "aplica_ieps": False,
                "aprobada_por": "SEED_MIGRACION",
            }).execute()
    else:
        print("[flete global] ya existe, se omite")

    migrados = 0
    omitidos = 0
    for cliente in clientes:
        id_cliente = str(cliente.get("id_cliente", "") or "")
        nombre = cliente.get("nombre_comercial", "?")
        if not id_cliente:
            print(f"[!] cliente sin id_cliente, se omite: {nombre}")
            continue

        existe_regla = (
            sb.table("reglas_fiscales_cliente")
            .select("id")
            .eq("despacho_id", DESPACHO_ID)
            .eq("id_cliente", id_cliente)
            .is_("vigente_hasta", "null")
            .execute()
        )
        if existe_regla.data:
            print(f"[reglas] {id_cliente} ({nombre}) ya tiene regla vigente, se omite")
            omitidos += 1
        else:
            iva_aplica = str(cliente.get("iva_aplica", "SI")).strip().upper() in ("SI", "SÍ")
            row = {
                "despacho_id": DESPACHO_ID,
                "id_cliente": id_cliente,
                "vigente_desde": hoy,
                "vigente_hasta": None,
                "iva_aplica": iva_aplica,
                "tasa_iva": 0.16,
                "retencion_iva_tasa": round(_to_float(cliente.get("retencion_iva", 0)) / 100, 4),
                "retencion_isr_tasa": round(_to_float(cliente.get("retencion_isr", 0)) / 100, 4),
                "ieps_tasa": round(_to_float(cliente.get("ieps_rate", 0)) / 100, 4),
                "requiere_revision_default": bool(cliente.get("requiere_revision", False)),
                "notas_fiscales": cliente.get("notas_fiscales", "") or "",
                "created_by": "SEED_MIGRACION",
            }
            print(f"[reglas] {id_cliente} ({nombre}): {row}")
            if apply:
                sb.table("reglas_fiscales_cliente").insert(row).execute()
            migrados += 1

        clave_default = str(cliente.get("clave_prod_serv_default", "") or "").strip()
        if not clave_default:
            print(f"[!] {id_cliente} ({nombre}) no tiene clave_prod_serv_default — revisar manualmente")
            continue

        existe_clave = (
            sb.table("catalogo_clave_prod_serv")
            .select("id")
            .eq("despacho_id", DESPACHO_ID)
            .eq("id_cliente", id_cliente)
            .eq("clave_prod_serv", clave_default)
            .execute()
        )
        if existe_clave.data:
            print(f"[clave] {id_cliente}/{clave_default} ya existe, se omite")
        else:
            clave_row = {
                "despacho_id": DESPACHO_ID,
                "id_cliente": id_cliente,
                "clave_prod_serv": clave_default,
                "descripcion_clave": "Clave default migrada de clientes.clave_prod_serv_default",
                "aplica_ieps": _to_float(cliente.get("ieps_rate", 0)) > 0,
                "aprobada_por": "SEED_MIGRACION",
            }
            print(f"[clave] {id_cliente}: {clave_row}")
            if apply:
                sb.table("catalogo_clave_prod_serv").insert(clave_row).execute()

    print(f"\nListo. Clientes migrados: {migrados}. Ya tenían regla (omitidos): {omitidos}.")
    if not apply:
        print("Esto fue un dry-run — nada se escribió. Vuelve a correr con --apply para aplicar.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true",
        help="Escribe de verdad en Supabase. Sin esta bandera, solo imprime (dry-run).",
    )
    args = parser.parse_args()
    migrar(apply=args.apply)
