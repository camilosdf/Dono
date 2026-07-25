# Arquitetura do Sistema Dono (Gestão de Insumos, Receitas e Cardápios)

## 1. Visão geral

O domínio descrito forma uma hierarquia de composição clara:

```
Insumo → Prato → Refeição → Menu (Evento)
   ↑         ↑         ↑         ↑
Fornecedor  Receita  Estilo de  Cronograma
/Cotação    /Ficha   Serviço    do evento
            Técnica
```

Cada nível "herda" custo do nível abaixo e recebe sua própria Classificação ABC (Pareto) calculada sobre o custo total daquele nível. Isso sugere um **motor de classificação ABC único e reutilizável**, parametrizado pelo escopo (gênero de insumo, prato, refeição, menu), em vez de quatro implementações separadas.

Proponho uma arquitetura em camadas, orientada a domínio (DDD-lite), com um serviço de IA desacoplado que consome os mesmos dados via API interna.

```
┌─────────────────────────────────────────────────────────┐
│  Frontend (Web app - gestão) + App mobile (chef/estoque) │
├─────────────────────────────────────────────────────────┤
│  API Gateway / BFF                                        │
├───────────────┬───────────────┬───────────────┬──────────┤
│  Módulo        │  Módulo       │  Módulo        │  Módulo  │
│  Insumos &     │  Receitas &   │  Cardápios &   │  IA &    │
│  Estoque       │  Custeio      │  Eventos       │  Cotação │
├───────────────┴───────────────┴───────────────┴──────────┤
│  Motor de Classificação ABC (serviço compartilhado)       │
├─────────────────────────────────────────────────────────┤
│  Módulo de Relatórios (BI leve / exportação)               │
├─────────────────────────────────────────────────────────┤
│  Banco de dados relacional + fila de eventos (async)       │
└─────────────────────────────────────────────────────────┘
```

## 2. Modelo de dados (entidades principais)

### 2.1 Insumo
```
Insumo {
  id, nome, genero (ALIMENTICIO | OPERACIONAL_UTENSILIO)
  categoria (subclasse do gênero — FK para tabela Categoria)
  unidade (KG | L | M | PC)
  peso_bruto, apresentacao (varejo|atacado + tipo)
  marcas_aceitaveis[] , localizacao_estoque
  consumivel (bool)  // false para utensílios
  classificacao_abc (A|B|C) // recalculada, não editável manualmente
}

LoteInsumo {              // controle de estoque real (FIFO/FEFO)
  id, insumo_id, fornecedor_id, valor_aquisicao,
  data_aquisicao, data_validade, quantidade, quantidade_disponivel
}

Fornecedor {
  id, nome, contato, categorias_fornecidas[], avaliacao,
  prazo_entrega_medio, condicoes_pagamento
}

Cotacao {
  id, insumo_id, fornecedor_id, preco_unitario, data_cotacao,
  validade_cotacao, origem (MANUAL | IA_ONLINE), status
}
```
Categoria é modelada como tabela própria (não enum fixo) para permitir novas subclasses sem alterar código — cada categoria referencia seu gênero pai.

### 2.2 Prato (Receita)
```
Prato {
  id, nome, genero_prato (Aperitivo, Entrada, Prato Principal, ...)
  tempo_preparo, rendimento_base, tamanho_porcao,
  modo_preparo (rich text/steps), instrucoes_apresentacao,
  equipamentos_utilizados[],
  temperatura_servico,
  armazenamento_pre_evento (faixa_temp, tempo_maximo_horas),
  margem_desperdicio_pct,        // "custo invisível" aplicado sobre o custo total
  custo_embalagem,               // por porção, quando aplicável (delivery)
  preco_venda_praticado,
  classificacao_abc (A|B|C)
}

ItemReceita {              // ligação Prato-Insumo (N:N com dados extras)
  id, prato_id, insumo_id,
  peso_bruto, fator_correcao, peso_liquido,   // PL = PB / FC
  unidade, tipo (ALIMENTICIO|OPERACIONAL|UTENSILIO),
  custo_unitario, custo_total_calculado   // peso_liquido * custo_unitario
}
```
O **Fator de Correção (FC)** é atributo do par Insumo+técnica de preparo (ex.: filé mignon limpo vs. com aparas), não do insumo isoladamente — por isso fica em `ItemReceita`, não em `Insumo`. Isso permite que o mesmo insumo tenha FC diferente em receitas diferentes.

**Custo total da receita** = Σ `custo_total_calculado` de todos os itens × (1 + `margem_desperdicio_pct`).
**Custo por porção (CMV)** = custo total da receita / rendimento_base.
**Margem de lucro bruta** = (preço_venda_praticado − custo_total_porção) / preço_venda_praticado — campo calculado, não armazenado, para nunca ficar dessincronizado do custo real.

### 2.2.1 Fichas técnicas como visões (não como entidades separadas)
Os três modelos de ficha compartilhados — Gerencial, Insumo e Operacional — não são tabelas novas: são **templates de relatório** que recortam e formatam os mesmos dados de `Prato` + `ItemReceita` + `Insumo` para públicos diferentes:

| Ficha | Público | Campos exibidos |
|---|---|---|
| **Gerencial** | Gestão/financeiro | Rendimento, tempo, tabela de ingredientes com PB/FC/PL/custos, custo total, margem de desperdício, CMV por porção, embalagem, preço de venda, margem de lucro |
| **Insumo** | Estoque/compras | Categoria, PB, unidade, custo unitário/total do insumo, equipamentos e condições de armazenamento |
| **Operacional** | Cozinha/produção | Tempo de preparo, rendimento, equipamentos, ingredientes e quantidades **sem custo**, modo de preparo em passos, apresentação, temperatura de serviço — propositalmente sem dados financeiros |

Isso vira um único módulo de geração de relatórios com 3 templates parametrizados pelo mesmo `prato_id`, em vez de manter dados duplicados. Reforça a decisão do §4.5 (Relatórios) abaixo.

### 2.3 Refeição
```
Refeicao {
  id, genero_refeicao (Café da Manhã, Almoço, Fine Dining, ...)
  data, horario_inicio, horario_fim, qtd_pessoas
  classificacao_abc (A|B|C)
}

ItemRefeicao {              // Prato dentro da refeição
  id, refeicao_id, prato_id, categoria_composicao,
  custo_snapshot           // custo do prato NO MOMENTO em que entrou na refeição
  (ex.: "Prato Principal", "Guarnição" — valida contra
  a composição obrigatória do gênero de refeição)
}
```

### 2.4 Menu (Evento)
```
Menu {
  id, nome_evento, data_criacao, estilo_servico_id,
  data_inicio, horario_inicio, data_fim, horario_fim, local_servico
}

EstiloServico {
  id, nome, descricao, dinamica
  // Buffet, À La Carte, À Francesa, À Inglesa Direto/Indireto,
  // À Russa, À Família
}

ItemMenu {                   // Refeição dentro do menu, ordenada
  id, menu_id, refeicao_id, ordem_cronologica,
  custo_snapshot            // custo da refeição no momento em que entrou no menu
}
```

**Imutabilidade de custo histórico**: quando o preço de um insumo sobe, o custo de `Prato`/`Refeicao`/`Menu` **futuros** (ainda não confirmados) é recalculado normalmente — mas eventos já realizados (refeições/menus servidos no passado) não podem ter seu custo alterado retroativamente, senão relatórios financeiros de eventos passados mudam sozinhos. Por isso `ItemRefeicao` e `ItemMenu` guardam `custo_snapshot`, gravado no momento em que o item é confirmado/servido, e os relatórios históricos leem o snapshot, não o custo atual do `Prato`.

### 2.5 Regras de composição por gênero
As composições obrigatórias descritas (ex.: café da manhã exige Padaria + Frios/Laticínios + Quentes + Bebidas + Frutas; bebidas geladas sempre com opção Diet) ficam em uma tabela `RegraComposicao` (gênero_refeição → categorias obrigatórias de prato), validada na criação de `ItemRefeicao` em vez de hardcoded — assim novos gêneros de refeição não exigem deploy de código.

## 3. Motor de Classificação ABC (Pareto 80/15/5) — materializado

Cálculo sob demanda a cada leitura de dashboard não escala bem quando há muitos insumos/pratos. A classificação ABC fica em uma tabela materializada `classificacoes_abc` (escopo, id_pai, item_id, custo, classe, percentual_acumulado, atualizado_em), lida diretamente pelos dashboards — nunca recalculada na hora da consulta.

```
classificarABC(escopo: {tipo, id_pai}, itens: [{id, custo}]) -> grava em classificacoes_abc
```

Algoritmo (inalterado):
1. Soma o custo total do escopo (ex.: todos os insumos de um gênero; todos os itens de um prato; todos os pratos de uma refeição; todas as refeições de um menu).
2. Ordena itens por custo decrescente.
3. Calcula percentual acumulado.
4. Classifica: A até 80% acumulado, B até 95%, C no restante.

**Gatilho de recálculo**: evento de domínio `PrecoAtualizado`, disparado ao cadastrar novo preço de aquisição de um insumo, propaga em cascata:
1. Atualiza custo do(s) `Prato`(s) que usam aquele insumo.
2. Atualiza custo da(s) `Refeicao`(ões) futuras que usam aquele(s) prato(s) (eventos já confirmados ficam intocados — ver `custo_snapshot` no §2.4).
3. Atualiza custo do(s) `Menu`(s) futuro(s) correspondente(s).
4. Reexecuta `classificarABC` em cada escopo afetado e regrava `classificacoes_abc`.

Roda em fila assíncrona (não bloqueia a requisição de quem cadastrou o preço). Uma fila simples (ex. RabbitMQ, ou até processamento em background do próprio banco/worker) é suficiente para o volume esperado de um restaurante — não há necessidade de Kafka a menos que o sistema passe a atender múltiplas unidades com alto volume de eventos simultâneos.

Nota técnica: essa classificação **não pode** ser implementada como coluna `GENERATED ALWAYS AS ... STORED` no Postgres, porque depende do custo de *todas* as outras linhas do mesmo escopo (é um cálculo de conjunto, não de linha) — precisa ser a tabela materializada + trigger/worker descritos acima.

## 4. Módulos funcionais

### 4.1 Insumos & Estoque
- CRUD de insumos, categorias, fornecedores, lotes.
- Controle FEFO (First-Expire-First-Out) com alertas de validade.
- Custo médio ponderado por insumo (recalculado a cada entrada de lote).
- Classificação ABC por gênero.

### 4.2 Receitas & Custeio
- Ficha técnica (CRUD de Prato + ItemReceita).
- Cálculo de custo por porção e custo total da receita.
- Simulação de rendimento (escalar receita para N pessoas).
- Classificação ABC de insumos dentro do prato.

### 4.3 Cardápios & Eventos
- Montagem de Refeição a partir de Pratos, validada por `RegraComposicao`.
- Montagem de Menu (evento) com refeições em ordem cronológica e Estilo de Serviço.
- Checagem de disponibilidade de estoque antes de confirmar o menu.
- Classificação ABC de pratos na refeição e de refeições no menu.

### 4.4 Cotações
- Cadastro manual de cotações por fornecedor.
- Comparação automática entre fornecedores (menor preço, melhor prazo).
- Histórico de variação de preço por insumo (série temporal).
- Geração de pedido de compra sugerido, priorizando insumos Categoria A (maior impacto financeiro).

### 4.5 Relatórios
- Emissão de Ficha Técnica em 3 templates (Gerencial, Insumo, Operacional — ver §2.2.1), gerados a partir dos mesmos dados de `Prato`/`ItemReceita`.
- Custo de prato/refeição/menu, com detalhamento por insumo.
- Curva ABC visual (gráfico de Pareto) em cada nível, lida direto de `classificacoes_abc`.
- Consumo por categoria/gênero em período.
- Ruptura de estoque projetada (baseada em consumo médio + eventos futuros agendados).
- **Previsão de compras (MRP)**: a partir dos Menus futuros agendados, calcula a necessidade bruta de insumos, subtrai o estoque atual (`LoteInsumo.quantidade_disponivel`) e gera lista de compras sugerida, priorizando insumos Categoria A.
- **Margem de contribuição do Menu**: custo total do Menu (via `custo_snapshot`) vs. preço de venda praticado, segregado por Refeição.
- Exportação PDF/Excel.

### 4.6 IA (dois recursos específicos pedidos)

**a) Cotação online assistida por IA**
- Agente de busca (scraping/API de fornecedores) que, dado um insumo (nome, categoria, marca aceitável, unidade), consulta fontes online e retorna candidatos a cotação: preço, fornecedor, prazo.
- Resultado entra como `Cotacao` com `origem = IA_ONLINE` e `status = PENDENTE_REVISAO` — **nunca** é aplicado automaticamente ao custo sem confirmação humana, para evitar erro de matching (ex.: unidade errada, marca incorreta, produto errado).
- Exceção: o **alarme** ("preço encontrado está X% abaixo do custo médio atual") pode disparar automaticamente, já que só notifica — quem decide comprar em maior volume é a pessoa responsável por compras, olhando a cotação pendente antes de aprová-la.
- Justificativa de design: cotação afeta custo real do restaurante — mantém humano no loop na parte que muda dinheiro, automatiza só a parte que só informa.

**b) Prospecção de pratos baseada em estoque**
- Dado o estoque atual (insumos disponíveis, com quantidade e validade), a IA sugere pratos executáveis, em dois modos distintos:
  - **Match direto** (pratos já cadastrados que usam aqueles insumos): query relacional simples (`JOIN` entre insumos em excesso/vencendo e `ItemReceita`) — não precisa de embeddings/vector DB, é busca estruturada.
  - **Sugestão criativa** (quando não há prato cadastrado que aproveite bem aqueles insumos): aí sim um LLM recebe a lista de insumos disponíveis + contexto (estilo do menu, estação do ano) e gera um rascunho de `Prato` novo (nome, modo de preparo, combinação de insumos).
  - Cada sugestão retorna: prato, insumos usados, insumos faltantes (se houver), impacto na Classificação ABC do estoque.
- Este recurso é sugestivo — o rascunho gerado pela IA fica pendente de aprovação; o chef revisa e só então o prato vira `Prato` formal no cadastro.

### 4.7 Permissões (RBAC)
Transversal a todos os módulos acima, não é um módulo de negócio separado:
- **Chef/Cozinha**: cria e edita `Prato` (receita, modo de preparo), consulta estoque, aprova/rejeita rascunhos de prato gerados pela IA.
- **Compras/Estoque**: cadastra `Insumo`, `LoteInsumo`, `Cotacao` manual, aprova cotações de IA pendentes.
- **Administrador**: único perfil que pode alterar `custo_medio_compra`/preço de aquisição de insumo diretamente (ação que dispara o evento `PrecoAtualizado` e recalcula custos em cascata) e configurar `RegraComposicao`/`EstiloServico`.
- **Gestão/Financeiro**: leitura de todos os relatórios, incluindo Ficha Gerencial e margens; sem permissão de escrita em custo.

## 5. Stack sugerida (enxuta no MVP, com caminho de evolução)

- **Backend**: monólito modular — Node.js/NestJS ou Python/FastAPI (produtividade maior no MVP) ou Java/Spring Boot (se a equipe já tem essa base e valoriza tipagem forte para cálculo financeiro). Qualquer uma sustenta a arquitetura em camadas descrita.
- **Banco de dados**: PostgreSQL, com a tabela materializada `classificacoes_abc` (§3) resolvendo a leitura de dashboard sem precisar de Redis desde o início. JSONB para `modo_preparo`/`instrucoes_apresentacao` (estrutura flexível por prato).
- **Fila/eventos**: fila simples (RabbitMQ ou equivalente) para o evento `PrecoAtualizado` e recálculo em cascata. Kafka só se o volume justificar (múltiplas unidades, alto throughput de eventos) — não é requisito de MVP.
- **Cache**: opcional no MVP; introduzir Redis apenas se a leitura de `classificacoes_abc`/relatórios directo do Postgres virar gargalo medido, não por antecipação.
- **IA**: microsserviço separado (Python + LangChain ou equivalente) consumindo LLM (OpenAI/Gemini) e um adaptador de scraping para cotação, com acesso restrito e majoritariamente somente-leitura ao banco de estoque/receitas. Vector DB (Chroma/Weaviate) só entra quando a sugestão criativa de pratos (§4.6b) estiver ativa — não é necessário para o match direto.
- **Frontend**: aplicação web para gestão (cadastros, relatórios) + app mobile leve para chefs/estoquistas (dar baixa em insumo, checar validade).

## 6. Ordem de implementação sugerida

1. Insumos, Categorias, Fornecedores, Lotes (base de tudo).
2. Motor de Classificação ABC materializado (usado por todos os módulos seguintes).
3. Receitas/Fichas técnicas + custeio, com `custo_snapshot` desde o início (evita retrabalho de migração depois).
4. RBAC básico (Chef / Compras / Admin / Gestão) — barato de fazer cedo, caro de adicionar depois num sistema que já mexe com custo.
5. Refeições e regras de composição.
6. Menus/Eventos + Estilos de Serviço.
7. Cotações manuais + relatórios básicos + MRP (previsão de compras).
8. IA: prospecção de pratos — match direto primeiro (SQL, baixo risco), sugestão criativa depois (LLM).
9. IA: cotação online (maior risco — precisa de revisão humana e integração externa).
