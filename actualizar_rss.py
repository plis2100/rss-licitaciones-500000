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
        "RSS-Adjudicaciones-Espana/4.0; "
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
    if elemento is None:
        return ""

    return elemento.tag.split("}")[-1]


def texto_elemento(elemento):
    if elemento is None:
        return ""

    texto = " ".join(elemento.itertext())
    return " ".join(texto.split()).strip()


def buscar_descendientes(elemento, nombres):
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


def buscar_descendiente(elemento, nombres):
    resultados = buscar_descendientes(elemento, nombres)
    return resultados[0] if resultados else None


def texto_descendiente(elemento, nombres):
    return texto_elemento(
        buscar_descendiente(elemento, nombres)
    )


def limpiar_texto(valor):
    if valor is None:
        return ""

    return " ".join(str(valor).split()).strip()


# ============================================================
# IMPORTES Y FECHAS
# ============================================================

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
                texto = texto.replace(".", "")
                texto = texto.replace(",", ".")
            else:
                texto = texto.replace(",", "")

        elif "," in texto:
            partes = texto.split(",")

            if len(partes[-1]) in (1, 2):
                texto = texto.replace(".", "")
                texto = texto.replace(",", ".")
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

    try:
        fecha = datetime.fromisoformat(
            texto.replace("Z", "+00:00")
        )

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
            return fecha.replace(tzinfo=ZONA_HORARIA)

        except ValueError:
            continue

    return None


def formatear_fecha(fecha):
    if fecha is None:
        return ""

    return fecha.astimezone(
        ZONA_HORARIA
    ).strftime("%d/%m/%Y")


def fecha_para_rss(fecha):
    if fecha is None:
        fecha = datetime.now(timezone.utc)

    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)

    return format_datetime(
        fecha.astimezone(timezone.utc)
    )


# ============================================================
# DESCARGA
# ============================================================

def descargar_xml(url):
    ultimo_error = None

    for intento in range(1, 4):
        try:
            print(f"Descargando: {url}")

            respuesta = requests.get(
                url,
                headers=CABECERAS,
                timeout=(
                    TIMEOUT_CONEXION,
                    TIMEOUT_LECTURA,
                ),
            )

            respuesta.raise_for_status()

            if not respuesta.content:
                raise RuntimeError(
                    "PLACSP devolvió un archivo vacío."
                )

            return respuesta.content

        except (
            requests.RequestException,
            RuntimeError,
        ) as error:
            ultimo_error = error
            print(f"Intento {intento} fallido: {error}")

            if intento < 3:
                espera = intento * 5
                print(
                    f"Reintentando dentro de "
                    f"{espera} segundos..."
                )
                time.sleep(espera)

    raise RuntimeError(
        "No se pudo descargar la información "
        f"de PLACSP: {ultimo_error}"
    )


def obtener_url_siguiente(raiz, url_actual):
    for elemento in raiz.iter():
        if nombre_local(elemento) != "link":
            continue

        relacion = limpiar_texto(
            elemento.attrib.get("rel")
        ).lower()

        if relacion != "next":
            continue

        href = limpiar_texto(
            elemento.attrib.get("href")
        )

        if href:
            return urljoin(url_actual, href)

    return None


def descargar_entradas():
    entradas = []
    url = URL_PLACSP
    visitadas = set()

    for pagina in range(1, MAXIMO_PAGINAS + 1):
        if not url or url in visitadas:
            break

        visitadas.add(url)

        print(
            f"Procesando página "
            f"{pagina}/{MAXIMO_PAGINAS}"
        )

        contenido = descargar_xml(url)

        try:
            raiz = ET.fromstring(contenido)

        except ET.ParseError as error:
            raise RuntimeError(
                "PLACSP no devolvió un XML válido: "
                f"{error}"
            ) from error

        entradas_pagina = [
            elemento
            for elemento in raiz.iter()
            if nombre_local(elemento) == "entry"
        ]

        entradas.extend(entradas_pagina)

        print(
            f"Entradas de esta página: "
            f"{len(entradas_pagina)}"
        )

        siguiente = obtener_url_siguiente(
            raiz,
            url,
        )

        if not siguiente:
            break

        url = siguiente

    print(
        f"Total de entradas descargadas: "
        f"{len(entradas)}"
    )

    return entradas


# ============================================================
# DATOS DE LA LICITACIÓN
# ============================================================

def obtener_estado(entrada):
    for nombre in (
        "ContractFolderStatusCode",
        "TenderResultCode",
        "ResultCode",
    ):
        for elemento in buscar_descendientes(
            entrada,
            nombre,
        ):
            valor = texto_elemento(
                elemento
            ).upper()

            if valor:
                return valor

    return ""


def esta_adjudicada(entrada):
    estado = obtener_estado(entrada)

    if estado in {
        "ADJ",
        "ADJUDICADA",
        "AWARDED",
        "RESOLVED",
    }:
        return True

    for resultado in buscar_descendientes(
        entrada,
        "TenderResult",
    ):
        if buscar_descendiente(
            resultado,
            "AwardDate",
        ) is not None:
            return True

    return False


def obtener_expediente(entrada):
    expediente = texto_descendiente(
        entrada,
        [
            "ContractFolderID",
            "ProcurementProjectID",
        ],
    )

    if expediente:
        return expediente

    for hijo in list(entrada):
        if nombre_local(hijo) == "id":
            identificador = texto_elemento(hijo)

            if identificador:
                return identificador

    return "SIN-EXPEDIENTE"


def obtener_objeto(entrada):
    for proyecto in buscar_descendientes(
        entrada,
        "ProcurementProject",
    ):
        objeto = texto_descendiente(
            proyecto,
            ["Name", "Description"],
        )

        if objeto:
            return objeto

    for hijo in list(entrada):
        if nombre_local(hijo) == "title":
            titulo = texto_elemento(hijo)

            if titulo:
                return titulo

    return "Adjudicación publicada en PLACSP"


def obtener_organo(entrada):
    for ubicacion in buscar_descendientes(
        entrada,
        "LocatedContractingParty",
    ):
        nombre = texto_descendiente(
            ubicacion,
            ["Name", "RegistrationName"],
        )

        if nombre:
            return nombre

    return ""


def obtener_url_licitacion(entrada):
    enlace_alternativo = ""

    for elemento in entrada.iter():
        if nombre_local(elemento) != "link":
            continue

        href = limpiar_texto(
            elemento.attrib.get("href")
        )

        if not href:
            continue

        tipo = limpiar_texto(
            elemento.attrib.get("type")
        ).lower()

        relacion = limpiar_texto(
            elemento.attrib.get("rel")
        ).lower()

        url = urljoin(URL_PLACSP, href)

        if "detalle_licitacion" in url:
            return url

        if relacion == "alternate":
            enlace_alternativo = url

        elif "text/html" in tipo:
            enlace_alternativo = url

    if enlace_alternativo:
        return enlace_alternativo

    return "https://contrataciondelestado.es/"


def obtener_fecha_publicacion(entrada):
    for nombre in (
        "published",
        "updated",
        "IssueDate",
    ):
        for elemento in entrada.iter():
            if nombre_local(elemento) == nombre:
                fecha = convertir_fecha(
                    texto_elemento(elemento)
                )

                if fecha:
                    return fecha

    return datetime.now(timezone.utc)


# ============================================================
# RESULTADO Y ADJUDICATARIO
# ============================================================

def obtener_adjudicatario(resultado):
    nombres = []
    identificadores = []

    zonas_ganadoras = buscar_descendientes(
        resultado,
        [
            "WinningParty",
            "ContractorParty",
        ],
    )

    for zona in zonas_ganadoras:
        for elemento in buscar_descendientes(
            zona,
            ["Name", "RegistrationName"],
        ):
            nombre = texto_elemento(elemento)

            if nombre and nombre not in nombres:
                nombres.append(nombre)

        for elemento in buscar_descendientes(
            zona,
            ["CompanyID", "ID"],
        ):
            identificador = texto_elemento(elemento)

            if (
                identificador
                and identificador not in identificadores
                and identificador not in nombres
            ):
                identificadores.append(identificador)

    if not nombres:
        for party_name in buscar_descendientes(
            resultado,
            "PartyName",
        ):
            nombre = texto_descendiente(
                party_name,
                "Name",
            )

            if nombre and nombre not in nombres:
                nombres.append(nombre)

    if not nombres:
        return ""

    adjudicatarios = []

    for posicion, nombre in enumerate(nombres[:5]):
        texto = nombre

        if posicion < len(identificadores):
            identificador = identificadores[posicion]

            if identificador not in nombre:
                texto = (
                    f"{nombre} "
                    f"({identificador})"
                )

        if texto not in adjudicatarios:
            adjudicatarios.append(texto)

    return " / ".join(adjudicatarios)


def obtener_importe_resultado(resultado):
    prioridades = (
        "PayableAmount",
        "TaxExclusiveAmount",
        "TotalAmount",
        "AwardedAmount",
        "EstimatedOverallContractAmount",
    )

    for nombre in prioridades:
        importes = []

        for elemento in buscar_descendientes(
            resultado,
            nombre,
        ):
            moneda = limpiar_texto(
                elemento.attrib.get(
                    "currencyID",
                    "EUR",
                )
            ).upper()

            if moneda and moneda != "EUR":
                continue

            importe = convertir_importe(
                texto_elemento(elemento)
            )

            if importe is not None:
                importes.append(importe)

        if importes:
            return max(importes)

    return None


def obtener_fecha_adjudicacion(resultado):
    for nombre in (
        "AwardDate",
        "ContractAwardDate",
        "DecisionDate",
    ):
        for elemento in buscar_descendientes(
            resultado,
            nombre,
        ):
            fecha = convertir_fecha(
                texto_elemento(elemento)
            )

            if fecha:
                return fecha

    return None


def obtener_resultados_validos(entrada):
    validos = []

    for resultado in buscar_descendientes(
        entrada,
        "TenderResult",
    ):
        adjudicatario = obtener_adjudicatario(
            resultado
        )

        importe = obtener_importe_resultado(
            resultado
        )

        fecha_adjudicacion = (
            obtener_fecha_adjudicacion(
                resultado
            )
        )

        if not adjudicatario:
            continue

        if importe is None:
            continue

        if importe < IMPORTE_MINIMO:
            continue

        # No usamos la fecha de publicación como si fuera
        # la fecha de adjudicación.
        if fecha_adjudicacion is None:
            print(
                "Descartada adjudicación sin "
                "fecha oficial."
            )
            continue

        validos.append(
            {
                "adjudicatario": adjudicatario,
                "importe": importe,
                "fecha": fecha_adjudicacion,
            }
        )

    return validos


def convertir_entrada(entrada):
    if not esta_adjudicada(entrada):
        return []

    expediente = obtener_expediente(entrada)
    objeto = obtener_objeto(entrada)
    organo = obtener_organo(entrada)
    url = obtener_url_licitacion(entrada)

    fecha_publicacion = obtener_fecha_publicacion(
        entrada
    )

    resultados = obtener_resultados_validos(
        entrada
    )

    noticias = []

    for resultado in resultados:
        adjudicatario = resultado["adjudicatario"]
        importe = resultado["importe"]
        fecha = resultado["fecha"]

        fecha_texto = formatear_fecha(fecha)
        importe_texto = formatear_importe(importe)

        titulo = (
            "ADJUDICADA ESPAÑA | "
            f"{fecha_texto} | "
            f"{adjudicatario} | "
            f"{importe_texto} | "
            f"{objeto}"
        )

        descripcion = [
            (
                "<p><strong>Estado:</strong> "
                "ADJUDICADA ESPAÑA</p>"
            ),
            (
                "<p><strong>Fecha de adjudicación:"
                "</strong> "
                f"{html.escape(fecha_texto)}</p>"
            ),
            (
                "<p><strong>Adjudicatario:</strong> "
                f"{html.escape(adjudicatario)}</p>"
            ),
            (
                "<p><strong>Importe adjudicado:"
                "</strong> "
                f"{html.escape(importe_texto)}</p>"
            ),
            (
                "<p><strong>Objeto:</strong> "
                f"{html.escape(objeto)}</p>"
            ),
            (
                "<p><strong>Expediente:</strong> "
                f"{html.escape(expediente)}</p>"
            ),
        ]

        if organo:
            descripcion.append(
                (
                    "<p><strong>Órgano de contratación:"
                    "</strong> "
                    f"{html.escape(organo)}</p>"
                )
            )

        descripcion.append(
            (
                f'<p><a href="{html.escape(url)}">'
                "Abrir adjudicación oficial"
                "</a></p>"
            )
        )

        texto_identificador = (
            "adjudicada-espana-v4|"
            f"{expediente}|"
            f"{fecha_texto}|"
            f"{adjudicatario}|"
            f"{importe:.2f}"
        )

        identificador = hashlib.sha256(
            texto_identificador.encode("utf-8")
        ).hexdigest()

        noticias.append(
            {
                "id": identificador,
                "titulo": titulo,
                "url": url,
                "descripcion": "".join(descripcion),
                "fecha_adjudicacion": fecha.isoformat(),
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
            ARCHIVO_HISTORIAL.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(contenido, list):
            return contenido

    except (
        OSError,
        json.JSONDecodeError,
    ) as error:
        print(
            f"No se pudo leer el historial: {error}"
        )

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
    noticias_por_id = {}

    for noticia in anteriores:
        identificador = noticia.get("id")

        if identificador:
            noticias_por_id[identificador] = noticia

    for noticia in nuevas:
        noticias_por_id[noticia["id"]] = noticia

    resultado = list(noticias_por_id.values())

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
    # Esta instrucción añade automáticamente xmlns:atom.
    ET.register_namespace(
        "atom",
        "http://www.w3.org/2005/Atom",
    )

    # CORRECCIÓN:
    # No se pone xmlns:atom manualmente porque provocaba:
    # ParseError: duplicate attribute.
    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
        },
    )

    canal = ET.SubElement(rss, "channel")

    ET.SubElement(
        canal,
        "title",
    ).text = (
        "Adjudicaciones España superiores "
        "a 500.000 €"
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
        "Adjudicaciones españolas con fecha "
        "oficial, adjudicatario e importe igual "
        "o superior a 500.000 euros."
    )

    ET.SubElement(
        canal,
        "language",
    ).text = "es-ES"

    ET.SubElement(
        canal,
        "lastBuildDate",
    ).text = fecha_para_rss(
        datetime.now(timezone.utc)
    )

    ET.SubElement(
        canal,
        "ttl",
    ).text = "300"

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
        entrada = ET.SubElement(
            canal,
            "item",
        )

        ET.SubElement(
            entrada,
            "title",
        ).text = noticia["titulo"]

        ET.SubElement(
            entrada,
            "link",
        ).text = noticia["url"]

        ET.SubElement(
            entrada,
            "guid",
            {
                "isPermaLink": "false",
            },
        ).text = noticia["id"]

        fecha = convertir_fecha(
            noticia.get("fecha_adjudicacion")
            or noticia.get("fecha_publicacion")
        )

        ET.SubElement(
            entrada,
            "pubDate",
        ).text = fecha_para_rss(fecha)

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

    # Verificación inmediata del XML generado.
    ET.parse(ARCHIVO_RSS)

    print("El archivo feed.xml es un XML válido.")


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

def main():
    print("========================================")
    print("RSS DE ADJUDICACIONES DE ESPAÑA")
    print("========================================")

    entradas = descargar_entradas()

    nuevas = []
    errores = 0

    for numero, entrada in enumerate(
        entradas,
        start=1,
    ):
        try:
            nuevas.extend(
                convertir_entrada(entrada)
            )

        except Exception as error:
            errores += 1

            print(
                f"Error en entrada {numero}: "
                f"{type(error).__name__}: {error}"
            )

    anteriores = cargar_historial()

    resultado = mezclar_noticias(
        nuevas,
        anteriores,
    )

    guardar_historial(resultado)
    crear_rss(resultado)

    print("")
    print("========================================")
    print("PROCESO FINALIZADO CORRECTAMENTE")
    print("========================================")
    print(
        f"Entradas descargadas: "
        f"{len(entradas)}"
    )
    print(
        f"Adjudicaciones nuevas válidas: "
        f"{len(nuevas)}"
    )
    print(
        f"Entradas guardadas en el RSS: "
        f"{len(resultado)}"
    )
    print(
        f"Entradas con error: "
        f"{errores}"
    )
    print(
        f"URL para Feedly: "
        f"{URL_RSS}"
    )

    if not nuevas:
        print("")
        print(
            "AVISO: el proceso funcionó, pero no se "
            "encontraron nuevas adjudicaciones que "
            "tuvieran fecha oficial, adjudicatario e "
            "importe mínimo de 500.000 euros."
        )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print(
            "Proceso cancelado.",
            file=sys.stderr,
        )
        sys.exit(130)

    except Exception as error:
        print(
            f"ERROR GENERAL: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        sys.exit(1)
