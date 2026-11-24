import hashlib
import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests


USUARIO_GITHUB = "plis2100"
REPOSITORIO_GITHUB = "rss-licitaciones-500000"

URL_RSS = (
    "https://raw.githubusercontent.com/"
    f"{USUARIO_GITHUB}/{REPOSITORIO_GITHUB}/main/feed.xml"
)
URL_PLACSP = "https://contrataciondelsectorpublico.gob.es/"
FUENTES = (
    (
        "Perfiles alojados",
        urljoin(
            URL_PLACSP,
            "sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom",
        ),
    ),
    (
        "Plataformas agregadas",
        urljoin(
            URL_PLACSP,
            "sindicacion/sindicacion_1044/PlataformasAgregadasSinMenores.atom",
        ),
    ),
)

ARCHIVO_RSS = Path("feed.xml")
ARCHIVO_HISTORIAL = Path("historial.json")

IMPORTE_MINIMO = 500_000.00
DIAS_RSS = 7
TIMEOUT_CONEXION = 15
TIMEOUT_LECTURA = 50

ZONA_HORARIA = ZoneInfo("Europe/Madrid")
ATOM = "{http://www.w3.org/2005/Atom}"

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; RSS-Adjudicaciones-Espana/4.0; "
        "+https://github.com/plis2100)"
    ),
    "Accept": "application/atom+xml,application/xml;q=0.9,text/xml;q=0.8,*/*;q=0.7",
}


def nombre_local(elemento):
    if elemento is None:
        return ""
    return elemento.tag.split("}")[-1]


def texto_elemento(elemento):
    if elemento is None:
        return ""
    return " ".join(" ".join(elemento.itertext()).split())


def buscar_descendientes(elemento, nombres):
    if elemento is None:
        return []

    nombres = {nombres} if isinstance(nombres, str) else set(nombres)
    return [
        descendiente
        for descendiente in elemento.iter()
        if nombre_local(descendiente) in nombres
    ]


def buscar_descendiente(elemento, nombres):
    resultados = buscar_descendientes(elemento, nombres)
    return resultados[0] if resultados else None


def texto_descendiente(elemento, nombres):
    return texto_elemento(buscar_descendiente(elemento, nombres))


def limpiar_texto(valor):
    if valor is None:
        return ""
    return " ".join(str(valor).split())


def convertir_importe(valor):
    if valor is None:
        return None

    if isinstance(valor, (int, float)):
        return float(valor)

    texto = re.sub(r"[^0-9,.\-]", "", limpiar_texto(valor))
    if not texto:
        return None

    try:
        if "," in texto and "." in texto:
            if texto.rfind(",") > texto.rfind("."):
                texto = texto.replace(".", "").replace(",", ".")
            else:
                texto = texto.replace(",", "")
        elif "," in texto:
            if len(texto.split(",")[-1]) in (1, 2):
                texto = texto.replace(",", ".")
            else:
                texto = texto.replace(",", "")
        elif texto.count(".") > 1:
            texto = texto.replace(".", "")

        return float(texto)
    except ValueError:
        return None


def formatear_importe(importe):
    texto = f"{importe:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{texto} €"


def convertir_fecha(valor):
    texto = limpiar_texto(valor)
    if not texto:
        return None

    try:
        fecha = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)
        return fecha.astimezone(ZONA_HORARIA)
    except ValueError:
        pass

    for formato in ("%Y-%m-%d", "%Y%m%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            fecha = datetime.strptime(texto, formato)
            return fecha.replace(tzinfo=ZONA_HORARIA)
        except ValueError:
            continue

    return None


def formatear_fecha(fecha):
    if fecha is None:
        return ""
    return fecha.astimezone(ZONA_HORARIA).strftime("%d/%m/%Y")


def fecha_para_rss(fecha):
    if fecha is None:
        fecha = datetime.now(timezone.utc)
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    return format_datetime(fecha.astimezone(timezone.utc))


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

            if not respuesta.content:
                raise RuntimeError("PLACSP devolvió un archivo vacío.")

            return respuesta.content
        except (requests.RequestException, RuntimeError) as error:
            ultimo_error = error
            print(f"Intento {intento} fallido: {error}")
            if intento < 3:
                espera = intento * 5
                print(f"Reintentando dentro de {espera} segundos...")
                time.sleep(espera)

    raise RuntimeError(f"No se pudo descargar la información de PLACSP: {ultimo_error}")


def obtener_url_siguiente(raiz, url_actual):
    for elemento in raiz.iter():
        if nombre_local(elemento) != "link":
            continue
        if limpiar_texto(elemento.get("rel")).lower() != "next":
            continue

        href = limpiar_texto(elemento.get("href"))
        if href:
            url = urlsplit(urljoin(url_actual, href))
            if url.hostname == "contrataciondelestado.es":
                url = url._replace(
                    scheme="https",
                    netloc="contrataciondelsectorpublico.gob.es",
                )
            return url.geturl()

    return None


def descargar_entradas(limite):
    for fuente, url in FUENTES:
        visitadas = set()
        cantidad = 0

        while url:
            if url in visitadas:
                raise RuntimeError(f"Paginación circular en {fuente}: {url}")

            visitadas.add(url)
            raiz = ET.fromstring(descargar_xml(url))
            if raiz.tag != f"{ATOM}feed":
                raise RuntimeError(f"{fuente} no devolvió un feed Atom: {url}")

            fechas = []
            for entrada in raiz.findall(f"{ATOM}entry"):
                fecha = convertir_fecha(entrada.findtext(f"{ATOM}updated"))
                if fecha:
                    fechas.append(fecha)
                    if fecha < limite:
                        continue

                cantidad += 1
                yield entrada

            # Las páginas Atom se ordenan por fecha de actualización.
            if fechas and min(fechas) < limite:
                break

            url = obtener_url_siguiente(raiz, url)

        print(f"{fuente}: {cantidad} entradas en {len(visitadas)} páginas")


def obtener_expediente(entrada):
    expediente = texto_descendiente(entrada, ["ContractFolderID", "ProcurementProjectID"])
    if expediente:
        return expediente

    for hijo in entrada:
        if nombre_local(hijo) == "id":
            identificador = texto_elemento(hijo)
            if identificador:
                return identificador

    return "SIN-EXPEDIENTE"


def obtener_objeto(entrada):
    for proyecto in buscar_descendientes(entrada, "ProcurementProject"):
        objeto = texto_descendiente(proyecto, ["Name", "Description"])
        if objeto:
            return objeto

    for hijo in entrada:
        if nombre_local(hijo) == "title":
            titulo = texto_elemento(hijo)
            if titulo:
                return titulo

    return "Adjudicación publicada en PLACSP"


def obtener_organo(entrada):
    for ubicacion in buscar_descendientes(entrada, "LocatedContractingParty"):
        nombre = texto_descendiente(ubicacion, ["Name", "RegistrationName"])
        if nombre:
            return nombre

    return ""


def obtener_url_licitacion(entrada):
    enlace_alternativo = ""

    for elemento in entrada:
        if nombre_local(elemento) != "link":
            continue

        href = limpiar_texto(elemento.get("href"))
        if not href:
            continue

        tipo = limpiar_texto(elemento.get("type")).lower()
        relacion = limpiar_texto(elemento.get("rel")).lower()
        url = urljoin(URL_PLACSP, href)

        if "detalle_licitacion" in url:
            return url
        if relacion in {"", "alternate"} or "text/html" in tipo:
            enlace_alternativo = url

    return enlace_alternativo or "https://contrataciondelestado.es/"


def obtener_fecha_publicacion(entrada):
    for nombre in ("updated", "published", "IssueDate"):
        for elemento in buscar_descendientes(entrada, nombre):
            fecha = convertir_fecha(texto_elemento(elemento))
            if fecha:
                return fecha

    return None


def obtener_adjudicatario(resultado):
    nombres = []
    identificadores = []

    for zona in buscar_descendientes(resultado, ["WinningParty", "ContractorParty"]):
        for elemento in buscar_descendientes(zona, ["Name", "RegistrationName"]):
            nombre = texto_elemento(elemento)
            if nombre and nombre not in nombres:
                nombres.append(nombre)

        for elemento in buscar_descendientes(zona, ["CompanyID", "ID"]):
            identificador = texto_elemento(elemento)
            if (
                identificador
                and identificador not in identificadores
                and identificador not in nombres
            ):
                identificadores.append(identificador)

    if not nombres:
        for parte in buscar_descendientes(resultado, "PartyName"):
            nombre = texto_descendiente(parte, "Name")
            if nombre and nombre not in nombres:
                nombres.append(nombre)

    adjudicatarios = []
    for posicion, nombre in enumerate(nombres):
        texto = nombre
        if posicion < len(identificadores):
            identificador = identificadores[posicion]
            if identificador not in nombre:
                texto = f"{nombre} ({identificador})"

        if texto not in adjudicatarios:
            adjudicatarios.append(texto)

    return " / ".join(adjudicatarios)


def obtener_importe_resultado(resultado):
    prioridades = ("PayableAmount", "TaxExclusiveAmount", "TotalAmount", "AwardedAmount")

    for nombre in prioridades:
        importes = []
        for elemento in buscar_descendientes(resultado, nombre):
            moneda = limpiar_texto(elemento.get("currencyID", "EUR")).upper()
            if moneda and moneda != "EUR":
                continue

            importe = convertir_importe(texto_elemento(elemento))
            if importe is not None:
                importes.append(importe)

        if importes:
            return max(importes)

    return None


def obtener_fecha_adjudicacion(resultado):
    for nombre in ("AwardDate", "ContractAwardDate", "DecisionDate"):
        for elemento in buscar_descendientes(resultado, nombre):
            fecha = convertir_fecha(texto_elemento(elemento))
            if fecha:
                return fecha

    return None


def obtener_resultados_validos(entrada):
    validos = []

    for resultado in buscar_descendientes(entrada, "TenderResult"):
        adjudicatario = obtener_adjudicatario(resultado)
        importe = obtener_importe_resultado(resultado)
        fecha = obtener_fecha_adjudicacion(resultado)

        codigo = buscar_descendiente(resultado, ["ResultCode", "TenderResultCode"])
        estado = texto_elemento(codigo).upper()
        if estado and estado not in {
            "8",
            "9",
            "ADJ",
            "ADJUDICADO",
            "ADJUDICADA",
            "FORMALIZADO",
            "FORMALIZADA",
            "AWARDED",
        }:
            continue
        if not estado and not adjudicatario:
            continue
        if importe is None or importe < IMPORTE_MINIMO:
            continue

        lote = texto_descendiente(resultado, "ProcurementProjectLotID")
        if not lote:
            proyecto_adjudicado = buscar_descendiente(resultado, "AwardedTenderedProject")
            lote = texto_descendiente(proyecto_adjudicado, "ID")

        validos.append(
            {
                "adjudicatario": adjudicatario or "Adjudicatario no publicado",
                "importe": importe,
                "fecha": fecha,
                "lote": lote,
            }
        )

    return validos


def convertir_entrada(entrada, detectada):
    expediente = obtener_expediente(entrada)
    objeto = obtener_objeto(entrada)
    organo = obtener_organo(entrada)
    url = obtener_url_licitacion(entrada)
    fecha_publicacion = obtener_fecha_publicacion(entrada)
    identidad_expediente = entrada.findtext(f"{ATOM}id") or f"{organo}|{expediente}"

    noticias = []
    for resultado in obtener_resultados_validos(entrada):
        adjudicatario = resultado["adjudicatario"]
        importe = resultado["importe"]
        fecha = resultado["fecha"]
        lote = resultado["lote"]

        fecha_texto = formatear_fecha(fecha) or "Fecha de adjudicación no publicada"
        objeto_resultado = f"{objeto} (lote {lote})" if lote else objeto
        importe_texto = formatear_importe(importe)

        titulo = (
            f"ADJUDICADA ESPAÑA | {fecha_texto} | {adjudicatario} | "
            f"{importe_texto} | {objeto_resultado}"
        )

        descripcion = [
            "<p><strong>Estado:</strong> ADJUDICADA ESPAÑA</p>",
            f"<p><strong>Fecha de adjudicación:</strong> {html.escape(fecha_texto)}</p>",
            f"<p><strong>Adjudicatario:</strong> {html.escape(adjudicatario)}</p>",
            f"<p><strong>Importe adjudicado:</strong> {html.escape(importe_texto)}</p>",
            f"<p><strong>Objeto:</strong> {html.escape(objeto_resultado)}</p>",
            f"<p><strong>Expediente:</strong> {html.escape(expediente)}</p>",
        ]

        if organo:
            descripcion.append(
                f"<p><strong>Órgano de contratación:</strong> {html.escape(organo)}</p>"
            )

        descripcion.append(
            f'<p><a href="{html.escape(url)}">Abrir adjudicación oficial</a></p>'
        )

        texto_identificador = (
            f"adjudicada-espana-v4|{expediente}|{fecha_texto}|{adjudicatario}|{importe:.2f}"
        )
        identificador_anterior = hashlib.sha256(
            texto_identificador.encode("utf-8")
        ).hexdigest()

        # Cada lote conserva su propio identificador.
        clave = f"{identidad_expediente}|{lote}|{adjudicatario.casefold()}"
        identificador = hashlib.sha256(clave.encode("utf-8")).hexdigest()

        noticias.append(
            {
                "id": identificador,
                "id_anterior": identificador_anterior,
                "detectada": detectada.isoformat(),
                "titulo": titulo,
                "url": url,
                "descripcion": "".join(descripcion),
                "fecha_adjudicacion": fecha.isoformat() if fecha else None,
                "fecha_publicacion": fecha_publicacion.isoformat() if fecha_publicacion else None,
                "expediente": expediente,
                "adjudicatario": adjudicatario,
                "importe": importe,
                "objeto": objeto,
            }
        )

    return noticias


def cargar_historial():
    if not ARCHIVO_HISTORIAL.exists():
        return []

    try:
        contenido = json.loads(ARCHIVO_HISTORIAL.read_text(encoding="utf-8"))
        if isinstance(contenido, list):
            return contenido
    except (OSError, json.JSONDecodeError) as error:
        print(f"No se pudo leer el historial: {error}")

    return []


def guardar_historial(noticias):
    ARCHIVO_HISTORIAL.write_text(
        json.dumps(noticias, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fecha_rss_noticia(noticia):
    return convertir_fecha(
        noticia.get("detectada")
        or noticia.get("fecha_adjudicacion")
        or noticia.get("fecha_publicacion")
    )


def mezclar_noticias(nuevas, anteriores):
    noticias_por_id = {noticia["id"]: noticia for noticia in anteriores}
    claves = {noticia.get("clave", noticia["id"]): noticia["id"] for noticia in anteriores}

    for noticia in nuevas:
        clave = noticia["id"]
        anterior = noticias_por_id.get(noticia.pop("id_anterior"))

        # Reutiliza el GUID antiguo cuando corresponde al mismo expediente.
        if anterior and "clave" not in anterior and anterior.get("url") == noticia["url"]:
            claves.setdefault(clave, anterior["id"])

        identificador = claves.setdefault(clave, clave)
        anterior = noticias_por_id.get(identificador)

        if anterior and "clave" in anterior:
            if anterior.get("fecha_adjudicacion") or not noticia.get("fecha_adjudicacion"):
                continue

        noticia["clave"] = clave
        noticia["id"] = identificador

        if anterior:
            fecha_anterior = fecha_rss_noticia(anterior)
            publicacion = convertir_fecha(anterior.get("fecha_publicacion"))
            if not publicacion or (publicacion.date() - fecha_anterior.date()).days <= 3:
                noticia["detectada"] = fecha_anterior.isoformat()

        noticias_por_id[identificador] = noticia

    return sorted(noticias_por_id.values(), key=fecha_rss_noticia, reverse=True)


def crear_rss(noticias, ahora):
    hoy = ahora.astimezone(ZONA_HORARIA).date()
    primer_dia = hoy - timedelta(days=DIAS_RSS - 1)

    # Solo entran adjudicaciones con fecha oficial dentro del plazo.
    recientes = []
    for noticia in noticias:
        fecha = convertir_fecha(noticia.get("fecha_adjudicacion"))
        if fecha and primer_dia <= fecha.date() <= hoy:
            recientes.append((fecha, noticia))

    recientes.sort(key=lambda par: (par[0], par[1]["id"]), reverse=True)
    print(f"Entradas recientes publicadas en RSS: {len(recientes)}")

    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
    rss = ET.Element("rss", {"version": "2.0"})
    canal = ET.SubElement(rss, "channel")

    ET.SubElement(canal, "title").text = "Adjudicaciones España superiores a 500.000 €"
    ET.SubElement(canal, "link").text = "https://contrataciondelestado.es/"
    ET.SubElement(canal, "description").text = (
        "Adjudicaciones españolas por importe igual o superior a 500.000 euros."
    )
    ET.SubElement(canal, "language").text = "es-ES"
    ET.SubElement(canal, "lastBuildDate").text = fecha_para_rss(datetime.now(timezone.utc))
    ET.SubElement(canal, "ttl").text = "60"

    ET.SubElement(
        canal,
        f"{ATOM}link",
        {"href": URL_RSS, "rel": "self", "type": "application/rss+xml"},
    )

    for fecha, noticia in recientes:
        entrada = ET.SubElement(canal, "item")
        ET.SubElement(entrada, "title").text = noticia["titulo"]
        ET.SubElement(entrada, "link").text = noticia["url"]
        ET.SubElement(entrada, "guid", {"isPermaLink": "false"}).text = noticia["id"]
        ET.SubElement(entrada, "pubDate").text = format_datetime(fecha)
        ET.SubElement(entrada, "description").text = noticia["descripcion"]
        ET.SubElement(entrada, "category").text = "ADJUDICADA ESPAÑA"
        ET.SubElement(entrada, "category").text = "Contratación pública"
        ET.SubElement(entrada, "category").text = "Más de 500.000 euros"

    arbol = ET.ElementTree(rss)
    ET.indent(arbol, space="  ")
    arbol.write(ARCHIVO_RSS, encoding="utf-8", xml_declaration=True)
    ET.parse(ARCHIVO_RSS)
    print("El archivo feed.xml es un XML válido.")


def main():
    print("========================================")
    print("RSS DE ADJUDICACIONES DE ESPAÑA")
    print("========================================")

    ahora = datetime.now(ZONA_HORARIA)
    limite = ahora - timedelta(days=DIAS_RSS)
    ultimas_versiones = {}

    for entrada in descargar_entradas(limite):
        identificador = entrada.findtext(f"{ATOM}id")
        fecha = obtener_fecha_publicacion(entrada) or limite
        clave = identificador or (obtener_url_licitacion(entrada), obtener_expediente(entrada))
        anterior = ultimas_versiones.get(clave)

        if anterior is None or fecha > anterior[0]:
            ultimas_versiones[clave] = (fecha, entrada)

    versiones_ordenadas = sorted(
        ultimas_versiones.values(),
        key=lambda par: par[0],
        reverse=True,
    )
    nuevas = [
        noticia
        for _, entrada in versiones_ordenadas
        for noticia in convertir_entrada(entrada, ahora)
    ]

    anteriores = cargar_historial()
    resultado = mezclar_noticias(nuevas, anteriores)
    crear_rss(resultado, ahora)
    guardar_historial(resultado)

    print(f"Adjudicaciones incorporadas al historial: {len(resultado) - len(anteriores)}")
    print(f"URL para Feedly: {URL_RSS}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Proceso cancelado.", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(f"ERROR GENERAL: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
