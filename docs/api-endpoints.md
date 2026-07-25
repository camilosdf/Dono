# API Dono — Especificação de Endpoints

**Versão:** 2.0.0  
**Última atualização:** Frente A (Previsão de Consumo + IA/RAG/OCR)

Sobre o `schema.sql` e `business-queries.sql` já definidos. REST sobre HTTP/JSON, autenticação via Bearer JWT, RBAC conforme perfil do usuário (`CHEF`, `COMPRAS`, `ADMIN`, `GESTAO` — §4.7 da arquitetura).

---

## Convenções gerais

- **Paginação**: `?page=1&page_size=50` em todo GET de lista. Resposta: `{ "items": [...], "total": N, "page": 1, "page_size": 50 }`.
- **Erros**: formato único `{ "error": { "code": "INSUMO_EM_USO", "message": "...", "details": {...} } }`, com HTTP status coerente (400 validação, 403 permissão, 404 não encontrado, 409 conflito de estado).
- **Concorrência**: recursos mutáveis (`Prato`, `Refeicao`, `Menu`) expõem `versao`/`atualizado_em`; updates usam `If-Match` ou campo `versao` no corpo para evitar sobrescrita silenciosa.
- **Auditoria**: toda mutação grava `usuario_id` (extraído do JWT) — necessário para RBAC e para o campo `cotacoes.aprovado_por`.

---

## 1. Autenticação & Usuários

| Método | Rota | Perfil | Descrição |
|---|---|---|---|
| POST | `/auth/login` | público | Autentica, retorna JWT |
| GET | `/me` | qualquer autenticado | Dados do usuário logado + perfil |
| GET | `/usuarios` | ADMIN | Lista usuários |
| POST | `/usuarios` | ADMIN | Cria usuário e define perfil |
| PATCH | `/usuarios/{id}` | ADMIN | Altera perfil/ativo |

---

## 2. Gêneros, Categorias & Fornecedores

| Método | Rota | Perfil | Descrição |
|---|---|---|---|
| GET | `/generos` | qualquer | Lista os 2 gêneros fixos |
| GET | `/categorias?genero=` | qualquer | Lista categorias, filtro opcional por gênero |
| POST | `/categorias` | ADMIN | Cria categoria (subclasse de gênero) |
| GET | `/fornecedores?ativo=` | qualquer | Lista fornecedores |
| POST | `/fornecedores` | COMPRAS, ADMIN | Cadastra fornecedor |
| GET | `/fornecedores/{id}` | qualquer | Detalhe + categorias atendidas + histórico de cotações |
| PATCH | `/fornecedores/{id}` | COMPRAS, ADMIN | Atualiza dados/avaliação |
| POST | `/fornecedores/{id}/categorias` | COMPRAS, ADMIN | Vincula categoria atendida |

---

## 3. Insumos, Lotes & Cotações

| Método | Rota | Perfil | Descrição |
|---|---|---|---|
| GET | `/insumos?categoria_id=&genero=&ativo=` | qualquer | Lista insumos com filtros |
| POST | `/insumos` | COMPRAS, ADMIN | Cadastra insumo |
| GET | `/insumos/{id}` | qualquer | Detalhe (inclui `custo_medio_ponderado` atual) |
| PATCH | `/insumos/{id}` | COMPRAS, ADMIN | Atualiza cadastro (não altera custo diretamente) |
| DELETE | `/insumos/{id}` | ADMIN | Soft delete (`ativo=false`); 409 se houver `itens_receita` referenciando (FK `ON DELETE RESTRICT`) |
| GET | `/insumos/{id}/lotes` | qualquer | Histórico de lotes (FEFO) |
| POST | `/insumos/{id}/lotes` | **ADMIN apenas** | Registra novo lote (preço de aquisição). Dispara trigger `fn_atualizar_custo_medio_insumo` → evento `PrecoAtualizado` → recálculo em cascata (assíncrono) |
| GET | `/insumos/{id}/cotacoes?status=` | qualquer | Histórico de cotações do insumo |
| POST | `/cotacoes` | COMPRAS, ADMIN | Cotação manual (`origem=MANUAL`) |
| POST | `/cotacoes/ia-online` | COMPRAS, ADMIN | Dispara o agente de IA (§4.6a) para um ou mais `insumo_id`. **Assíncrono** — retorna `202 Accepted` + `job_id` |
| GET | `/cotacoes/ia-online/jobs/{job_id}` | COMPRAS, ADMIN | Status do job; ao concluir, lista as `Cotacao` criadas com `status=PENDENTE_REVISAO` |
| PATCH | `/cotacoes/{id}/aprovar` | COMPRAS, ADMIN | Aprova cotação pendente → grava `aprovado_por`, `status=APROVADA`; se o usuário optar por aplicar, cria um novo `lote_insumo` a partir do preço aprovado |
| PATCH | `/cotacoes/{id}/rejeitar` | COMPRAS, ADMIN | Marca como `REJEITADA`, não afeta custo |

---

## 4. Pratos & Fichas Técnicas

| Método | Rota | Perfil | Descrição |
|---|---|---|---|
| GET | `/pratos?genero_prato=&status=` | qualquer | Lista pratos |
| POST | `/pratos` | CHEF, ADMIN | Cria prato **com** `itens_receita` aninhados |
| GET | `/pratos/{id}` | qualquer | Detalhe completo (dados do prato + itens de receita + custo total calculado) |
| PATCH | `/pratos/{id}` | CHEF, ADMIN | Atualiza dados do prato (modo de preparo, rendimento etc.) |
| PUT | `/pratos/{id}/itens-receita` | CHEF, ADMIN | Substitui a lista de insumos da receita (recalcula custo) |
| PATCH | `/pratos/{id}/aprovar` | CHEF, ADMIN | Move `status: PENDENTE_APROVACAO → ATIVO` |
| DELETE | `/pratos/{id}` | ADMIN | Soft delete (`status=INATIVO`); bloqueado (409) se em uso por `itens_refeicao` |
| GET | `/pratos/{id}/abc` | qualquer | Classificação ABC dos insumos dentro do prato (lida de `classificacoes_abc`) |
| GET | `/pratos/{id}/ficha-tecnica?tipo=gerencial\|insumo\|operacional` | qualquer | Gera um dos 3 templates de ficha (§2.2.1). `insumo` requer `?insumo_id=` |
| GET | `/pratos/{id}/ficha-tecnica?tipo=gerencial&formato=pdf` | qualquer | Mesmo relatório em PDF — **implementado**, ver `app/ficha_tecnica_pdf.py` |

---

## 5. Regras de Composição & Estilos de Serviço

| Método | Rota | Perfil | Descrição |
|---|---|---|---|
| GET | `/regras-composicao?genero_refeicao=` | qualquer | Lista regras (categorias obrigatórias por gênero de refeição) |
| POST | `/regras-composicao` | ADMIN | Cria regra |
| DELETE | `/regras-composicao/{id}` | ADMIN | Remove regra |
| GET | `/estilos-servico` | qualquer | Lista os 7 estilos (Buffet, À La Carte, À Francesa, À Inglesa Direto/Indireto, À Russa, À Família) |
| POST | `/estilos-servico` | ADMIN | Cadastra estilo customizado |

---

## 6. Refeições

| Método | Rota | Perfil | Descrição |
|---|---|---|---|
| GET | `/refeicoes?data=&genero_refeicao=&status=` | qualquer | Lista refeições |
| POST | `/refeicoes` | CHEF, ADMIN | Cria refeição (status inicial `PLANEJADA`) |
| GET | `/refeicoes/{id}` | qualquer | Detalhe + itens (pratos) |
| POST | `/refeicoes/{id}/itens` | CHEF, ADMIN | Adiciona prato à refeição. **Validação**: `pratos.genero_prato` do prato precisa satisfazer alguma `regra_composicao.genero_prato_obrigatorio` do `genero_refeicao` — 422 se violar |
| DELETE | `/refeicoes/{id}/itens/{item_id}` | CHEF, ADMIN | Remove prato da refeição (só se `status=PLANEJADA`) |
| PATCH | `/refeicoes/{id}/confirmar` | CHEF, ADMIN | `status → CONFIRMADA`. Dispara `fn_snapshot_custo_refeicao` — congela `custo_snapshot` de cada item e materializa a Classificação ABC do escopo REFEICAO na hora |
| PATCH | `/refeicoes/{id}/executar` | CHEF, ADMIN | `status: CONFIRMADA → EXECUTADA`. Dá baixa REAL (FEFO) dos insumos consumíveis (`insumos.consumivel=TRUE`) usados na refeição, proporcional a `qtd_pessoas`, e registra cada baixa em `movimentacoes_estoque`. 422 `ESTOQUE_INSUFICIENTE` se faltar insumo |
| PATCH | `/refeicoes/{id}/servir` | CHEF, ADMIN | `status: EXECUTADA → SERVIDA` |
| PATCH | `/refeicoes/{id}/cancelar` | CHEF, ADMIN | `status → CANCELADA`. Permitido a partir de `PLANEJADA`/`CONFIRMADA` (cancelamento simples) **ou** `EXECUTADA` — estorna o estoque de verdade (`fn_estornar_execucao_refeicao`). Bloqueado a partir de `SERVIDA` |
| GET | `/refeicoes/{id}/abc` | qualquer | Classificação ABC dos pratos dentro da refeição |

Fluxo de status: `PLANEJADA → CONFIRMADA → EXECUTADA → SERVIDA`, com `CANCELADA` alcançável a partir de `PLANEJADA`, `CONFIRMADA` ou `EXECUTADA`.

---

## 7. Menus / Eventos

| Método | Rota | Perfil | Descrição |
|---|---|---|---|
| GET | `/menus?data_inicio=&status=` | qualquer | Lista menus/eventos |
| POST | `/menus` | CHEF, GESTAO, ADMIN | Cria menu (nome do evento, estilo de serviço, local, datas) |
| GET | `/menus/{id}` | qualquer | Detalhe + refeições em ordem cronológica |
| POST | `/menus/{id}/itens` | CHEF, GESTAO, ADMIN | Adiciona refeição ao menu com `ordem_cronologica` |
| PATCH | `/menus/{id}/confirmar` | GESTAO, ADMIN | `status → CONFIRMADO`. Dispara `fn_snapshot_custo_menu` — congela `custo_snapshot` de cada refeição no evento |
| PATCH | `/menus/{id}/realizar` | GESTAO, ADMIN | `status → REALIZADO`, após o evento acontecer |
| PATCH | `/menus/{id}/cancelar` | GESTAO, ADMIN | `status → CANCELADO` |
| GET | `/menus/{id}/abc` | qualquer | Classificação ABC das refeições dentro do menu |
| GET | `/menus/{id}/margem-contribuicao` | GESTAO, ADMIN | Custo total (via snapshots) vs. preço de venda, segregado por refeição |

---

## 8. Relatórios (existente)

| Método | Rota | Perfil | Descrição |
|---|---|---|---|
| GET | `/relatorios/curva-abc?escopo=INSUMO_GENERO\|PRATO\|REFEICAO\|MENU&id=` | GESTAO, ADMIN, COMPRAS | Leitura direta de `classificacoes_abc` |
| GET | `/relatorios/mrp?data_limite=` | COMPRAS, ADMIN | Executa `fn_mrp_previsao_compras` — lista de compras sugerida |
| GET | `/relatorios/ruptura-estoque?dias=7` | COMPRAS, ADMIN | Insumos que vencem ou zeram em N dias |
| GET | `/relatorios/consumo?categoria_id=&periodo_inicio=&periodo_fim=` | GESTAO, ADMIN | Consumo real **líquido** por categoria/gênero no período, a partir de `movimentacoes_estoque` |
| GET | `/relatorios/margem-menu/{menu_id}` | GESTAO, ADMIN | Mesmo dado de `/menus/{id}/margem-contribuicao`, formatado para exportação |
| GET | `/relatorios/{qualquer-acima}?formato=pdf\|xlsx` | conforme acima | Todos os relatórios de `/relatorios` aceitam exportação — **implementado**, ver `app/exportacao.py` |

---

## 9. Movimentações de Estoque (Perdas e Ajustes)

| Método | Rota | Perfil | Descrição |
|---|---|---|---|
| GET | `/movimentacoes/tipos-perda` | qualquer autenticado | Lista tipos de perda ativos (catálogo) |
| POST | `/movimentacoes/perda` | COMPRAS, ADMIN | Registra perda/ajuste manual de estoque. Se `lote_id` for omitido, aplica FEFO. Registra movimentação `AJUSTE_MANUAL` com `tipo_perda_id` e `observacao`. Auditoria preenchida automaticamente. |
| GET | `/movimentacoes?insumo_id=&tipo=&tipo_perda=&periodo_inicio=&periodo_fim=` | qualquer autenticado | Lista histórico de movimentações com filtros |

**Exemplo POST /movimentacoes/perda:**
```json
{
  "insumo_id": "uuid",
  "quantidade": 2.5,
  "tipo_perda": "VALIDADE",
  "observacao": "Lote venceu antes do uso",
  "lote_id": "uuid (opcional)"
}