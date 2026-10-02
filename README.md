# Agente de inventario con IA

Asistente conversacional para la tienda de suministros para cafeterías de Carla. Tiene dos partes:

- **`api/app.py`**: API REST con FastAPI. Guarda el inventario en `products.csv`.
- **`agent.py`**: agente por terminal que usa un LLM (Groq) y llama a la API como tools. Registra cada evento en `conversation_log.csv`.

El bucle del agente está escrito a mano en Python, sin LangChain ni otros frameworks.

## Instalación

```bash
uv sync                      # instala fastapi, uvicorn, openai, python-dotenv, httpx
cp .env.example .env         # y pon tu GROQ_API_KEY
```

## Cómo arrancarlo

Necesitas **dos terminales**. Arranca primero la API.

```bash
# Terminal 1: API
uv run uvicorn api.app:app --reload

# Terminal 2: agente
uv run python agent.py
```

(Si tienes el entorno virtual activado, puedes omitir `uv run`.)

Para salir del agente, escribe `salir` o pulsa `Ctrl + C`. Detén primero el agente y luego la API. Puedes relanzar el agente sin reiniciar la API: el log solo añade filas, nunca sobreescribe.

La primera vez que arranca, la API crea `products.csv` con un catálogo de ejemplo de 10 productos.

## Ejemplos

```
Carla > acaban de llegar 30 litros de leche de avena
  -> list_inventory({})
  -> update_stock({"product_id": 3, "delta": 30})
Agente > Listo, la leche de avena ha pasado de 8 a 38 litros.

Carla > vendimos 12 bolsas de arábica hoy
Carla > añade 5 botellas de sirope de caramelo y dime qué está por agotarse
Carla > ¿qué productos están por agotarse?
```

## API

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/inventory` | Lista todos los productos |
| `POST` | `/inventory` | Crea un producto `{name, quantity, unit}` → `201`; `409` si el nombre ya existe |
| `PATCH` | `/inventory/{product_id}` | Aplica `{delta}` (+ entrada, − salida) → `404` si no existe, `409` si el stock quedaría negativo |
| `GET` | `/inventory/alerts?threshold=10` | Productos con cantidad por debajo del umbral (por defecto 10) |

Los datos no válidos devuelven `422`. Documentación interactiva: http://127.0.0.1:8000/docs

## Bucle del agente

Cada mensaje de Carla pasa por **Observar → Pensar → Actuar → Actualizar → Repetir**:

1. **Observar**: se lee el input del terminal y se añade al historial.
2. **Pensar**: se envía el historial completo y las definiciones de las tools al LLM.
3. **Actuar**: si el LLM pide tools, `agent.py` llama al endpoint correspondiente de la API.
4. **Actualizar**: el resultado se añade al historial como mensaje `tool`.
5. **Repetir** hasta que el LLM responde sin pedir más tools (como máximo 10 pasos por mensaje).

Tools: `list_inventory`, `add_product`, `update_stock`, `get_low_stock_alerts`.

## Registro de conversación

`conversation_log.csv` tiene estas columnas: `actor` (`user` / `agent` / `tool`), `message`, `tool_call` y `timestamp` (ISO 8601). Una llamada a tool se registra como `agent` con los argumentos y el nombre de la tool. Su resultado se registra como `tool`.

## Configuración opcional (`.env`)

| Variable | Valor por defecto |
|---|---|
| `GROQ_MODEL` | `llama-3.3-70b-versatile` |
| `INVENTORY_API_URL` | `http://127.0.0.1:8000` |
| `LOW_STOCK_THRESHOLD` | `10` |
