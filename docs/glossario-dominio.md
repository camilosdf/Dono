# Glossário de Domínio — Sistema Dono
**Ubiquitous Language · Versão 1.0 · Agosto 2026**

Este documento define os termos canônicos do domínio gastronômico do Sistema Dono. Todo código, comentário, commit, teste e conversa entre desenvolvedores e especialistas de negócio deve usar exatamente estes termos — sem sinônimos, abreviações ou traduções livres. Quando um termo aqui definido conflitar com um nome de variável ou coluna existente, o código deve ser ajustado para seguir o glossário, não o contrário.

---

## Como usar este documento

Cada entrada segue o padrão:

> **Termo** — Definição canônica. Distinções importantes de termos parecidos. Artefato de código correspondente.

Termos em **negrito dentro de definições** são referências cruzadas a outras entradas deste glossário.

---

## Domínio Principal: Gastronomia de Eventos

---

### Insumo

Qualquer matéria-prima, material ou utensílio utilizado no preparo, serviço ou operação do restaurante/bufê. Não é sinônimo de "ingrediente" — utensílios também são Insumos.

Todo Insumo pertence a exatamente um **Gênero** e uma **Categoria**.

**Artefato:** tabela `insumos`, classe `InsumoOut` em `routes/insumos.py`.

---

### Gênero (de Insumo)

Classificação de primeiro nível dos **Insumos**. Existem exatamente dois Gêneros fixos no sistema:

- **ALIMENTICIO** — Alimentos e bebidas que compõem receitas.
- **OPERACIONAL_UTENSILIO** — Materiais não alimentícios (descartáveis, limpeza, utensílios, energéticos).

Gênero é a unidade de escopo da **Curva ABC** de Insumos: a classificação A/B/C de um Insumo é sempre relativa ao seu Gênero, nunca ao universo total de Insumos.

**Artefato:** tabela `generos` (2 linhas fixas), seed em `scripts/seeds.sql`.

---

### Categoria (de Insumo)

Classificação de segundo nível dos **Insumos**, subordinada ao **Gênero**. Exemplos dentro do Gênero Alimentício: Secos e Despensa, Hortifruti, Carnes/Aves/Peixes, Laticínios e Frios, Bebidas, Congelados. Exemplos dentro do Gênero Operacional: Descartáveis e Embalagens, Limpeza e Higienização, Utensílios.

Categoria é extensível sem alteração de código — novas subclasses são inseridas na tabela sem deploy.

**Artefato:** tabela `categorias`, endpoint `POST /categorias`.

---

### Lote

Entrada de estoque de um **Insumo** com rastreabilidade individual: fornecedor, data de aquisição, data de validade, quantidade adquirida e quantidade ainda disponível. É a unidade de controle FEFO.

Um Lote nunca é editado após criação — alterações de preço geram um novo Lote, não atualizam o existente. Isso preserva o histórico de custo.

Inserir um Lote dispara automaticamente o recálculo do **Custo Médio Ponderado** do Insumo e o evento `PrecoAtualizado` no **Outbox**.

**Artefato:** tabela `lotes_insumo`, endpoint `POST /insumos/{id}/lotes`.

---

### FEFO (First-Expire-First-Out)

Política de baixa de estoque que consome primeiro os **Lotes** com data de validade mais próxima. É a política padrão do sistema para todos os **Insumos** consumíveis. Implementada via índice `idx_lotes_fefo` e nas funções SQL de execução de refeição.

Não confundir com FIFO (First-In-First-Out), que ordena por data de entrada, não de validade.

---

### Custo Médio Ponderado

Custo unitário atual de um **Insumo**, calculado como média ponderada pelo volume de todos os **Lotes** registrados: `SUM(valor_aquisicao × quantidade) / SUM(quantidade)`. Recalculado automaticamente a cada novo Lote via trigger `trg_lote_insumo_custo`.

É o valor usado como `custo_unitario_registrado` nos **Itens de Receita** no momento do cadastro — não o preço do último lote.

**Artefato:** coluna `insumos.custo_medio_ponderado`, função `fn_atualizar_custo_medio_insumo`.

---

### Fator de Correção (FC)

Relação entre o **Peso Bruto** e o **Peso Líquido** de um **Insumo** dentro de uma **Receita**: `FC = PB / PL`. Representa a perda de corte/limpeza (aparas, ossos, casca). Um FC de 1,30 significa que 1,30 kg bruto rende 1,00 kg líquido.

O FC é atributo do par Insumo+técnica de preparo, não do Insumo isolado — o mesmo filé mignon tem FC diferente se servido inteiro ou em cubos. Por isso o FC fica em **Item de Receita**, não em Insumo.

**Artefato:** coluna `itens_receita.fator_correcao`.

---

### Peso Bruto (PB)

Quantidade de um **Insumo** comprada/utilizada antes de qualquer perda de corte ou limpeza. É a quantidade sobre a qual o custo é calculado: `custo_total = PB × custo_unitario`. O sistema deliberadamente usa PB no custeio, não Peso Líquido, porque o restaurante paga pela peça inteira comprada.

**Artefato:** coluna `itens_receita.peso_bruto`, coluna gerada `itens_receita.custo_total_calculado`.

---

### Peso Líquido (PL)

Quantidade utilizável de um **Insumo** após perdas de corte/limpeza: `PL = PB / FC`. Usado para calcular rendimento e porções — não para custeio.

**Artefato:** coluna gerada `itens_receita.peso_liquido`.

---

### Prato

Preparação culinária com receita base definida: lista de **Insumos** com quantidades, modo de preparo, rendimento base em porções, instruções de apresentação e controle de armazenamento. É a entidade que define custo de produção.

Não é o mesmo que "item de cardápio" ou "item de menu" — um Prato pode aparecer em múltiplas **Refeições** e **Menus**.

Todo Prato pertence a exatamente um **Gênero de Prato**.

**Artefato:** tabela `pratos`, endpoint `POST /pratos`.

---

### Gênero de Prato

Classificação funcional de um **Prato** dentro de uma refeição. Determina em quais **Gêneros de Refeição** o Prato pode aparecer, validado pelas **Regras de Composição**. Exemplos: Aperitivo & Couvert, Entrada, Prato Principal, Guarnição, Sorbet/Queijos, Sobremesa, Digestivo/Café, Padaria, Frios/Laticínios, Quentes, Frutas, Salgados Quentes e Assados, Finger Food, Bebida Quente, Bebida Fria.

**Artefato:** coluna `pratos.genero_prato`, referenciada em `regras_composicao.genero_prato_obrigatorio`.

---

### Item de Receita

Linha da receita de um **Prato**: um **Insumo** específico com seu **Peso Bruto**, **Fator de Correção**, tipo (ALIMENTICIO, OPERACIONAL ou UTENSILIO) e custo unitário registrado no momento do cadastro. O custo total da linha é calculado automaticamente como `PB × custo_unitario_registrado`.

**Artefato:** tabela `itens_receita`, colunas geradas `peso_liquido` e `custo_total_calculado`.

---

### Margem de Desperdício

Percentual adicional aplicado ao custo total de ingredientes de um **Prato** para cobrir perdas imprevisíveis de produção (queima, derramamento, ajuste de tempero). Diferente do **Fator de Correção**, que cobre perdas de corte/limpeza já conhecidas. Os dois não se sobrepõem: FC cobre perda de matéria-prima, margem de desperdício cobre perda de produto em produção.

`custo_total_receita = custo_ingredientes × (1 + margem_desperdicio_pct / 100)`

**Artefato:** coluna `pratos.margem_desperdicio_pct`.

---

### CMV por Porção (Custo de Mercadoria Vendida)

Custo de produção de uma porção de um **Prato**: `CMV = custo_total_receita / rendimento_base_porcoes`. Inclui ingredientes com margem de desperdício, mas não embalagem. É o indicador financeiro central para precificação.

**Artefato:** calculado em `GET /pratos/{id}/ficha-tecnica?tipo=gerencial`.

---

### Ficha Técnica

Relatório derivado de um **Prato** — não uma entidade separada. Existe em três templates para públicos distintos:

- **Gerencial**: para gestão e financeiro. Inclui PB, FC, PL, custo unitário, custo total por ingrediente, CMV por porção, margem de desperdício, custo de embalagem, preço de venda e margem de lucro bruta.
- **Insumo**: para estoque e compras. Foca em categoria, quantidades e condições de armazenamento. Requer `?insumo_id=` para filtrar por ingrediente específico.
- **Operacional**: para cozinha e produção. Inclui modo de preparo, apresentação, temperatura de serviço e equipamentos — deliberadamente sem dados financeiros.

**Artefato:** endpoint `GET /pratos/{id}/ficha-tecnica?tipo=gerencial|insumo|operacional`, geração PDF em `app/ficha_tecnica_pdf.py`.

---

### Refeição

Conjunto de **Pratos** servido em um horário específico para um número determinado de pessoas. É a unidade de planejamento e execução operacional do sistema.

Toda Refeição pertence a exatamente um **Gênero de Refeição** e passa pelo ciclo de vida: `PLANEJADA → CONFIRMADA → EXECUTADA → SERVIDA`, com `CANCELADA` alcançável a partir de PLANEJADA, CONFIRMADA ou EXECUTADA.

**Artefato:** tabela `refeicoes`, endpoints em `routes/refeicoes.py`.

---

### Gênero de Refeição

Tipo de serviço que define a composição obrigatória de uma **Refeição** via **Regras de Composição**. Exemplos: Café da Manhã, Lanche da Manhã, Almoço Executivo, Lanche da Tarde, Jantar, Colação, Fine Dining, Coquetel, Coffee Break.

**Artefato:** coluna `refeicoes.genero_refeicao`, referenciada em `regras_composicao.genero_refeicao`.

---

### Regra de Composição

Vínculo entre um **Gênero de Refeição** e os **Gêneros de Prato** aceitos nele. Validada ao adicionar um Prato a uma Refeição: se o gênero do Prato não constar nas regras do gênero da Refeição, a operação é rejeitada com `422 COMPOSICAO_INVALIDA`.

Regras são extensíveis sem deploy — novas combinações são inseridas na tabela.

**Artefato:** tabela `regras_composicao`, validação em `POST /refeicoes/{id}/itens`.

---

### Item de Refeição

Vínculo entre uma **Refeição** e um **Prato**. Armazena o **Custo Snapshot** do Prato no momento em que a Refeição é confirmada.

**Artefato:** tabela `itens_refeicao`, coluna `custo_snapshot`.

---

### Confirmação (de Refeição ou Menu)

Transição de status que congela o **Custo Snapshot** de cada item. Irreversível no sentido de que o snapshot não pode ser recalculado após a confirmação — cancelar é o único caminho para desfazer, e o snapshot histórico é preservado.

**Artefato:** `PATCH /refeicoes/{id}/confirmar`, trigger `fn_snapshot_custo_refeicao`.

---

### Execução (de Refeição)

Transição `CONFIRMADA → EXECUTADA`. É o momento em que o estoque real é debitado: o sistema calcula a quantidade de cada **Insumo** necessária (proporcional a `qtd_pessoas / rendimento_base`), debita os **Lotes** em ordem FEFO e registra cada baixa em **Movimentações de Estoque** com tipo `BAIXA_EXECUCAO`. Se o estoque for insuficiente para qualquer Insumo consumível, a operação é rejeitada com `422 ESTOQUE_INSUFICIENTE`.

É a fronteira entre planejamento e realidade operacional.

**Artefato:** `PATCH /refeicoes/{id}/executar`, função SQL `fn_executar_refeicao`.

---

### Estorno (de Execução)

Reversão de uma **Execução**: transição `EXECUTADA → CANCELADA`. Devolve ao estoque exatamente o que foi debitado, nos mesmos **Lotes**, via função SQL `fn_estornar_execucao_refeicao`. Gera **Movimentações de Estoque** do tipo `ESTORNO_CANCELAMENTO`. As movimentações originais de `BAIXA_EXECUCAO` não são apagadas — o histórico de "debitou e depois reverteu" é preservado.

**Artefato:** `PATCH /refeicoes/{id}/cancelar` (quando status = EXECUTADA), função SQL `fn_estornar_execucao_refeicao`.

---

### Custo Snapshot

Custo congelado no momento da **Confirmação** de uma **Refeição** ou **Menu**. Imutável após gravação. Garante que relatórios financeiros de eventos passados não mudem quando o preço de um **Insumo** sobe no futuro.

Em `itens_refeicao`: custo por porção do Prato no momento da confirmação da Refeição.
Em `itens_menu`: soma de `custo_snapshot × qtd_pessoas` de todos os itens da Refeição, representando o custo total daquela Refeição no evento.

**Artefato:** colunas `itens_refeicao.custo_snapshot` e `itens_menu.custo_snapshot`.

---

### Menu

Evento gastronômico completo: conjunto de **Refeições** em ordem cronológica, com estilo de serviço, local, datas e horários definidos. É a unidade de proposta comercial — o que o cliente contrata.

Ciclo de vida: `PLANEJADO → CONFIRMADO → REALIZADO`, com `CANCELADO` alcançável a qualquer momento antes de REALIZADO.

**Artefato:** tabela `menus`, endpoints em `routes/menus.py`.

---

### Item de Menu

Vínculo entre um **Menu** e uma **Refeição**, com ordem cronológica. Armazena o **Custo Snapshot** da Refeição no contexto do evento (custo total × número de pessoas).

**Artefato:** tabela `itens_menu`.

---

### Estilo de Serviço

Protocolo de atendimento que define como os pratos chegam aos convidados. Exemplos: Franco-Americano (Buffet/Self-Service), À La Carte, À Francesa, À Inglesa Direto, À Inglesa Indireto (Gueridon), À Russa, À Família. Associado ao **Menu**, não à **Refeição** — um evento inteiro segue um estilo, não cada refeição individualmente.

**Artefato:** tabela `estilos_servico`, seed em `scripts/seeds.sql`.

---

### Curva ABC (Classificação ABC)

Classificação financeira Pareto (80/15/5) aplicada em quatro escopos distintos:

- **INSUMO_GENERO**: insumos classificados pelo total gasto no gênero. A = 80% do custo, B = 15%, C = 5%.
- **PRATO**: insumos classificados pelo custo dentro de um prato específico.
- **REFEICAO**: pratos classificados pelo custo dentro de uma refeição.
- **MENU**: refeições classificadas pelo custo dentro de um menu/evento.

A classificação é materializada (não calculada em tempo real) e atualizada pelo **Worker de Outbox** após eventos de preço. Não pode ser coluna `GENERATED` porque depende do conjunto de linhas do escopo, não de uma linha individual.

**Artefato:** tabela `classificacoes_abc`, funções `fn_recalcular_abc_*` em `business-queries.sql`.

---

### Outbox (eventos_dominio)

Tabela que funciona como fila de eventos de domínio entre o banco e o **Worker de Outbox**. Cada evento é gravado atomicamente junto com a operação que o gerou (ex.: inserção de Lote grava evento `PrecoAtualizado` no mesmo trigger). O Worker consome os eventos e propaga efeitos: recálculo de custo em cascata (Prato → Refeição → Menu) e recálculo da **Curva ABC**.

Eventos com falha são retentados até 3 vezes e então movidos para dead-letter (`bloqueado_em IS NOT NULL`).

**Artefato:** tabela `eventos_dominio`, função `fn_processar_eventos_pendentes`, `app/worker.py`.

---

### Worker de Outbox

Processo assíncrono que consome o **Outbox** em loop, usando `FOR UPDATE SKIP LOCKED` para suportar múltiplas réplicas sem processamento duplicado. Injeta contexto de auditoria como processo de sistema (`ip_origem = 'worker://dono-worker'`).

**Artefato:** `app/worker.py`.

---

### Worker de IA

Processo assíncrono que processa jobs de IA (`ia_jobs`): OCR de notas fiscais, geração de embeddings para RAG. Injeta contexto de auditoria com o `usuario_id` do solicitante do job.

**Artefato:** `app/ai_worker.py`.

---

### Worker de Previsão

Processo assíncrono que executa periodicamente `fn_calcular_previsao_consumo` para todos os **Insumos** ativos, combinando demanda de **Menus** agendados (via MRP, calculado uma única vez para todo o horizonte) com média histórica de **Movimentações de Estoque**. Usa lock Redis para evitar execuções concorrentes entre réplicas.

**Artefato:** `app/forecast_worker.py`.

---

### Movimentação de Estoque

Registro imutável de qualquer entrada ou saída de estoque, com tipo explícito. Tipos:

| Tipo | Direção | Quando |
|---|---|---|
| `ENTRADA` | + | Novo **Lote** cadastrado |
| `BAIXA_EXECUCAO` | − | **Execução** de Refeição (baixa FEFO) |
| `ESTORNO_CANCELAMENTO` | + | **Estorno** de Execução |
| `PERDA_VALIDADE` | − | Descarte por vencimento |
| `PERDA_QUEBRA` | − | Acidente, contaminação |
| `PERDA_PRODUCAO` | − | Sobra suja, rendimento abaixo do esperado |
| `AJUSTE_INVENTARIO` | ± | Inventário físico |
| `TRANSFERENCIA` | ± | Entre estoques/unidades |
| `DEVOLUCAO` | + | Devolução a fornecedor |

Movimentações nunca são apagadas — o histórico é a fonte de verdade para consumo real, relatórios de perdas e calibração do **Worker de Previsão**.

**Artefato:** tabela `movimentacoes_estoque`, endpoint `POST /movimentacoes/perda`.

---

### MRP (Material Requirements Planning / Previsão de Compras)

Cálculo que, dado um horizonte de datas, determina a necessidade bruta de cada **Insumo** para todos os **Menus** agendados (PLANEJADO ou CONFIRMADO), subtrai o estoque disponível e gera lista de compras sugerida. Prioriza **Insumos** Classe A da **Curva ABC**.

O MRP é a entrada primária do **Worker de Previsão**: a necessidade de evento é alocada na data do menu, não diluída pelo horizonte inteiro.

**Artefato:** função SQL `fn_mrp_previsao_compras`, endpoint `GET /relatorios/mrp`.

---

### Previsão de Consumo

Estimativa de quantidade necessária de um **Insumo** para cada dia do horizonte, combinando:
1. **MRP por evento** (demanda de Menus agendados, alocada na data do evento — método `MRP_EVENTO`).
2. **Média histórica** de **Movimentações** `BAIXA_EXECUCAO` por dia da semana (calibração — método `MEDIA_HISTORICA`).
3. Quando ambos existem: método `HIBRIDO_EVENTO_HISTORICO`.

**Artefato:** função SQL `fn_calcular_previsao_consumo`, tabela `previsoes_consumo`, `app/forecast_worker.py`.

---

### Event Store

Log auditável e imutável de todos os eventos de negócio significativos (além do **Outbox**, que é consumível). Cada evento registra `aggregate_type`, `aggregate_id`, `event_type`, `version`, `usuario_id`, `ip_origem` e `user_agent`. Permite reconstrução de estado e auditoria regulatória.

**Artefato:** tabela `event_store`, função `fn_registrar_evento`, contexto de sessão via `fn_set_audit_context`.

---

### Cotação

Proposta de preço de um **Insumo** por um **Fornecedor**, com origem (`MANUAL` ou `IA_ONLINE`) e status (`PENDENTE_REVISAO`, `APROVADA`, `REJEITADA`). Uma Cotação aprovada pode gerar um novo **Lote** ao preço cotado. Cotações de IA nunca são aplicadas automaticamente — exigem aprovação humana.

**Artefato:** tabela `cotacoes`, endpoints `POST /cotacoes` e `PATCH /cotacoes/{id}/aprovar`.

---

### RAG (Retrieval-Augmented Generation)

Módulo de IA que responde perguntas usando documentos internos (fichas técnicas, POPs, especificações) como contexto. O documento é convertido em embedding vetorial e armazenado via PgVector no próprio PostgreSQL. A consulta busca os documentos mais similares e os injeta no prompt do modelo local (Ollama).

**Artefato:** tabela `documentos` (com coluna `embedding vector`), `app/rag.py`, endpoint `POST /ia/rag/consultar`.

---

### OCR de Nota Fiscal

Pipeline de extração de dados de notas fiscais (imagem ou PDF) usando Tesseract como motor primário e PaddleOCR como fallback. O resultado é pré-preenchido para revisão humana antes de gerar **Lotes** — nunca importado automaticamente.

**Artefato:** `app/ocr.py`, `app/ai_worker.py`, tipo de job `OCR_NOTA` em `ia_jobs`.

---

### RBAC (Role-Based Access Control)

Controle de acesso por perfil de usuário. Quatro perfis:

| Perfil | Responsabilidades principais |
|---|---|
| `CHEF` | Cria e edita **Pratos**, adiciona itens a **Refeições**, executa e cancela Refeições |
| `COMPRAS` | Cadastra **Insumos**, **Lotes**, **Cotações** manuais, aprova cotações de IA |
| `ADMIN` | Acesso total, incluindo gestão de usuários e configurações de sistema |
| `GESTAO` | Leitura de relatórios financeiros, confirmação e realização de **Menus** |

**Artefato:** `app/dependencies.py` (`require_perfil`), `app/middleware.py` (`RateLimitMiddleware`, `AuditContextMiddleware`).

---

## Termos que NÃO devem ser usados

| Evitar | Usar em vez disso |
|---|---|
| Ingrediente | **Insumo** (quando alimentício) ou **Item de Receita** (quando no contexto de um Prato) |
| Cardápio | **Menu** (o evento) ou **Ficha Técnica** (o documento do Prato) |
| Produto | **Insumo** ou **Prato**, dependendo do contexto |
| Consumo | **Movimentação de Estoque** (tipo `BAIXA_EXECUCAO`) — "consumo" é ambíguo |
| Baixar estoque | **Executar** (a Refeição) — é a operação que gera a baixa |
| Semiacabado | Não existe no modelo atual — usar **Prato** com status `PENDENTE_APROVACAO` para rascunhos |
| Preparação | **Prato** (como entidade) ou **Execução** (como operação) |
| Custo real | **Custo Snapshot** (histórico) ou **Custo Médio Ponderado** (atual do Insumo) |
| Estoque mínimo | Não implementado — usar **Ruptura de Estoque** (relatório de projeção) |
| Receita | **Prato** (como entidade com receita embutida) — "receita" sozinha é ambígua com receita financeira |

---

## Mapa de artefatos por termo

| Termo | Tabela(s) | Endpoint(s) principal | Função SQL |
|---|---|---|---|
| Insumo | `insumos` | `POST /insumos` | — |
| Lote | `lotes_insumo` | `POST /insumos/{id}/lotes` | `fn_atualizar_custo_medio_insumo` |
| Prato | `pratos`, `itens_receita` | `POST /pratos` | — |
| Ficha Técnica | — (relatório) | `GET /pratos/{id}/ficha-tecnica` | — |
| Refeição | `refeicoes`, `itens_refeicao` | `POST /refeicoes` | `fn_snapshot_custo_refeicao` |
| Execução | `movimentacoes_estoque` | `PATCH /refeicoes/{id}/executar` | `fn_executar_refeicao` |
| Estorno | `movimentacoes_estoque` | `PATCH /refeicoes/{id}/cancelar` | `fn_estornar_execucao_refeicao` |
| Menu | `menus`, `itens_menu` | `POST /menus` | `fn_snapshot_custo_menu` |
| Curva ABC | `classificacoes_abc` | `GET /relatorios/curva-abc` | `fn_recalcular_abc_*` |
| MRP | — (função) | `GET /relatorios/mrp` | `fn_mrp_previsao_compras` |
| Previsão | `previsoes_consumo` | `GET /relatorios/previsoes` | `fn_calcular_previsao_consumo` |
| Movimentação | `movimentacoes_estoque` | `POST /movimentacoes/perda` | `fn_registrar_perda` |
| Event Store | `event_store` | — (interno) | `fn_registrar_evento`, `fn_set_audit_context` |
| Outbox | `eventos_dominio` | — (interno) | `fn_processar_eventos_pendentes` |
| Cotação | `cotacoes` | `POST /cotacoes` | — |
