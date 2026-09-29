-- Extiende `pendientes` para soportar la aprobación del cliente (botones
-- Sí/No) además de la revisión de ANB. Correr en el SQL Editor de Supabase.
-- Aditivo: default seguro para las filas existentes.

alter table pendientes
    add column if not exists tipo_aprobacion text not null default 'anb_revision';

alter table pendientes
    add column if not exists canal_id_aprobador text;
