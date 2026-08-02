# Architecture Decision Records — Sistema Dono

Este diretório contém os registros de decisões arquiteturais (ADRs) do Sistema Dono. Cada ADR documenta uma decisão técnica ou de domínio significativa: o contexto em que foi tomada, a decisão em si, a justificativa e as consequências.

## Como usar

- **Antes de propor uma mudança:** leia o ADR relevante. Se a mudança contradiz uma decisão registrada, documente por que o contexto mudou.
- **Ao tomar uma nova decisão significativa:** crie um novo ADR seguindo o template abaixo.
- **ADRs são imutáveis após aceitos.** Se uma decisão muda, o ADR original é marcado como `Substituído por ADR NNN` e um novo ADR é criado.

## Template

```markdown
# ADR NNN — Título curto

**Status:** Proposto | Aceito | Substituído por ADR NNN  
**Data:** AAAA-MM  
**Contexto:** módulo ou área afetada

## Contexto
## Decisão
## Justificativa
## Consequências
## Alternativas rejeitadas
```

## Índice

| ADR | Título | Status |
|---|---|---|
| [001](001-custo-peso-bruto.md) | Custo calculado sobre Peso Bruto (PB), não Peso Líquido (PL) | Aceito |
| [002](002-imutabilidade-custo-historico.md) | Imutabilidade de custo histórico via custo_snapshot | Aceito |
| [003](003-logica-no-banco-plpgsql.md) | Lógica transacional crítica em PL/pgSQL no banco | Aceito |
| [004](004-outbox-event-store-hibrido.md) | Modelo híbrido: Outbox + Event Store + estado relacional | Aceito |
| [005](005-abc-materializada.md) | ABC materializada em tabela, não coluna GENERATED | Aceito |
| [006](006-redis-rate-limit-apenas.md) | Redis apenas para rate limiting e locks de worker | Aceito |
| [007](007-migrations-raw-sql-sem-alembic.md) | Migrações de schema como raw SQL versionado, sem Alembic | Aceito |
| [008](008-ia-local-ollama-pgvector.md) | IA local via Ollama + PgVector, sem dependência de nuvem | Aceito |
| [009](009-previsao-por-evento-nao-serie-temporal.md) | Previsão de consumo por evento, não por série temporal contínua | Aceito |
