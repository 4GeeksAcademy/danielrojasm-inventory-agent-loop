"""API REST de inventario para la tienda de suministros de Carla.

Los productos se guardan en products.csv (raíz del proyecto) para que
persistan entre reinicios del servidor.

Arrancar con:  uvicorn api.app:app --reload
"""

import csv
import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

BASE_DIR = Path(__file__).resolve().parent.parent
PRODUCTS_FILE = Path(os.getenv("PRODUCTS_FILE", BASE_DIR / "products.csv"))
DEFAULT_THRESHOLD = float(os.getenv("LOW_STOCK_THRESHOLD", 10))
FIELDNAMES = ["id", "name", "quantity", "unit"]

# Catálogo inicial: se escribe solo si products.csv todavía no existe.
SEED_PRODUCTS = [
    {"name": "Café arábica en grano (bolsa 1 kg)", "quantity": 40, "unit": "bolsas"},
    {"name": "Café robusta en grano (bolsa 1 kg)", "quantity": 18, "unit": "bolsas"},
    {"name": "Leche de avena", "quantity": 8, "unit": "litros"},
    {"name": "Leche entera", "quantity": 36, "unit": "litros"},
    {"name": "Azúcar moreno", "quantity": 12.5, "unit": "kg"},
    {"name": "Vasos de papel 12 oz", "quantity": 500, "unit": "unidades"},
    {"name": "Tapas para vasos 12 oz", "quantity": 6, "unit": "paquetes"},
    {"name": "Filtros de papel V60", "quantity": 4, "unit": "cajas"},
    {"name": "Sirope de vainilla", "quantity": 9, "unit": "botellas"},
    {"name": "Cacao en polvo", "quantity": 3.5, "unit": "kg"},
]

_lock = threading.Lock()


# ---------- Modelos ----------

class ProductIn(BaseModel):
    name: str = Field(..., min_length=1, description="Nombre del producto")
    quantity: float = Field(..., ge=0, description="Cantidad inicial en stock")
    unit: str = Field(..., min_length=1, description="Unidad de medida (unidades, kg, litros...)")

    @field_validator("name", "unit")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("no puede estar vacío")
        return value


class StockUpdate(BaseModel):
    delta: float = Field(
        ...,
        description="Cambio de stock: positivo para entradas, negativo para salidas",
    )

    @field_validator("delta")
    @classmethod
    def non_zero(cls, value: float) -> float:
        if value == 0:
            raise ValueError("delta no puede ser 0")
        return value


class Product(BaseModel):
    id: int
    name: str
    quantity: int | float
    unit: str


# ---------- Persistencia en CSV ----------

def _normalize(number: float) -> float | int:
    """Devuelve 30 en lugar de 30.0 para que el CSV y el JSON sean legibles."""
    number = round(float(number), 3)
    return int(number) if number.is_integer() else number


def _read_products() -> list[dict]:
    if not PRODUCTS_FILE.exists():
        return []
    with PRODUCTS_FILE.open(newline="", encoding="utf-8") as f:
        return [
            {
                "id": int(row["id"]),
                "name": row["name"],
                "quantity": _normalize(row["quantity"]),
                "unit": row["unit"],
            }
            for row in csv.DictReader(f)
        ]


def _write_products(products: list[dict]) -> None:
    # Escritura atómica: se escribe en un temporal y se reemplaza el original.
    tmp = PRODUCTS_FILE.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(products)
    tmp.replace(PRODUCTS_FILE)


def _seed_if_missing() -> None:
    with _lock:
        if PRODUCTS_FILE.exists():
            return
        products = [{"id": i, **p} for i, p in enumerate(SEED_PRODUCTS, start=1)]
        _write_products(products)


# ---------- App ----------

app = FastAPI(
    title="Inventario de la tienda de Carla",
    description="API de inventario para una tienda de suministros para cafeterías.",
    version="1.0.0",
)

_seed_if_missing()


@app.get("/inventory", response_model=list[Product])
def list_inventory():
    """Devuelve la lista completa de productos."""
    with _lock:
        return _read_products()


@app.post("/inventory", response_model=Product, status_code=status.HTTP_201_CREATED)
def add_product(product: ProductIn):
    """Añade un nuevo producto al inventario."""
    with _lock:
        products = _read_products()
        if any(p["name"].casefold() == product.name.casefold() for p in products):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ya existe un producto llamado '{product.name}'. "
                "Usa PATCH /inventory/{product_id} para cambiar su stock.",
            )
        new = {
            "id": max((p["id"] for p in products), default=0) + 1,
            "name": product.name,
            "quantity": _normalize(product.quantity),
            "unit": product.unit,
        }
        products.append(new)
        _write_products(products)
        return new


@app.get("/inventory/alerts", response_model=list[Product])
def low_stock_alerts(
    threshold: float = Query(
        DEFAULT_THRESHOLD, ge=0, description="Se devuelven los productos con cantidad por debajo de este valor"
    ),
):
    """Devuelve los productos cuya cantidad está por debajo del umbral."""
    with _lock:
        return [p for p in _read_products() if p["quantity"] < threshold]


@app.patch("/inventory/{product_id}", response_model=Product)
def update_stock(product_id: int, update: StockUpdate):
    """Suma (entrada) o resta (salida) stock a un producto existente."""
    with _lock:
        products = _read_products()
        product = next((p for p in products if p["id"] == product_id), None)
        if product is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No existe ningún producto con id {product_id}.",
            )
        new_quantity = product["quantity"] + update.delta
        if new_quantity < 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Stock insuficiente para '{product['name']}': hay {product['quantity']} "
                    f"{product['unit']} y se intentan retirar {_normalize(abs(update.delta))}."
                ),
            )
        product["quantity"] = _normalize(new_quantity)
        _write_products(products)
        return product
