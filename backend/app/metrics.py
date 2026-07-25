# backend/app/metrics.py

from prometheus_client import Counter, Gauge, Histogram

# ============================
# IA
# ============================

IA_REQUESTS = Counter(
    "dono_ai_requests_total",
    "Total de consultas à IA",
)

IA_DURATION = Histogram(
    "dono_ai_duration_seconds",
    "Tempo gasto nas consultas à IA",
)

OCR_DOCUMENTS = Counter(
    "dono_ocr_documents_total",
    "Documentos OCR processados",
)

# ============================
# Estoque
# ============================

STOCK_MOVEMENTS = Counter(
    "dono_stock_movements_total",
    "Movimentações de estoque",
    ["tipo"],
)

STOCK_LOSSES = Counter(
    "dono_stock_losses_total",
    "Perdas de estoque",
)

# ============================
# Financeiro
# ============================

FINANCIAL_ENTRIES = Counter(
    "dono_financial_entries_total",
    "Lançamentos financeiros",
)

FINANCIAL_VALUE = Counter(
    "dono_financial_value_total",
    "Valor financeiro movimentado",
)

# ============================
# Produção
# ============================

MEALS_PRODUCED = Counter(
    "dono_meals_produced_total",
    "Refeições produzidas",
)

MENU_GENERATIONS = Counter(
    "dono_menu_generations_total",
    "Cardápios gerados",
)

FORECAST_EXECUTIONS = Counter(
    "dono_forecasts_total",
    "Previsões executadas",
)