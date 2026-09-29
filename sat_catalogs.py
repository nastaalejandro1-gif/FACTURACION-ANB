"""
Catálogos estáticos SAT usados por fiscal_engine y por el prompt de Claude.

No hace llamadas de red ni depende de Supabase — son tablas fijas del
catálogo oficial del SAT para CFDI 4.0.
"""

# ---------------------------------------------------------------------------
# Regímenes fiscales — catálogo SAT (los mismos 17 códigos que ya usaba el
# prompt de claude_client.py). Sirve para que Claude no invente un código al
# leer una CSF, y para el motor de cálculo (validate_regimen_fiscal).
# ---------------------------------------------------------------------------

REGIMENES_FISCALES_VALIDOS: dict[str, str] = {
    "601": "General de Ley Personas Morales",
    "603": "Personas Morales con Fines no Lucrativos",
    "605": "Sueldos y Salarios e Ingresos Asimilados a Salarios",
    "606": "Arrendamiento",
    "607": "Régimen de Enajenación o Adquisición de Bienes",
    "608": "Demás ingresos",
    "610": "Residentes en el Extranjero sin Establecimiento Permanente en México",
    "611": "Ingresos por Dividendos (socios y accionistas)",
    "612": "Personas Físicas con Actividades Empresariales y Profesionales",
    "614": "Ingresos por intereses",
    "616": "Sin obligaciones fiscales",
    "621": "RESICO Personas Físicas",
    "622": "Actividades Agrícolas, Ganaderas, Silvícolas y Pesqueras",
    "625": "Régimen de Actividades Empresariales con ingresos a través de Plataformas Tecnológicas",
    "626": "RESICO Personas Morales",
    "628": "Hidrocarburos",
    "629": "De los Regímenes Fiscales Preferentes y de las Empresas Multinacionales",
}

# ---------------------------------------------------------------------------
# Régimen fiscal -> tipo de persona esperado, SOLO para los códigos que el
# despacho ya etiquetaba de forma inequívoca en el prompt de producción
# anterior (601, 603 = PM; 605-614, 621, 625 = PF; 626 = PM).
#
# Los códigos que el despacho NUNCA etiquetó como PF/PM exclusivo (610, 616,
# 622, 628, 629 — el catálogo oficial SAT los permite para ambos tipos de
# persona) se dejan fuera de este dict a propósito: no se inventa una
# restricción no verificada. Para esos códigos, fiscal_engine no hace
# validación cruzada régimen<->tipo_persona, solo usa tipo_persona_from_rfc.
#
# Fuente de verdad real de tipo_persona: la longitud del RFC (ver
# tipo_persona_from_rfc), que es estructura RFC estándar y no depende de
# este catálogo. Este dict es exclusivamente una validación cruzada
# adicional — si algún día se agrega un código sin confirmar contra el
# catálogo oficial SAT vigente, mejor dejarlo fuera que adivinar.
# ---------------------------------------------------------------------------

REGIMEN_FISCAL_TIPO_PERSONA: dict[str, frozenset[str]] = {
    "601": frozenset({"PM"}),
    "603": frozenset({"PM"}),
    "605": frozenset({"PF"}),
    "606": frozenset({"PF"}),
    "607": frozenset({"PF"}),
    "608": frozenset({"PF"}),
    "611": frozenset({"PF"}),
    "612": frozenset({"PF"}),
    "614": frozenset({"PF"}),
    "621": frozenset({"PF"}),
    "625": frozenset({"PF"}),
    "626": frozenset({"PM"}),
}


def tipo_persona_from_rfc(rfc: str) -> str:
    """
    Deriva PF/PM de la LONGITUD del RFC — estructura estándar, no un catálogo:
    Persona Moral = 3 letras + 6 dígitos + 3 alfanuméricos = 12 caracteres.
    Persona Física = 4 letras + 6 dígitos + 3 alfanuméricos = 13 caracteres.

    Requiere un RFC ya validado por ReceptorData.validate_rfc (formato correcto).
    """
    rfc = rfc.strip().upper()
    if len(rfc) == 13:
        return "PF"
    if len(rfc) == 12:
        return "PM"
    raise ValueError(f"RFC de longitud inválida para derivar tipo de persona: '{rfc}'")


def regimen_coincide_con_tipo_persona(regimen_fiscal: str, tipo_persona: str) -> bool:
    """
    Validación cruzada: si el régimen fiscal tiene una restricción conocida
    (ver REGIMEN_FISCAL_TIPO_PERSONA), el tipo_persona derivado del RFC debe
    coincidir. Si el régimen no está en el dict (caso ambiguo u oficialmente
    válido para ambos), no hay nada que validar y se retorna True.
    """
    restriccion = REGIMEN_FISCAL_TIPO_PERSONA.get(regimen_fiscal)
    if restriccion is None:
        return True
    return tipo_persona in restriccion
