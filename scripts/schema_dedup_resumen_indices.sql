-- Oct 2026: deduplicación de updates de Telegram, resumen único del cron e índices.
-- Aplicado con: python scripts/run_sql.py scripts/schema_dedup_resumen_indices.sql

-- Telegram reintenta un update si no recibe 200 a tiempo: el webhook registra
-- cada update_id y descarta los repetidos (ver sheets_client.registrar_update).
CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id bigint PRIMARY KEY,
    recibido timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE telegram_updates ENABLE ROW LEVEL SECURITY;  -- solo la service key

-- /check-pending manda un solo resumen y no repite un pendiente en 24h.
ALTER TABLE pendientes ADD COLUMN IF NOT EXISTS notificado_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_clientes_canal ON clientes (canal, canal_id);
CREATE INDEX IF NOT EXISTS idx_clientes_despacho_activo ON clientes (despacho_id, activo);
CREATE INDEX IF NOT EXISTS idx_pendientes_canal_mensaje ON pendientes (canal_id, telegram_message_id);
CREATE INDEX IF NOT EXISTS idx_pendientes_estado_timestamp ON pendientes (estado, timestamp);
CREATE INDEX IF NOT EXISTS idx_bitacora_origen ON bitacora (uuid_factura_origen, tipo, estado);
CREATE INDEX IF NOT EXISTS idx_bitacora_canal_timestamp ON bitacora (canal_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_bitacora_emisor_timestamp ON bitacora (rfc_emisor, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_bitacora_folio ON bitacora (folio_fiscal);
CREATE INDEX IF NOT EXISTS idx_telegram_updates_recibido ON telegram_updates (recibido);

NOTIFY pgrst, 'reload schema';
