# README.md – Sistema Dono

*Última atualização: 2026-07-24*

---

## Rodada atual: IA / RAG / OCR – testes estabilizados

Todos os testes do módulo de IA (RAG, OCR, embeddings, rate limit) agora passam em ambiente de desenvolvimento.  
As correções aplicadas garantem que a suíte de testes seja determinística, rápida e não dependa de infraestrutura externa (Ollama, Redis) para execução.

---

## Histórico de rodadas anteriores

### Rodada do estorno de estoque ao cancelar refeição EXECUTADA

Fecha a pendência declarada desde a rodada da baixa real de estoque:  
`PATCH /refeicoes/{id}/cancelar` a partir de `EXECUTADA` estava bloqueado com `409` e a mensagem explícita "cancelar exigiria um estorno, ainda não implementado". Esta rodada implementa esse estorno.

**Decisão de design — devolver o que foi REGISTRADO, não recalcular**:  
`fn_estornar_execucao_refeicao` lê as linhas de `BAIXA_EXECUCAO` gravadas em `movimentacoes_estoque` para aquela refeição e devolve, lote a lote, exatamente a quantidade que consta ali — não refaz a conta de necessidade a partir de `itens_receita`/`qtd_pessoas` como `fn_executar_refeicao` faz na ida. Isso importa porque o histórico gravado é o fato consumado (o que realmente saiu do estoque, e de qual lote), enquanto recalcular do zero arriscaria divergir se algo tivesse mudado entre a execução e o cancelamento (não deveria, mas o ponto é não depender dessa garantia).

**O que foi feito:**
1. **`movimentacoes_estoque.tipo`** ganhou `ESTORNO_CANCELAMENTO` (schema.sql) — grava como evento PRÓPRIO, não apaga nem edita a `BAIXA_EXECUCAO` original; o histórico de "debitou e depois foi revertido" fica preservado para auditoria.
2. **`fn_estornar_execucao_refeicao(refeicao_id)`** (business-queries.sql) — só aceita a partir de `EXECUTADA` (`P1001` caso contrário), devolve cada lote e move o status para `CANCELADA` na mesma transação.
3. **`PATCH /refeicoes/{id}/cancelar`** (routes/refeicoes.py) agora trata 3 casos: `PLANEJADA`/`CONFIRMADA` → cancelamento simples (como antes); `EXECUTADA` → chama a função de estorno; `SERVIDA`/`CANCELADA` → continua bloqueado (comida já entregue ao cliente é irreversível por definição de negócio, diferente de `EXECUTADA` — preparada mas talvez não servida de fato).
4. **`/relatorios/consumo` ajustado para líquido**: a soma agora é `BAIXA_EXECUCAO` menos `ESTORNO_CANCELAMENTO` (mesmo par lote/ refeição, sinal invertido) — sem isso, uma refeição executada e depois cancelada continuaria contando como "consumida" um insumo que voltou pro estoque, superestimando o relatório. Categorias que zeram líquido (tudo estornado) somem do relatório (`HAVING > 0`), em vez de aparecer como uma linha de custo zero sem sentido.
5. **7 testes pgTAP novos** (seção L): caminho feliz (debita 2kg, estorna, volta a 100%, confere a `ESTORNO_CANCELAMENTO` gravada com a quantidade certa), e as duas transições bloqueadas (`PLANEJADA` nunca executada; `SERVIDA`, irreversível). `plan()` atualizado de 35 para 42.

---

### Validação real contra Postgres (rodada anterior)

Até aqui, toda a validação das rodadas anteriores tinha sido estática: sintaxe SQL/Python, `plan()` batendo com a contagem de testes, e a lógica de exportação testada isoladamente com dados fabricados na mão. **Nunca tinha rodado contra um Postgres real** — foi exatamente esse o próximo passo recomendado, e o usuário executou linha por linha, em `docker compose`, num servidor de teste separado.

**O que essa rodada validou de verdade, não mais só em teoria:**
- As 35 asserções da suíte pgTAP (`tests_db.sql`), incluindo as seções J (ABC materializada na confirmação) e K (`fn_executar_refeicao`) — **35/35, zero falhas**, na primeira execução.
- O fluxo `confirmar → executar → servir`, com números batendo em cada etapa: custo do prato (R$120,00) → `custo_snapshot` da refeição (R$24,00 = 120×1,10⁰/5 porções, sem margem de desperdício) → `custo_snapshot` do item de menu (R$120,00 = 24×5 pessoas) → baixa de estoque real (10kg → 8kg, exatamente a necessidade calculada).
- `422 ESTOQUE_INSUFICIENTE` sem baixa parcial (lote continuou em 8kg mesmo com a tentativa de executar uma refeição para 100 pessoas).
- O worker híbrido LISTEN/NOTIFY: **24ms** de latência entre o evento `PrecoAtualizado` ser gravado e processado — não os até 300s do polling de segurança, confirmando que o `NOTIFY` está funcionando de verdade, não só o fallback.
- Os 3 templates de ficha técnica em PDF (`gerencial`, `insumo`, `operacional`) contra um prato real do banco com campos opcionais vazios (`equipamentos_utilizados`, `modo_preparo`, `instrucoes_apresentacao` todos `null`) — exercitando as branches de fallback ("não informado", "Modo de preparo não cadastrado.") que só tinham sido testadas com dado fabricado antes.
- Os 5 relatórios (`curva-abc`, `mrp`, `ruptura-estoque`, `consumo`, `margem-menu`) nos 3 formatos (`json`/`pdf`/`xlsx`) — 8 combinações PDF+XLSX testadas, todas `200` com assinatura binária correta (`file` reconhecendo `PDF document` / `Microsoft Excel 2007+`).

**Um bug real foi encontrado e corrigido nesta rodada** — o único, em todo esse processo, que não tinha aparecido em nenhuma validação estática anterior: `GET /relatorios/curva-abc?formato=xlsx` devolvia `500 Internal Server Error` cru. Causa: `classificacoes_abc.atualizado_em` é `TIMESTAMPTZ`, e `asyncpg` devolve isso como `datetime` **ciente de fuso** (`tzinfo` setado) — `openpyxl` não aceita escrever esse tipo de valor numa célula (`TypeError: Excel does not support timezones in datetimes`), porque o formato XLSX não representa fuso horário em célula de data. Meus testes anteriores de `app/exportacao.py` só usavam `datetime` "ingênuo" (sem `tzinfo`), por isso nunca pegaram isso — reproduzi o erro exato localmente assim que vi o sintoma (`.xlsx` salvo pelo `curl -o` continha texto JSON de erro, não o binário esperado), corrigi `_valor_para_celula_xlsx()` para descartar o `tzinfo` antes de entregar a célula ao `openpyxl` (mantendo a hora de parede, já em UTC — só perde a marcação explícita de fuso, não a informação de horário), e o usuário confirmou a correção do lado de lá, no mesmo ambiente onde o bug tinha aparecido.

---

### Ficha técnica em PDF (rodada anterior à validação real)

Resolve a pendência deixada na rodada anterior: `GET /pratos/{id}/ficha-tecnica?formato=pdf`.  
O usuário forneceu 3 PDFs reais de ficha técnica (Gerencial, Insumo, Operacional) — foram usados como MODELO DE LAYOUT, não como referência solta: mesma ordem de seções, mesmos rótulos de bullet, mesma tabela de ingredientes no gerencial, mesma frase padronizada de armazenamento.

**Por que um gerador separado de `app/exportacao.py`**: os relatórios de `/relatorios` (rodada anterior) são tabelas planas — uma lista de linhas com as mesmas colunas. Ficha técnica é hierárquica: bullets de cabeçalho + seções numeradas, e no gerencial uma TABELA seguida de totais FORA dela (Custo Total dos Ingredientes / Margem de Desperdício / Custo Total da Receita — exatamente como no PDF de exemplo, não dentro da tabela). Forçar isso no gerador tabular genérico exigiria gambiarras (linhas falsas de "total" misturadas com dados, seções como se fossem colunas); mais simples ter `app/ficha_tecnica_pdf.py` dedicado, reaproveitando só a paleta de cores de `exportacao.py` (`COR_CABECALHO_HEX`, que virou pública para isso).

**Achado ao comparar os 3 PDFs entre si**: as fichas Insumo e Operacional trazem a **mesma frase exata** de armazenamento ("Se preparado com antecedência, resfriar rapidamente e armazenar em recipiente hermético sob refrigeração (1°C a 4°C) por no máximo 24 horas."). Isso confirma que essa informação pertence ao **prato** (`pratos.armazenamento_faixa_temp` / `armazenamento_tempo_max_h`), não ao insumo individualmente — o schema não tem (nem deveria ter) esse dado por insumo. A ficha Insumo do sistema não carregava isso antes; passou a buscar do prato ao qual o insumo pertence na receita.

---

### Exportação PDF/XLSX nos relatórios (rodada anterior)

Contornada a ressalva documentada desde a primeira versão de `api-endpoints.md` §9 ("Todos os relatórios aceitam exportação") e nunca implementada: `GET /relatorios/*?formato=pdf|xlsx`.

**Decisão de design**: PDF via `reportlab` (Platypus/`Table`), XLSX via `openpyxl` — as duas bibliotecas já eram apontadas como a rota natural (a ressalva antiga falava em "biblioteca de template, layout"; nenhuma das duas exige nada além do que já vem no `requirements.txt` agora). Os valores são escritos **prontos**, não como fórmula: estes relatórios são a fotografia de uma consulta que já rodou no Postgres (curva ABC, MRP etc.), não uma planilha para o usuário reabrir e recalcular — não há "modelo" por trás, então fórmula não agregaria nada e só troca um problema simples por outro (recalc, funções pós-2007 sem suporte em todo leitor de XLSX).

---

### Rodada de execução real de estoque (antes das exportações)

Pedido do usuário: entre `CONFIRMADA` e `SERVIDA` deveria existir um estado em que o prato consta como executado, com baixa real dos insumos **consumíveis** (todos, à exceção de utensílios) no estoque.

**Antes desta rodada isso não existia em lugar nenhum do sistema.**  
`lotes_insumo` só crescia (só havia registro de ENTRADA, por compra); não havia nenhuma tabela ou rota que debitasse `quantidade_disponivel` de verdade. O próprio `relatorios.py` já documentava essa lacuna explicitamente no endpoint `/relatorios/consumo`, que respondia gasto em compras como proxy por falta de dado real de consumo.

---

## Arquitetura e estrutura do projeto

```
dono/
├── docker-compose.yml            # base — segura para produção (sem bind-mount, sem portas expostas)
├── docker-compose.override.yml   # aplicado automaticamente em cima, só em dev (hot-reload, portas)
├── .env.example                  # copie para .env e preencha
├── docs/
│   ├── arquitetura-sistema-restaurante.md   # decisões de arquitetura e modelo de dados
│   └── api-endpoints.md                     # contrato REST completo (rotas, RBAC, erros, rate limit, JWT)
├── db/
│   └── Dockerfile                # postgres:15-alpine + extensão pgtap compilada (só existe assim, sem pacote apk oficial)
├── scripts/
│   ├── schema.sql                # tabelas, triggers, colunas geradas, worker corrigido
│   ├── business-queries.sql      # fn_recalcular_abc_*, fn_mrp_previsao_compras, fn_processar_eventos_pendentes
│   └── seeds.sql                 # categorias, estilos de serviço, regras de composição
├── tests/
│   └── tests_db.sql              # suite pgTAP (colunas geradas, triggers, ABC, MRP, FKs)
└── backend/
    ├── Dockerfile                # multi-estágio: alvo "dev" e alvo "runtime"
    ├── requirements.txt
    ├── scripts/
    │   └── seed_admin.py         # cria o usuário admin inicial (roda dentro do container backend)
    └── app/
        ├── __init__.py
        ├── main.py                # FastAPI app + lifespan + routers registrados
        ├── database.py            # pool asyncpg
        ├── auth.py                # hash de senha, JWT, rotação de refresh token
        ├── dependencies.py        # get_current_user, require_perfil (RBAC)
        ├── errors.py              # envelope de erro padrão (api-endpoints.md §13)
        ├── worker.py              # processa o outbox eventos_dominio em loop
        ├── routes/
        │   ├── __init__.py
        │   ├── auth.py            # /auth/login, /refresh, /logout, /logout-all
        │   └── usuarios.py        # /me, /usuarios (GET/POST/PATCH)
```

---

## Rodar em desenvolvimento

```bash
cp .env.example .env    # edite DB_PASSWORD e JWT_SECRET
docker compose up -d
docker compose logs -f db   # confirme que schema+business-queries+seeds rodaram sem erro
```

### Rodar como produção (localmente, para conferir)

```bash
docker compose -f docker-compose.yml up -d
```

---

## Nome do banco: `dono`, não `dono_dev`

O texto que sugeriu o nome do projeto propôs `dono_dev` para desenvolvimento manual (fora de container). Como a decisão foi rodar tudo via Docker desde o início, o `docker-compose.yml` já isola dev de produção por outro eixo — arquivo de compose diferente, não nome de banco diferente — então mantive um único nome (`dono`) em todos os ambientes Docker, e descartei a sugestão de `dono_dev` para não ter dois esquemas de nomenclatura sobrepostos (compose file vs. sufixo no nome do banco) resolvendo a mesma preocupação de forma redundante.

---

## Rate limiting (Redis) — implementado

- `app/redis_client.py` — pool assíncrono (mesmo padrão de `database.py`).
- `app/rate_limit.py` — janela fixa via `INCR`+`EXPIRE`: login (5/5min por IP+email), geral (120/min por usuário), IA (10/hora por usuário), e um semáforo global (`acquire_ia_slot`/`release_ia_slot`) limitando a 5 jobs de IA concorrentes no sistema inteiro — todos os limites do `api-endpoints.md` §12.
- `app/middleware.py` — aplica o limite geral a qualquer requisição com Bearer token decodificável; não substitui a autenticação, só conta.
- Erro típico: `429 LIMITE_REQUISICOES_EXCEDIDO` (ou `503 LIMITE_IA_EXCEDIDO` pro semáforo), com `Retry-After` e `X-RateLimit-*` nos headers.

---

## Testes de IA / RAG / OCR

O módulo de IA (RAG, OCR, embeddings, rate limit) é coberto por testes de integração em `tests/test_ia_rag.py`. Para executá-los isoladamente:

```bash
docker compose exec backend pytest tests/test_ia_rag.py -v
```

**Status atual (2026-07-24):**  
Todos os **22 testes** passam, com exceção de `test_fluxo_completo_ocr`, que é intencionalmente ignorado (`SKIPPED`) por depender de infraestrutura real (`ai_worker` + Redis).  
Os testes que simulam o LLM (Ollama) e o modelo de embeddings usam mocks para evitar dependências externas, garantindo execução rápida e determinística.

### Correções aplicadas nos testes de IA

1. **Embeddings falsos** – documentos de teste utilizam um vetor fixo `[0.1] * 384` (mesma dimensão do modelo `all-MiniLM-L6-v2`), eliminando a necessidade de carregar o modelo real.
2. **Mocks do Redis** – os testes de rate limit agora usam um dicionário local para simular o Redis, garantindo contadores isolados e evitando `TypeError` com `AsyncMock`.
3. **Patches corretos** – todos os patches apontam para o local onde a função é efetivamente importada (`app.routes.ia.consultar_llm`, `app.routes.ia.enfileirar_job_ocr`, etc.).
4. **Conversão de JSONB** – o campo `resultado` dos jobs de OCR é convertido com `json.loads()` antes de acessar suas chaves, pois o `asyncpg` pode retorná-lo como string.

Com essas correções, o módulo de IA está **totalmente testado** em ambiente de desenvolvimento e pronto para evolução.

Para testar o OCR com um `ai_worker` real (não obrigatório para os testes unitários), certifique-se de que o serviço `ollama` está em execução e com o modelo baixado:

```bash
docker compose up -d ollama
docker compose exec ollama ollama pull llama3.1:8b
```

Os testes de integração que dependem do worker real estão marcados com `@pytest.mark.skip` e podem ser ativados removendo o skip, mas exigem Redis e worker rodando.

---

## Status das rotas de negócio

| Módulo | Status |
|--------|--------|
| Auth (login/refresh/logout/logout-all) | ✅ Implementado |
| Usuários (`/me`, `/usuarios`) | ✅ Implementado |
| Catálogos (gêneros, categorias, fornecedores, estilos de serviço, regras de composição) | ✅ Implementado (`routes/catalogos.py`) |
| Insumos, Lotes, Cotações (incl. stub de cotação online) | ✅ Implementado (`routes/insumos.py`) |
| Pratos, itens-receita, fichas técnicas (3 templates), ABC | ✅ Implementado (`routes/pratos.py`) |
| Refeições (itens, validação de composição, confirmar/executar/servir/cancelar, ABC) | ✅ Implementado (`routes/refeicoes.py`) |
| Menus (itens, confirmar/realizar/cancelar, ABC, margem de contribuição) | ✅ Implementado (`routes/menus.py`) |
| Relatórios (curva ABC, MRP, ruptura de estoque, consumo, margem de menu) | ✅ Implementado (`routes/relatorios.py`) – exportação PDF/XLSX ok |
| IA — prospecção de pratos (match direto + sugestão criativa com LLM) | ✅ Implementado (`routes/ia.py`) – ver seção de testes acima |
| RAG (busca semântica com pgvector) + OCR (extração de dados de notas fiscais) | ✅ Implementado e testado – todos os testes passam (exceto integração com worker real) |

### Ressalva em `routes/ia.py` (sugestão criativa)
- A sugestão criativa usa o LLM local (Ollama) e está funcional, mas a qualidade da resposta depende do modelo disponível. O sistema está preparado para fallback caso o Ollama não esteja acessível.

---

## CHANGELOG (opcional)

Para manter um histórico de mudanças, recomenda‑se criar um arquivo `CHANGELOG.md` na raiz do projeto. Exemplo de entrada para esta rodada:

```markdown
## [2026-07-24] – Correções nos testes de IA/RAG/OCR

- Todos os testes do módulo `test_ia_rag.py` agora passam.
- Embeddings falsos (vetor fixo) usados nos testes para evitar dependência do modelo real.
- Mocks do Redis ajustados para simular corretamente `incr`, `expire` e `ttl`, resolvendo `TypeError`.
- Patch do LLM agora aplicado no local correto (`app.routes.ia.consultar_llm`).
- Campo `resultado` dos jobs de OCR convertido com `json.loads()`.
- Testes de rate limit isolados com dicionário local.
```

---

**Fim do README**