"""Agente de inventario por CLI.

Bucle del agente implementado a mano (sin frameworks):
    Observar -> Pensar -> Actuar -> Actualizar -> Repetir

Requiere que la API esté en marcha (uvicorn api.app:app --reload) y una
GROQ_API_KEY en el fichero .env.

Arrancar con:  python agent.py
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from openai import APIError, OpenAI

load_dotenv()

# Evita errores de codificación con acentos en terminales de Windows.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

API_URL = os.getenv("INVENTORY_API_URL", "http://127.0.0.1:8000").rstrip("/")
MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
LOG_FILE = Path(os.getenv("CONVERSATION_LOG", Path(__file__).resolve().parent / "conversation_log.csv"))
LOG_FIELDS = ["actor", "message", "tool_call", "timestamp"]
MAX_STEPS = 10  # límite de iteraciones por mensaje para evitar bucles infinitos
EXIT_WORDS = {"salir", "exit", "quit", "adiós", "adios"}

SYSTEM_PROMPT = """Eres el asistente de inventario de la tienda de suministros para cafeterías de Carla \
(dos locales). Hablas en español, de forma breve, cercana y directa.

Puedes consultar y modificar el inventario con las herramientas disponibles. Reglas:
- Para actualizar el stock necesitas el product_id. Si no lo conoces, llama primero a list_inventory \
y busca el producto por nombre (la coincidencia puede ser aproximada: "arábica" = "Café arábica en grano").
- Llegadas, entregas o reposiciones => delta POSITIVO. Ventas, consumos, mermas o roturas => delta NEGATIVO.
- Si el producto no existe y Carla está registrando una llegada, créalo con add_product usando la cantidad \
recibida como cantidad inicial. Si el nombre es ambiguo entre varios productos, pregunta antes de actuar.
- Nunca inventes cantidades ni ids: usa siempre los datos que devuelven las herramientas.
- Si una herramienta devuelve un error, explícaselo a Carla con palabras sencillas.
- Tras modificar el stock, confirma la cantidad final con su unidad. Si queda por debajo de 10, avísalo."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_inventory",
            "description": (
                "Devuelve todos los productos del inventario con su id, nombre, cantidad y unidad. "
                "Úsala para consultar stock o para averiguar el product_id de un producto por su nombre."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_product",
            "description": "Registra un producto NUEVO que todavía no existe en el inventario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nombre del producto, p. ej. 'Leche de soja'"},
                    "quantity": {"type": "number", "description": "Cantidad inicial en stock (>= 0)"},
                    "unit": {
                        "type": "string",
                        "description": "Unidad de medida: unidades, kg, litros, bolsas, cajas, botellas...",
                    },
                },
                "required": ["name", "quantity", "unit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_stock",
            "description": (
                "Suma o resta stock a un producto existente. Usa delta positivo para entradas "
                "(llegadas, entregas) y negativo para salidas (ventas, consumo, mermas)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer", "description": "Id del producto (obtenlo con list_inventory)"},
                    "delta": {"type": "number", "description": "Cambio de cantidad: +30 para una llegada, -12 para una venta"},
                },
                "required": ["product_id", "delta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_low_stock_alerts",
            "description": "Devuelve los productos cuya cantidad está por debajo del umbral (por defecto 10).",
            "parameters": {
                "type": "object",
                "properties": {
                    "threshold": {
                        "type": "number",
                        "description": "Umbral de stock bajo. Omítelo para usar el valor por defecto (10).",
                    },
                },
                "required": [],
            },
        },
    },
]


# ---------- Registro de conversación ----------

def log_event(actor: str, message: str, tool_call: str = "") -> None:
    """Añade una fila a conversation_log.csv (solo adición, nunca sobreescribe)."""
    write_header = not LOG_FILE.exists() or LOG_FILE.stat().st_size == 0
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "actor": actor,
            "message": message,
            "tool_call": tool_call,
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        })


# ---------- Tools -> endpoints de la API ----------

http = httpx.Client(base_url=API_URL, timeout=10)


def _request(method: str, path: str, **kwargs) -> dict | list:
    try:
        response = http.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        return {"error": f"No se pudo conectar con la API de inventario ({exc.__class__.__name__})."}
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if response.is_error:
        return {"error": body.get("detail", body) if isinstance(body, dict) else body, "status": response.status_code}
    return body


def list_inventory() -> list | dict:
    return _request("GET", "/inventory")


def add_product(name: str, quantity: float, unit: str) -> dict:
    return _request("POST", "/inventory", json={"name": name, "quantity": quantity, "unit": unit})


def update_stock(product_id: int, delta: float) -> dict:
    return _request("PATCH", f"/inventory/{product_id}", json={"delta": delta})


def get_low_stock_alerts(threshold: float | None = None) -> list | dict:
    params = {"threshold": threshold} if threshold is not None else None
    return _request("GET", "/inventory/alerts", params=params)


TOOL_FUNCTIONS = {
    "list_inventory": list_inventory,
    "add_product": add_product,
    "update_stock": update_stock,
    "get_low_stock_alerts": get_low_stock_alerts,
}


def run_tool(name: str, raw_args: str) -> str:
    """Ejecuta la tool elegida por el LLM y devuelve el resultado como texto JSON."""
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return json.dumps({"error": f"Tool desconocida: {name}"}, ensure_ascii=False)
    try:
        args = json.loads(raw_args or "{}") or {}
        result = func(**args)
    except (json.JSONDecodeError, TypeError) as exc:
        result = {"error": f"Argumentos no válidos para {name}: {exc}"}
    return json.dumps(result, ensure_ascii=False)


# ---------- Bucle del agente ----------

def agent_turn(client: OpenAI, messages: list[dict]) -> str:
    """Procesa un mensaje del usuario (ya añadido a `messages`) hasta obtener una respuesta final."""
    for _ in range(MAX_STEPS):
        # Pensar: el LLM decide si responder o qué tool usar.
        completion = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.2,
        )
        reply = completion.choices[0].message

        assistant_msg = {"role": "assistant", "content": reply.content or ""}
        if reply.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                for call in reply.tool_calls
            ]
        messages.append(assistant_msg)

        # Sin tool calls pendientes => respuesta final, fin del bucle.
        if not reply.tool_calls:
            answer = reply.content or ""
            log_event("agent", answer)
            return answer

        # Actuar: ejecutar cada tool y Actualizar: inyectar el resultado en el historial.
        for call in reply.tool_calls:
            name, args = call.function.name, call.function.arguments
            log_event("agent", args or "{}", name)
            print(f"  -> {name}({args or ''})")

            result = run_tool(name, args)
            log_event("tool", result, name)

            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    answer = "Lo siento, no he podido completar la petición en un número razonable de pasos."
    messages.append({"role": "assistant", "content": answer})
    log_event("agent", answer)
    return answer


def check_api() -> bool:
    try:
        http.get("/inventory").raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def main() -> None:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        sys.exit("Falta GROQ_API_KEY. Créala en el fichero .env (ver .env.example).")
    if not check_api():
        sys.exit(f"La API no responde en {API_URL}. Arráncala antes con: uvicorn api.app:app --reload")

    client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    print("Asistente de inventario listo. Escribe 'salir' para terminar.\n")
    while True:
        # Observar: leer el input del usuario.
        try:
            user_input = input("Carla > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input.lower() in EXIT_WORDS:
            break

        log_event("user", user_input)
        messages.append({"role": "user", "content": user_input})

        try:
            answer = agent_turn(client, messages)
        except APIError as exc:
            answer = f"Error al contactar con el modelo: {exc.message}"
            log_event("agent", answer)
        except KeyboardInterrupt:
            print()
            break

        print(f"Agente > {answer}\n")

    print("¡Hasta luego!")


if __name__ == "__main__":
    main()
