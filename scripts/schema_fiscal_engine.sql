-- Esquema nuevo para el motor de cálculo fiscal (restructure jun-sep 2026).
-- Correr UNA VEZ en el SQL Editor de Supabase (mismo patrón que los índices
-- pendientes ya anotados en TODOS.md). Puramente aditivo: no modifica ni
-- borra nada de la tabla `clientes` existente.
--
-- Después de correr esto, ejecutar scripts/migrate_fiscal_rules.py para
-- poblar estas tablas a partir de los datos actuales de `clientes`.

create table if not exists catalogo_clave_prod_serv (
    id uuid primary key default gen_random_uuid(),
    despacho_id text not null,
    id_cliente text,  -- NULL = clave global del despacho (ej. flete 78101800)
    clave_prod_serv varchar(8) not null,
    descripcion_clave text not null,
    aplica_ieps boolean not null default false,
    aprobada_por text not null,
    fecha_aprobacion timestamptz not null default now(),
    activa boolean not null default true,
    veces_usada integer not null default 0,
    created_at timestamptz not null default now(),
    unique (despacho_id, id_cliente, clave_prod_serv)
);

create index if not exists idx_catalogo_clave_cliente_activa
    on catalogo_clave_prod_serv (despacho_id, id_cliente, activa);

create table if not exists reglas_fiscales_cliente (
    id uuid primary key default gen_random_uuid(),
    despacho_id text not null,
    id_cliente text not null,
    vigente_desde date not null,
    vigente_hasta date,  -- NULL = regla vigente actual
    iva_aplica boolean not null,
    tasa_iva numeric(5, 4) not null default 0.1600,
    retencion_iva_tasa numeric(6, 4) not null default 0,
    retencion_isr_tasa numeric(6, 4) not null default 0,
    ieps_tasa numeric(6, 4) not null default 0,
    requiere_revision_default boolean not null default false,
    notas_fiscales text not null default '',
    created_at timestamptz not null default now(),
    created_by text not null
);

create index if not exists idx_reglas_fiscales_cliente_vigencia
    on reglas_fiscales_cliente (despacho_id, id_cliente, vigente_desde desc);
