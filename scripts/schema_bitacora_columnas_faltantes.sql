-- La tabla `bitacora` real no tiene estas 3 columnas que el código (desde
-- antes de este restructure) siempre intenta escribir -- log_to_bitacora
-- probablemente ha estado fallando en cada invocación. Correr en el SQL
-- Editor de Supabase.

alter table bitacora
    add column if not exists tipo text not null default 'ingreso';

alter table bitacora
    add column if not exists uuid_factura_origen text;

alter table bitacora
    add column if not exists imp_saldo_insoluto numeric;
