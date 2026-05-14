# TODOS — Agente de Facturación ANB

## Fase 2 — COMPLETADA ✅

- [x] **Cantidad y unidad por concepto** — `ConceptoItem` con cantidad, clave_unidad (default E48), precio_unitario.
- [x] **Múltiples conceptos por factura** — `conceptos: list[ConceptoItem]`, payload FacturAPI con items[].
- [x] **PPD → forma_pago = 99** — validador en modelo + safeguard en payload.
- [x] **Flujo en un solo mensaje** — Claude pide todo junto, extrae sin loop de preguntas.
- [x] **Fix precio a FacturAPI** — tax_included=false, price=precio_unitario, retención IVA correcta.

## Pendiente Fase 2.5

- [ ] **Leer cotización en PDF**
  El cliente adjunta una cotización en PDF; Claude extrae automáticamente los conceptos,
  cantidades y montos sin preguntarlos. El modelo ya soporta múltiples conceptos.

## Pendientes Fase 4 (flujos complejos)

- [ ] **REP — Recibo Electrónico de Pago (Complemento de Pago)**
  Cuando una factura PPD se cobra, el cliente avisa al bot con la fecha y monto del pago.
  El bot genera el complemento de pago referenciando el UUID de la factura original.
  Requiere: buscar la factura en Bitácora por folio/RFC, validar saldo pendiente,
  construir el nodo `Pagos` con `DoctoRelacionado`. FacturAPI endpoint: `POST /v2/invoices`
  con `type: "P"`.

## Pendientes Fase 3

- [ ] Email de entrega (XML + PDF) vía Resend
- [ ] Dashboard de facturas para Alejandro
- [ ] Cache de ClaveProdServ por cliente (después de ver patrones en producción)

## Pre-SaaS (antes de vender a otro despacho)

- [ ] Migrar de Google Sheets a Supabase (multi-tenant, sin race conditions)
- [ ] Gestión de bot de Telegram por despacho (un bot por organización)
- [ ] Formulario de onboarding web para nuevos despachos
- [ ] Integración WhatsApp Business (canal dominante en MX profesional)
- [ ] Suscripción vía Stripe
