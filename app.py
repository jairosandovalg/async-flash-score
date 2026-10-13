import asyncio
import os
import re
import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# --- CONFIGURACIÓN ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8923959866:AAES1dc4LAsedUKUsGR4p5D1SkaMt7nKyes")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7272170952")
CONCURRENCIA_MAXIMA = 3  # Ideal para límites de CPU y RAM
ARCHIVO_ALERTADOS = "alertados.txt"


# --- PERSISTENCIA / MEMORIA ---

def cargar_alertados() -> set:
    """Lee los IDs de partidos previamente notificados."""
    if not os.path.exists(ARCHIVO_ALERTADOS):
        return set()
    try:
        with open(ARCHIVO_ALERTADOS, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except Exception as e:
        print(f"Error al leer {ARCHIVO_ALERTADOS}: {e}")
        return set()


def guardar_alertado(id_partido: str):
    """Registra un ID de partido notificado en el archivo de texto."""
    try:
        with open(ARCHIVO_ALERTADOS, "a", encoding="utf-8") as f:
            f.write(f"{id_partido}\n")
    except Exception as e:
        print(f"Error al guardar {id_partido} en {ARCHIVO_ALERTADOS}: {e}")


# --- PARSEADORES NUMÉRICOS ---

def a_decimal(val_str: str) -> float:
    if not val_str or val_str in ("-", "- - -"):
        return 0.0
    val_limpio = str(val_str).replace("%", "").replace(",", ".").strip()
    match = re.search(r"[-+]?\d*\.?\d+", val_limpio)
    try:
        return float(match.group()) if match else 0.0
    except (ValueError, AttributeError):
        return 0.0


def a_entero(val_str: str) -> int:
    if not val_str or val_str in ("-", "- - -"):
        return 0
    match = re.search(r"\d+", str(val_str))
    try:
        return int(match.group()) if match else 0
    except (ValueError, AttributeError):
        return 0


def parsear_cuotas_1x2(cuotas_str: str) -> dict:
    cuotas = {"1": 0.0, "X": 0.0, "2": 0.0}
    if not cuotas_str or cuotas_str == "- - -":
        return cuotas
    cuotas_limpio = cuotas_str.replace(",", ".")
    m = re.findall(r"(?:1|X|2):\s*([0-9\.]+)", cuotas_limpio)
    if len(m) == 3:
        cuotas["1"], cuotas["X"], cuotas["2"] = a_decimal(m[0]), a_decimal(m[1]), a_decimal(m[2])
    return cuotas


def parsear_marcador(marcador_str: str) -> tuple[int, int]:
    if not marcador_str or marcador_str == "- - -":
        return -1, -1
    marcador_limpio = re.sub(r"\(.*?\)", "", marcador_str)
    nums = re.findall(r"\b\d+\b", marcador_limpio)
    if len(nums) >= 2:
        return a_entero(nums[0]), a_entero(nums[1])
    return -1, -1


# --- TELEGRAM ---

async def enviar_alerta_telegram_async(session: aiohttp.ClientSession, mensaje: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            data = await resp.json()
            if not data.get("ok"):
                print(f"[ERROR TG] Respuesta de la API: {data.get('description')}")
            return resp.status == 200
    except Exception as e:
        print(f"Error Telegram: {e}")
        return False


# --- ESTADÍSTICAS ---

def parsear_bloque_estadisticas(soup_bloque) -> dict:
    stats = {}

    for fila in soup_bloque.select('div[data-testid="wcl-statistics"]'):
        etiqueta_el = fila.select_one(".wcl-label_sO4bA span, [data-testid='wcl-scores-simple-text-01'], .wcl-name_2lXWg")
        if not etiqueta_el:
            continue
        nombre = etiqueta_el.get_text(strip=True)
        val_l = fila.select_one(".wcl-labelRow_42JBQ > div:first-child .wcl-value_Ywp3J")
        val_v = fila.select_one(".wcl-awayValue_smmfR .wcl-value_Ywp3J")
        stats[f"{nombre} (L)"] = val_l.get_text(strip=True) if val_l else "-"
        stats[f"{nombre} (V)"] = val_v.get_text(strip=True) if val_v else "-"

    shot_container = soup_bloque.select_one('[class*="shotOnTargetStats_"]')
    if shot_container:
        for selector in ['[class*="offTargetBar_"]', '[class*="onTargetBar_"]']:
            barra = shot_container.select_one(selector)
            if barra:
                label = barra.select_one('[class*="wcl-label_"]')
                vals = barra.select('[class*="wcl-value_"]')
                if label:
                    nombre = label.get_text(strip=True)
                    stats[f"{nombre} (L)"] = vals[0].get_text(strip=True) if len(vals) > 0 else "-"
                    stats[f"{nombre} (V)"] = vals[-1].get_text(strip=True) if len(vals) > 1 else "-"

    for badge in soup_bloque.select('[class*="incidentValueBadge_"]'):
        svg = badge.select_one("svg")
        if not svg:
            continue
        test_id = svg.get("data-testid", "").lower()
        tipo = None
        if "corner" in test_id:
            tipo = "Córneres"
        elif "yellow-card" in test_id or "yellow" in test_id:
            tipo = "Tarjetas amarillas"
        elif "red-card" in test_id or "red" in test_id:
            tipo = "Tarjetas rojas"

        if tipo:
            spans = [s.get_text(strip=True) for s in badge.select("span") if s.get_text(strip=True)]
            stats[f"{tipo} (L)"] = spans[0] if len(spans) >= 1 else "-"
            stats[f"{tipo} (V)"] = spans[-1] if len(spans) >= 2 else "-"

    return stats


def formatear_mensaje_partido(reg: dict) -> str:
    stats = reg.get("Stats", {})
    metricas_fijas = [
        "Posesión de balón",
        "Goles esperados (xG)",
        "Remates a puerta",
        "Remates fuera",
        "Córneres",
        "Tarjetas amarillas"
    ]
    lineas = []
    procesadas = set()

    for m in metricas_fijas:
        kl = next((k for k in stats if m.lower() in k.lower() and "(L)" in k), None)
        kv = next((k for k in stats if m.lower() in k.lower() and "(V)" in k), None)
        if kl or kv:
            lineas.append(f"• <b>{m}:</b> {stats.get(kl, '-') if kl else '-'} | {stats.get(kv, '-') if kv else '-'}")
            if kl: procesadas.add(kl.replace(" (L)", ""))
            if kv: procesadas.add(kv.replace(" (V)", ""))

    for k, v in stats.items():
        base_name = k.replace(" (L)", "").replace(" (V)", "")
        if base_name not in procesadas and not any(f.lower() in base_name.lower() for f in metricas_fijas):
            val_l = stats.get(f"{base_name} (L)", "-")
            val_v = stats.get(f"{base_name} (V)", "-")
            lineas.append(f"• <b>{base_name}:</b> {val_l} | {val_v}")
            procesadas.add(base_name)

    stats_texto = ("\n\n📊 <b>Estadísticas Principales (L | V):</b>\n" + "\n".join(lineas)) if lineas else "\n\n📊 <i>Sin estadísticas en directo disponibles</i>"
    tipo_alerta = reg.get("Tipo_Alerta", "SEÑAL DE VALOR EN VIVO")
    recomendacion = reg.get("Recomendacion", "Revisar mercado")

    return (
        f"🎯 <b>SEÑAL: {tipo_alerta}</b>\n\n"
        f"💡 <b>Acción recomendada:</b> {recomendacion}\n\n"
        f"⚔️ <b>Partido:</b> {reg['Partido en Vivo']}\n"
        f"🔢 <b>Marcador:</b> {reg['Marcador']}\n"
        f"⏱ <b>Minuto:</b> {reg['Minuto']} ({reg['Tiempo/Estado']})\n"
        f"📈 <b>Cuotas (1X2):</b> {reg['Cuotas']}"
        f"{stats_texto}"
    )


# --- CRITERIOS DE ALERTA ---

def cumple_criterios_alerta(partido: dict) -> bool:
    nombre = partido.get("Partido en Vivo", "Desconocido")
    estado = partido.get("Tiempo/Estado", "").upper()

    estados_excluidos = ["FINALIZADO", "FIN", "FT", "APL.", "POSTP.", "DESCANSO", "DESC."]
    if any(ex in estado for ex in estados_excluidos):
        return False

    minuto_int = a_entero(partido.get("Minuto", ""))
    es_ventana_valida = minuto_int >= 40 or any(t in estado for t in ["2T", "2º", "2ND", "PRÓRROGA", "PRORROGA", "ET"])
    if not es_ventana_valida:
        return False

    marcador_str = partido.get("Marcador", "")
    gol_l, gol_v = parsear_marcador(marcador_str)
    
    # Condición estricta: Únicamente marcador 0 a 0
    if gol_l != 0 or gol_v != 0:
        return False

    cuotas_dict = parsear_cuotas_1x2(partido.get("Cuotas", ""))
    cuota_x = cuotas_dict["X"]

    if cuota_x <= 2.00:
        return False

    partido["Tipo_Alerta"] = "APOSTAR AL EMPATE (0-0)"
    partido["Recomendacion"] = f"Apostar al Empate (Cuota X: {cuota_x:.2f} | Minuto: {minuto_int}' | Marcador: {gol_l}-{gol_v})"
    print(f"✅ [SEÑAL VÁLIDA: EMPATE 0-0] {nombre} -> {gol_l}-{gol_v} | Min: {minuto_int} | Cuota X: {cuota_x:.2f}")
    return True


# --- NAVEGACIÓN Y EXTRACCIÓN ---

async def procesar_partido(context, p_div_data: dict, semaforo: asyncio.Semaphore, tg_session: aiohttp.ClientSession, ya_alertados: set):
    # Descarte temprano si ya fue alertado previamente
    if p_div_data["id"] in ya_alertados:
        return

    async with semaforo:
        url_partido = f"https://www.flashscore.pe/partido/{p_div_data['id']}/"
        url_stats = f"{url_partido}#/resumen-del-partido/estadisticas-del-partido/0"
        page = None

        datos_partido = {
            "Partido en Vivo": p_div_data["nombre"],
            "Marcador": "- - -",
            "Cuotas": "- - -",
            "Tiempo/Estado": "-",
            "Minuto": "-",
            "Stats": {}
        }

        try:
            page = await context.new_page()
            await page.goto(url_partido, timeout=25000, wait_until="domcontentloaded")

            try:
                await page.wait_for_selector("div.detailScore__wrapper", timeout=4000)
            except Exception:
                pass

            try:
                await page.wait_for_selector("[data-testid='wcl-oddsValue']", timeout=3500)
            except Exception:
                pass

            soup_resumen = BeautifulSoup(await page.content(), "html.parser")

            score = soup_resumen.select_one("div.detailScore__wrapper")
            if score:
                datos_partido["Marcador"] = score.get_text(separator=" ", strip=True)

            status = soup_resumen.select_one("span.fixedHeaderDuel__detailStatus")
            if status:
                datos_partido["Tiempo/Estado"] = status.get_text(strip=True)

            minuto = soup_resumen.select_one("span.eventTime")
            if minuto:
                datos_partido["Minuto"] = minuto.get_text(strip=True)

            spans_odds = soup_resumen.select("[data-testid='wcl-oddsValue']")
            valores_cuotas = [s.get_text(strip=True) for s in spans_odds if s.get_text(strip=True)]
            if len(valores_cuotas) >= 3:
                datos_partido["Cuotas"] = f"1: {valores_cuotas[0]} | X: {valores_cuotas[1]} | 2: {valores_cuotas[2]}"

            gol_l, gol_v = parsear_marcador(datos_partido["Marcador"])
            minuto_int = a_entero(datos_partido["Minuto"])
            estado_txt = datos_partido["Tiempo/Estado"].upper()
            es_valido_tiempo = minuto_int >= 40 or any(t in estado_txt for t in ["2T", "2º", "2ND", "PRÓRROGA", "ET"])

            # Filtro temprano de recursos: si no es 0-0 o no cumple minuto, descartar sin cargar estadísticas
            if (gol_l != 0 or gol_v != 0) or not es_valido_tiempo:
                return

            # Navegación a estadísticas
            tab_stats = page.locator('a[data-analytics-alias="match-statistics"], a:has-text("ESTADÍSTICAS"), button:has-text("ESTADÍSTICAS")').first
            pudo_clicar = False

            if await tab_stats.count() > 0 and await tab_stats.is_visible():
                try:
                    await tab_stats.click(timeout=1500)
                    pudo_clicar = True
                except Exception:
                    pudo_clicar = False

            if not pudo_clicar:
                await page.goto(url_stats, timeout=15000, wait_until="domcontentloaded")

            try:
                await page.wait_for_selector('div[data-testid="statGroup"], div[data-testid="wcl-statistics"]', timeout=3000)
            except Exception:
                pass

            soup_stats = BeautifulSoup(await page.content(), "html.parser")
            grupos = soup_stats.select('div[data-testid="statGroup"]')
            bloque_stats = None

            for grupo in grupos:
                cabecera = grupo.select_one('[data-testid="wcl-headerSection-text"], [class*="header"]')
                if cabecera and "principal" in cabecera.get_text(strip=True).lower():
                    bloque_stats = grupo
                    break

            if not bloque_stats:
                bloque_stats = grupos[0] if grupos else soup_stats.select_one('div[data-testid="match-history"]')

            if bloque_stats:
                datos_partido["Stats"] = parsear_bloque_estadisticas(bloque_stats)

            if cumple_criterios_alerta(datos_partido):
                msg = formatear_mensaje_partido(datos_partido)
                enviado = await enviar_alerta_telegram_async(tg_session, msg)
                if enviado:
                    guardar_alertado(p_div_data["id"])
                    ya_alertados.add(p_div_data["id"])
                    print(f"🚀 Señal enviada y guardada con éxito: {p_div_data['nombre']}")
                else:
                    print(f"❌ Falló el envío a Telegram para {p_div_data['nombre']}")

        except Exception as e:
            print(f"Aviso en partido {p_div_data['nombre']}: {e}")
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass


async def ejecutar_escaneo_async():
    print("Iniciando escaneo asíncrono en Flashscore...")
    ya_alertados = cargar_alertados()
    print(f"Partidos previamente alertados en memoria: {len(ya_alertados)}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--blink-settings=imagesEnabled=false"  # Desactiva imágenes
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        main = await context.new_page()

        try:
            await main.goto("https://www.flashscore.pe/", timeout=35000, wait_until="domcontentloaded")
            btn_live = "//div[contains(@class, 'filters__text') and text()='EN DIRECTO']"
            await main.wait_for_selector(btn_live, timeout=15000)
            await main.locator(btn_live).click()
            await main.wait_for_timeout(3000)

            soup = BeautifulSoup(await main.content(), "html.parser")
            partidos_divs = soup.find_all("div", id=lambda x: x and x.startswith("g_1_"))

            if not partidos_divs:
                print("No se encontraron partidos en vivo.")
                return

            partidos_data = []
            for p_div in partidos_divs:
                id_p = p_div.get('id').split('_')[-1]
                h = p_div.find("div", class_=lambda c: c and "home" in c.lower() and "participant" in c.lower())
                a = p_div.find("div", class_=lambda c: c and "away" in c.lower() and "participant" in c.lower())
                partidos_data.append({
                    "id": id_p,
                    "nombre": f"{h.get_text(strip=True) if h else 'Local'} vs {a.get_text(strip=True) if a else 'Visitante'}"
                })

            print(f"Partidos en vivo detectados: {len(partidos_data)}. Procesando...")
            await main.close()

            semaforo = asyncio.Semaphore(CONCURRENCIA_MAXIMA)
            async with aiohttp.ClientSession() as tg_session:
                tareas = [
                    procesar_partido(context, p_data, semaforo, tg_session, ya_alertados)
                    for p_data in partidos_data
                ]
                await asyncio.gather(*tareas, return_exceptions=True)

        finally:
            await browser.close()
            print("Escaneo finalizado correctamente.")


if __name__ == "__main__":
    asyncio.run(ejecutar_escaneo_async())
