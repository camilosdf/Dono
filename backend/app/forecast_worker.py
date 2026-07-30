# backend/app/forecast_worker.py — Sistema Dono
#
# Worker dedicado para atualização das previsões de consumo (Fase 4).
# Roda em loop infinito, executando a função PL/pgSQL fn_atualizar_previsoes_consumo
# a cada intervalo configurável (padrão 24 horas).
#
# Estratégia:
#   - Usa asyncio + asyncpg para conexão com o banco.
#   - Executa a função SQL que recalcula as previsões para todos os insumos ativos.
#   - Loga o número de insumos processados (a função SQL não retorna contagem,
#     então o log é apenas informativo).
#   - Em caso de erro, registra a exceção e aguarda o próximo intervalo.
#   - Para evitar execuções concorrentes, usa um lock no Redis (opcional).
#
# Uso:
#   python -m app.forecast_worker
#
# Variáveis de ambiente:
#   FORECAST_INTERVAL_HOURS: intervalo em horas entre execuções (padrão 24)
#   FORECAST_DAYS_AHEAD: número de dias para previsão (padrão 30)
#   FORECAST_HISTORICAL_DAYS: dias de histórico para média (padrão 90)
#
# docker-compose.yml deve incluir:
#   forecast_worker:
#     build: ./backend
#     command: python -m app.forecast_worker
#     environment:
#       - FORECAST_INTERVAL_HOURS=24
#       - FORECAST_DAYS_AHEAD=30
#       - FORECAST_HISTORICAL_DAYS=90
#     depends_on:
#       - db
#       - redis

import os
import asyncio
import asyncpg
import logging
from datetime import datetime, timedelta
from app.redis_client import get_redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("dono-forecast-worker")

# Configurações padrão
DEFAULT_INTERVAL_HOURS = 24
DEFAULT_DAYS_AHEAD = 30
DEFAULT_HISTORICAL_DAYS = 90


async def run_forecast(db_pool: asyncpg.Pool, days_ahead: int, historical_days: int) -> None:
    """Executa a atualização das previsões de consumo.
    Chama a função SQL fn_atualizar_previsoes_consumo e registra o resultado."""
    async with db_pool.acquire() as conn:
        try:
            logger.info(
                "Iniciando atualização de previsões (dias_ahead=%s, historico=%s)",
                days_ahead, historical_days
            )
            start = datetime.now()

            # Define contexto de auditoria do worker de previsão
            await conn.execute(
                "SELECT fn_set_audit_context($1::uuid, $2, $3)",
                None, "worker://dono-forecast-worker", "dono-forecast-worker"
            )

            # Chama a função SQL que recalcula e insere as previsões
            await conn.execute(
                "SELECT fn_atualizar_previsoes_consumo($1, $2)",
                days_ahead, historical_days
            )

            elapsed = (datetime.now() - start).total_seconds()
            logger.info("Previsões atualizadas com sucesso em %.2f segundos", elapsed)

            # (Opcional) Preenche consumo real para comparação
            await conn.execute("SELECT fn_preencher_consumo_real()")
            logger.info("Consumo real preenchido para datas passadas")

        except Exception as e:
            logger.exception("Erro ao atualizar previsões: %s", str(e))
            raise


async def main() -> None:
    """Loop principal do worker."""
    database_url = os.environ["DATABASE_URL"]

    # Lê configurações do ambiente
    interval_hours = int(os.getenv("FORECAST_INTERVAL_HOURS", DEFAULT_INTERVAL_HOURS))
    days_ahead = int(os.getenv("FORECAST_DAYS_AHEAD", DEFAULT_DAYS_AHEAD))
    historical_days = int(os.getenv("FORECAST_HISTORICAL_DAYS", DEFAULT_HISTORICAL_DAYS))

    logger.info(
        "Worker de Previsão iniciado. Intervalo: %s horas, Dias à frente: %s, Histórico: %s dias",
        interval_hours, days_ahead, historical_days
    )

    # Cria pool de conexões
    db_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)

    try:
        while True:
            try:
                # Lock Redis: garante que apenas uma réplica executa por vez.
                # TTL = intervalo + 10min de margem para execucoes longas.
                redis = get_redis()
                lock_key = "dono:forecast_worker:lock"
                lock_ttl = interval_hours * 3600 + 600
                acquired = await redis.set(lock_key, "1", nx=True, ex=lock_ttl)
                if not acquired:
                    logger.info("Outra replica ja esta executando o forecast -- aguardando.")
                    await asyncio.sleep(interval_hours * 3600)
                    continue
                try:
                    await run_forecast(db_pool, days_ahead, historical_days)
                finally:
                    await redis.delete(lock_key)
                logger.info("Aguardando %s horas ate a proxima execucao", interval_hours)
                await asyncio.sleep(interval_hours * 3600)

            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception:
                # Em caso de erro inesperado no loop, espera 5 minutos e tenta novamente
                logger.exception("Erro inesperado no loop principal. Tentando novamente em 5 minutos...")
                await asyncio.sleep(300)

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Worker de Previsão interrompido.")
    finally:
        await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())