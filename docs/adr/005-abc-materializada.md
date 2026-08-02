# ADR 005 — ABC materializada em tabela, não coluna GENERATED

**Status:** Aceito  
**Data:** 2026-07  
**Contexto:** Sistema Dono — motor de Classificação ABC

---

## Contexto

A Classificação ABC (Pareto 80/15/5) precisa ser calculada em quatro escopos: INSUMO_GENERO, PRATO, REFEICAO, MENU. A questão é onde armazenar e quando calcular.

## Decisão

A classificação ABC é armazenada na tabela materializada `classificacoes_abc` e recalculada por funções SQL (`fn_recalcular_abc_*`) disparadas pelo worker após eventos de preço — nunca calculada em tempo real na consulta.

## Justificativa

Colunas `GENERATED ALWAYS AS ... STORED` no PostgreSQL só podem depender de colunas da própria linha — não do conjunto de outras linhas. A classificação ABC requer ordenação e soma acumulada sobre todas as linhas do escopo (window functions), o que é impossível em coluna gerada.

Calcular na hora da consulta (`SELECT` com window functions) funcionaria, mas não escala: com muitos insumos/pratos, cada leitura de dashboard recalcula tudo do zero. A tabela materializada torna a leitura O(1) — `SELECT * FROM classificacoes_abc WHERE escopo_tipo = $1 AND escopo_id_pai = $2`.

## Consequências

- `classificacoes_abc` tem índice em `(escopo_tipo, escopo_id_pai)` para leitura eficiente e `(item_id, atualizado_em DESC)` para queries do MRP.
- O worker recalcula o escopo afetado após cada evento `PrecoAtualizado` — não recalcula escopos não afetados.
- Criação de novo Prato com itens de receita recalcula ABC do PRATO imediatamente na rota (não espera o worker, pois a criação da receita não é um evento de preço).
- `404 ABC_NAO_CALCULADO` é retornado para escopos que ainda não tiveram nenhum evento processado.

## Alternativas rejeitadas

**Coluna GENERATED:** impossível — depende do conjunto de linhas. Rejeitado por limitação técnica do PostgreSQL.

**Cálculo em tempo real na consulta:** não escala para dashboards com muitos itens. Rejeitado.
