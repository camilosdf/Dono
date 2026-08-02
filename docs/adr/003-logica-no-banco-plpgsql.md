# ADR 003 — Lógica transacional crítica em PL/pgSQL no banco

**Status:** Aceito  
**Data:** 2026-07  
**Contexto:** Sistema Dono — arquitetura geral

---

## Contexto

Operações que envolvem múltiplas tabelas com garantia de consistência imediata (baixa FEFO de estoque, snapshot de custo, recálculo de custo médio ponderado, processamento do outbox) podem ser implementadas na camada de aplicação (Python/FastAPI) ou no banco (PL/pgSQL).

## Decisão

Regras que precisam de **atomicidade transacional** ou operam sobre múltiplas linhas com garantia de consistência imediata ficam no banco como funções PL/pgSQL e triggers. Regras que envolvem estado externo ao banco, orquestração de serviços ou políticas de negócio que mudam com frequência ficam na aplicação.

Funções críticas no banco:
- `fn_executar_refeicao` — baixa FEFO com verificação de estoque
- `fn_estornar_execucao_refeicao` — estorno lote a lote
- `fn_snapshot_custo_refeicao` / `fn_snapshot_custo_menu` — congelamento de custo
- `fn_atualizar_custo_medio_insumo` — custo médio ponderado + outbox
- `fn_processar_eventos_pendentes` — consumo do outbox com SKIP LOCKED
- `fn_recalcular_abc_*` — classificação ABC materializada

## Justificativa

Uma race condition entre dois requests simultâneos de "confirmar refeição" é impossível com triggers — em Python puro exigiria locks explícitos difíceis de acertar. A atomicidade do PostgreSQL garante que ou toda a baixa FEFO acontece ou nada acontece — sem estados intermediários inconsistentes.

O custo da portabilidade de banco perdida é aceitável: o projeto usa PostgreSQL deliberadamente pelas suas capacidades (JSONB, colunas geradas, `FOR UPDATE SKIP LOCKED`, PgVector, pgTAP) e não há plano de migrar para outro banco.

## Limites desta decisão

O banco **não** deve receber:
- Lógica de autenticação/autorização (fica em `dependencies.py`)
- Regras de rate limiting (fica em `rate_limit.py` + Redis)
- Orquestração de workers ou jobs de IA
- Políticas comerciais que mudam com frequência

## Consequências

- `asyncpg` (não SQLAlchemy/ORM) é usado deliberadamente: ORM abstrairia o banco e dificultaria uso de funções PL/pgSQL, colunas geradas e triggers — a ferramenta certa para o modelo escolhido.
- Testes de banco usam pgTAP (42 assertions) para validar funções SQL diretamente.
- Migrações futuras de schema devem ser raw SQL versionado, não autogenerate de ORM.

## Alternativas rejeitadas

**Toda lógica em Python:** perderia atomicidade transacional em operações multi-tabela. Rejeitado.

**ORM (SQLAlchemy):** abstrairia o banco e conflitaria com colunas geradas e triggers. Rejeitado explicitamente — ver README.
