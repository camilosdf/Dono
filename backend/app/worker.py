# backend/app/worker.py — Sistema Dono
#
# Modo híbrido: LISTEN/NOTIFY (reação em milissegundos) + polling de
# segurança em intervalo longo (heartbeat). O trigger trg_eventos_dominio_notify
# (schema.sql) dispara NOTIFY 'evento_novo' a cada INSERT em eventos_dominio.
#
# Por que manter o polling de segurança em vez de confiar só no NOTIFY:
# NOTIFY do Postgres não é persistido — se não houver um LISTENer conectado
# no instante exato do INSERT (worker reiniciando, conexão de LISTEN caída
# um segundo antes), a notificação se perde de vez, sem fila de reentrega.
# O polling de segurança garante que, mesmo perdendo o NOTIFY, o evento é
# pego no máximo WORKER_FALLBACK_INTERVAL segundos depois.
#
# ATUALIZAÇÃO (Hardening do Worker):
#   - A lógica de retry, lock e limite de tentativas agora está concentrada
#     na função PL/pgSQL fn_processar_eventos_pendentes (business-queries.sql).
#   - O worker apenas dispara a função e monitora se há eventos com falha
#     permanente (tentativas >= 3) registrando um alerta no log.
#   - Isso mantém o worker leve e focado em orquestração, enquanto a
#     resiliência fica no banco (onde os dados residem).
import os
import asyncio
import asyncpg
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dono-worker")

FALLBACK_INTERVAL_DEFAULT = 300  # 5 min — só heartbeat, não é o caminho normal


async def process_events(db_pool: asyncpg.Pool) -> None:
    """Chama fn_processar_eventos_pendentes(), que consome o outbox
    eventos_dominio e recalcula custo + Classificação ABC em cascata
    (Insumo/Gênero -> Prato -> Refeição -> Menu). Ver scripts/business-queries.sql.
    Idempotente: processar de novo sem nada pendente é barato (SELECT vazio).
    
    Além disso, faz uma consulta para verificar se há eventos com falha
    permanente (tentativas >= 3) e registra um aviso no log para alertar
    a equipe de operações.
    """
    async with db_pool.acquire() as conn:
        try:
            # Define contexto de auditoria do worker (processo de sistema,
            # sem usuario_id real — distinguível no event_store por ip_origem)
            await conn.execute(
                "SELECT fn_set_audit_context($1::uuid, $2, $3)",
                None, "worker://dono-worker", "dono-worker/outbox"
            )
            # 1. Processa eventos pendentes com a lógica de retry/lock no banco
            await conn.execute("SELECT fn_processar_eventos_pendentes();")
            
            # 2. Verifica se há eventos que atingiram o limite de tentativas
            #    e ficaram bloqueados permanentemente (bloqueado_em IS NOT NULL)
            contagem_erro = await conn.fetchval(
                """SELECT count(*) FROM eventos_dominio 
                   WHERE processado = TRUE 
                     AND bloqueado_em IS NOT NULL
                     AND tentativas >= 3"""
            )
            if contagem_erro > 0:
                logger.warning(
                    f"Existem {contagem_erro} evento(s) com falha permanente aguardando revisão manual. "
                    "Verifique a tabela eventos_dominio (bloqueado_em IS NOT NULL)."
                )
                
        except Exception:
            logger.exception("Erro ao processar eventos pendentes")


async def _listen_once(db_pool: asyncpg.Pool, fallback_seconds: int) -> None:
    """Abre UMA conexão dedicada (fora do pool de requisições — LISTEN é
    por conexão, não pode ir e voltar de um pool normal) e fica nela até
    cair ou o processo encerrar. Cada NOTIFY recebido, ou o timeout de
    fallback_seconds vencendo, dispara um process_events()."""
    queue: asyncio.Queue = asyncio.Queue()

    def _on_notify(connection, pid, channel, payload):
        queue.put_nowait(payload)

    conn = await asyncpg.connect(dsn=os.environ["DATABASE_URL"])
    try:
        await conn.add_listener("evento_novo", _on_notify)
        logger.info(
            "LISTEN evento_novo conectado — polling de segurança a cada %ss",
            fallback_seconds,
        )
        # Drena qualquer evento que já estivesse pendente antes de conectar
        # (ex.: evento criado enquanto o worker estava fora do ar).
        await process_events(db_pool)

        while True:
            try:
                await asyncio.wait_for(queue.get(), timeout=fallback_seconds)
                # Uma ou mais notificações chegaram: esvazia a fila sem
                # reprocessar em série — fn_processar_eventos_pendentes()
                # já pega TODOS os pendentes numa única passada, então
                # rodá-la mais de uma vez por rajada de NOTIFY é
                # desperdício, não incorreção.
                while not queue.empty():
                    queue.get_nowait()
            except asyncio.TimeoutError:
                pass  # venceu o heartbeat — processa mesmo sem NOTIFY

            await process_events(db_pool)
    finally:
        try:
            await conn.remove_listener("evento_novo", _on_notify)
        except Exception:
            pass
        await conn.close()


async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    # Compatibilidade: quem já tinha WORKER_INTERVAL configurado (nome
    # antigo, do polling puro) continua funcionando, só que agora como
    # intervalo de fallback em vez de intervalo único de polling.
    fallback_seconds = int(
        os.getenv("WORKER_FALLBACK_INTERVAL", os.getenv("WORKER_INTERVAL", str(FALLBACK_INTERVAL_DEFAULT)))
    )

    logger.info(
        "Worker Dono iniciado — modo híbrido LISTEN/NOTIFY + polling de segurança (%ss)",
        fallback_seconds,
    )

    # Mantenha 1 réplica deste worker enquanto fn_processar_eventos_pendentes
    # não usar "FOR UPDATE SKIP LOCKED" na leitura de eventos_dominio — ver
    # comentário em docker-compose.yml. O modo híbrido não muda essa
    # restrição: um NOTIFY acorda TODAS as réplicas simultaneamente, o que
    # tornaria a contenção sem SKIP LOCKED ainda mais visível, não menos.
    db_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)

    try:
        while True:
            try:
                await _listen_once(db_pool, fallback_seconds)
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception:
                # Conexão de LISTEN caiu (rede, restart do Postgres etc.).
                # O texto que propôs este modo híbrido não cobria este
                # caso — sem isto, uma queda de conexão mataria o processo
                # de vez, perdendo também o polling de segurança. Espera
                # um pouco e reconecta; enquanto isso, eventos continuam
                # sendo processados assim que a próxima conexão subir e
                # drenar o pendente logo no início de _listen_once().
                logger.exception(
                    "Conexão de LISTEN caiu — reconectando em 5s "
                    "(nenhum evento é perdido, só atrasa até a reconexão)"
                )
                await asyncio.sleep(5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Worker interrompido")
    finally:
        await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())