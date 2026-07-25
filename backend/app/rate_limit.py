# backend/app/rate_limit.py — Sistema Dono
#
# Janela fixa (fixed window) via Redis INCR+EXPIRE — simples e suficiente
# pros limites documentados em api-endpoints.md §12. Não é perfeitamente
# preciso na borda da janela (pico duplo possível bem no limite entre
# duas janelas), mas é o que o próprio §12 já assumia ao propor Redis
# só pra isso — trade-off aceito de propósito, não um descuido.
from fastapi import HTTPException, Request

from app.errors import error_detail
from app.redis_client import get_redis


async def _fixed_window_check(key: str, limit: int, window_seconds: int) -> None:
    r = get_redis()
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, window_seconds)
    if count > limit:
        ttl = await r.ttl(key)
        ttl = ttl if ttl and ttl > 0 else window_seconds
        raise HTTPException(
            status_code=429,
            detail=error_detail("LIMITE_REQUISICOES_EXCEDIDO", "Limite de requisições excedido"),
            headers={
                "Retry-After": str(ttl),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(ttl),
            },
        )


async def check_login_rate_limit(request: Request, email: str) -> None:
    """5 tentativas / 5 min por IP+email (§12). Aplicado dentro da própria
    rota de login, não via middleware — precisa do email do corpo da
    requisição, que o middleware não tem acesso limpo antes do FastAPI
    parsear o body."""
    ip = request.client.host if request.client else "desconhecido"
    await _fixed_window_check(f"ratelimit:login:{ip}:{email}", limit=5, window_seconds=300)


async def check_ia_rate_limit(usuario_id: str) -> None:
    """10 req/hora por usuário nas rotas de IA (§12) — separado do limite
    geral porque cada chamada tem custo real (tokens de LLM, risco de
    bloqueio do site raspado)."""
    await _fixed_window_check(f"ratelimit:ia:{usuario_id}", limit=10, window_seconds=3600)


async def check_general_rate_limit(usuario_id: str) -> None:
    """120 req/min por usuário autenticado, aplicado globalmente via
    RateLimitMiddleware (app/middleware.py)."""
    await _fixed_window_check(f"ratelimit:geral:{usuario_id}", limit=120, window_seconds=60)


class IASlotIndisponivel(HTTPException):
    def __init__(self, retry_after: int = 5):
        super().__init__(
            status_code=503,
            detail=error_detail("LIMITE_IA_EXCEDIDO", "Muitos jobs de IA em andamento — tente novamente em instantes"),
            headers={"Retry-After": str(retry_after)},
        )


async def acquire_ia_slot(max_concorrentes: int = 5) -> None:
    """Semáforo global (não por usuário) — no máximo N jobs de IA
    (cotação online + prospecção) processando ao mesmo tempo no sistema
    inteiro. Ver §12: 'Jobs de IA concorrentes (global): máx. 5
    simultâneos'."""
    r = get_redis()
    atual = await r.incr("ratelimit:ia:concorrentes")
    if atual > max_concorrentes:
        await r.decr("ratelimit:ia:concorrentes")
        raise IASlotIndisponivel()


async def release_ia_slot() -> None:
    r = get_redis()
    await r.decr("ratelimit:ia:concorrentes")
