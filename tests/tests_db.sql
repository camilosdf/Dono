-- =====================================================================
-- Sistema Dono — Testes de Banco de Dados (pgTAP)
-- Cobrem: colunas geradas, trigger de custo médio + outbox, triggers de
-- snapshot (Refeição/Menu) e sua imutabilidade, motor ABC nos 4 escopos
-- (incluindo a regressão da dupla multiplicação corrigida no Menu), MRP,
-- e FKs ON DELETE RESTRICT.
--
-- PRÉ-REQUISITOS (aplicar nesta ordem antes de rodar):
--   1. schema.sql
--   2. seeds.sql          (categorias, estilos_servico, regras_composicao)
--   3. business-queries.sql (fn_recalcular_abc_*, fn_mrp_previsao_compras)
--   4. CREATE EXTENSION IF NOT EXISTS pgtap;
--
-- Rodar com: pg_prove --ext sql -d <database_de_teste> tests_db.sql
-- Banco de teste deve estar "limpo" além do schema+seeds (sem dados de
-- produção), pois os testes de ABC/MRP assumem que só os fixtures deste
-- arquivo existem nos escopos que eles calculam.
-- =====================================================================

BEGIN;
SELECT plan(42);

-- =====================================================================
-- A) Colunas geradas em itens_receita (peso_liquido, custo_total_calculado)
--    Caso real: Filé Mignon da Ficha Técnica Gerencial (PB 2,600kg,
--    FC 1,30, custo unitário R$60,00 → custo total R$156,00 sobre PB)
-- =====================================================================
SAVEPOINT sp_a;

INSERT INTO insumos (id, nome, categoria_id, unidade)
VALUES ('a0000000-0000-0000-0000-000000000001', 'Filé Mignon [teste]',
        (SELECT id FROM categorias WHERE nome = 'Carnes, Aves e Peixes'), 'KG');

INSERT INTO pratos (id, nome, genero_prato, rendimento_base_porcoes)
VALUES ('a0000000-0000-0000-0000-000000000002', 'Prato Teste A', 'Prato Principal', 10);

INSERT INTO itens_receita (id, prato_id, insumo_id, tipo, peso_bruto, fator_correcao, custo_unitario_registrado)
VALUES ('a0000000-0000-0000-0000-000000000003',
        'a0000000-0000-0000-0000-000000000002',
        'a0000000-0000-0000-0000-000000000001',
        'ALIMENTICIO', 2.600, 1.300, 60.00);

SELECT is(
    (SELECT peso_liquido FROM itens_receita WHERE id = 'a0000000-0000-0000-0000-000000000003')::numeric,
    2.0000::numeric,
    'peso_liquido = peso_bruto / fator_correcao (PB/FC)'
);

SELECT is(
    (SELECT custo_total_calculado FROM itens_receita WHERE id = 'a0000000-0000-0000-0000-000000000003')::numeric,
    156.0000::numeric,
    'custo_total_calculado usa PESO BRUTO (decisão de domínio: paga-se pela peça inteira comprada)'
);

ROLLBACK TO SAVEPOINT sp_a;

-- =====================================================================
-- B) Trigger fn_atualizar_custo_medio_insumo + outbox eventos_dominio
-- =====================================================================
SAVEPOINT sp_b;

INSERT INTO insumos (id, nome, categoria_id, unidade)
VALUES ('b0000000-0000-0000-0000-000000000001', 'Insumo Teste B',
        (SELECT id FROM categorias WHERE nome = 'Secos e Despensa'), 'KG');

INSERT INTO lotes_insumo (insumo_id, valor_aquisicao, data_aquisicao, quantidade, quantidade_disponivel)
VALUES ('b0000000-0000-0000-0000-000000000001', 50.00, CURRENT_DATE, 10, 10);

SELECT is(
    (SELECT custo_medio_ponderado FROM insumos WHERE id = 'b0000000-0000-0000-0000-000000000001')::numeric,
    50.0000::numeric,
    'custo médio após 1º lote = valor do próprio lote'
);

INSERT INTO lotes_insumo (insumo_id, valor_aquisicao, data_aquisicao, quantidade, quantidade_disponivel)
VALUES ('b0000000-0000-0000-0000-000000000001', 70.00, CURRENT_DATE, 10, 10);

SELECT is(
    (SELECT custo_medio_ponderado FROM insumos WHERE id = 'b0000000-0000-0000-0000-000000000001')::numeric,
    60.0000::numeric,
    'custo médio ponderado após 2º lote = (50×10 + 70×10) / 20 = 60'
);

SELECT is(
    (SELECT count(*) FROM eventos_dominio
      WHERE tipo = 'PrecoAtualizado'
        AND payload ->> 'insumo_id' = 'b0000000-0000-0000-0000-000000000001')::int,
    2,
    'cada INSERT em lotes_insumo grava um evento PrecoAtualizado no outbox'
);

ROLLBACK TO SAVEPOINT sp_b;

-- =====================================================================
-- C+D) Triggers de snapshot (Refeição e Menu) + imutabilidade histórica
--      + regressão: ABC de Menu não deve multiplicar qtd_pessoas 2×
-- =====================================================================
SAVEPOINT sp_cd;

INSERT INTO insumos (id, nome, categoria_id, unidade)
VALUES ('c0000000-0000-0000-0000-000000000001', 'Insumo Teste C',
        (SELECT id FROM categorias WHERE nome = 'Hortifruti'), 'KG');

INSERT INTO pratos (id, nome, genero_prato, rendimento_base_porcoes, margem_desperdicio_pct)
VALUES ('c0000000-0000-0000-0000-000000000002', 'Prato Teste C', 'Prato Principal', 10, 10.00);

INSERT INTO itens_receita (id, prato_id, insumo_id, tipo, peso_bruto, fator_correcao, custo_unitario_registrado)
VALUES ('c0000000-0000-0000-0000-000000000003',
        'c0000000-0000-0000-0000-000000000002',
        'c0000000-0000-0000-0000-000000000001',
        'ALIMENTICIO', 10.000, 1.000, 10.00);
-- custo_total_calculado = 10 × 10 = 100.00
-- custo por porção esperado = 100 × (1 + 10/100) / 10 = 11.00

INSERT INTO refeicoes (id, genero_refeicao, data, horario_inicio, horario_fim, qtd_pessoas)
VALUES ('c0000000-0000-0000-0000-000000000004', 'Almoço Executivo', CURRENT_DATE, '12:00', '15:00', 4);

INSERT INTO itens_refeicao (id, refeicao_id, prato_id, categoria_composicao)
VALUES ('c0000000-0000-0000-0000-000000000005',
        'c0000000-0000-0000-0000-000000000004',
        'c0000000-0000-0000-0000-000000000002',
        'Prato Principal');

SELECT ok(
    (SELECT custo_snapshot FROM itens_refeicao WHERE id = 'c0000000-0000-0000-0000-000000000005') IS NULL,
    'custo_snapshot é NULL enquanto a refeição está PLANEJADA (ainda não confirmada)'
);

UPDATE refeicoes SET status = 'CONFIRMADA' WHERE id = 'c0000000-0000-0000-0000-000000000004';

SELECT is(
    (SELECT custo_snapshot FROM itens_refeicao WHERE id = 'c0000000-0000-0000-0000-000000000005')::numeric,
    11.0000::numeric,
    'ao confirmar a refeição, custo_snapshot = custo_total×(1+margem)/rendimento = 100×1.10/10 = 11.00'
);

-- Simula um reajuste de preço no insumo já usado numa refeição confirmada
-- (o que um worker de recálculo faria em itens_receita.custo_unitario_registrado)
UPDATE itens_receita SET custo_unitario_registrado = 999.00
 WHERE id = 'c0000000-0000-0000-0000-000000000003';

-- Tenta "reconfirmar" (status já é CONFIRMADA — a condição OLD.status <> 'CONFIRMADA'
-- do trigger deve bloquear o recálculo)
UPDATE refeicoes SET status = 'CONFIRMADA' WHERE id = 'c0000000-0000-0000-0000-000000000004';

SELECT is(
    (SELECT custo_snapshot FROM itens_refeicao WHERE id = 'c0000000-0000-0000-0000-000000000005')::numeric,
    11.0000::numeric,
    'IMUTABILIDADE: custo_snapshot permanece 11.00 mesmo após o insumo subir de preço — evento já confirmado não muda retroativamente'
);

INSERT INTO menus (id, nome_evento, estilo_servico_id, data_inicio, horario_inicio, data_fim, horario_fim)
VALUES ('c0000000-0000-0000-0000-000000000006', 'Evento Teste',
        (SELECT id FROM estilos_servico LIMIT 1),
        CURRENT_DATE, '12:00', CURRENT_DATE, '15:00');

INSERT INTO itens_menu (id, menu_id, refeicao_id, ordem_cronologica)
VALUES ('c0000000-0000-0000-0000-000000000007',
        'c0000000-0000-0000-0000-000000000006',
        'c0000000-0000-0000-0000-000000000004',
        1);

UPDATE menus SET status = 'CONFIRMADO' WHERE id = 'c0000000-0000-0000-0000-000000000006';

SELECT is(
    (SELECT custo_snapshot FROM itens_menu WHERE id = 'c0000000-0000-0000-0000-000000000007')::numeric,
    44.0000::numeric,
    'ao confirmar o menu, itens_menu.custo_snapshot = SUM(itens_refeicao.custo_snapshot × qtd_pessoas) = 11.00 × 4 = 44.00'
);

SELECT fn_recalcular_abc_menu('c0000000-0000-0000-0000-000000000006');

SELECT is(
    (SELECT custo FROM classificacoes_abc
      WHERE escopo_tipo = 'MENU'
        AND escopo_id_pai = 'c0000000-0000-0000-0000-000000000006'
        AND item_id = 'c0000000-0000-0000-0000-000000000004')::numeric,
    44.0000::numeric,
    'REGRESSÃO: fn_recalcular_abc_menu usa itens_menu.custo_snapshot direto (44.00), sem multiplicar por qtd_pessoas de novo (evitando o bug de 176.00 já corrigido)'
);

ROLLBACK TO SAVEPOINT sp_cd;

-- =====================================================================
-- E) Motor ABC — escopo INSUMO_GENERO (fronteiras exatas 80%/95%)
-- =====================================================================
SAVEPOINT sp_e;

INSERT INTO insumos (id, nome, categoria_id, unidade) VALUES
    ('e0000000-0000-0000-0000-000000000001', 'Insumo E-A (alto custo)',
     (SELECT id FROM categorias WHERE nome = 'Bebidas'), 'KG'),
    ('e0000000-0000-0000-0000-000000000002', 'Insumo E-B (custo médio)',
     (SELECT id FROM categorias WHERE nome = 'Bebidas'), 'KG'),
    ('e0000000-0000-0000-0000-000000000003', 'Insumo E-C (baixo custo)',
     (SELECT id FROM categorias WHERE nome = 'Bebidas'), 'KG');

INSERT INTO lotes_insumo (insumo_id, valor_aquisicao, data_aquisicao, quantidade, quantidade_disponivel) VALUES
    ('e0000000-0000-0000-0000-000000000001', 800.00, CURRENT_DATE, 1, 1),  -- 80% do total
    ('e0000000-0000-0000-0000-000000000002', 150.00, CURRENT_DATE, 1, 1),  -- +15% = 95%
    ('e0000000-0000-0000-0000-000000000003',  50.00, CURRENT_DATE, 1, 1);  -- +5%  = 100%

SELECT fn_recalcular_abc_insumo_genero('ALIMENTICIO');

SELECT is(
    (SELECT classe FROM classificacoes_abc WHERE escopo_tipo = 'INSUMO_GENERO'
      AND item_id = 'e0000000-0000-0000-0000-000000000001'),
    'A', 'insumo com 80% acumulado exato → Classe A (limite <=80 inclusive)'
);
SELECT is(
    (SELECT classe FROM classificacoes_abc WHERE escopo_tipo = 'INSUMO_GENERO'
      AND item_id = 'e0000000-0000-0000-0000-000000000002'),
    'B', 'insumo que leva o acumulado a 95% exato → Classe B (limite <=95 inclusive)'
);
SELECT is(
    (SELECT classe FROM classificacoes_abc WHERE escopo_tipo = 'INSUMO_GENERO'
      AND item_id = 'e0000000-0000-0000-0000-000000000003'),
    'C', 'insumo que fecha em 100% → Classe C'
);

ROLLBACK TO SAVEPOINT sp_e;

-- =====================================================================
-- F) Motor ABC — escopo PRATO (mesmo padrão 80/15/5, dentro de um prato)
-- =====================================================================
SAVEPOINT sp_f;

INSERT INTO pratos (id, nome, genero_prato, rendimento_base_porcoes)
VALUES ('f0000000-0000-0000-0000-000000000001', 'Prato Teste F', 'Prato Principal', 1);

INSERT INTO insumos (id, nome, categoria_id, unidade) VALUES
    ('f0000000-0000-0000-0000-000000000002', 'Insumo F-A',
     (SELECT id FROM categorias WHERE nome = 'Congelados'), 'KG'),
    ('f0000000-0000-0000-0000-000000000003', 'Insumo F-B',
     (SELECT id FROM categorias WHERE nome = 'Congelados'), 'KG'),
    ('f0000000-0000-0000-0000-000000000004', 'Insumo F-C',
     (SELECT id FROM categorias WHERE nome = 'Congelados'), 'KG');

INSERT INTO itens_receita (prato_id, insumo_id, tipo, peso_bruto, fator_correcao, custo_unitario_registrado) VALUES
    ('f0000000-0000-0000-0000-000000000001', 'f0000000-0000-0000-0000-000000000002', 'ALIMENTICIO', 1, 1, 800.00),
    ('f0000000-0000-0000-0000-000000000001', 'f0000000-0000-0000-0000-000000000003', 'ALIMENTICIO', 1, 1, 150.00),
    ('f0000000-0000-0000-0000-000000000001', 'f0000000-0000-0000-0000-000000000004', 'ALIMENTICIO', 1, 1,  50.00);

SELECT fn_recalcular_abc_prato('f0000000-0000-0000-0000-000000000001');

SELECT is(
    (SELECT classe FROM classificacoes_abc WHERE escopo_tipo = 'PRATO'
      AND item_id = 'f0000000-0000-0000-0000-000000000002'),
    'A', 'insumo de maior custo dentro do prato → Classe A'
);
SELECT is(
    (SELECT classe FROM classificacoes_abc WHERE escopo_tipo = 'PRATO'
      AND item_id = 'f0000000-0000-0000-0000-000000000003'),
    'B', 'insumo intermediário dentro do prato → Classe B'
);
SELECT is(
    (SELECT classe FROM classificacoes_abc WHERE escopo_tipo = 'PRATO'
      AND item_id = 'f0000000-0000-0000-0000-000000000004'),
    'C', 'insumo de menor custo dentro do prato → Classe C'
);

ROLLBACK TO SAVEPOINT sp_f;

-- =====================================================================
-- G) MRP — fn_mrp_previsao_compras (necessidade bruta − estoque)
-- =====================================================================
SAVEPOINT sp_g;

INSERT INTO insumos (id, nome, categoria_id, unidade) VALUES
    ('70000000-0000-0000-0000-000000000001', 'Insumo G1 (falta estoque)',
     (SELECT id FROM categorias WHERE nome = 'Laticínios e Frios'), 'KG'),
    ('70000000-0000-0000-0000-000000000002', 'Insumo G2 (estoque suficiente)',
     (SELECT id FROM categorias WHERE nome = 'Laticínios e Frios'), 'KG');

INSERT INTO lotes_insumo (insumo_id, valor_aquisicao, data_aquisicao, quantidade, quantidade_disponivel) VALUES
    ('70000000-0000-0000-0000-000000000001', 10.00, CURRENT_DATE, 4, 4),  -- só 4kg em estoque
    ('70000000-0000-0000-0000-000000000002', 10.00, CURRENT_DATE, 5, 5);  -- 5kg em estoque

INSERT INTO pratos (id, nome, genero_prato, rendimento_base_porcoes) VALUES
    ('70000000-0000-0000-0000-000000000003', 'Prato G1', 'Prato Principal', 5),   -- 5 porções por lote de 5kg
    ('70000000-0000-0000-0000-000000000004', 'Prato G2', 'Guarnição', 10);        -- 10 porções por lote de 2kg

INSERT INTO itens_receita (prato_id, insumo_id, tipo, peso_bruto, fator_correcao, custo_unitario_registrado) VALUES
    ('70000000-0000-0000-0000-000000000003', '70000000-0000-0000-0000-000000000001', 'ALIMENTICIO', 5, 1, 10.00),
    ('70000000-0000-0000-0000-000000000004', '70000000-0000-0000-0000-000000000002', 'ALIMENTICIO', 2, 1, 10.00);

INSERT INTO refeicoes (id, genero_refeicao, data, horario_inicio, horario_fim, qtd_pessoas)
VALUES ('70000000-0000-0000-0000-000000000005', 'Jantar', CURRENT_DATE, '18:00', '21:30', 10);

INSERT INTO itens_refeicao (refeicao_id, prato_id, categoria_composicao) VALUES
    ('70000000-0000-0000-0000-000000000005', '70000000-0000-0000-0000-000000000003', 'Prato Principal'),
    ('70000000-0000-0000-0000-000000000005', '70000000-0000-0000-0000-000000000004', 'Guarnição');

INSERT INTO menus (id, nome_evento, estilo_servico_id, data_inicio, horario_inicio, data_fim, horario_fim, status)
VALUES ('70000000-0000-0000-0000-000000000006', 'Evento MRP',
        (SELECT id FROM estilos_servico LIMIT 1),
        CURRENT_DATE, '18:00', CURRENT_DATE, '21:30', 'PLANEJADO');

INSERT INTO itens_menu (menu_id, refeicao_id, ordem_cronologica)
VALUES ('70000000-0000-0000-0000-000000000006', '70000000-0000-0000-0000-000000000005', 1);

-- G1: necessidade = peso_liquido(5) × (qtd_pessoas(10) / rendimento(5)) = 10kg; estoque 4kg → falta 6kg
SELECT is(
    (SELECT necessidade_liquida FROM fn_mrp_previsao_compras(CURRENT_DATE + 7)
      WHERE insumo_id = '70000000-0000-0000-0000-000000000001')::numeric,
    6.000::numeric,
    'MRP: necessidade líquida = necessidade bruta − estoque disponível (10 - 4 = 6kg)'
);

-- G2: necessidade = 2 × (10/10) = 2kg; estoque 5kg (suficiente) → não deve aparecer na lista
SELECT ok(
    NOT EXISTS (
        SELECT 1 FROM fn_mrp_previsao_compras(CURRENT_DATE + 7)
         WHERE insumo_id = '70000000-0000-0000-0000-000000000002'
    ),
    'MRP: insumo com estoque suficiente para a necessidade não aparece na lista de compras'
);

SELECT is(
    (SELECT classe_abc FROM fn_mrp_previsao_compras(CURRENT_DATE + 7)
      WHERE insumo_id = '70000000-0000-0000-0000-000000000001'),
    'C', 'MRP: sem classificação ABC prévia, insumo assume Classe C por padrão (COALESCE)'
);

ROLLBACK TO SAVEPOINT sp_g;

-- =====================================================================
-- H) FKs ON DELETE RESTRICT (insumo em uso / prato em uso)
-- =====================================================================
SAVEPOINT sp_h;

INSERT INTO insumos (id, nome, categoria_id, unidade)
VALUES ('80000000-0000-0000-0000-000000000001', 'Insumo H',
        (SELECT id FROM categorias WHERE nome = 'Utensílios'), 'PC');
INSERT INTO pratos (id, nome, genero_prato, rendimento_base_porcoes)
VALUES ('80000000-0000-0000-0000-000000000002', 'Prato H', 'Prato Principal', 1);
INSERT INTO itens_receita (prato_id, insumo_id, tipo, peso_bruto, fator_correcao, custo_unitario_registrado)
VALUES ('80000000-0000-0000-0000-000000000002', '80000000-0000-0000-0000-000000000001', 'UTENSILIO', 1, 1, 1);

-- Nota: usamos a forma de 2 argumentos (sql, errcode) de propósito — a
-- de 3 argumentos (sql, errcode, texto) faz essa versão do pgTAP tratar
-- o 3º texto como MENSAGEM DE ERRO ESPERADA (comparação exata), não como
-- descrição livre do teste. Como a mensagem de violação de FK do Postgres
-- é gerada automaticamente (nome de constraint etc.), tentar casar um
-- texto fixo com ela é frágil. A forma de 2 argumentos verifica só o
-- SQLSTATE, que é o que realmente importa aqui.
SELECT throws_ok(
    $$DELETE FROM insumos WHERE id = '80000000-0000-0000-0000-000000000001'$$,
    '23503'
);

INSERT INTO refeicoes (id, genero_refeicao, data, horario_inicio, horario_fim, qtd_pessoas)
VALUES ('80000000-0000-0000-0000-000000000003', 'Jantar', CURRENT_DATE, '18:00', '21:00', 1);
INSERT INTO itens_refeicao (refeicao_id, prato_id, categoria_composicao)
VALUES ('80000000-0000-0000-0000-000000000003', '80000000-0000-0000-0000-000000000002', 'Prato Principal');

SELECT throws_ok(
    $$DELETE FROM pratos WHERE id = '80000000-0000-0000-0000-000000000002'$$,
    '23503'
);

ROLLBACK TO SAVEPOINT sp_h;

-- =====================================================================
-- J) REGRESSÃO: confirmar Refeição/Menu já materializa classificacoes_abc
--    na hora, mesmo sem NENHUM evento PrecoAtualizado ter sido processado
--    antes. Gap relatado numa análise externa (que dizia já estar
--    corrigido — não estava, ver README.md "Gap: ABC não materializada
--    na confirmação") e corrigido de verdade nesta rodada, nos triggers
--    fn_snapshot_custo_refeicao / fn_snapshot_custo_menu (schema.sql).
-- =====================================================================
SAVEPOINT sp_j;

INSERT INTO insumos (id, nome, categoria_id, unidade)
VALUES ('90000000-0000-0000-0000-000000000001', 'Insumo Teste J',
        (SELECT id FROM categorias WHERE nome = 'Hortifruti'), 'KG');

INSERT INTO pratos (id, nome, genero_prato, rendimento_base_porcoes, margem_desperdicio_pct)
VALUES ('90000000-0000-0000-0000-000000000002', 'Prato Teste J', 'Prato Principal', 10, 10.00);

-- custo_unitario_registrado vem direto no INSERT, sem nenhum
-- lotes_insumo inserido para este insumo — de propósito: sem lote, o
-- trigger trg_lote_insumo_custo nunca dispara, logo NUNCA existe um
-- evento PrecoAtualizado para este insumo. Se o bug relatado ainda
-- estivesse presente, classificacoes_abc para REFEICAO/MENU abaixo
-- ficaria vazia para sempre (só o worker populava, e só reagindo a um
-- evento que aqui nunca acontece).
INSERT INTO itens_receita (id, prato_id, insumo_id, tipo, peso_bruto, fator_correcao, custo_unitario_registrado)
VALUES ('90000000-0000-0000-0000-000000000003',
        '90000000-0000-0000-0000-000000000002',
        '90000000-0000-0000-0000-000000000001',
        'ALIMENTICIO', 10.000, 1.000, 10.00);
-- custo_total_calculado = 10 × 10 = 100.00
-- custo_snapshot por porção esperado = 100 × 1.10 / 10 = 11.00

INSERT INTO refeicoes (id, genero_refeicao, data, horario_inicio, horario_fim, qtd_pessoas)
VALUES ('90000000-0000-0000-0000-000000000004', 'Almoço Executivo', CURRENT_DATE, '12:00', '15:00', 4);

INSERT INTO itens_refeicao (id, refeicao_id, prato_id, categoria_composicao)
VALUES ('90000000-0000-0000-0000-000000000005',
        '90000000-0000-0000-0000-000000000004',
        '90000000-0000-0000-0000-000000000002',
        'Prato Principal');

UPDATE refeicoes SET status = 'CONFIRMADA' WHERE id = '90000000-0000-0000-0000-000000000004';

SELECT is(
    (SELECT count(*)::int FROM classificacoes_abc
      WHERE escopo_tipo = 'REFEICAO' AND escopo_id_pai = '90000000-0000-0000-0000-000000000004'),
    1,
    'REGRESSÃO: confirmar a refeição já materializa classificacoes_abc (REFEICAO), sem depender de nenhum evento PrecoAtualizado'
);

SELECT is(
    (SELECT custo FROM classificacoes_abc
      WHERE escopo_tipo = 'REFEICAO' AND escopo_id_pai = '90000000-0000-0000-0000-000000000004'
        AND item_id = '90000000-0000-0000-0000-000000000002')::numeric,
    11.0000::numeric,
    'ABC de REFEICAO recém-materializada usa o custo_snapshot recém-gravado (11.00), não o fallback on-the-fly'
);

INSERT INTO menus (id, nome_evento, estilo_servico_id, data_inicio, horario_inicio, data_fim, horario_fim)
VALUES ('90000000-0000-0000-0000-000000000006', 'Evento Teste J',
        (SELECT id FROM estilos_servico LIMIT 1),
        CURRENT_DATE, '12:00', CURRENT_DATE, '15:00');

INSERT INTO itens_menu (id, menu_id, refeicao_id, ordem_cronologica)
VALUES ('90000000-0000-0000-0000-000000000007',
        '90000000-0000-0000-0000-000000000006',
        '90000000-0000-0000-0000-000000000004',
        1);

UPDATE menus SET status = 'CONFIRMADO' WHERE id = '90000000-0000-0000-0000-000000000006';

SELECT is(
    (SELECT count(*)::int FROM classificacoes_abc
      WHERE escopo_tipo = 'MENU' AND escopo_id_pai = '90000000-0000-0000-0000-000000000006'),
    1,
    'REGRESSÃO: confirmar o menu já materializa classificacoes_abc (MENU), sem depender de nenhum evento PrecoAtualizado'
);

ROLLBACK TO SAVEPOINT sp_j;

-- =====================================================================
-- K) fn_executar_refeicao — baixa real de estoque (novo estado EXECUTADA,
--    entre CONFIRMADA e SERVIDA)
-- =====================================================================

-- K1: caminho feliz — baixa FEFO correta, movimentação registrada,
--     status vira EXECUTADA
SAVEPOINT sp_k1;

INSERT INTO insumos (id, nome, categoria_id, unidade)
VALUES ('aa000000-0000-0000-0000-000000000001', 'Insumo K1',
        (SELECT id FROM categorias WHERE nome = 'Carnes, Aves e Peixes'), 'KG');

INSERT INTO lotes_insumo (id, insumo_id, valor_aquisicao, data_aquisicao, quantidade, quantidade_disponivel)
VALUES ('aa000000-0000-0000-0000-000000000002', 'aa000000-0000-0000-0000-000000000001',
        10.00, CURRENT_DATE, 100, 100);

INSERT INTO pratos (id, nome, genero_prato, rendimento_base_porcoes, margem_desperdicio_pct)
VALUES ('aa000000-0000-0000-0000-000000000003', 'Prato K1', 'Prato Principal', 5, 0);

INSERT INTO itens_receita (id, prato_id, insumo_id, tipo, peso_bruto, fator_correcao, custo_unitario_registrado)
VALUES ('aa000000-0000-0000-0000-000000000004',
        'aa000000-0000-0000-0000-000000000003',
        'aa000000-0000-0000-0000-000000000001',
        'ALIMENTICIO', 2.000, 1.000, 10.00);
-- peso_liquido = 2kg; rendimento_base = 5 porções

INSERT INTO refeicoes (id, genero_refeicao, data, horario_inicio, horario_fim, qtd_pessoas)
VALUES ('aa000000-0000-0000-0000-000000000005', 'Jantar', CURRENT_DATE, '18:00', '21:00', 5);
-- necessidade = peso_liquido(2) × (qtd_pessoas(5)/rendimento(5)) = 2kg

INSERT INTO itens_refeicao (refeicao_id, prato_id, categoria_composicao)
VALUES ('aa000000-0000-0000-0000-000000000005', 'aa000000-0000-0000-0000-000000000003', 'Prato Principal');

UPDATE refeicoes SET status = 'CONFIRMADA' WHERE id = 'aa000000-0000-0000-0000-000000000005';

SELECT fn_executar_refeicao('aa000000-0000-0000-0000-000000000005');

SELECT is(
    (SELECT status FROM refeicoes WHERE id = 'aa000000-0000-0000-0000-000000000005'),
    'EXECUTADA',
    'fn_executar_refeicao move o status para EXECUTADA no caminho feliz'
);

SELECT is(
    (SELECT quantidade_disponivel FROM lotes_insumo WHERE id = 'aa000000-0000-0000-0000-000000000002')::numeric,
    98.000::numeric,
    'baixa real: quantidade_disponivel do lote cai exatamente a necessidade (100 - 2 = 98)'
);

SELECT is(
    (SELECT quantidade FROM movimentacoes_estoque
      WHERE refeicao_id = 'aa000000-0000-0000-0000-000000000005'
        AND insumo_id = 'aa000000-0000-0000-0000-000000000001')::numeric,
    2.000::numeric,
    'movimentacoes_estoque registra a baixa (tipo BAIXA_EXECUCAO) com a quantidade certa'
);

ROLLBACK TO SAVEPOINT sp_k1;

-- K2: estoque insuficiente — levanta P1002 e NÃO deixa baixa parcial
--     (a passada 1, só leitura, precisa impedir qualquer UPDATE em
--     lotes_insumo quando falta insumo)
SAVEPOINT sp_k2;

INSERT INTO insumos (id, nome, categoria_id, unidade)
VALUES ('bb000000-0000-0000-0000-000000000001', 'Insumo K2',
        (SELECT id FROM categorias WHERE nome = 'Carnes, Aves e Peixes'), 'KG');

INSERT INTO lotes_insumo (id, insumo_id, valor_aquisicao, data_aquisicao, quantidade, quantidade_disponivel)
VALUES ('bb000000-0000-0000-0000-000000000002', 'bb000000-0000-0000-0000-000000000001',
        10.00, CURRENT_DATE, 10, 10);  -- só 10kg em estoque

INSERT INTO pratos (id, nome, genero_prato, rendimento_base_porcoes, margem_desperdicio_pct)
VALUES ('bb000000-0000-0000-0000-000000000003', 'Prato K2', 'Prato Principal', 1, 0);

INSERT INTO itens_receita (id, prato_id, insumo_id, tipo, peso_bruto, fator_correcao, custo_unitario_registrado)
VALUES ('bb000000-0000-0000-0000-000000000004',
        'bb000000-0000-0000-0000-000000000003',
        'bb000000-0000-0000-0000-000000000001',
        'ALIMENTICIO', 50.000, 1.000, 10.00);
-- peso_liquido = 50kg; rendimento_base = 1 porção

INSERT INTO refeicoes (id, genero_refeicao, data, horario_inicio, horario_fim, qtd_pessoas)
VALUES ('bb000000-0000-0000-0000-000000000005', 'Jantar', CURRENT_DATE, '18:00', '21:00', 1);
-- necessidade = 50 × (1/1) = 50kg, só há 10kg — falta

INSERT INTO itens_refeicao (refeicao_id, prato_id, categoria_composicao)
VALUES ('bb000000-0000-0000-0000-000000000005', 'bb000000-0000-0000-0000-000000000003', 'Prato Principal');

UPDATE refeicoes SET status = 'CONFIRMADA' WHERE id = 'bb000000-0000-0000-0000-000000000005';

SELECT throws_ok(
    $$SELECT fn_executar_refeicao('bb000000-0000-0000-0000-000000000005')$$,
    'P1002'
);

SELECT is(
    (SELECT quantidade_disponivel FROM lotes_insumo WHERE id = 'bb000000-0000-0000-0000-000000000002')::numeric,
    10.000::numeric,
    'estoque insuficiente NÃO deixa baixa parcial — quantidade_disponivel permanece intacta (10)'
);

SELECT is(
    (SELECT status FROM refeicoes WHERE id = 'bb000000-0000-0000-0000-000000000005'),
    'CONFIRMADA',
    'estoque insuficiente NÃO avança o status — refeição continua CONFIRMADA, não EXECUTADA'
);

ROLLBACK TO SAVEPOINT sp_k2;

-- K3: utensílio (insumos.consumivel = FALSE) fica de fora da baixa —
--     "todos à exceção de utensílios", conforme especificado
SAVEPOINT sp_k3;

INSERT INTO insumos (id, nome, categoria_id, unidade, consumivel)
VALUES ('cc000000-0000-0000-0000-000000000001', 'Insumo Alimentício K3',
        (SELECT id FROM categorias WHERE nome = 'Carnes, Aves e Peixes'), 'KG', TRUE),
       ('cc000000-0000-0000-0000-000000000002', 'Utensílio K3',
        (SELECT id FROM categorias WHERE nome = 'Utensílios'), 'PC', FALSE);

INSERT INTO lotes_insumo (id, insumo_id, valor_aquisicao, data_aquisicao, quantidade, quantidade_disponivel) VALUES
    ('cc000000-0000-0000-0000-000000000003', 'cc000000-0000-0000-0000-000000000001', 10.00, CURRENT_DATE, 100, 100),
    ('cc000000-0000-0000-0000-000000000004', 'cc000000-0000-0000-0000-000000000002', 5.00, CURRENT_DATE, 100, 100);

INSERT INTO pratos (id, nome, genero_prato, rendimento_base_porcoes, margem_desperdicio_pct)
VALUES ('cc000000-0000-0000-0000-000000000005', 'Prato K3', 'Prato Principal', 5, 0);

INSERT INTO itens_receita (prato_id, insumo_id, tipo, peso_bruto, fator_correcao, custo_unitario_registrado) VALUES
    ('cc000000-0000-0000-0000-000000000005', 'cc000000-0000-0000-0000-000000000001', 'ALIMENTICIO', 2.000, 1.000, 10.00),
    ('cc000000-0000-0000-0000-000000000005', 'cc000000-0000-0000-0000-000000000002', 'UTENSILIO',    1.000, 1.000,  5.00);

INSERT INTO refeicoes (id, genero_refeicao, data, horario_inicio, horario_fim, qtd_pessoas)
VALUES ('cc000000-0000-0000-0000-000000000006', 'Jantar', CURRENT_DATE, '18:00', '21:00', 5);

INSERT INTO itens_refeicao (refeicao_id, prato_id, categoria_composicao)
VALUES ('cc000000-0000-0000-0000-000000000006', 'cc000000-0000-0000-0000-000000000005', 'Prato Principal');

UPDATE refeicoes SET status = 'CONFIRMADA' WHERE id = 'cc000000-0000-0000-0000-000000000006';

SELECT fn_executar_refeicao('cc000000-0000-0000-0000-000000000006');

SELECT is(
    (SELECT quantidade_disponivel FROM lotes_insumo WHERE id = 'cc000000-0000-0000-0000-000000000004')::numeric,
    100.000::numeric,
    'insumo NÃO consumível (utensílio) fica de fora da baixa — estoque permanece 100'
);

SELECT ok(
    NOT EXISTS (
        SELECT 1 FROM movimentacoes_estoque
         WHERE refeicao_id = 'cc000000-0000-0000-0000-000000000006'
           AND insumo_id = 'cc000000-0000-0000-0000-000000000002'
    ),
    'nenhuma movimentacoes_estoque é criada para o utensílio'
);

ROLLBACK TO SAVEPOINT sp_k3;

-- K4: transição inválida — refeição ainda PLANEJADA (não confirmada)
SAVEPOINT sp_k4;

INSERT INTO refeicoes (id, genero_refeicao, data, horario_inicio, horario_fim, qtd_pessoas)
VALUES ('dd000000-0000-0000-0000-000000000001', 'Jantar', CURRENT_DATE, '18:00', '21:00', 2);

SELECT throws_ok(
    $$SELECT fn_executar_refeicao('dd000000-0000-0000-0000-000000000001')$$,
    'P1001'
);

ROLLBACK TO SAVEPOINT sp_k4;

-- =====================================================================
-- L) fn_estornar_execucao_refeicao — estorno de estoque ao cancelar uma
--    refeição EXECUTADA (fecha a pendência declarada na rodada da baixa
--    real: "cancelar depois de EXECUTADA exigiria um estorno, ainda não
--    implementado")
-- =====================================================================

-- L1: caminho feliz — devolve exatamente o que foi debitado, registra o
--     ESTORNO_CANCELAMENTO, status vira CANCELADA
SAVEPOINT sp_l1;

INSERT INTO insumos (id, nome, categoria_id, unidade)
VALUES ('ee000000-0000-0000-0000-000000000001', 'Insumo L1',
        (SELECT id FROM categorias WHERE nome = 'Carnes, Aves e Peixes'), 'KG');

INSERT INTO lotes_insumo (id, insumo_id, valor_aquisicao, data_aquisicao, quantidade, quantidade_disponivel)
VALUES ('ee000000-0000-0000-0000-000000000002', 'ee000000-0000-0000-0000-000000000001',
        10.00, CURRENT_DATE, 100, 100);

INSERT INTO pratos (id, nome, genero_prato, rendimento_base_porcoes, margem_desperdicio_pct)
VALUES ('ee000000-0000-0000-0000-000000000003', 'Prato L1', 'Prato Principal', 5, 0);

INSERT INTO itens_receita (id, prato_id, insumo_id, tipo, peso_bruto, fator_correcao, custo_unitario_registrado)
VALUES ('ee000000-0000-0000-0000-000000000004',
        'ee000000-0000-0000-0000-000000000003',
        'ee000000-0000-0000-0000-000000000001',
        'ALIMENTICIO', 2.000, 1.000, 10.00);

INSERT INTO refeicoes (id, genero_refeicao, data, horario_inicio, horario_fim, qtd_pessoas)
VALUES ('ee000000-0000-0000-0000-000000000005', 'Jantar', CURRENT_DATE, '18:00', '21:00', 5);
-- necessidade = 2kg × (5/5) = 2kg

INSERT INTO itens_refeicao (refeicao_id, prato_id, categoria_composicao)
VALUES ('ee000000-0000-0000-0000-000000000005', 'ee000000-0000-0000-0000-000000000003', 'Prato Principal');

UPDATE refeicoes SET status = 'CONFIRMADA' WHERE id = 'ee000000-0000-0000-0000-000000000005';
SELECT fn_executar_refeicao('ee000000-0000-0000-0000-000000000005');

SELECT is(
    (SELECT quantidade_disponivel FROM lotes_insumo WHERE id = 'ee000000-0000-0000-0000-000000000002')::numeric,
    98.000::numeric,
    'pré-condição: execução debitou 2kg (100 -> 98) antes do estorno'
);

SELECT fn_estornar_execucao_refeicao('ee000000-0000-0000-0000-000000000005');

SELECT is(
    (SELECT status FROM refeicoes WHERE id = 'ee000000-0000-0000-0000-000000000005'),
    'CANCELADA',
    'fn_estornar_execucao_refeicao move o status de EXECUTADA para CANCELADA'
);

SELECT is(
    (SELECT quantidade_disponivel FROM lotes_insumo WHERE id = 'ee000000-0000-0000-0000-000000000002')::numeric,
    100.000::numeric,
    'estorno devolve exatamente o que foi debitado — quantidade_disponivel volta a 100 (98 + 2)'
);

SELECT is(
    (SELECT count(*)::int FROM movimentacoes_estoque
      WHERE refeicao_id = 'ee000000-0000-0000-0000-000000000005' AND tipo = 'ESTORNO_CANCELAMENTO'),
    1,
    'estorno grava sua PRÓPRIA movimentação (ESTORNO_CANCELAMENTO) — não apaga/edita a BAIXA_EXECUCAO original'
);

SELECT is(
    (SELECT quantidade FROM movimentacoes_estoque
      WHERE refeicao_id = 'ee000000-0000-0000-0000-000000000005' AND tipo = 'ESTORNO_CANCELAMENTO')::numeric,
    2.000::numeric,
    'quantidade do estorno bate com a quantidade da baixa original (2kg)'
);

ROLLBACK TO SAVEPOINT sp_l1;

-- L2: transição inválida — não é possível estornar uma refeição que
--     ainda está PLANEJADA (nunca foi executada, não há o que devolver)
SAVEPOINT sp_l2;

INSERT INTO refeicoes (id, genero_refeicao, data, horario_inicio, horario_fim, qtd_pessoas)
VALUES ('ee000000-0000-0000-0000-000000000006', 'Jantar', CURRENT_DATE, '18:00', '21:00', 2);

SELECT throws_ok(
    $$SELECT fn_estornar_execucao_refeicao('ee000000-0000-0000-0000-000000000006')$$,
    'P1001'
);

ROLLBACK TO SAVEPOINT sp_l2;

-- L3: transição inválida — depois de SERVIDA não há mais estorno
--     (comida já entregue; irreversível por definição de negócio)
SAVEPOINT sp_l3;

INSERT INTO insumos (id, nome, categoria_id, unidade)
VALUES ('ee000000-0000-0000-0000-000000000007', 'Insumo L3',
        (SELECT id FROM categorias WHERE nome = 'Carnes, Aves e Peixes'), 'KG');

INSERT INTO lotes_insumo (insumo_id, valor_aquisicao, data_aquisicao, quantidade, quantidade_disponivel)
VALUES ('ee000000-0000-0000-0000-000000000007', 10.00, CURRENT_DATE, 100, 100);

INSERT INTO pratos (id, nome, genero_prato, rendimento_base_porcoes, margem_desperdicio_pct)
VALUES ('ee000000-0000-0000-0000-000000000008', 'Prato L3', 'Prato Principal', 5, 0);

INSERT INTO itens_receita (prato_id, insumo_id, tipo, peso_bruto, fator_correcao, custo_unitario_registrado)
VALUES ('ee000000-0000-0000-0000-000000000008', 'ee000000-0000-0000-0000-000000000007',
        'ALIMENTICIO', 2.000, 1.000, 10.00);

INSERT INTO refeicoes (id, genero_refeicao, data, horario_inicio, horario_fim, qtd_pessoas)
VALUES ('ee000000-0000-0000-0000-000000000009', 'Jantar', CURRENT_DATE, '18:00', '21:00', 5);

INSERT INTO itens_refeicao (refeicao_id, prato_id, categoria_composicao)
VALUES ('ee000000-0000-0000-0000-000000000009', 'ee000000-0000-0000-0000-000000000008', 'Prato Principal');

UPDATE refeicoes SET status = 'CONFIRMADA' WHERE id = 'ee000000-0000-0000-0000-000000000009';
SELECT fn_executar_refeicao('ee000000-0000-0000-0000-000000000009');
UPDATE refeicoes SET status = 'SERVIDA' WHERE id = 'ee000000-0000-0000-0000-000000000009';

SELECT throws_ok(
    $$SELECT fn_estornar_execucao_refeicao('ee000000-0000-0000-0000-000000000009')$$,
    'P1001'
);

ROLLBACK TO SAVEPOINT sp_l3;

-- =====================================================================
-- I) Sanidade dos seeds (categorias e estilos de serviço)
-- =====================================================================
SELECT is((SELECT count(*) FROM categorias)::int, 11, '11 categorias seedadas (6 Alimentício + 5 Operacional/Utensílios)');
SELECT is((SELECT count(*) FROM estilos_servico)::int, 7, '7 estilos de serviço seedados');

SELECT * FROM finish();
ROLLBACK;
