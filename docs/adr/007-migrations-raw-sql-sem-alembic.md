# ADR 007 — Migrações de schema como raw SQL versionado, sem Alembic

**Status:** Aceito  
**Data:** 2026-07  
**Contexto:** Sistema Dono — gestão de schema do banco de dados

---

## Contexto

O schema do banco evoluiu durante o desenvolvimento. Quando o schema estabilizar, será necessária uma estratégia de migração para aplicar mudanças em produção sem recriar o banco do zero.

## Decisão

Migrações são raw SQL versionado, aplicadas manualmente ou por ferramenta simples (Flyway, golang-migrate ou script shell). **Alembic não é adotado.**

Durante a fase atual de desenvolvimento (schema ainda evoluindo, sem dados de produção), o schema é aplicado integralmente via `docker-entrypoint-initdb.d` a cada reinicialização do volume.

## Justificativa

Alembic `--autogenerate` compara modelos SQLAlchemy com o banco para gerar migrações. Este projeto não usa ORM — usa `asyncpg` + SQL puro. Sem modelos SQLAlchemy, `autogenerate` não funciona como descrito; exigiria duplicar todo o schema em modelos só para o Alembic diff.

Alembic com migrações raw SQL (sem autogenerate) é viável, mas adiciona uma dependência e um processo de migration para um schema que ainda muda a cada sessão — sem nenhum dado de produção a proteger. O custo de overhead supera o benefício neste momento.

O gatilho correto para adotar migrations formais é: **primeiro deploy com dados reais de produção**. Neste momento, `schema.sql` vira o baseline (migration V001) e toda alteração subsequente ganha um arquivo SQL numerado.

## Consequências

- `scripts/schema.sql` é a fonte de verdade do schema atual.
- `scripts/business-queries.sql` contém funções e triggers.
- `scripts/seeds.sql` contém dados iniciais obrigatórios.
- Os três são aplicados em ordem pelo `docker-entrypoint-initdb.d` na primeira inicialização do volume.
- Ao entrar em produção: congelar `schema.sql` como `migrations/V001__baseline.sql` e adotar migrations incrementais a partir daí.
- `dono_test` (banco de testes) recria o schema do zero a cada sessão pytest via `DROP SCHEMA public CASCADE` — isolamento total sem migrations.

## Alternativas rejeitadas

**Alembic com autogenerate:** requereria modelos SQLAlchemy que conflitam com a decisão de usar asyncpg + SQL puro (ADR 003). Rejeitado explicitamente — ver README.

**Alembic com raw SQL:** viável tecnicamente, mas overhead desnecessário enquanto não há dados de produção. Revisitar no primeiro deploy.
