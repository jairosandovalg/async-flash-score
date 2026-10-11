import asyncio
import os
import re
import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# --- CONFIGURACIÓN ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8923959866:AAES1dc4LAsedUKUsGR4p5D1SkaMt7nKyes")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7272170952")
CONCURRENCIA_MAXIMA = 8


# --- PARSEADORES NUMÉRICOS (CONVERSIÓN CONTROLADA) ---

def a_decimal(val_str: str) -> float:
    """Limpia y convierte a número flotante manejando comas o puntos."""
    if not val_str or val_str in ("-", "- - -"):
        return 0.0
    val_limpio = str(val_str).replace("%", "").replace(",", ".").strip()
    match = re.search(r"[-+]?\d*\.?\d+", val_limpio)
    try:
        return float(match.group()) if match else 0.0
    except (ValueError, AttributeError):
        return 0.0


def a_entero(val_str: str) -> int:
    """Extrae el primer número entero de una cadena."""
    if not val_str or val_str in ("-", "- - -"):
        return 0
    match = re.search(r"\d+", str(val_str))
    try:
        return int(match.group()) if match else 0
    except (ValueError, AttributeError):
        return 0


def parsear_cuotas_1x2(cuotas_str: str) -> dict:
    """Extrae las cuotas {'1': float, 'X': float, '2': float}."""
    cuotas = {"1": 0.0, "X": 0.0, "2": 0.0}
    if not cuotas_str or cuotas_str == "- - -":
        return cuotas
    cuotas_limpio = cuotas_str.replace(",", ".")
    m = re.findall(r"(?:1|X|2):\s*([0-9\.]+)", cuotas_limpio)
    if len(m) == 3:
        cuotas["1"], cuotas["X"], cuotas["2"] = a_decimal(m[0]), a_decimal(m[1]), a_decimal(m[2])
    return cuotas


def parsear_marcador(marcador_str: str) -> tuple[int, int]:
    """Extrae los goles (local: int, visitante: int) limpiando tiempos parciales."""
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


# --- PARSEO DE BLOQUES HTML ---

def parsear_bloque_estadisticas(soup_bloque) -> dict:
    stats = {}

    # 1. Filas métricas estándar (Posesión, xG, Pases, Faltas, etc.)
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


# --- EVALUACIÓN FILTRO POR FILTRO CON CONVERSIÓN PREVIA ---

def cumple_criterios_alerta(partido: dict) -> bool:
    nombre = partido.get("Partido en Vivo", "Desconocido")
    estado = partido.get("Tiempo/Estado", "").upper()

    # FILTRO 1: Estado jugable (excluir descansos y partidos terminados o suspendidos)
    estados_excluidos = ["FINALIZADO", "FIN", "FT", "APL.", "POSTP.", "DESCANSO", "DESC."]
    if any(ex in estado for ex in estados_excluidos):
        print(f"[Filtro 1 NO CUMPLE] {nombre} | Estado no jugable: '{estado}'")
        return False

    # CONVERSIÓN: Minuto a entero
    minuto_int = a_entero(partido.get("Minuto", ""))

    # FILTRO 2: Ventana de tiempo (Minuto >= 40 o en 2T/Prórroga)
    es_ventana_valida = minuto_int >= 40 or any(t in estado for t in ["2T", "2º", "2ND", "PRÓRROGA", "PRORROGA", "ET"])
    if not es_ventana_valida:
        print(f"[Filtro 2 NO CUMPLE] {nombre} | Minuto inferior a 40 (Min: {minuto_int}', Estado: '{estado}')")
        return False

    # CONVERSIÓN: Marcador a enteros
    marcador_str = partido.get("Marcador", "")
    gol_l, gol_v = parsear_marcador(marcador_str)
    if gol_l == -1 or gol_v == -1:
        print(f"[Filtro 3 NO CUMPLE] {nombre} | Marcador ilegible: '{marcador_str}'")
        return False

    # CONVERSIÓN: Cuotas a float
    cuotas_dict = parsear_cuotas_1x2(partido.get("Cuotas", ""))
    cuota_x = cuotas_dict["X"]

    # =========================================================================
    # ESTRATEGIA 1: APOSTAR AL EMPATE (Minuto >= 40, empate y cuota X > 2.00)
    # =========================================================================
    if gol_l == gol_v and cuota_x > 2.00:
        partido["Tipo_Alerta"] = "APOSTAR AL EMPATE"
        partido["Recomendacion"] = (
            f"Apostar al Empate (Cuota X: {cuota_x:.2f} | Minuto: {minuto_int}' | Marcador: {gol_l}-{gol_v})"
        )
        print(f"✅ [SEÑAL: EMPATE] {nombre} -> {gol_l}-{gol_v} | Min: {minuto_int} | Cuota X: {cuota_x:.2f}")
        return True

    # =========================================================================
    # FUTURAS ESTRATEGIAS (Espacio preparado para Próximo Gol / Ofensiva):
    # =========================================================================
    # stats = partido.get("Stats", {})
    # tiros_puerta_l = a_entero(stats.get("Remates a puerta (L)", 0))
    # tiros_puerta_v = a_entero(stats.get("Remates a puerta (V)", 0))
    #
    # if gol_l == gol_v and (tiros_puerta_l + tiros_puerta_v >= 8):
    #     partido["Tipo_Alerta"] = "APOSTAR PRÓXIMO GOL / OFENSIVA"
    #     partido["Recomendacion"] = "Alta presión ofensiva en empate. Buscar Over o Próximo Gol."
    #     return True

    return False


# --- NAVEGACIÓN Y EXTRACCIÓN ---

async def procesar_partido(context, p_div_data: dict, semaforo: asyncio.Semaphore, tg_session: aiohttp.ClientSession):
    async with semaforo:
        url_partido = f"https://www.flashscore.pe/partido/{p_div_data['id']}/"
        url_stats = f"{url_partido}#/resumen-del-partido/estadisticas-del-partido/0"
        page = None

        datos_partido = {
            "Partido en Vivo": p_div_data["nombre"],
            "Marcador": p_div_data.get("marcador_previo", "- - -"),
            "Cuotas": "- - -",
            "Tiempo/Estado": p_div_data.get("estado_previo", "-"),
            "Minuto": p_div_data.get("minuto_previo", "-"),
            "Stats": {}
        }

        try:
            page = await context.new_page()

            # Bloqueo de recursos pesados para acelerar la navegación
            await page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ["image", "stylesheet", "font", "media"]
                else route.continue_()
            )

            await page.goto(url_partido, timeout=12000, wait_until="domcontentloaded")

            # Intentar obtener las cuotas
            try:
                await page.wait_for_selector("[data-testid='wcl-oddsValue']", timeout=2500)
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

            # Descartar temprano si la cuota de empate no supera 2.00
            cuotas_dict = parsear_cuotas_1x2(datos_partido["Cuotas"])
            if cuotas_dict["X"] <= 2.00:
                print(f"[DESCARTADO] {p_div_data['nombre']} | Cuota X ({cuotas_dict['X']}) <= 2.00")
                return

            # Cargar sección de estadísticas
            tab_stats = page.locator('a[data-analytics-alias="match-statistics"], a:has-text("ESTADÍSTICAS"), button:has-text("ESTADÍSTICAS")').first
            pudo_clicar = False

            if await tab_stats.count() > 0 and await tab_stats.is_visible():
                try:
                    await tab_stats.click(timeout=1500)
                    pudo_clicar = True
                except Exception:
                    pudo_clicar = False

            if not pudo_clicar:
                await page.goto(url_stats, timeout=10000, wait_until="domcontentloaded")

            try:
                await page.wait_for_selector('div[data-testid="statGroup"], div[data-testid="wcl-statistics"]', timeout=2500)
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

            # Evaluar y disparar alerta
            if cumple_criterios_alerta(datos_partido):
                msg = formatear_mensaje_partido(datos_partido)
                enviado = await enviar_alerta_telegram_async(tg_session, msg)
                if enviado:
                    print(f"🚀 Señal enviada a Telegram con éxito: {p_div_data['nombre']}")
                else:
                    print(f"❌ Falló el envío por HTTP a Telegram para {p_div_data['nombre']}")

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
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--blink-settings=imagesEnabled=false"
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        main = await context.new_page()

        try:
            await main.goto("https://www.flashscore.pe/", timeout=25000, wait_until="domcontentloaded")
            btn_live = "//div[contains(@class, 'filters__text') and text()='EN DIRECTO']"
            await main.wait_for_selector(btn_live, timeout=12000)
            await main.locator(btn_live).click()
            await main.wait_for_timeout(2000)

            soup = BeautifulSoup(await main.content(), "html.parser")
            partidos_divs = soup.find_all("div", id=lambda x: x and x.startswith("g_1_"))

            if not partidos_divs:
                print("No se encontraron partidos en vivo.")
                return

            candidatos = []
            for p_div in partidos_divs:
                id_p = p_div.get('id').split('_')[-1]
                h = p_div.find("div", class_=lambda c: c and "home" in c.lower() and "participant" in c.lower())
                a = p_div.find("div", class_=lambda c: c and "away" in c.lower() and "participant" in c.lower())

                score_home = p_div.find("span", class_=lambda c: c and "score--home" in c.lower())
                score_away = p_div.find("span", class_=lambda c: c and "score--away" in c.lower())
                stage_el = p_div.find("div", class_=lambda c: c and "stage" in c.lower())

                score_h_txt = score_home.get_text(strip=True) if score_home else ""
                score_a_txt = score_away.get_text(strip=True) if score_away else ""
                stage_txt = stage_el.get_text(strip=True) if stage_el else ""

                minuto_val = a_entero(stage_txt)
                es_2t_o_prorroga = any(t in stage_txt.upper() for t in ["2T", "2º", "2ND", "PRÓRROGA", "PRORROGA", "ET"])

                # Descartar si el marcador es visible y no están empatados
                if score_h_txt != "" and score_a_txt != "":
                    if a_entero(score_h_txt) != a_entero(score_a_txt):
                        continue

                # Descartar si no ha alcanzado el minuto 40 o el segundo tiempo
                if not (minuto_val >= 40 or es_2t_o_prorroga):
                    continue

                candidatos.append({
                    "id": id_p,
                    "nombre": f"{h.get_text(strip=True) if h else 'Local'} vs {a.get_text(strip=True) if a else 'Visitante'}",
                    "marcador_previo": f"{score_h_txt} - {score_a_txt}" if score_h_txt else "- - -",
                    "minuto_previo": str(minuto_val) if minuto_val > 0 else "-",
                    "estado_previo": stage_txt
                })

            print(f"Total en vivo: {len(partidos_divs)} | Candidatos válidos (>=40' y empate): {len(candidatos)}")
            await main.close()

            if not candidatos:
                print("Ningún partido cumple las condiciones previas.")
                return

            semaforo = asyncio.Semaphore(CONCURRENCIA_MAXIMA)
            async with aiohttp.ClientSession() as tg_session:
                tareas = [procesar_partido(context, p_data, semaforo, tg_session) for p_data in candidatos]
                await asyncio.gather(*tareas)

        finally:
            await browser.close()
            print("Escaneo finalizado.")


if __name__ == "__main__":
    asyncio.run(ejecutar_escaneo_async())
