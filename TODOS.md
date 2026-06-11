# TODOS — Agente de Facturación ANB

## Fase 2 — COMPLETADA ✅

- [x] Cantidad y unidad por concepto
- [x] Múltiples conceptos por factura
- [x] PPD → forma_pago = 99
- [x] Flujo en un solo mensaje
- [x] Fix precio a FacturAPI

## Fase 2.5 — COMPLETADA ✅

- [x] **Leer cotización en PDF** — Claude extrae conceptos, cantidades y montos automáticamente
- [x] **Migración a Supabase** — base de datos permanente, sin tokens que expiran, base para SaaS
- [x] **Errores silenciosos corregidos** — crashes en background task ahora llegan por Telegram

## Pendientes antes de lanzar con más clientes

- [ ] **REP — Recibo Electrónico de Pago (Complemento de Pago)** ← SIGUIENTE
  Cuando una factura PPD se cobra, el cliente avisa al bot con la fecha y monto del pago.
  El bot genera el complemento referenciando el UUID de la factura original.
- [ ] **Pasar FacturAPI de sandbox a live** — cuando terminen las pruebas con el cliente actual
- [ ] **Agregar los 15 clientes en Supabase** — tabla clientes, un row por cliente

## Fase 3

- [ ] Email de entrega (XML + PDF) vía Resend — ya está el código, solo falta configurar RESEND_API_KEY en Railway
- [ ] Dashboard de facturas para Alejandro
- [ ] Cache de ClaveProdServ por cliente (después de ver patrones en producción)

## Pendientes de la auditoría (jun 2026) — antes de crecer

- [ ] Deduplicar updates de Telegram por update_id (hoy un update reentregado puede duplicar una factura de auto-timbre)
- [ ] REP con sobrepago → requiere_revision en vez de recortar el saldo a 0
- [ ] Cron /check-pending: marcar notificado o mandar un solo resumen (hoy re-notifica todo en cada corrida)
- [ ] Claude tool use paralelo: responder todos los tool_use o usar disable_parallel_tool_use
- [ ] Índices en Supabase (correr en SQL Editor):
      clientes(canal, canal_id), pendientes(canal_id, telegram_message_id),
      pendientes(estado, timestamp), bitacora(uuid_factura_origen, tipo, estado)
- [ ] NO escalar Railway a 2+ réplicas ni workers hasta migrar los locks de memoria a advisory locks de Postgres
- [ ] Bitácora: no pisar datos del registro original al rechazar (upsert sobre el mismo id)
- [ ] Rate limit por chat (proteger costo de Anthropic) y límite de tamaño de PDF (~10MB)
- [ ] Formato Telegram: Claude escribe markdown pero se envía como HTML; mensajes >4096 chars fallan
- [ ] Borrar código muerto de Google: scripts/get_refresh_token.py, scripts/test_sheets.py, vars Google en conftest.py

## Pre-SaaS (antes de vender a otro despacho)

- [ ] Gestión de bot de Telegram por despacho (un bot por organización)
- [ ] Formulario de onboarding web para nuevos despachos
- [ ] Integración WhatsApp Business (canal dominante en MX profesional)
- [ ] Suscripción vía Stripe
