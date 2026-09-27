import hashlib
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests


# ============================================================
# CONFIGURACIÓN
# ============================================================

USUARIO_GITHUB = "plis2100"
REPOSITORIO_GITHUB = "rss-licitaciones-500000"

URL_RSS = (
    f"https://raw.githubusercontent.com/"
    f"{USUARIO_GITHUB}/{REPOSITORIO_GITHUB}/main/feed.xml"
)

URL_PLACSP = (
    "https://contrataciondelestado.es/sindicacion/"
    "sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"
)

ARCHIVO_RSS = Path("feed.xml")
ARCHIVO_HISTORIAL = Path("historial.json")

IMPORTE_MINIMO = 500_000.00

MAXIMO_PAGINAS = 12
MAXIMO_ENTRADAS_RSS = 500

TIMEOUT_CONEXION = 15
TIMEOUT_LECTURA = 50

ZONA_HORARIA = ZoneInfo("Europe/Madrid")

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; "
        "RSS-Adjudicaciones-Espana/3.0; "
        "+https://github.com/plis2100)"
    ),
    "Accept": (
        "application/atom+xml,"
        "application/xml;q=0.9,"
        "text/xml;q=0.8,*/*;q=0.7"
    ),
}


# ============================================================
# FUNCIONES XML
# ============================================================

def nombre_local(elemento):
    """Elimina el namespace del nombre de una etiqueta XML."""
    if elemento is None:
        return ""

    return elemento.tag.split("}")[-1]


def texto_elemento(elemento):
    """Devuelve todo el texto contenido en un elemento."""
    if elemento is None:
        return ""

    texto = " ".join(elemento.itertext())
    return " ".join(texto.split()).strip()


def buscar_descendiente(elemento, nombres):
    """
    Busca el primer descendiente cuyo nombre local coincida
    con alguno de los nombres indicados.
    """
    if elemento is None:
        return None

    if isinstance(nombres, str):
        nombres = {nombres}
    else:
        nombres = set(nombres)

    for descendiente in elemento.iter():
        if nombre_local(descendiente) in nombres:
            return descendiente

    return None


def buscar_descendientes(elemento, nombres):
    """Busca todos los descendientes con los nombres indicados."""
    if elemento is None:
        return []

    if isinstance(nombres, str):
        nombres = {nombres}
    else:
        nombres = set(nombres)

    return [
        descendiente
        for descendiente in elemento.iter()
        if nombre_local(descendiente) in nombres
    ]


def texto_descendiente(elemento, nombres):
    return texto_elemento(buscar_descendiente(elemento, nombres))


def hijos_directos(elemento, nombre):
    if elemento is None:
        return []

    return [
        hijo
        for hijo in list(elemento)
        if nombre_local(hijo) == nombre
    ]


# ============================================================
# CONVERSIÓN DE DATOS
# ============================================================

def limpiar_texto(valor):
    if valor is None:
        return ""

    return " ".join(str(valor).split()).strip()


def convertir_importe(valor):
    if valor is None:
        return None

    if isinstance(valor, (int, float)):
        return float(valor)

    texto = limpiar_texto(valor)

    if not texto:
        return None

    texto = (
        texto.replace("\u00a0", "")
        .replace("EUR", "")
        .replace("€", "")
        .replace(" ", "")
    )

    texto = re.sub(r"[^0-9,.\-]", "", texto)

    if not texto:
        return None

    try:
        if "," in texto and "." in texto:
            if texto.rfind(",") > texto.rfind("."):
                texto = texto.replace(".", "").replace(",", ".")
            else:
                texto = texto.replace(",", "")

        elif "," in texto:
            partes = texto.split(",")

            if len(partes[-1]) in (1, 2):
                texto = texto.replace(".", "").replace(",", ".")
            else:
                texto = texto.replace(",", "")

        elif texto.count(".") > 1:
            texto = texto.replace(".", "")

        return float(texto)

    except ValueError:
        return None


def formatear_importe(importe):
    texto = f"{importe:,.2f}"
    texto = texto.replace(",", "X")
    texto = texto.replace(".", ",")
    texto = texto.replace("X", ".")
    return f"{texto} €"


def convertir_fecha(valor):
    texto = limpiar_texto(valor)

    if not texto:
        return None

    texto_iso = texto.replace("Z", "+00:00")

    try:
        fecha = datetime.fromisoformat(texto_iso)

        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)

        return fecha.astimezone(ZONA_HORARIA)

    except ValueError:
        pass

    formatos = (
        "%Y-%m-%d",
        "%Y%m%d",
        "%d/%m/%Y",
        "%Y-%m-%d %H:%M:%S",
    )

    for formato in formatos:
        try:
            fecha = datetime.strptime(texto, formato)
            fecha = fecha.replace(tzinfo=ZONA_HORARIA)
            return fecha

        except ValueError:
            continue

    return None


def formatear_fecha(fecha):
    if fecha is None:
        return ""

    return fecha.astimezone(ZONA_HORARIA).strftime("%d/%m/%Y")


def fecha_rss(fecha):
    if fecha is None:
        fecha = datetime.now(timezone.utc)

    return format_datetime(fecha.astimezone(timezone.utc))


# ============================================================
# DESCARGA DE PLACSP
# ============================================================

def descargar_xml(url):
    ultimo_error = None

    for intento in range(1, 4):
        try:
            print(f"Descargando: {url}")

            respuesta = requests.get(
                url,
                headers=CABECERAS,
                timeout=(TIMEOUT_CONEXION, TIMEOUT_LECTURA),
            )

            respuesta.raise_for_status()

            contenido = respuesta.content

            if not contenido:
                raise RuntimeError("PLACSP devolvió un archivo vacío.")

            return contenido

        except requests.RequestException as error:
            ultimo_error = error
            print(f"Intento {intento} fallido: {error}")

            if intento < 3:
                espera = intento * 5
                print(f"Reintentando dentro de {espera} segundos...")
                time.sleep(espera)

    raise RuntimeError(
        f"No se pudo descargar la información de PLACSP: {ultimo_error}"
    )


def obtener_url_siguiente(raiz, url_actual):
    for elemento in raiz.iter():
        if nombre_local(elemento) != "link":
            continue

        relacion = limpiar_texto(elemento.attrib.get("rel")).lower()

        if relacion != "next":
            continue

        href = limpiar_texto(elemento.attrib.get("href"))

        if href:
            return urljoin(url_actual, href)

    return None


def descargar_entradas():
    entradas = []
    url = URL_PLACSP
    urls_visitadas = set()

    for numero_pagina in range(1, MAXIMO_PAGINAS + 1):
        if not url or url in urls_visitadas:
            break

        urls_visitadas.add(url)

        print(f"Procesando página {numero_pagina}/{MAXIMO_PAGINAS}")

        contenido = descargar_xml(url)

        try:
            raiz = ET.fromstring(contenido)

        except ET.ParseError as error:
            raise RuntimeError(
                f"PLACSP no devolvió un XML válido: {error}"
            ) from error

        entradas_pagina = [
            elemento
            for elemento in raiz.iter()
            if nombre_local(elemento) == "entry"
        ]

        print(
            f"Entradas encontradas en esta página: "
            f"{len(entradas_pagina)}"
        )

        entradas.extend(entradas_pagina)

        siguiente = obtener_url_siguiente(raiz, url)

        if not siguiente:
            break

        url = siguiente

    print(f"Total de entradas descargadas: {len(entradas)}")
    return entradas


# ============================================================
# EXTRACCIÓN DE LICITACIONES
# ============================================================

def obtener_estado(entrada):
    candidatos = [
        "ContractFolderStatusCode",
        "TenderResultCode",
        "ResultCode",
    ]

    for nombre in candidatos:
        for elemento in buscar_descendientes(entrada, nombre):
            valor = texto_elemento(elemento).upper()

            if valor:
                return valor

    return ""


def esta_adjudicada(entrada):
    estado = obtener_estado(entrada)

    codigos_adjudicados = {
        "ADJ",
        "ADJUDICADA",
        "AWARDED",
        "RESOLVED",
    }

    if estado in codigos_adjudicados:
        return True

    # Algunas entradas incorporan la adjudicación dentro de TenderResult
    # aunque el código general no sea fácilmente identificable.
    resultados = buscar_descendientes(entrada, "TenderResult")

    for resultado in resultados:
        if buscar_descendiente(resultado, "AwardDate") is not None:
            return True

    return False


def obtener_url_licitacion(entrada):
    # Preferencia por enlaces HTML de la entrada Atom.
    for elemento in entrada.iter():
        if nombre_local(elemento) != "link":
            continue

        href = limpiar_texto(elemento.attrib.get("href"))
        tipo = limpiar_texto(elemento.attrib.get("type")).lower()
        relacion = limpiar_texto(elemento.attrib.get("rel")).lower()

        if not href:
            continue

        if (
            "text/html" in tipo
            or relacion == "alternate"
            or "detalle_licitacion" in href
        ):
            return urljoin(URL_PLACSP, href)

    # Segunda posibilidad: URI incluida dentro de ContractFolderStatus.
    for elemento in entrada.iter():
        valor = texto_elemento(elemento)

        if (
            valor.startswith("https://")
            and "contrataciondelestado.es" in valor
        ):
            return valor

    return "https://contrataciondelestado.es/"


def obtener_expediente(entrada):
    expediente = texto_descendiente(
        entrada,
        [
            "ContractFolderID",
            "ProcurementProjectID",
            "ID",
        ],
    )

    return expediente or "SIN-EXPEDIENTE"


def obtener_objeto(entrada):
    proyectos = buscar_descendientes(entrada, "ProcurementProject")

    for proyecto in proyectos:
        nombre = texto_descendiente(
            proyecto,
            ["Name", "Description"],
        )

        if nombre:
            return nombre

    titulo_atom = ""

    for hijo in list(entrada):
        if nombre_local(hijo) == "title":
            titulo_atom = texto_elemento(hijo)
            break

    return titulo_atom or "Adjudicación publicada en PLACSP"


def obtener_organo_contratacion(entrada):
    ubicaciones = buscar_descendientes(
        entrada,
        "LocatedContractingParty",
    )

    for ubicacion in ubicaciones:
        nombre = texto_descendiente(
            ubicacion,
            ["Name", "PartyName"],
        )

        if nombre:
            return nombre

    return ""


def obtener_adjudicatario(resultado):
    partes_ganadoras = buscar_descendientes(
        resultado,
        [
            "WinningParty",
            "WinningPartyReference",
            "ContractorParty",
        ],
    )

    nombres = []
    identificadores = []

    for parte in partes_ganadoras:
        for nombre_elemento in buscar_descendientes(
            parte,
            ["Name", "RegistrationName"],
        ):
            nombre = texto_elemento(nombre_elemento)

            if nombre and nombre not in nombres:
                nombres.append(nombre)

        for id_elemento in buscar_descendientes(
            parte,
            ["ID", "CompanyID"],
        ):
            identificador = texto_elemento(id_elemento)

            if (
                identificador
                and identificador not in identificadores
                and identificador not in nombres
            ):
                identificadores.append(identificador)

    if not nombres:
        # Compatibilidad con versiones diferentes del esquema CODICE.
        for elemento in buscar_descendientes(resultado, "PartyName"):
            nombre = texto_descendiente(elemento, "Name")

            if nombre and nombre not in nombres:
                nombres.append(nombre)

    if not nombres:
        return ""

    adjudicatarios = []

    for posicion, nombre in enumerate(nombres[:5]):
        if posicion < len(identificadores):
            identificador = identificadores[posicion]

            if identificador and identificador not in nombre:
                texto = f"{nombre} ({identificador})"
            else:
                texto = nombre
        else:
            texto = nombre

        if texto not in adjudicatarios:
            adjudicatarios.append(texto)

    return " / ".join(adjudicatarios)


def obtener_importe_resultado(resultado):
    prioridades = [
        "PayableAmount",
        "TaxExclusiveAmount",
        "TotalAmount",
        "AwardedAmount",
        "EstimatedOverallContractAmount",
    ]

    for nombre in prioridades:
        importes = []

        for elemento in buscar_descendientes(resultado, nombre):
            moneda = limpiar_texto(
                elemento.attrib.get("currencyID", "EUR")
            ).upper()

            if moneda and moneda != "EUR":
                continue

            importe = convertir_importe(texto_elemento(elemento))

            if importe is not None and importe >= 0:
                importes.append(importe)

        if importes:
            return max(importes)

    return None


def obtener_fecha_adjudicacion(resultado):
    campos = [
        "AwardDate",
        "ContractAwardDate",
        "DecisionDate",
    ]

    for campo in campos:
        for elemento in buscar_descendientes(resultado, campo):
            fecha = convertir_fecha(texto_elemento(elemento))

            if fecha:
                return fecha

    return None


def obtener_fecha_publicacion(entrada):
    for campo in ("published", "updated", "IssueDate"):
        for elemento in entrada.iter():
            if nombre_local(elemento) == campo:
                fecha = convertir_fecha(texto_elemento(elemento))

                if fecha:
                    return fecha

    return datetime.now(timezone.utc)


def obtener_resultados_adjudicados(entrada):
    resultados = buscar_descendientes(entrada, "TenderResult")
    encontrados = []

    for resultado in resultados:
        adjudicatario = obtener_adjudicatario(resultado)
        importe = obtener_importe_resultado(resultado)
        fecha = obtener_fecha_adjudicacion(resultado)

        # No generamos una noticia incompleta.
        if not adjudicatario:
            continue

        if importe is None or importe < IMPORTE_MINIMO:
            continue

        if fecha is None:
            print(
                "Descartada adjudicación sin fecha oficial "
                "de adjudicación."
            )
            continue

        encontrados.append(
            {
                "adjudicatario": adjudicatario,
                "importe": importe,
                "fecha_adjudicacion": fecha,
            }
        )

    return encontrados


def convertir_entrada(entrada):
    if not esta_adjudicada(entrada):
        return []

    expediente = obtener_expediente(entrada)
    objeto = obtener_objeto(entrada)
    organo = obtener_organo_contratacion(entrada)
    enlace = obtener_url_licitacion(entrada)
    fecha_publicacion = obtener_fecha_publicacion(entrada)

    resultados = obtener_resultados_adjudicados(entrada)
    noticias = []

    for resultado in resultados:
        adjudicatario = resultado["adjudicatario"]
        importe = resultado["importe"]
        fecha_adjudicacion = resultado["fecha_adjudicacion"]

        fecha_texto = formatear_fecha(fecha_adjudicacion)
        importe_texto = formatear_importe(importe)

        titulo = (
            f"ADJUDICADA ESPAÑA | "
            f"{fecha_texto} | "
            f"{adjudicatario} | "
            f"{importe_texto} | "
            f"{objeto}"
        )

        descripcion = [
            "<p><strong>Estado:</strong> ADJUDICADA ESPAÑA</p>",
            (
                "<p><strong>Fecha de adjudicación:</strong> "
                f"{html.escape(fecha_texto)}</p>"
            ),
            (
                "<p><strong>Adjudicatario:</strong> "
                f"{html.escape(adjudicatario)}</p>"
            ),
            (
                "<p><strong>Importe adjudicado:</strong> "
                f"{html.escape(importe_texto)}</p>"
            ),
            (
                "<p><strong>Objeto:</strong> "
                f"{html.escape(objeto)}</p>"
            ),
            (
                "<p><strong>Número de expediente:</strong> "
                f"{html.escape(expediente)}</p>"
            ),
        ]

        if organo:
            descripcion.append(
                "<p><strong>Órgano de contratación:</strong> "
                f"{html.escape(organo)}</p>"
            )

        descripcion.append(
            f'<p><a href="{html.escape(enlace)}">'
            "Abrir adjudicación en la Plataforma de Contratación"
            "</a></p>"
        )

        identificador_original = (
            f"adjudicada-espana-v3-fecha|"
            f"{expediente}|"
            f"{fecha_texto}|"
            f"{adjudicatario}|"
            f"{importe:.2f}"
        )

        identificador = hashlib.sha256(
            identificador_original.encode("utf-8")
        ).hexdigest()

        noticias.append(
            {
                "id": identificador,
                "titulo": titulo,
                "url": enlace,
                "descripcion": "".join(descripcion),
                "fecha_adjudicacion": fecha_adjudicacion.isoformat(),
                "fecha_publicacion": fecha_publicacion.isoformat(),
                "expediente": expediente,
                "adjudicatario": adjudicatario,
                "importe": importe,
                "objeto": objeto,
            }
        )

    return noticias


# ============================================================
# HISTORIAL
# ============================================================

def cargar_historial():
    if not ARCHIVO_HISTORIAL.exists():
        return []

    try:
        contenido = json.loads(
            ARCHIVO_HISTORIAL.read_text(encoding="utf-8")
        )

        if isinstance(contenido, list):
            return contenido

    except (OSError, json.JSONDecodeError) as error:
        print(f"No se pudo leer el historial anterior: {error}")

    return []


def guardar_historial(noticias):
    ARCHIVO_HISTORIAL.write_text(
        json.dumps(
            noticias[:MAXIMO_ENTRADAS_RSS],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def mezclar_noticias(nuevas, anteriores):
    por_id = {}

    for noticia in anteriores:
        identificador = noticia.get("id")

        if identificador:
            por_id[identificador] = noticia

    for noticia in nuevas:
        por_id[noticia["id"]] = noticia

    resultado = list(por_id.values())

    resultado.sort(
        key=lambda noticia: (
            noticia.get("fecha_adjudicacion")
            or noticia.get("fecha_publicacion")
            or ""
        ),
        reverse=True,
    )

    return resultado[:MAXIMO_ENTRADAS_RSS]


# ============================================================
# CREACIÓN DEL RSS
# ============================================================

def crear_rss(noticias):
    ET.register_namespace(
        "atom",
        "http://www.w3.org/2005/Atom",
    )

    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            "xmlns:atom": "http://www.w3.org/2005/Atom",
        },
    )

    canal = ET.SubElement(rss, "channel")

    ET.SubElement(canal, "title").text = (
        "Adjudicaciones España superiores a 500.000 €"
    )

    ET.SubElement(canal, "link").text = (
        "https://contrataciondelestado.es/"
    )

    ET.SubElement(canal, "description").text = (
        "Adjudicaciones españolas con fecha oficial, adjudicatario "
        "e importe igual o superior a 500.000 euros."
    )

    ET.SubElement(canal, "language").text = "es-ES"

    ET.SubElement(canal, "lastBuildDate").text = fecha_rss(
        datetime.now(timezone.utc)
    )

    ET.SubElement(canal, "ttl").text = "300"

    ET.SubElement(
        canal,
        "{http://www.w3.org/2005/Atom}link",
        {
            "href": URL_RSS,
            "rel": "self",
            "type": "application/rss+xml",
        },
    )

    for noticia in noticias:
        entrada = ET.SubElement(canal, "item")

        ET.SubElement(entrada, "title").text = noticia["titulo"]
        ET.SubElement(entrada, "link").text = noticia["url"]

        ET.SubElement(
            entrada,
            "guid",
            {"isPermaLink": "false"},
        ).text = noticia["id"]

        fecha = convertir_fecha(
            noticia.get("fecha_adjudicacion")
            or noticia.get("fecha_publicacion")
        )

        ET.SubElement(entrada, "pubDate").text = fecha_rss(fecha)

        ET.SubElement(
            entrada,
            "description",
        ).text = noticia["descripcion"]

        ET.SubElement(
            entrada,
            "category",
        ).text = "ADJUDICADA ESPAÑA"

        ET.SubElement(
            entrada,
            "category",
        ).text = "Contratación pública"

        ET.SubElement(
            entrada,
            "category",
        ).text = "Más de 500.000 euros"

    arbol = ET.ElementTree(rss)
    ET.indent(arbol, space="  ")

    arbol.write(
        ARCHIVO_RSS,
        encoding="utf-8",
        xml_declaration=True,
    )


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

def main():
    print("==================================================")
    print("RSS DE ADJUDICACIONES DE ESPAÑA")
    print("==================================================")

    entradas = descargar_entradas()

    nuevas = []
    errores = 0

    for numero, entrada in enumerate(entradas, start=1):
        try:
            noticias = convertir_entrada(entrada)
            nuevas.extend(noticias)

        except Exception as error:
            errores += 1
            print(
                f"Error procesando la entrada {numero}: "
                f"{type(error).__name__}: {error}"
            )

    anteriores = cargar_historial()
    resultado = mezclar_noticias(nuevas, anteriores)

    guardar_historial(resultado)
    crear_rss(resultado)

    print("")
    print("==================================================")
    print("PROCESO FINALIZADO CORRECTAMENTE")
    print("==================================================")
    print(f"Entradas descargadas: {len(entradas)}")
    print(f"Adjudicaciones nuevas válidas: {len(nuevas)}")
    print(f"Entradas guardadas en el RSS: {len(resultado)}")
    print(f"Entradas con error: {errores}")
    print(f"Archivo generado: {ARCHIVO_RSS}")
    print(f"URL para Feedly: {URL_RSS}")

    if not nuevas:
        print("")
        print(
            "AVISO: la consulta terminó correctamente, pero en las "
            "páginas revisadas no había nuevas adjudicaciones con "
            "fecha, adjudicatario e importe superior a 500.000 €."
        )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("Proceso cancelado.", file=sys.stderr)
        sys.exit(130)

    except Exception as error:
        print(
            f"ERROR GENERAL: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        sys.exit(1)
