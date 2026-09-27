import hashlib
import html
import json
import re
import sys
import xml.etree.ElementTree as ET

from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


FUENTES = [
    {
        "nombre": "Perfiles del contratante",
        "url": (
            "https://contrataciondelsectorpublico.gob.es/"
            "sindicacion/sindicacion_643/"
            "licitacionesPerfilesContratanteCompleto3.atom"
        ),
    },
    {
        "nombre": "Plataformas agregadas",
        "url": (
            "https://contrataciondelsectorpublico.gob.es/"
            "sindicacion/sindicacion_1044/"
            "PlataformasAgregadasSinMenores.atom"
        ),
    },
]

IMPORTE_MINIMO = 500000.00
MAXIMO_PAGINAS_POR_FUENTE = 3
MAXIMO_LICITACIONES = 1500

VERSION_GUID = "v4-adjudicada-espana-adjudicatario"

ARCHIVO_RSS = Path("feed.xml")
ARCHIVO_ESTADO = Path("estado.json")

URL_RSS = (
    "https://raw.githubusercontent.com/"
    "plis2100/rss-licitaciones-500000/main/feed.xml"
)


def nombre_local(elemento):
    return elemento.tag.split("}")[-1].split(":")[-1]


def limpiar_texto(texto):
    if texto is None:
        return ""

    return re.sub(r"\s+", " ", str(texto)).strip()


def buscar_primero(elemento, nombre):
    if elemento is None:
        return None

    for candidato in elemento.iter():
        if nombre_local(candidato) == nombre:
            return candidato

    return None


def buscar_todos(elemento, nombre):
    if elemento is None:
        return []

    return [
        candidato
        for candidato in elemento.iter()
        if nombre_local(candidato) == nombre
    ]


def texto_primero(elemento, nombre):
    candidato = buscar_primero(elemento, nombre)

    if candidato is None:
        return ""

    return limpiar_texto(candidato.text)


def texto_hijo_directo(elemento, nombre):
    if elemento is None:
        return ""

    for hijo in list(elemento):
        if nombre_local(hijo) == nombre:
            return limpiar_texto(hijo.text)

    return ""


def convertir_importe(texto):
    if not texto:
        return 0.0

    texto = limpiar_texto(texto)
    texto = texto.replace("EUR", "")
    texto = texto.replace("€", "")
    texto = texto.replace(" ", "")

    try:
        return float(texto)
    except ValueError:
        pass

    try:
        texto = texto.replace(".", "").replace(",", ".")
        return float(texto)
    except ValueError:
        return 0.0


def formatear_importe(importe):
    return (
        f"{importe:,.2f}"
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
        + " €"
    )


def convertir_fecha(fecha):
    if not fecha:
        return datetime.now(timezone.utc)

    try:
        resultado = datetime.fromisoformat(
            limpiar_texto(fecha).replace("Z", "+00:00")
        )

        if resultado.tzinfo is None:
            resultado = resultado.replace(tzinfo=timezone.utc)

        return resultado

    except ValueError:
        return datetime.now(timezone.utc)


def crear_sesion():
    sesion = requests.Session()

    reintentos = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )

    adaptador = HTTPAdapter(max_retries=reintentos)

    sesion.mount("https://", adaptador)
    sesion.mount("http://", adaptador)

    sesion.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "application/atom+xml,application/xml,"
                "text/xml,*/*;q=0.8"
            ),
            "Accept-Language": "es-ES,es;q=0.9",
        }
    )

    return sesion


def descargar_xml(sesion, url):
    print(f"Descargando: {url}", flush=True)

    respuesta = sesion.get(
        url,
        timeout=90,
        allow_redirects=True,
    )

    respuesta.raise_for_status()

    if len(respuesta.content) < 200:
        raise RuntimeError(
            f"El fichero descargado está vacío: {url}"
        )

    return respuesta.content, respuesta.url


def obtener_enlace_siguiente(raiz, url_actual):
    for elemento in list(raiz):
        if nombre_local(elemento) != "link":
            continue

        if elemento.attrib.get("rel") == "next":
            href = elemento.attrib.get("href", "").strip()

            if href:
                return urljoin(url_actual, href)

    return ""


def obtener_nombre_organo(estado):
    parte = buscar_primero(
        estado,
        "LocatedContractingParty",
    )

    if parte is None:
        parte = buscar_primero(
            estado,
            "ContractingParty",
        )

    if parte is None:
        return "Órgano no informado"

    party_name = buscar_primero(
        parte,
        "PartyName",
    )

    if party_name is not None:
        nombre = texto_primero(
            party_name,
            "Name",
        )

        if nombre:
            return nombre

    nombre = texto_primero(
        parte,
        "Name",
    )

    return nombre or "Órgano no informado"


def obtener_adjudicatarios(estado):
    adjudicatarios = []

    for resultado in buscar_todos(
        estado,
        "TenderResult",
    ):
        adjudicatario = buscar_primero(
            resultado,
            "WinningParty",
        )

        if adjudicatario is None:
            continue

        party_name = buscar_primero(
            adjudicatario,
            "PartyName",
        )

        nombre = ""

        if party_name is not None:
            nombre = texto_primero(
                party_name,
                "Name",
            )

        if not nombre:
            nombre = texto_primero(
                adjudicatario,
                "Name",
            )

        nif = ""

        identificacion = buscar_primero(
            adjudicatario,
            "PartyIdentification",
        )

        if identificacion is not None:
            nif = texto_primero(
                identificacion,
                "ID",
            )

        if nombre:
            adjudicatario_completo = nombre

            if nif:
                adjudicatario_completo += f" ({nif})"

            if adjudicatario_completo not in adjudicatarios:
                adjudicatarios.append(adjudicatario_completo)

    return adjudicatarios


def obtener_fecha_adjudicacion(estado):
    fechas = []

    for resultado in buscar_todos(
        estado,
        "TenderResult",
    ):
        fecha = texto_primero(
            resultado,
            "AwardDate",
        )

        if fecha and fecha not in fechas:
            fechas.append(fecha)

    return ", ".join(fechas)


def obtener_importes_adjudicacion(estado):
    importes_sin_iva = []
    importes_con_iva = []

    for resultado in buscar_todos(
        estado,
        "TenderResult",
    ):
        proyecto_adjudicado = buscar_primero(
            resultado,
            "AwardedTenderedProject",
        )

        if proyecto_adjudicado is None:
            continue

        total = buscar_primero(
            proyecto_adjudicado,
            "LegalMonetaryTotal",
        )

        if total is None:
            continue

        sin_iva = convertir_importe(
            texto_primero(
                total,
                "TaxExclusiveAmount",
            )
        )

        con_iva = convertir_importe(
            texto_primero(
                total,
                "PayableAmount",
            )
        )

        if sin_iva > 0:
            importes_sin_iva.append(sin_iva)

        if con_iva > 0:
            importes_con_iva.append(con_iva)

    return (
        sum(importes_sin_iva),
        sum(importes_con_iva),
    )


def obtener_tipo_contrato(proyecto):
    codigo = texto_primero(
        proyecto,
        "TypeCode",
    )

    equivalencias = {
        "1": "Obras",
        "2": "Suministros",
        "3": "Servicios",
        "21": "Gestión de servicios públicos",
        "22": "Concesión de obras",
        "31": "Servicios especiales",
        "40": "Administrativo especial",
        "50": "Privado",
        "7": "Patrimonial",
        "8": "Otros",
    }

    return equivalencias.get(codigo, codigo)


def obtener_procedimiento(estado):
    proceso = buscar_primero(
        estado,
        "TenderingProcess",
    )

    if proceso is None:
        return ""

    codigo = texto_primero(
        proceso,
        "ProcedureCode",
    )

    equivalencias = {
        "1": "Abierto",
        "2": "Restringido",
        "3": "Negociado con publicidad",
        "4": "Negociado sin publicidad",
        "5": "Diálogo competitivo",
        "6": "Contrato menor",
        "100": "Normas internas",
        "101": "Derivado de acuerdo marco",
        "102": "Basado en sistema dinámico",
        "999": "Otros",
    }

    return equivalencias.get(codigo, codigo)


def obtener_cpv(proyecto):
    codigos = []

    for clasificacion in buscar_todos(
        proyecto,
        "RequiredCommodityClassification",
    ):
        codigo = texto_primero(
            clasificacion,
            "ItemClassificationCode",
        )

        if codigo and codigo not in codigos:
            codigos.append(codigo)

    return ", ".join(codigos[:10])


def obtener_url(entry):
    url = ""

    for elemento in list(entry):
        if nombre_local(elemento) != "link":
            continue

        href = elemento.attrib.get(
            "href",
            "",
        ).strip()

        rel = elemento.attrib.get(
            "rel",
            "",
        )

        if href and rel != "self":
            return href

        if href:
            url = href

    if url:
        return url

    return texto_hijo_directo(
        entry,
        "id",
    )


def extraer_adjudicacion(entry, fuente):
    estado = buscar_primero(
        entry,
        "ContractFolderStatus",
    )

    if estado is None:
        return None

    estado_codigo = texto_primero(
        estado,
        "ContractFolderStatusCode",
    ).upper()

    # Solo expedientes adjudicados.
    if estado_codigo != "ADJ":
        return None

    expediente = texto_primero(
        estado,
        "ContractFolderID",
    )

    proyecto = buscar_primero(
        estado,
        "ProcurementProject",
    )

    if proyecto is None:
        return None

    objeto = texto_primero(
        proyecto,
        "Name",
    )

    if not objeto:
        objeto = texto_hijo_directo(
            entry,
            "title",
        )

    if not objeto:
        objeto = "Objeto no informado"

    presupuesto = buscar_primero(
        proyecto,
        "BudgetAmount",
    )

    valor_estimado = convertir_importe(
        texto_primero(
            presupuesto,
            "EstimatedOverallContractAmount",
        )
    )

    presupuesto_sin_iva = convertir_importe(
        texto_primero(
            presupuesto,
            "TaxExclusiveAmount",
        )
    )

    presupuesto_con_iva = convertir_importe(
        texto_primero(
            presupuesto,
            "TotalAmount",
        )
    )

    if valor_estimado > 0:
        importe_filtro = valor_estimado
        criterio_importe = "Valor estimado"
    else:
        importe_filtro = presupuesto_sin_iva
        criterio_importe = "Presupuesto base sin IVA"

    if importe_filtro <= IMPORTE_MINIMO:
        return None

    adjudicatarios = obtener_adjudicatarios(estado)

    if adjudicatarios:
        texto_adjudicatarios = ", ".join(adjudicatarios)
    else:
        texto_adjudicatarios = "Adjudicatario no informado"

    (
        adjudicacion_sin_iva,
        adjudicacion_con_iva,
    ) = obtener_importes_adjudicacion(estado)

    fecha_adjudicacion = obtener_fecha_adjudicacion(
        estado
    )

    organo = obtener_nombre_organo(estado)
    tipo_contrato = obtener_tipo_contrato(proyecto)
    procedimiento = obtener_procedimiento(estado)
    cpv = obtener_cpv(proyecto)
    url = obtener_url(entry)

    actualizado = texto_hijo_directo(
        entry,
        "updated",
    )

    publicado = texto_hijo_directo(
        entry,
        "published",
    )

    fecha_dt = convertir_fecha(
        actualizado or publicado
    )

    lugar = buscar_primero(
        proyecto,
        "RealizedLocation",
    )

    provincia = texto_primero(
        lugar,
        "CountrySubentity",
    )

    if not provincia:
        provincia = texto_primero(
            lugar,
            "CountrySubentityCode",
        )

    identidad = expediente or texto_hijo_directo(
        entry,
        "id",
    )

    if not identidad:
        identidad = (
            f"{objeto}|{organo}|{importe_filtro}"
        )

    guid = hashlib.sha256(
        f"{VERSION_GUID}|{identidad}".encode("utf-8")
    ).hexdigest()

    titulo = (
        f"ADJUDICADA ESPAÑA | "
        f"{texto_adjudicatarios} | "
        f"{formatear_importe(importe_filtro)} | "
        f"{objeto}"
    )

    if len(titulo) > 450:
        titulo = titulo[:447] + "..."

    descripcion = (
        f"<p><strong>Estado:</strong> "
        f"ADJUDICADA ESPAÑA</p>"
        f"<p><strong>Adjudicatario:</strong> "
        f"{html.escape(texto_adjudicatarios)}</p>"
        f"<p><strong>Expediente:</strong> "
        f"{html.escape(expediente)}</p>"
        f"<p><strong>Objeto:</strong> "
        f"{html.escape(objeto)}</p>"
        f"<p><strong>Órgano de contratación:</strong> "
        f"{html.escape(organo)}</p>"
        f"<p><strong>{html.escape(criterio_importe)}:</strong> "
        f"{html.escape(formatear_importe(importe_filtro))}</p>"
    )

    if valor_estimado:
        descripcion += (
            f"<p><strong>Valor estimado:</strong> "
            f"{html.escape(formatear_importe(valor_estimado))}</p>"
        )

    if presupuesto_sin_iva:
        descripcion += (
            f"<p><strong>Presupuesto sin IVA:</strong> "
            f"{html.escape(formatear_importe(presupuesto_sin_iva))}</p>"
        )

    if presupuesto_con_iva:
        descripcion += (
            f"<p><strong>Presupuesto con IVA:</strong> "
            f"{html.escape(formatear_importe(presupuesto_con_iva))}</p>"
        )

    if adjudicacion_sin_iva:
        descripcion += (
            f"<p><strong>Importe adjudicado sin IVA:</strong> "
            f"{html.escape(formatear_importe(adjudicacion_sin_iva))}</p>"
        )

    if adjudicacion_con_iva:
        descripcion += (
            f"<p><strong>Importe adjudicado con IVA:</strong> "
            f"{html.escape(formatear_importe(adjudicacion_con_iva))}</p>"
        )

    if fecha_adjudicacion:
        descripcion += (
            f"<p><strong>Fecha de adjudicación:</strong> "
            f"{html.escape(fecha_adjudicacion)}</p>"
        )

    if tipo_contrato:
        descripcion += (
            f"<p><strong>Tipo de contrato:</strong> "
            f"{html.escape(tipo_contrato)}</p>"
        )

    if procedimiento:
        descripcion += (
            f"<p><strong>Procedimiento:</strong> "
            f"{html.escape(procedimiento)}</p>"
        )

    if provincia:
        descripcion += (
            f"<p><strong>Lugar de ejecución:</strong> "
            f"{html.escape(provincia)}</p>"
        )

    if cpv:
        descripcion += (
            f"<p><strong>Código CPV:</strong> "
            f"{html.escape(cpv)}</p>"
        )

    descripcion += (
        f"<p><strong>Fuente:</strong> "
        f"{html.escape(fuente)}</p>"
        f'<p><a href="{html.escape(url)}">'
        f"Consultar la adjudicación oficial</a></p>"
    )

    return {
        "id": guid,
        "version": VERSION_GUID,
        "clave_expediente": identidad,
        "expediente": expediente,
        "titulo": titulo,
        "objeto": objeto,
        "organo": organo,
        "adjudicatarios": adjudicatarios,
        "texto_adjudicatarios": texto_adjudicatarios,
        "valor_estimado": valor_estimado,
        "presupuesto_sin_iva": presupuesto_sin_iva,
        "presupuesto_con_iva": presupuesto_con_iva,
        "importe_filtro": importe_filtro,
        "criterio_importe": criterio_importe,
        "adjudicacion_sin_iva": adjudicacion_sin_iva,
        "adjudicacion_con_iva": adjudicacion_con_iva,
        "fecha_adjudicacion": fecha_adjudicacion,
        "tipo_contrato": tipo_contrato,
        "procedimiento": procedimiento,
        "estado": "ADJ",
        "cpv": cpv,
        "lugar": provincia,
        "fuente": fuente,
        "url": url,
        "fecha_iso": fecha_dt.isoformat(),
        "descripcion": descripcion,
    }


def descargar_fuente(sesion, fuente):
    url = fuente["url"]
    visitadas = set()
    adjudicaciones = []

    for numero_pagina in range(
        1,
        MAXIMO_PAGINAS_POR_FUENTE + 1,
    ):
        if not url or url in visitadas:
            break

        visitadas.add(url)

        print(
            f'{fuente["nombre"]}: página {numero_pagina}',
            flush=True,
        )

        contenido, url_final = descargar_xml(
            sesion,
            url,
        )

        try:
            raiz = ET.fromstring(contenido)

        except ET.ParseError as error:
            raise RuntimeError(
                f'XML incorrecto en {fuente["nombre"]}: {error}'
            ) from error

        entradas = [
            elemento
            for elemento in list(raiz)
            if nombre_local(elemento) == "entry"
        ]

        print(
            f"Entradas examinadas: {len(entradas)}",
            flush=True,
        )

        for entrada in entradas:
            adjudicacion = extraer_adjudicacion(
                entrada,
                fuente["nombre"],
            )

            if adjudicacion:
                adjudicaciones.append(adjudicacion)

        url = obtener_enlace_siguiente(
            raiz,
            url_final,
        )

    print(
        f'{fuente["nombre"]}: '
        f"{len(adjudicaciones)} adjudicaciones válidas",
        flush=True,
    )

    return adjudicaciones


def cargar_estado():
    if not ARCHIVO_ESTADO.exists():
        return []

    try:
        contenido = json.loads(
            ARCHIVO_ESTADO.read_text(
                encoding="utf-8",
            )
        )

        if isinstance(contenido, dict):
            contenido = contenido.get(
                "licitaciones",
                [],
            )

        if isinstance(contenido, list):
            return contenido

    except Exception as error:
        print(
            f"No se pudo leer estado.json: {error}",
            flush=True,
        )

    return []


def combinar_adjudicaciones(nuevas, anteriores):
    resultado = []
    expedientes_vistos = set()

    nuevas.sort(
        key=lambda elemento: elemento.get(
            "fecha_iso",
            "",
        ),
        reverse=True,
    )

    for adjudicacion in nuevas + anteriores:
        if adjudicacion.get(
            "estado",
            "",
        ).upper() != "ADJ":
            continue

        if adjudicacion.get("version") != VERSION_GUID:
            continue

        importe = float(
            adjudicacion.get(
                "importe_filtro",
                0,
            )
            or 0
        )

        if importe <= IMPORTE_MINIMO:
            continue

        titulo = adjudicacion.get(
            "titulo",
            "",
        )

        if not titulo.startswith(
            "ADJUDICADA ESPAÑA |"
        ):
            continue

        clave = (
            adjudicacion.get("clave_expediente")
            or adjudicacion.get("expediente")
            or adjudicacion.get("id")
        )

        if not clave:
            continue

        if clave in expedientes_vistos:
            continue

        expedientes_vistos.add(clave)
        resultado.append(adjudicacion)

    resultado.sort(
        key=lambda elemento: elemento.get(
            "fecha_iso",
            "",
        ),
        reverse=True,
    )

    return resultado[:MAXIMO_LICITACIONES]


def guardar_estado(adjudicaciones):
    contenido = {
        "actualizado": datetime.now(
            timezone.utc
        ).isoformat(),
        "version": VERSION_GUID,
        "filtro_estado": "ADJ",
        "importe_minimo": IMPORTE_MINIMO,
        "cantidad": len(adjudicaciones),
        "licitaciones": adjudicaciones,
    }

    ARCHIVO_ESTADO.write_text(
        json.dumps(
            contenido,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def crear_rss(adjudicaciones):
    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            "xmlns:atom": "http://www.w3.org/2005/Atom",
            "xmlns:content": (
                "http://purl.org/rss/1.0/modules/content/"
            ),
        },
    )

    canal = ET.SubElement(
        rss,
        "channel",
    )

    ET.SubElement(
        canal,
        "title",
    ).text = (
        "Adjudicaciones España superiores a 500.000 €"
    )

    ET.SubElement(
        canal,
        "link",
    ).text = (
        "https://contrataciondelestado.es/"
    )

    ET.SubElement(
        canal,
        "description",
    ).text = (
        "Adjudicaciones publicadas en la Plataforma de "
        "Contratación del Sector Público de España con valor "
        "estimado superior a 500.000 euros."
    )

    ET.SubElement(
        canal,
        "language",
    ).text = "es-es"

    ET.SubElement(
        canal,
        "generator",
    ).text = "GitHub Actions - plis2100"

    ET.SubElement(
        canal,
        "ttl",
    ).text = "300"

    ET.SubElement(
        canal,
        "lastBuildDate",
    ).text = format_datetime(
        datetime.now(timezone.utc)
    )

    atom_link = ET.SubElement(
        canal,
        "atom:link",
    )

    atom_link.set("href", URL_RSS)
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    for adjudicacion in adjudicaciones:
        item = ET.SubElement(
            canal,
            "item",
        )

        ET.SubElement(
            item,
            "title",
        ).text = adjudicacion["titulo"]

        ET.SubElement(
            item,
            "link",
        ).text = adjudicacion["url"]

        guid = ET.SubElement(
            item,
            "guid",
        )

        guid.set(
            "isPermaLink",
            "false",
        )

        guid.text = adjudicacion["id"]

        fecha = convertir_fecha(
            adjudicacion.get(
                "fecha_iso",
                "",
            )
        )

        ET.SubElement(
            item,
            "pubDate",
        ).text = format_datetime(fecha)

        ET.SubElement(
            item,
            "description",
        ).text = adjudicacion["descripcion"]

        contenido = ET.SubElement(
            item,
            "content:encoded",
        )

        contenido.text = adjudicacion["descripcion"]

    arbol = ET.ElementTree(rss)
    ET.indent(arbol, space="  ")

    arbol.write(
        ARCHIVO_RSS,
        encoding="utf-8",
        xml_declaration=True,
    )


def main():
    try:
        sesion = crear_sesion()
        nuevas = []
        fuentes_correctas = 0

        for fuente in FUENTES:
            try:
                nuevas.extend(
                    descargar_fuente(
                        sesion,
                        fuente,
                    )
                )

                fuentes_correctas += 1

            except Exception as error:
                print(
                    f'AVISO: Falló {fuente["nombre"]}: {error}',
                    file=sys.stderr,
                    flush=True,
                )

        if fuentes_correctas == 0:
            raise RuntimeError(
                "No se pudo descargar ninguna fuente oficial."
            )

        anteriores = cargar_estado()

        adjudicaciones = combinar_adjudicaciones(
            nuevas,
            anteriores,
        )

        guardar_estado(adjudicaciones)
        crear_rss(adjudicaciones)

        print(
            f"RSS creada correctamente con "
            f"{len(adjudicaciones)} adjudicaciones.",
            flush=True,
        )

        print(
            f"URL para Feedly: {URL_RSS}",
            flush=True,
        )

    except Exception as error:
        print(
            f"ERROR: {error}",
            file=sys.stderr,
            flush=True,
        )

        sys.exit(1)


if __name__ == "__main__":
    main()
