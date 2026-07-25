# backend/app/errors.py — Sistema Dono
def error_detail(code: str, message: str, details: dict | None = None) -> dict:
    """Monta o corpo de erro no formato único definido em api-endpoints.md
    (§13 Contratos de erro): {"error": {"code", "message", "details"?}}."""
    body: dict = {"code": code, "message": message}
    if details:
        body["details"] = details
    return {"error": body}
