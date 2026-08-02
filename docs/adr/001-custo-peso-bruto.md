# ADR 001 — Custo calculado sobre Peso Bruto (PB), não Peso Líquido (PL)

**Status:** Aceito  
**Data:** 2026-07  
**Contexto:** Sistema Dono — módulo de Fichas Técnicas e custeio de receitas

---

## Contexto

Ao registrar um Item de Receita, o sistema precisa calcular o custo do insumo naquele prato. Existem duas abordagens:

- **Peso Bruto (PB):** quantidade comprada antes de corte/limpeza.
- **Peso Líquido (PL):** quantidade aproveitável após perda de corte (`PL = PB / FC`).

## Decisão

O custo total do item de receita é calculado sobre o **Peso Bruto**:

```
custo_total_calculado = peso_bruto × custo_unitario_registrado
```

## Justificativa

O restaurante paga pela peça inteira comprada. Um filé mignon de 2,600 kg a R$ 60,00/kg custa R$ 156,00, independentemente de o rendimento líquido ser 2,000 kg. Calcular sobre Peso Líquido daria R$ 120,00 — como se a perda de corte saísse de graça, o que não reflete a realidade financeira.

A Ficha Técnica Gerencial do domínio confirma: `PB 2,600 kg × R$ 60,00 = R$ 156,00`. A soma dos ingredientes (R$ 175,43) só fecha com custeio sobre PB.

O Fator de Correção existe para informar rendimento, não para reduzir custo.

## Consequências

- `itens_receita.custo_total_calculado` é coluna `GENERATED ALWAYS AS (peso_bruto * custo_unitario_registrado) STORED`.
- `itens_receita.peso_liquido` é coluna `GENERATED ALWAYS AS (peso_bruto / NULLIF(fator_correcao, 0)) STORED` — usada para porções, não custeio.
- `margem_desperdicio_pct` cobre perdas de produção — não se sobrepõe ao FC.
- Propostas de mudança para custeio sobre PL devem ser rejeitadas sem validação explícita do domínio.

## Alternativas rejeitadas

**Custeio sobre Peso Líquido:** subestimaria o custo real. Rejeitado.
