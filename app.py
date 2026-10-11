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
