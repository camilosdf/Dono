# ADR 009 — Previsão de consumo por evento, não por série temporal contínua

**Status:** Aceito  
**Data:** 2026-08  
**Contexto:** Sistema Dono — módulo de Previsão de Consumo (forecast_worker)

---

## Contexto

O `forecast_worker` precisa estimar a demanda futura de insumos para alimentar o MRP e alertas de ruptura de estoque. Existem duas abordagens:

1. **Série temporal contínua:** média móvel de consumo histórico por dia da semana.
2. **MRP por evento:** necessidade calculada a partir dos menus agendados, alocada na data do evento.

## Decisão

A entrada primária da previsão são os **menus agendados** (MRP calculado uma única vez para todo o horizonte). A média histórica de `movimentacoes_estoque.BAIXA_EXECUCAO` é usada como **calibração** para dias sem evento agendado.

O MRP é calculado **uma única vez** para todo o horizonte, com necessidade de cada evento alocada na **data de início do menu** — não diluída pelo número de dias do horizonte.

Três métodos de saída:
- `MRP_EVENTO`: dia tem evento agendado, sem histórico.
- `MEDIA_HISTORICA`: dia sem evento agendado, com ou sem histórico.
- `HIBRIDO_EVENTO_HISTORICO`: dia tem evento agendado e histórico.

## Justificativa

Gastronomia de eventos tem demanda **discreta por evento**, não contínua. Um casamento para 200 pessoas consome em uma noite mais do que um mês inteiro de almoços executivos. Séries temporais puras tratariam esse pico como outlier ou o diluiriam pelo período — subestimando a compra necessária para o evento específico.

A versão anterior da função chamava `fn_mrp_previsao_compras(v_data)` dentro de um loop dia×insumo: com 30 dias e 50 insumos ativos, isso eram 1.500 chamadas ao MRP, cada uma com JOINs pesados. Além de ineficiente, dividia a necessidade do evento por `p_dias` — se um evento em dia 15 precisa de 10 kg, o sistema previa 0,33 kg/dia, subestimando o pico real.

## Consequências

- `fn_calcular_previsao_consumo` cria tabela temporária `_mrp_horizonte` com `ON COMMIT DROP` para armazenar o MRP calculado uma única vez.
- O MRP agora é o `JOIN` direto em `menus`/`itens_menu`/`refeicoes`/`itens_receita` — não a chamada recursiva à `fn_mrp_previsao_compras`.
- Dias com `quantidade_prevista = 0` e método `MEDIA_HISTORICA` são normais para sistemas novos sem histórico — não indicam bug.
- O campo `metodo` em `previsoes_consumo` permite ao usuário entender a origem de cada previsão.
- Validado com dados sintéticos: evento em +3 dias gera `quantidade_prevista = 10.000` com método `MRP_EVENTO` na data exata.

## Alternativas rejeitadas

**Série temporal pura (ARIMA, Prophet):** trata picos de evento como outliers, não como demanda planejada. Inapropriado para gastronomia de eventos. Rejeitado.

**MRP chamado por dia×insumo (implementação anterior):** N×M chamadas ao MRP — ineficiente e matematicamente errado (dilui necessidade do evento). Substituído por esta decisão.
