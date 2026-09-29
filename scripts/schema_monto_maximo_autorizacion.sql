-- Facturas por encima de este monto requieren autorización de ANB antes
-- de pasar a confirmación del cliente. Default $100,000 MXN para todos
-- los clientes existentes -- editable por cliente después si hace falta.

alter table reglas_fiscales_cliente
    add column if not exists monto_maximo_sin_autorizacion numeric not null default 100000.00;
