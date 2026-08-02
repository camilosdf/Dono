# ADR 002 — Imutabilidade de custo histórico via custo_snapshot

**Status:** Aceito  
**Data:** 2026-07  
**Contexto:** Sistema Dono — módulo de Refeições e Menus

---

## Contexto

Quando o preço de um insumo sobe após um evento já realizado, o sistema precisa decidir: atualiza o custo histórico retroativamente ou preserva o valor original?

## Decisão

Ao confirmar uma Refeição, o sistema congela o custo de cada prato em `itens_refeicao.custo_snapshot`. Ao confirmar um Menu, congela o custo total de cada refeição em `itens_menu.custo_snapshot`. Esses valores são **imutáveis após gravação**.

Variações futuras de preço recalculam apenas eventos futuros (ainda não confirmados).

## Justificativa

Relatórios financeiros de eventos passados não podem mudar quando um insumo sobe de preço semanas depois. Se o custo fosse recalculado retroativamente, o P&L de um evento realizado em julho mudaria em agosto — tornando impossível fechar qualquer mês financeiro com confiança.

O snapshot é o fato consumado: "neste evento, nesta data, este prato custou X". É o dado que o cliente foi cobrado, que entrou no CMV do período, que foi reportado ao financeiro.

## Consequências

- `PATCH /refeicoes/{id}/confirmar` dispara trigger `fn_snapshot_custo_refeicao` que calcula e grava `custo_snapshot` em todos os `itens_refeicao`. O trigger só dispara na transição `OLD.status <> 'CONFIRMADA'` — reconfirmar não recalcula.
- `PATCH /menus/{id}/confirmar` dispara trigger `fn_snapshot_custo_menu` que soma `custo_snapshot × qtd_pessoas` por refeição.
- Não existe rota de "desconfirmar" — cancelar é o único caminho para desfazer, e o snapshot histórico é preservado mesmo após cancelamento.
- `fn_recalcular_abc_*` lê snapshots diretamente — não recalcula custos, apenas reclassifica com base nos valores congelados.

## Alternativas rejeitadas

**Custo calculado sempre em tempo real:** tornaria relatórios históricos instáveis. Rejeitado.

**Snapshot apenas no Menu, não na Refeição:** perderia a granularidade por prato dentro do evento. Rejeitado.
