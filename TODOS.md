# TODOS — Agente de Facturación ANB

## Pendientes Fase 2 (próximos)

- [ ] **Cantidad y unidad en el concepto**
  Agregar campos `cantidad` (número) y `clave_unidad` (clave SAT: E48=Servicio, H87=Pieza, KGM=Kilogramo, etc.)
  al modelo `FacturaData`, al tool schema de Claude, y al payload de FacturAPI.
  Claude debe preguntar: "¿Cuántas unidades y de qué tipo? (ej. 1 Servicio, 5 piezas, 10 kg)"
  Default para despachos contables: 1 / E48 (Servicio).

- [ ] **Múltiples conceptos por factura**
  `FacturaData.concepto` actualmente es un `str` (un solo concepto). Cambiar a lista de items:
  `conceptos: list[ConceptoItem]` donde `ConceptoItem` tiene `descripcion`, `clave_prod_serv`,
  `cantidad`, `clave_unidad`, `precio_unitario`. El payload de FacturAPI ya soporta `items[]`
  con múltiples entradas. Claude debe preguntar si hay más conceptos antes de confirmar.

- [ ] **PPD → forma_pago = 99 automático** ✅ implementado en código y prompt.
  Cuando el método de pago es PPD, la forma de pago siempre es "99" (Por Definir) por regla SAT.
  Claude no debe preguntar la forma de pago si el cliente elige PPD.

- [ ] **Leer cotización en PDF**
  Permitir que el cliente adjunte una cotización en PDF para que Claude extraiga
  automáticamente concepto, cantidad, monto y unidad sin preguntarlos uno por uno.

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
