# ADR 006 — Redis apenas para rate limiting e locks de worker

**Status:** Aceito  
**Data:** 2026-07  
**Contexto:** Sistema Dono — decisão de infraestrutura de cache

---

## Contexto

Redis pode ser usado para múltiplos fins: cache de consultas, session store, pub/sub, rate limiting, locks distribuídos. A decisão é quais usos adotar agora.

## Decisão

Redis é usado **exclusivamente** para:

1. **Rate limiting** (contadores de janela fixa por usuário/IP): login (5/5min), geral (120/min), IA (10/hora), semáforo global de jobs de IA (máx. 5 simultâneos).
2. **Locks distribuídos de workers**: `forecast_worker` usa `SET NX EX` para garantir que apenas uma réplica execute por vez.

Redis **não** é usado para:
- Cache de consultas SQL ou relatórios
- Cache de `classificacoes_abc`
- Session store de JWT
- Pub/sub de eventos (substituído pelo Outbox no Postgres)

## Justificativa

Rate limiting exige estado compartilhado entre réplicas do backend — um contador em memória local não funciona com mais de uma instância. Redis é a solução padrão e foi introduzido exclusivamente para isso.

Cache de `classificacoes_abc` e relatórios não foi introduzido porque o Postgres com a tabela materializada já serve dashboards em O(1) — introduzir cache antes de medir gargalo seria otimização prematura. Se o banco se tornar gargalo medido, Redis entra para cache nesse momento, não antes.

JWT access tokens não são armazenados em lugar algum — são autocontidos e expiram em 15 minutos. Refresh tokens são armazenados no Postgres (hashed), não no Redis.

## Consequências

- `app/redis_client.py` mantém pool assíncrono mínimo.
- `app/rate_limit.py` implementa janela fixa via `INCR + EXPIRE`.
- `app/middleware.py` aplica rate limiting geral a todas as requisições autenticadas.
- `forecast_worker.py` usa `redis.set(key, "1", nx=True, ex=ttl)` antes de executar e `redis.delete(key)` no `finally`.
- Mock de Redis em testes usa `AsyncMock` para todos os métodos — sem dependência de Redis real nos testes.

## Alternativas rejeitadas

**Redis como cache de ABC e relatórios:** introduziria invalidação de cache complexa sem gargalo medido. Rejeitado até evidência de necessidade.

**Tabela Postgres UNLOGGED para rate limiting:** mais lento (lock por linha a cada request) e sem TTL nativo. Rejeitado em favor do Redis já presente.

**Pub/sub Redis para eventos de domínio:** o Outbox no Postgres já desacopla produção de consumo de forma transacional. Redis pub/sub perderia eventos se o worker estiver down. Rejeitado.
