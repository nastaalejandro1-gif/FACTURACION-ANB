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

## Fase 4 — Restructure del motor fiscal (sep 2026) — COMPLETADA (código) ⚠️ falta probar en vivo

Plan completo en `.claude/plans/adaptive-forging-papert.md`. Principio: Claude extrae y
clasifica, el código calcula (Decimal, tolerancia cero). Migrado a producción local
(no pusheado a origin/main todavía — pendiente de pruebas manuales antes del push).

- [x] `fiscal_engine.py` — motor de cálculo en Decimal, IEPS/IVA/retenciones/total,
      redondeo ROUND_HALF_UP por concepto, REP con parcialidades.
- [x] `escalation.py` — enum cerrado: clave nueva, validación aritmética/cruzada,
      error de timbre, RFC inválido, receptor extranjero sin RFC. Reemplaza el
      `requiere_revision` de libre interpretación de Claude.
- [x] Catálogo de `clave_prod_serv` por cliente en Supabase (`catalogo_clave_prod_serv`) +
      reglas fiscales versionadas (`reglas_fiscales_cliente`) — migrados los 2 clientes
      actuales (Envoy, Sin Culpa) desde `clientes`.
- [x] Aprobación del cliente con botones Sí/No auditables antes de timbrar (ya no hay
      auto-timbre directo) — con chequeo de seguridad de que un cliente no pueda
      aprobar la factura de otro.
- [x] REP con sobrepago → escala en vez de recortar el saldo a 0 (ver ítem de auditoría abajo).
- [x] **Probar contra Claude real** — 3 conversaciones reales (Sin Culpa) contra el sandbox
      de Anthropic: flujo normal con claves del catálogo, clave fuera de catálogo (escala a
      "NUEVA" correctamente), y fuera de alcance (nota de crédito → mensaje fijo). Los 3 se
      comportaron como se diseñó.
- [x] **Verificar redondeo contra el Anexo 20** — timbrado real en sandbox de FacturAPI:
      total $1,325.76 coincide centavo a centavo con lo calculado localmente.
      🔴 **Encontró un bug real**: las retenciones (IVA/ISR) mandaban la tasa "semántica" a
      FacturAPI, que las recalculaba contra una base distinta y devolvía un monto ~6x más
      grande (125.05 en vez de 20.01). Corregido — ver commit "fix: retenciones en FacturAPI
      back-calculadas". Sin este timbrado real nunca se hubiera detectado con tests unitarios.
- [ ] **Probar REP contra el sandbox real** — el flujo de facturas de ingreso ya se validó
      en vivo (Claude + FacturAPI); REP solo tiene cobertura de tests unitarios/mocks todavía.
- [x] Tasas de impuesto exactas transportadas a FacturAPI (ya no se recalculan por división) —
      excepto retenciones, que se back-calculan a propósito (ver arriba).
- [ ] **Revisar `aplica_ieps` de claves nuevas aprobadas** — hoy el botón "Aprobar" de
      ANB siempre guarda `aplica_ieps=False` por default (más seguro que sobre-cobrar);
      si una clave nueva SÍ lleva IEPS, hay que corregirlo a mano en Supabase después de
      aprobar.
- [ ] **Push a origin/main + deploy** — todo el restructure está en 8 commits locales,
      nada pusheado todavía. Requiere confirmación explícita antes de hacer push (el bot
      está en producción con clientes reales).
- [ ] Pasar FacturAPI de sandbox a live — cuando terminen las pruebas del restructure
- [ ] Agregar los 13 clientes restantes en Supabase (clientes + reglas_fiscales_cliente +
      catalogo_clave_prod_serv) — hoy solo Envoy y Sin Culpa están migrados

## Fase 3

- [ ] Email de entrega (XML + PDF) vía Resend — ya está el código, solo falta configurar RESEND_API_KEY en Railway
- [ ] Dashboard de facturas para Alejandro
- [ ] Cache de ClaveProdServ por cliente (después de ver patrones en producción)

## Pendientes de la auditoría (jun 2026) — antes de crecer

- [ ] Deduplicar updates de Telegram por update_id (hoy un update reentregado puede duplicar una factura de auto-timbre)
- [x] REP con sobrepago → escala (VALIDACION_ARITMETICA) en vez de recortar el saldo a 0 — hecho en Fase 4
- [ ] Cron /check-pending: marcar notificado o mandar un solo resumen (hoy re-notifica todo en cada corrida)
- [x] Claude tool use paralelo — corregido: disable_parallel_tool_use en la llamada principal +
      tool_choice="none" en la llamada de confirmación (esa NUNCA debía poder llamar una tool).
      Encontrado probando en vivo un mensaje con REP + factura nueva mezclados — sin el fix,
      la conversación quedaba rota para siempre para ese cliente.
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
