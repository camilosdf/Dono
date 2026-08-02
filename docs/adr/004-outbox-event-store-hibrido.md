# ADR 004 — Modelo híbrido: Outbox + Event Store + estado relacional

**Status:** Aceito  
**Data:** 2026-07  
**Contexto:** Sistema Dono — arquitetura de eventos

---

## Contexto

O sistema precisa propagar mudanças de preço de insumos em cascata (Insumo → Prato → Refeição → Menu → ABC), auditar operações de negócio e manter estado atual eficiente para consultas. Existem três abordagens principais:

1. **Estado relacional puro:** tabelas de estado, sem eventos.
2. **Event Sourcing puro:** estado reconstruído apenas por replay de eventos.
3. **Modelo híbrido:** eventos para auditoria e propagação, tabelas de estado para consulta eficiente.

## Decisão

O sistema usa um **modelo híbrido** com três componentes distintos:

- **Outbox (`eventos_dominio`):** fila consumível de eventos de domínio. Cada evento é gravado atomicamente com a operação que o gerou (trigger). O worker consome e propaga efeitos (recálculo de custo, ABC). Eventos processados são marcados `processado = TRUE` — não são apagados.
- **Event Store (`event_store`):** log auditável imutável de operações de negócio significativas. Nunca consumido — existe apenas para auditoria e reconstrução de estado se necessário. Cada evento registra `usuario_id`, `ip_origem`, `user_agent` via `fn_set_audit_context`.
- **Tabelas de estado:** `insumos.custo_medio_ponderado`, `classificacoes_abc`, `previsoes_consumo`, `projecao_estoque_atual` — mantidas atualizadas por triggers e workers, lidas diretamente pelos dashboards e relatórios.

## Justificativa

Event Sourcing puro exigiria eliminar as tabelas de estado e reconstruir tudo por replay — trocaria consultas O(1) por replay O(N) a cada leitura de dashboard. Para o volume atual (um restaurante), o custo de complexidade não tem retorno.

O modelo híbrido entrega os benefícios principais de Event Sourcing (auditabilidade, propagação desacoplada, reconstrução possível) sem os custos (projeções complexas, handlers de reconstrução, latência de leitura).

## Consequências

- O Outbox usa `FOR UPDATE SKIP LOCKED` para suportar múltiplas réplicas do worker sem processamento duplicado.
- Eventos com falha são retentados até 3 vezes e marcados em dead-letter (`bloqueado_em IS NOT NULL`, `tentativas >= 3`).
- O worker injeta contexto de auditoria como processo de sistema (`ip_origem = 'worker://dono-worker'`).
- `projecao_estoque_atual` é um read model atualizado por trigger no `event_store` — CQRS pragmático sem a complexidade de handlers separados.

## Alternativas rejeitadas

**Event Sourcing puro:** consultas de dashboard passariam de O(1) para O(N) replay. Complexidade de projeção sem benefício proporcional no volume atual. Rejeitado.

**Estado relacional puro sem eventos:** perderia auditabilidade e propagação desacoplada. Rejeitado.

**Kafka/RabbitMQ:** fila externa introduziria nova peça de infraestrutura para um volume que o Outbox no próprio Postgres resolve. Revisitar se o sistema passar a atender múltiplas unidades com alto throughput.
