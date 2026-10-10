import asyncio
import os
import re
import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# --- CONFIGURACIÓN ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8923959866:AAES1dc4LAsedUKUsGR4p5D1SkaMt7nKyes")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7272170952")
CONCURRENCIA_MAXIMA = 3


# --- PARSEADORES NUMÉRICOS Y AUXILIARES ---

def extraer_numero(val_str: str) -> float:
    """Extrae el primer número flotante o entero de una cadena de texto."""
    if not val_str or val_str == "-":
        return 0.0
    val_limpio = val_str.replace("%", "").strip()
    match = re.search(r"[-+]?\d*\.?\d+", val_limpio)
    return float(match.group()) if match else 0.0


def parsear_cuotas_1x2(cuotas_str: str) -> dict:
    """Extrae las cuotas {'1': float, 'X': float, '2': float}."""
    cuotas = {"1": 0.0, "X": 0.0, "2": 0.0}
    if not cuotas_str or cuotas_str == "- - -":
        return cuotas
    m = re.findall(r"(?:1|X|2):\s*([0-9\.]+)", cuotas_str)
    if len(m) == 3:
        cuotas["1"], cuotas["X"], cuotas["2"] = float(m[0]), float(m[1]), float(m[2])
    return cuotas


def parsear_marcador(marcador_str: str) -> tuple[int, int]:
    """Extrae los goles (local, visitante) del marcador."""
    nums = re.findall(r"\d+", marcador_str)
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    return -1, -1


def obtener_minuto_int(minuto_str: str) -> int:
    """Extrae el minuto en valor entero."""
    m = re.search(r"\d+", minuto_str)
    return int(m.group()) if m else 0


# --- COMUNICACIÓN TELEGRAM ---

async def enviar_alerta_telegram_async(session: aiohttp.ClientSession, mensaje: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Error Telegram: {e}")
        return False


# --- EXTRACCIÓN Y FORMATEO ---

def parsear_bloque_estadisticas(soup_bloque) -> dict:
    stats = {}

    # 1. Filas métricas estándar (Posesión, xG, Toques en área rival, etc.)
    for fila in soup_bloque.select('div[data-testid="wcl-statistics"]'):
        etiqueta_el = fila.select_one(".wcl-label_sO4bA span, [data-testid='wcl-scores-simple-text-01'], .wcl-name_2lXWg")
        if not etiqueta_el:
            continue
        nombre = etiqueta_el.get_text(strip=True)
        val_l = fila.select_one(".wcl-labelRow_42JBQ > div:first-child .wcl-value_Ywp3J")
        val_v = fila.select_one(".wcl-awayValue_smmfR .wcl-value_Ywp3J")
        stats[f"{nombre} (L)"] = val_l.get_text(strip=True) if val_l else "-"
        stats[f"{nombre} (V)"] = val_v.get_text(strip=True) if val_v else "-"

    # 2. Remates fuera y Remates a puerta
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

    # 3. Badges SVG (Córneres, Tarjetas amarillas y rojas)
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
        "Tarjetas amarillas",
        "Tarjetas rojas",
        "Toques en el área rival"
    ]
    lineas = []
    procesadas = set()

    for m in metricas_fijas:
        kl = next((k for k in stats if m.lower() in k.lower() and "(L)" in k), None)
        kv = next((k for k in stats if m.lower() in k.lower() and "(V)" in k), None)
        lineas.append(f"• <b>{m}:</b> {stats.get(kl, '-') if kl else '-'} | {stats.get(kv, '-') if kv else '-'}")
        if kl:
            procesadas.add(kl.replace(" (L)", ""))
        if kv:
            procesadas.add(kv.replace(" (V)", ""))

    for k, v in stats.items():
        base_name = k.replace(" (L)", "").replace(" (V)", "")
        if base_name not in procesadas and not any(f.lower() in base_name.lower() for f in metricas_fijas):
            val_l = stats.get(f"{base_name} (L)", "-")
            val_v = stats.get(f"{base_name} (V)", "-")
            lineas.append(f"• <b>{base_name}:</b> {val_l} | {val_v}")
            procesadas.add(base_name)

    stats_texto = "\n\n📊 <b>Estadísticas Principales (L | V):</b>\n" + "\n".join(lineas)
    recomendacion = reg.get("Recomendacion", "Revisar mercado")

    return (
        f"🎯 <b>SEÑAL DE VALOR EN VIVO</b>\n\n"
        f"💡 <b>Estrategia Sugerida:</b> {recomendacion}\n\n"
        f"⚔️ <b>Partido:</b> {reg['Partido en Vivo']}\n"
        f"🔢 <b>Marcador:</b> {reg['Marcador']}\n"
        f"⏱ <b>Minuto:</b> {reg['Minuto']} ({reg['Tiempo/Estado']})\n"
        f"📈 <b>Cuotas (1X2):</b> {reg['Cuotas']}"
        f"{stats_texto}"
    )


# --- LÓGICA DE FILTROS Y CRITERIOS CUANTITATIVOS ---

def cumple_criterios_alerta(partido: dict) -> bool:
    estado = partido.get("Tiempo/Estado", "").upper()
    estados_excluidos = ["FINALIZADO", "FIN", "FT", "APL.", "POSTP.", "DESCANSO", "DESC."]
    if any(ex in estado for ex in estados_excluidos):
        return False

    marcador = partido.get("Marcador", "")
    if not marcador or marcador == "- - -":
        return False

    gol_l, gol_v = parsear_marcador(marcador)
    if gol_l == -1:
        return False

    # Filtro temporal: Minuto 48 al 78 (Segundo tiempo con volumen estadístico consolidado)
    minuto = obtener_minuto_int(partido.get("Minuto", ""))
    if minuto < 48 or minuto > 78:
        return False

    stats = partido.get("Stats", {})
    if not stats:
        return False

    def get_stat(nombre_parcial, lado):
        k = next((k for k in stats if nombre_parcial.lower() in k.lower() and f"({lado})" in k), None)
        return extraer_numero(stats.get(k, "0")) if k else 0.0

    remates_puerta_l = get_stat("puerta", "L")
    remates_puerta_v = get_stat("puerta", "V")
    xg_l = get_stat("xg", "L")
    xg_v = get_stat("xg", "V")
    corners_l = get_stat("córner", "L")
    corners_v = get_stat("córner", "V")
    posesion_l = get_stat("posesión", "L")
    posesion_v = get_stat("posesión", "V")
    rojas_l = get_stat("rojas", "L")
    rojas_v = get_stat("rojas", "V")
    toques_area_l = get_stat("toques en el área", "L")
    toques_area_v = get_stat("toques en el área", "V")

    cuotas = parsear_cuotas_1x2(partido.get("Cuotas", ""))

    # CONDICIÓN A: Empate con Dominio Ofensivo Local y Cuota Rentable (>= 1.90)
    #if gol_l == gol_v and rojas_l == 0:
        #dif_puerta = remates_puerta_l - remates_puerta_v
        #dif_xg = xg_l - xg_v
        #dif_corners = corners_l - corners_v
        #dif_toques = toques_area_l - toques_area_v

        #if dif_puerta >= 3 and dif_xg >= 0.50 and dif_corners >= 3 and posesion_l >= 58.0 and dif_toques >= 6:
            #if cuotas["1"] >= 1.90:
                #partido["Recomendacion"] = f"Apostar Gana Local (Cuota actual: {cuotas['1']})"
                #return True

    # CONDICIÓN B: Empate con Dominio Ofensivo Visitante y Cuota Rentable (>= 1.90)
    #if gol_l == gol_v and rojas_v == 0:
        #dif_puerta = remates_puerta_v - remates_puerta_l
        #dif_xg = xg_v - xg_l
        #dif_corners = corners_v - corners_l
        #dif_toques = toques_area_v - toques_area_l

        #if dif_puerta >= 3 and dif_xg >= 0.50 and dif_corners >= 3 and posesion_v >= 58.0 and dif_toques >= 6:
            #if cuotas["2"] >= 1.90:
                #partido["Recomendacion"] = f"Apostar Gana Visitante (Cuota actual: {cuotas['2']})"
                #return True

    # CONDICIÓN C: Asedio Ofensivo Alto para Mercado de Goles (Over / Próximo Gol)
    # Total xG alto acumulado y volumen constante de remates pero marcador con pocos goles
    #total_goles = gol_l + gol_v
    #if total_goles <= 2 and (xg_l + xg_v) >= 2.10 and (remates_puerta_l + remates_puerta_v) >= 7:
        #equipo_presionando = "Local" if xg_l > xg_v else "Visitante"
        #partido["Recomendacion"] = f"Próximo Gol / Over Goles adicional (Presión alta de {equipo_presionando})"
        #return True

    return False


# --- NAVEGACIÓN Y EXTRACCIÓN CON PLAYWRIGHT ---

async def procesar_partido(context, p_div_data: dict, semaforo: asyncio.Semaphore, tg_session: aiohttp.ClientSession):
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

            # 1. Cargar vista principal (Marcador, Minuto y Cuotas)
            await page.goto(url_partido, timeout=30000, wait_until="domcontentloaded")

            try:
                await page.wait_for_selector("div.detailScore__wrapper", timeout=6000)
            except Exception:
                pass

            try:
                await page.wait_for_selector("[data-testid='wcl-oddsValue']", timeout=4000)
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

            # Extraer cuotas 1X2
            botones = soup_resumen.find_all("button", attrs={"data-analytics-bookmaker-id": "660"})
            valores_cuotas = []
            for btn in botones:
                span = btn.find("span", {"data-testid": "wcl-oddsValue"})
                if span and span.get_text(strip=True):
                    valores_cuotas.append(span.get_text(strip=True))
                if len(valores_cuotas) == 3:
                    break

            if len(valores_cuotas) >= 3:
                datos_partido["Cuotas"] = f"1: {valores_cuotas[0]} | X: {valores_cuotas[1]} | 2: {valores_cuotas[2]}"

            # 2. Navegación a ESTADÍSTICAS
            tab_stats = page.locator('a[data-analytics-alias="match-statistics"], a:has-text("ESTADÍSTICAS"), button:has-text("ESTADÍSTICAS")').first
            pudo_clicar = False

            if await tab_stats.count() > 0 and await tab_stats.is_visible():
                try:
                    await tab_stats.click(timeout=3000)
                    pudo_clicar = True
                except Exception:
                    pudo_clicar = False

            if not pudo_clicar:
                await page.goto(url_stats, timeout=20000, wait_until="domcontentloaded")

            # 3. Esperar que los nodos de estadísticas aparezcan en el DOM
            try:
                await page.wait_for_selector('div[data-testid="statGroup"], div[data-testid="wcl-statistics"]', timeout=7000)
                await page.wait_for_timeout(800)
            except Exception:
                await page.wait_for_timeout(1500)

            soup_stats = BeautifulSoup(await page.content(), "html.parser")
            bloque_stats = None
            grupos = soup_stats.select('div[data-testid="statGroup"]')

            for grupo in grupos:
                cabecera = grupo.select_one('[data-testid="wcl-headerSection-text"], [class*="header"]')
                if cabecera and "principal" in cabecera.get_text(strip=True).lower():
                    bloque_stats = grupo
                    break

            if not bloque_stats:
                bloque_stats = grupos[0] if grupos else soup_stats.select_one('div[data-testid="match-history"]') or soup_stats

            if bloque_stats:
                datos_partido["Stats"] = parsear_bloque_estadisticas(bloque_stats)

            # 4. Validar filtros y disparar notificación
            if cumple_criterios_alerta(datos_partido):
                msg = formatear_mensaje_partido(datos_partido)
                await enviar_alerta_telegram_async(tg_session, msg)
                print(f"✓ Señal enviada a Telegram: {p_div_data['nombre']}")
            else:
                print(f"Descartado por filtros: {p_div_data['nombre']} | Min: {datos_partido['Minuto']} | Marcador: {datos_partido['Marcador']}")

        except Exception as e:
            print(f"Error procesando {p_div_data['nombre']}: {e}")
        finally:
            if page:
                await page.close()


async def ejecutar_escaneo_async():
    print("Iniciando escaneo asíncrono en Flashscore...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
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
                tareas = [procesar_partido(context, p_data, semaforo, tg_session) for p_data in partidos_data]
                await asyncio.gather(*tareas)

        finally:
            await browser.close()
            print("Escaneo finalizado.")


if __name__ == "__main__":
    asyncio.run(ejecutar_escaneo_async())
