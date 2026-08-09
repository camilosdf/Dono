-- =====================================================================
-- Sistema Dono — Queries de Negócio (Curva ABC, MRP, Execução, Estorno,
-- Perdas, Worker, Previsão, RAG e Event Sourcing)
-- Rodam sobre o schema.sql já apresentado
-- =====================================================================

-- ---------------------------------------------------------------------
-- PADRÃO COMUM: como qualquer um dos 4 escopos calcula a curva ABC
-- ---------------------------------------------------------------------
-- 1. custo_total_escopo   = SUM(custo) de todos os itens do escopo
-- 2. custo_acumulado      = SUM(custo) ORDER BY custo DESC (running total)
-- 3. percentual_acumulado = custo_acumulado / custo_total_escopo
-- 4. classe = 'A' se percentual_acumulado <= 80%
--             'B' se <= 95%
--             'C' caso contrário
-- Window functions resolvem os passos 2-3 em uma única passada, sem
-- subquery correlacionada por linha — por isso não pode ser GENERATED
-- (depende do conjunto todo), mas é barato de calcular via SQL puro.

-- =====================================================================
-- 1) ESCOPO: Insumo dentro do Gênero (Alimentício vs Operacional)
--    Custo = total gasto no insumo (soma dos lotes comprados)
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_recalcular_abc_insumo_genero(p_genero VARCHAR)
RETURNS VOID AS $$
DECLARE
    v_genero_id UUID;
BEGIN
    SELECT id INTO v_genero_id FROM generos WHERE nome = p_genero;

    DELETE FROM classificacoes_abc
     WHERE escopo_tipo = 'INSUMO_GENERO'
       AND escopo_id_pai = v_genero_id;

    INSERT INTO classificacoes_abc (escopo_tipo, escopo_id_pai, item_id, custo, percentual_acumulado, classe)
    SELECT
        'INSUMO_GENERO',
        v_genero_id,
        insumo_id,
        custo,
        pct_acumulado,
        CASE WHEN pct_acumulado <= 80 THEN 'A'
             WHEN pct_acumulado <= 95 THEN 'B'
             ELSE 'C' END
    FROM (
        SELECT
            gasto.insumo_id,
            gasto.custo,
            ROUND(
                COALESCE(
                    100.0 * SUM(gasto.custo) OVER (ORDER BY gasto.custo DESC
                                                    ROWS UNBOUNDED PRECEDING)
                    / NULLIF(SUM(gasto.custo) OVER (), 0)
                , 100.0)
            , 2) AS pct_acumulado
        FROM (
            SELECT i.id AS insumo_id,
                   SUM(l.valor_aquisicao * l.quantidade) AS custo
              FROM insumos i
              JOIN lotes_insumo l ON l.insumo_id = i.id
              JOIN categorias c ON c.id = i.categoria_id
             WHERE c.genero_id = v_genero_id
             GROUP BY i.id
        ) gasto
    ) ranked;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- 2) ESCOPO: Insumo dentro do Prato
--    Custo = itens_receita.custo_total_calculado
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_recalcular_abc_prato(p_prato_id UUID)
RETURNS VOID AS $$
BEGIN
    DELETE FROM classificacoes_abc
     WHERE escopo_tipo = 'PRATO' AND escopo_id_pai = p_prato_id;

    INSERT INTO classificacoes_abc (escopo_tipo, escopo_id_pai, item_id, custo, percentual_acumulado, classe)
    SELECT
        'PRATO', p_prato_id, insumo_id, custo, pct_acumulado,
        CASE WHEN pct_acumulado <= 80 THEN 'A'
             WHEN pct_acumulado <= 95 THEN 'B'
             ELSE 'C' END
    FROM (
        SELECT
            ir.insumo_id,
            ir.custo_total_calculado AS custo,
            ROUND(
                COALESCE(
                    100.0 * SUM(ir.custo_total_calculado) OVER (ORDER BY ir.custo_total_calculado DESC
                                                                 ROWS UNBOUNDED PRECEDING)
                    / NULLIF(SUM(ir.custo_total_calculado) OVER (), 0)
                , 100.0)
            , 2) AS pct_acumulado
        FROM itens_receita ir
        WHERE ir.prato_id = p_prato_id
    ) ranked;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- 3) ESCOPO: Prato dentro da Refeição
--    Custo = itens_refeicao.custo_snapshot (se confirmada) ou custo
--    calculado on-the-fly (se ainda em planejamento)
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_recalcular_abc_refeicao(p_refeicao_id UUID)
RETURNS VOID AS $$
BEGIN
    DELETE FROM classificacoes_abc
     WHERE escopo_tipo = 'REFEICAO' AND escopo_id_pai = p_refeicao_id;

    INSERT INTO classificacoes_abc (escopo_tipo, escopo_id_pai, item_id, custo, percentual_acumulado, classe)
    WITH custos AS (
        SELECT
            ir.prato_id,
            COALESCE(
                ir.custo_snapshot,
                (SELECT SUM(ic.custo_total_calculado) * (1 + p.margem_desperdicio_pct / 100.0)
                        / NULLIF(p.rendimento_base_porcoes, 0)
                   FROM itens_receita ic WHERE ic.prato_id = ir.prato_id)
            ) AS custo
        FROM itens_refeicao ir
        JOIN pratos p ON p.id = ir.prato_id
        WHERE ir.refeicao_id = p_refeicao_id
    )
    SELECT
        'REFEICAO', p_refeicao_id, prato_id, custo, pct_acumulado,
        CASE WHEN pct_acumulado <= 80 THEN 'A'
             WHEN pct_acumulado <= 95 THEN 'B'
             ELSE 'C' END
    FROM (
        SELECT
            prato_id,
            custo,
            ROUND(
                COALESCE(
                    100.0 * SUM(custo) OVER (ORDER BY custo DESC ROWS UNBOUNDED PRECEDING)
                    / NULLIF(SUM(custo) OVER (), 0)
                , 100.0)
            , 2) AS pct_acumulado
        FROM custos
    ) ranked;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- 4) ESCOPO: Refeição dentro do Menu
--    Custo = itens_menu.custo_snapshot (JÁ é o total daquela refeição no
--    evento — o trigger fn_snapshot_custo_menu, no schema.sql, grava
--    SUM(itens_refeicao.custo_snapshot) × qtd_pessoas. Multiplicar por
--    qtd_pessoas de novo aqui contaria a mesma pessoa duas vezes.)
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_recalcular_abc_menu(p_menu_id UUID)
RETURNS VOID AS $$
BEGIN
    DELETE FROM classificacoes_abc
     WHERE escopo_tipo = 'MENU' AND escopo_id_pai = p_menu_id;

    INSERT INTO classificacoes_abc (escopo_tipo, escopo_id_pai, item_id, custo, percentual_acumulado, classe)
    SELECT
        'MENU', p_menu_id, refeicao_id, custo, pct_acumulado,
        CASE WHEN pct_acumulado <= 80 THEN 'A'
             WHEN pct_acumulado <= 95 THEN 'B'
             ELSE 'C' END
    FROM (
        SELECT
            im.refeicao_id,
            im.custo_snapshot AS custo,
            ROUND(
                COALESCE(
                    100.0 * SUM(im.custo_snapshot) OVER (ORDER BY im.custo_snapshot DESC
                                                           ROWS UNBOUNDED PRECEDING)
                    / NULLIF(SUM(im.custo_snapshot) OVER (), 0)
                , 100.0)
            , 2) AS pct_acumulado
        FROM itens_menu im
        WHERE im.menu_id = p_menu_id
          AND im.custo_snapshot IS NOT NULL
    ) ranked;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- EXECUÇÃO DE REFEIÇÃO — baixa real de estoque (FEFO)
-- Transição refeicoes.status: CONFIRMADA -> EXECUTADA (entre confirmar
-- e servir). Debita quantidade_disponivel dos insumos CONSUMÍVEIS
-- (insumos.consumivel = TRUE — todos, à exceção de utensílios) usados
-- na refeição, proporcional a qtd_pessoas, e registra cada baixa em
-- movimentacoes_estoque (schema.sql §6b).
-- 
-- ATUALIZAÇÃO (Event Sourcing): Agora também insere um evento na
-- tabela event_store (tipo 'ESTOQUE_DEBITADO') para cada lote debitado,
-- permitindo reconstrução total do histórico.
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_executar_refeicao(
    p_refeicao_id UUID,
    p_usuario_id UUID DEFAULT NULL,
    p_ip VARCHAR(45) DEFAULT NULL,
    p_user_agent TEXT DEFAULT NULL
)
RETURNS VOID AS $$
DECLARE
    v_qtd_pessoas   INTEGER;
    v_status        VARCHAR(20);
    v_faltas        JSONB := '[]'::jsonb;
    v_need          RECORD;
    v_disponivel    NUMERIC(12,3);
    v_lote          RECORD;
    v_restante      NUMERIC(12,3);
    v_retirar       NUMERIC(12,3);
    v_usuario_id    UUID;
    v_ip            VARCHAR(45);
    v_user_agent    TEXT;
BEGIN
    -- Usa parâmetros explícitos se fornecidos, senão tenta da sessão
    IF p_usuario_id IS NOT NULL THEN
        v_usuario_id := p_usuario_id;
    ELSE
        v_usuario_id := NULLIF(current_setting('app.usuario_id', true), '')::UUID;
    END IF;

    IF p_ip IS NOT NULL THEN
        v_ip := p_ip;
    ELSE
        v_ip := current_setting('app.ip_origem', true);
    END IF;

    IF p_user_agent IS NOT NULL THEN
        v_user_agent := p_user_agent;
    ELSE
        v_user_agent := current_setting('app.user_agent', true);
    END IF;

    -- Lock na própria refeição
    SELECT qtd_pessoas, status INTO v_qtd_pessoas, v_status
      FROM refeicoes WHERE id = p_refeicao_id FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Refeição % não encontrada', p_refeicao_id USING ERRCODE = 'P1000';
    END IF;

    IF v_status <> 'CONFIRMADA' THEN
        RAISE EXCEPTION 'Só é possível executar uma refeição a partir de CONFIRMADA (status atual: %)', v_status
            USING ERRCODE = 'P1001';
    END IF;

    -- Passada 1: validação de estoque
    FOR v_need IN
        SELECT i.id AS insumo_id, i.nome,
               ROUND(SUM(ir.peso_liquido * (v_qtd_pessoas::numeric / NULLIF(p.rendimento_base_porcoes, 0))), 3) AS qtd_necessaria
          FROM itens_refeicao irf
          JOIN pratos p ON p.id = irf.prato_id
          JOIN itens_receita ir ON ir.prato_id = irf.prato_id
          JOIN insumos i ON i.id = ir.insumo_id
         WHERE irf.refeicao_id = p_refeicao_id
           AND i.consumivel = TRUE
         GROUP BY i.id, i.nome
    LOOP
        SELECT COALESCE(SUM(quantidade_disponivel), 0) INTO v_disponivel
          FROM lotes_insumo WHERE insumo_id = v_need.insumo_id;

        IF v_disponivel < v_need.qtd_necessaria THEN
            v_faltas := v_faltas || jsonb_build_object(
                'insumo_id', v_need.insumo_id,
                'nome', v_need.nome,
                'necessario', v_need.qtd_necessaria,
                'disponivel', v_disponivel,
                'falta', ROUND(v_need.qtd_necessaria - v_disponivel, 3)
            );
        END IF;
    END LOOP;

    IF jsonb_array_length(v_faltas) > 0 THEN
        RAISE EXCEPTION 'Estoque insuficiente para executar a refeição: %', v_faltas::text
            USING ERRCODE = 'P1002', DETAIL = v_faltas::text;
    END IF;

    -- Passada 2: baixa FEFO
    FOR v_need IN
        SELECT i.id AS insumo_id,
               ROUND(SUM(ir.peso_liquido * (v_qtd_pessoas::numeric / NULLIF(p.rendimento_base_porcoes, 0))), 3) AS qtd_necessaria
          FROM itens_refeicao irf
          JOIN pratos p ON p.id = irf.prato_id
          JOIN itens_receita ir ON ir.prato_id = irf.prato_id
          JOIN insumos i ON i.id = ir.insumo_id
         WHERE irf.refeicao_id = p_refeicao_id
           AND i.consumivel = TRUE
         GROUP BY i.id
    LOOP
        v_restante := v_need.qtd_necessaria;

        FOR v_lote IN
            SELECT id, quantidade_disponivel
              FROM lotes_insumo
             WHERE insumo_id = v_need.insumo_id
               AND quantidade_disponivel > 0
             ORDER BY data_validade NULLS LAST, data_aquisicao
             FOR UPDATE
        LOOP
            EXIT WHEN v_restante <= 0;
            v_retirar := LEAST(v_lote.quantidade_disponivel, v_restante);

            UPDATE lotes_insumo
               SET quantidade_disponivel = quantidade_disponivel - v_retirar
             WHERE id = v_lote.id;

            -- Registra movimentação
            INSERT INTO movimentacoes_estoque (lote_insumo_id, insumo_id, refeicao_id, quantidade, tipo,
                                               usuario_id, ip_origem, user_agent, tipo_perda_id, observacao)
            VALUES (v_lote.id, v_need.insumo_id, p_refeicao_id, v_retirar, 'BAIXA_EXECUCAO',
                    v_usuario_id, v_ip, v_user_agent, NULL, NULL);

            -- =====================================================================
            -- EVENT SOURCING: insere evento no event_store
            -- =====================================================================
            PERFORM fn_registrar_evento(
                'LOTE_INSUMO',                 -- aggregate_type
                v_lote.id,                     -- aggregate_id
                'ESTOQUE_DEBITADO',            -- event_type
                jsonb_build_object(
                    'insumo_id', v_need.insumo_id,
                    'quantidade', v_retirar,
                    'refeicao_id', p_refeicao_id,
                    'lote_insumo_id', v_lote.id,
                    'estoque_anterior', v_lote.quantidade_disponivel + v_retirar,
                    'estoque_novo', v_lote.quantidade_disponivel,
                    'motivo', 'EXECUCAO_REFEICAO'
                )
            );

            v_restante := v_restante - v_retirar;
        END LOOP;

        IF v_restante > 0 THEN
            RAISE EXCEPTION 'Condição de corrida: insumo % ficou sem estoque suficiente entre a checagem e a baixa (restam % a debitar) — tente novamente', v_need.insumo_id, v_restante
                USING ERRCODE = 'P1002';
        END IF;
    END LOOP;

    UPDATE refeicoes SET status = 'EXECUTADA' WHERE id = p_refeicao_id;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- ESTORNO DE EXECUÇÃO — devolve ao estoque o que foi debitado
-- Transição refeicoes.status: EXECUTADA -> CANCELADA (cancelar depois de
-- executada). Devolve, LOTE A LOTE, exatamente o que foi debitado por
-- fn_executar_refeicao — não recalcula a necessidade de novo.
--
-- ATUALIZAÇÃO (Event Sourcing): Agora insere evento 'ESTOQUE_CREDITADO'
-- para cada lote restaurado.
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_estornar_execucao_refeicao(
    p_refeicao_id UUID,
    p_usuario_id UUID DEFAULT NULL,
    p_ip VARCHAR(45) DEFAULT NULL,
    p_user_agent TEXT DEFAULT NULL
)
RETURNS VOID AS $$
DECLARE
    v_status VARCHAR(20);
    v_mov    RECORD;
    v_usuario_id UUID;
    v_ip VARCHAR(45);
    v_user_agent TEXT;
BEGIN
    IF p_usuario_id IS NOT NULL THEN
        v_usuario_id := p_usuario_id;
    ELSE
        v_usuario_id := NULLIF(current_setting('app.usuario_id', true), '')::UUID;
    END IF;

    IF p_ip IS NOT NULL THEN
        v_ip := p_ip;
    ELSE
        v_ip := current_setting('app.ip_origem', true);
    END IF;

    IF p_user_agent IS NOT NULL THEN
        v_user_agent := p_user_agent;
    ELSE
        v_user_agent := current_setting('app.user_agent', true);
    END IF;

    SELECT status INTO v_status FROM refeicoes WHERE id = p_refeicao_id FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Refeição % não encontrada', p_refeicao_id USING ERRCODE = 'P1000';
    END IF;

    IF v_status <> 'EXECUTADA' THEN
        RAISE EXCEPTION 'Só é possível estornar a partir de EXECUTADA (status atual: %)', v_status
            USING ERRCODE = 'P1001';
    END IF;

    FOR v_mov IN
        SELECT lote_insumo_id, insumo_id, quantidade
          FROM movimentacoes_estoque
         WHERE refeicao_id = p_refeicao_id AND tipo = 'BAIXA_EXECUCAO'
    LOOP
        UPDATE lotes_insumo
           SET quantidade_disponivel = quantidade_disponivel + v_mov.quantidade
         WHERE id = v_mov.lote_insumo_id;

        INSERT INTO movimentacoes_estoque (lote_insumo_id, insumo_id, refeicao_id, quantidade, tipo,
                                           usuario_id, ip_origem, user_agent, tipo_perda_id, observacao)
        VALUES (v_mov.lote_insumo_id, v_mov.insumo_id, p_refeicao_id, v_mov.quantidade, 'ESTORNO_CANCELAMENTO',
                v_usuario_id, v_ip, v_user_agent, NULL, NULL);

        -- =====================================================================
        -- EVENT SOURCING: insere evento de crédito
        -- =====================================================================
        PERFORM fn_registrar_evento(
            'LOTE_INSUMO',
            v_mov.lote_insumo_id,
            'ESTOQUE_CREDITADO',
            jsonb_build_object(
                'insumo_id', v_mov.insumo_id,
                'quantidade', v_mov.quantidade,
                'refeicao_id', p_refeicao_id,
                'lote_insumo_id', v_mov.lote_insumo_id,
                'motivo', 'ESTORNO_CANCELAMENTO'
            )
        );
    END LOOP;

    UPDATE refeicoes SET status = 'CANCELADA' WHERE id = p_refeicao_id;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- REGISTRO DE PERDA / AJUSTE MANUAL
--
-- ATUALIZAÇÃO (Event Sourcing): Agora insere evento 'ESTOQUE_PERDA' para
-- cada lote afetado.
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_registrar_perda(
    p_insumo_id UUID,
    p_quantidade NUMERIC,
    p_tipo_perda_nome VARCHAR,
    p_observacao TEXT DEFAULT NULL,
    p_lote_id UUID DEFAULT NULL,
    p_usuario_id UUID DEFAULT NULL,
    p_ip VARCHAR(45) DEFAULT NULL,
    p_user_agent TEXT DEFAULT NULL
)
RETURNS VOID AS $$
DECLARE
    v_tipo_perda_id UUID;
    v_lote RECORD;
    v_restante NUMERIC(12,3);
    v_retirar NUMERIC(12,3);
    v_usuario_id UUID;
    v_ip VARCHAR(45);
    v_user_agent TEXT;
BEGIN
    IF p_quantidade <= 0 THEN
        RAISE EXCEPTION 'Quantidade deve ser positiva' USING ERRCODE = 'P2000';
    END IF;

    SELECT id INTO v_tipo_perda_id FROM tipos_perda WHERE nome = p_tipo_perda_nome AND ativo = TRUE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Tipo de perda "%" não encontrado ou inativo', p_tipo_perda_nome USING ERRCODE = 'P2001';
    END IF;

    IF p_usuario_id IS NOT NULL THEN
        v_usuario_id := p_usuario_id;
    ELSE
        v_usuario_id := NULLIF(current_setting('app.usuario_id', true), '')::UUID;
    END IF;

    IF p_ip IS NOT NULL THEN
        v_ip := p_ip;
    ELSE
        v_ip := current_setting('app.ip_origem', true);
    END IF;

    IF p_user_agent IS NOT NULL THEN
        v_user_agent := p_user_agent;
    ELSE
        v_user_agent := current_setting('app.user_agent', true);
    END IF;

    IF p_lote_id IS NOT NULL THEN
        SELECT id, quantidade_disponivel INTO v_lote
          FROM lotes_insumo
         WHERE id = p_lote_id AND insumo_id = p_insumo_id
           FOR UPDATE;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'Lote % não encontrado para o insumo %', p_lote_id, p_insumo_id USING ERRCODE = 'P2002';
        END IF;

        IF v_lote.quantidade_disponivel < p_quantidade THEN
            RAISE EXCEPTION 'Estoque insuficiente no lote: disponível %, solicitado %',
                v_lote.quantidade_disponivel, p_quantidade USING ERRCODE = 'P2003';
        END IF;

        UPDATE lotes_insumo
           SET quantidade_disponivel = quantidade_disponivel - p_quantidade
         WHERE id = p_lote_id;

        INSERT INTO movimentacoes_estoque (
            lote_insumo_id, insumo_id, refeicao_id, quantidade, tipo,
            usuario_id, ip_origem, user_agent, tipo_perda_id, observacao
        ) VALUES (
            p_lote_id, p_insumo_id, NULL, p_quantidade, 'AJUSTE_MANUAL',
            v_usuario_id, v_ip, v_user_agent, v_tipo_perda_id, p_observacao
        );

        -- =====================================================================
        -- EVENT SOURCING: evento de perda para o lote específico
        -- =====================================================================
        PERFORM fn_registrar_evento(
            'LOTE_INSUMO',
            p_lote_id,
            'ESTOQUE_PERDA',
            jsonb_build_object(
                'insumo_id', p_insumo_id,
                'quantidade', p_quantidade,
                'tipo_perda', p_tipo_perda_nome,
                'observacao', p_observacao,
                'lote_insumo_id', p_lote_id,
                'estoque_anterior', v_lote.quantidade_disponivel + p_quantidade,
                'estoque_novo', v_lote.quantidade_disponivel
            )
        );

    ELSE
        v_restante := p_quantidade;

        FOR v_lote IN
            SELECT id, quantidade_disponivel
              FROM lotes_insumo
             WHERE insumo_id = p_insumo_id
               AND quantidade_disponivel > 0
             ORDER BY data_validade NULLS LAST, data_aquisicao
             FOR UPDATE
        LOOP
            EXIT WHEN v_restante <= 0;

            v_retirar := LEAST(v_lote.quantidade_disponivel, v_restante);

            UPDATE lotes_insumo
               SET quantidade_disponivel = quantidade_disponivel - v_retirar
             WHERE id = v_lote.id;

            INSERT INTO movimentacoes_estoque (
                lote_insumo_id, insumo_id, refeicao_id, quantidade, tipo,
                usuario_id, ip_origem, user_agent, tipo_perda_id, observacao
            ) VALUES (
                v_lote.id, p_insumo_id, NULL, v_retirar, 'AJUSTE_MANUAL',
                v_usuario_id, v_ip, v_user_agent, v_tipo_perda_id, p_observacao
            );

            -- =====================================================================
            -- EVENT SOURCING: evento de perda para cada lote
            -- =====================================================================
            PERFORM fn_registrar_evento(
                'LOTE_INSUMO',
                v_lote.id,
                'ESTOQUE_PERDA',
                jsonb_build_object(
                    'insumo_id', p_insumo_id,
                    'quantidade', v_retirar,
                    'tipo_perda', p_tipo_perda_nome,
                    'observacao', p_observacao,
                    'lote_insumo_id', v_lote.id,
                    'estoque_anterior', v_lote.quantidade_disponivel + v_retirar,
                    'estoque_novo', v_lote.quantidade_disponivel
                )
            );

            v_restante := v_restante - v_retirar;
        END LOOP;

        IF v_restante > 0 THEN
            RAISE EXCEPTION 'Estoque insuficiente para registrar a perda: faltam %', v_restante USING ERRCODE = 'P2003';
        END IF;
    END IF;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- WORKER: consumo do outbox eventos_dominio (§9 do schema)
-- Chamado periodicamente (ex.: a cada poucos segundos) ou por LISTEN/NOTIFY
--
-- ATUALIZAÇÃO (Hardening do Worker + Auditoria):
--  - Processa eventos em lotes (LIMIT 100) com FOR UPDATE SKIP LOCKED.
--  - Cada evento é processado em sub-bloco com SAVEPOINT; após 3 falhas,
--    o evento é marcado como processado e bloqueado_em é preenchido.
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_processar_eventos_pendentes()
RETURNS VOID AS $$
DECLARE
    ev RECORD;
    v_insumo_id UUID;
    v_prato RECORD;
    v_refeicao RECORD;
    v_menu RECORD;
    v_genero VARCHAR;
BEGIN
    FOR ev IN 
        SELECT * FROM eventos_dominio 
        WHERE processado = FALSE 
          AND (tentativas < 3 OR tentativas IS NULL)
        ORDER BY criado_em 
        LIMIT 100 
        FOR UPDATE SKIP LOCKED
    LOOP
        BEGIN
            IF ev.tipo = 'PrecoAtualizado' THEN
                v_insumo_id := (ev.payload ->> 'insumo_id')::UUID;

                UPDATE itens_receita ir
                   SET custo_unitario_registrado = i.custo_medio_ponderado
                  FROM insumos i
                 WHERE i.id = v_insumo_id AND ir.insumo_id = v_insumo_id;

                FOR v_prato IN SELECT DISTINCT prato_id FROM itens_receita WHERE insumo_id = v_insumo_id LOOP
                    PERFORM fn_recalcular_abc_prato(v_prato.prato_id);
                END LOOP;

                SELECT g.nome INTO v_genero
                  FROM insumos i
                  JOIN categorias c ON c.id = i.categoria_id
                  JOIN generos g ON g.id = c.genero_id
                 WHERE i.id = v_insumo_id;
                PERFORM fn_recalcular_abc_insumo_genero(v_genero);

                FOR v_refeicao IN
                    SELECT DISTINCT irf.refeicao_id
                      FROM itens_refeicao irf
                      JOIN refeicoes r ON r.id = irf.refeicao_id
                     WHERE irf.prato_id IN (SELECT prato_id FROM itens_receita WHERE insumo_id = v_insumo_id)
                       AND r.status IN ('PLANEJADA', 'CONFIRMADA', 'EXECUTADA')
                LOOP
                    PERFORM fn_recalcular_abc_refeicao(v_refeicao.refeicao_id);
                END LOOP;

                FOR v_menu IN
                    SELECT DISTINCT im.menu_id
                      FROM itens_menu im
                      JOIN menus m ON m.id = im.menu_id
                     WHERE im.refeicao_id IN (
                         SELECT DISTINCT irf.refeicao_id
                           FROM itens_refeicao irf
                          WHERE irf.prato_id IN (SELECT prato_id FROM itens_receita WHERE insumo_id = v_insumo_id)
                     )
                       AND m.status IN ('PLANEJADO', 'CONFIRMADO')
                LOOP
                    PERFORM fn_recalcular_abc_menu(v_menu.menu_id);
                END LOOP;
            END IF;

            UPDATE eventos_dominio 
               SET processado = TRUE, processado_em = now() 
             WHERE id = ev.id;

        EXCEPTION
            WHEN OTHERS THEN
                UPDATE eventos_dominio 
                   SET tentativas = tentativas + 1,
                       ultimo_erro = SUBSTRING(SQLERRM, 1, 500)
                 WHERE id = ev.id;

                UPDATE eventos_dominio 
                   SET processado = TRUE,
                       bloqueado_em = now(),
                       ultimo_erro = 'FALHA_PERMANENTE: ' || ultimo_erro
                 WHERE id = ev.id AND tentativas >= 3;
        END;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- MRP — Previsão de Compras
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_mrp_previsao_compras(p_data_limite DATE)
RETURNS TABLE (
    insumo_id           UUID,
    insumo_nome         VARCHAR,
    unidade             VARCHAR,
    necessidade_bruta   NUMERIC,
    estoque_disponivel  NUMERIC,
    necessidade_liquida NUMERIC,
    classe_abc          CHAR,
    fornecedor_sugerido VARCHAR
) AS $$
BEGIN
    RETURN QUERY
    WITH necessidade AS (
        SELECT
            ir.insumo_id,
            SUM(ir.peso_liquido * (r.qtd_pessoas::numeric / NULLIF(p.rendimento_base_porcoes, 0))) AS qtd_necessaria
        FROM itens_menu im
        JOIN menus m        ON m.id = im.menu_id
        JOIN refeicoes r     ON r.id = im.refeicao_id
        JOIN itens_refeicao irf ON irf.refeicao_id = r.id
        JOIN pratos p        ON p.id = irf.prato_id
        JOIN itens_receita ir ON ir.prato_id = p.id
        WHERE m.status IN ('PLANEJADO', 'CONFIRMADO')
          AND m.data_inicio <= p_data_limite
        GROUP BY ir.insumo_id
    ),
    estoque AS (
        SELECT l.insumo_id, SUM(l.quantidade_disponivel) AS disponivel
          FROM lotes_insumo l
         GROUP BY l.insumo_id
    ),
    abc_insumo AS (
        SELECT DISTINCT ON (item_id) item_id, classe
          FROM classificacoes_abc
         WHERE escopo_tipo = 'INSUMO_GENERO'
         ORDER BY item_id, atualizado_em DESC
    ),
    fornecedor_pref AS (
        SELECT DISTINCT ON (i.id) i.id AS insumo_id, f.nome AS fornecedor_nome
          FROM insumos i
          JOIN fornecedores_categorias fc ON fc.categoria_id = i.categoria_id
          JOIN fornecedores f ON f.id = fc.fornecedor_id AND f.ativo
         ORDER BY i.id, f.avaliacao DESC NULLS LAST
    )
    SELECT
        n.insumo_id,
        i.nome,
        i.unidade,
        ROUND(n.qtd_necessaria, 3),
        COALESCE(e.disponivel, 0),
        ROUND(GREATEST(n.qtd_necessaria - COALESCE(e.disponivel, 0), 0), 3) AS necessidade_liquida,
        COALESCE(ab.classe, 'C'),
        fp.fornecedor_nome
    FROM necessidade n
    JOIN insumos i ON i.id = n.insumo_id
    LEFT JOIN estoque e ON e.insumo_id = n.insumo_id
    LEFT JOIN abc_insumo ab ON ab.item_id = n.insumo_id
    LEFT JOIN fornecedor_pref fp ON fp.insumo_id = n.insumo_id
    WHERE n.qtd_necessaria - COALESCE(e.disponivel, 0) > 0
    ORDER BY
        CASE COALESCE(ab.classe, 'C') WHEN 'A' THEN 1 WHEN 'B' THEN 2 ELSE 3 END,
        necessidade_liquida DESC;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- PREVISÃO DE CONSUMO (Fase 4)
-- =====================================================================

-- ---------------------------------------------------------------------
-- fn_calcular_previsao_consumo
-- Calcula previsão de consumo combinando:
--   1. MRP por evento (necessidade alocada na data do menu, calculada 1x para todo o horizonte)
--   2. Média histórica de BAIXA_EXECUCAO por dia da semana (calibração)
-- Isso substitui a abordagem anterior que chamava fn_mrp_previsao_compras N×M vezes
-- (uma por dia por insumo) e diluía a necessidade do evento por p_dias.
CREATE OR REPLACE FUNCTION fn_calcular_previsao_consumo(
    p_insumo_id UUID,
    p_dias INTEGER DEFAULT 30,
    p_historico INTEGER DEFAULT 90
)
RETURNS TABLE (
    data_referencia DATE,
    quantidade_prevista NUMERIC(12,3),
    metodo VARCHAR(30)
) AS $$
DECLARE
    v_data DATE;
    v_semana_dia INTEGER;
    v_consumo_dia_semana NUMERIC(12,3);
    v_necessidade_evento NUMERIC(12,3);
    v_metodo VARCHAR(30);
BEGIN
    -- ----------------------------------------------------------------
    -- 1. MRP calculado UMA ÚNICA VEZ para todo o horizonte.
    --    Resultado em tabela temporária: necessidade bruta por data de
    --    início do menu (não diluída por dia — cada evento tem seu pico
    --    na data em que ocorre, não espalhado pelo horizonte inteiro).
    -- ----------------------------------------------------------------
    CREATE TEMP TABLE IF NOT EXISTS _mrp_horizonte (
        insumo_id UUID,
        data_evento DATE,
        necessidade NUMERIC(12,3)
    ) ON COMMIT DROP;

    TRUNCATE _mrp_horizonte;

    INSERT INTO _mrp_horizonte (insumo_id, data_evento, necessidade)
    SELECT
        ir.insumo_id,
        m.data_inicio AS data_evento,
        SUM(ir.peso_liquido * (r.qtd_pessoas::numeric / NULLIF(p.rendimento_base_porcoes, 0)))
            AS necessidade
    FROM itens_menu im
    JOIN menus m         ON m.id = im.menu_id
    JOIN refeicoes r     ON r.id = im.refeicao_id
    JOIN itens_refeicao irf ON irf.refeicao_id = r.id
    JOIN pratos p        ON p.id = irf.prato_id
    JOIN itens_receita ir ON ir.prato_id = p.id
    WHERE m.status IN ('PLANEJADO', 'CONFIRMADO')
      AND m.data_inicio BETWEEN CURRENT_DATE AND CURRENT_DATE + p_dias
      AND ir.insumo_id = p_insumo_id
    GROUP BY ir.insumo_id, m.data_inicio;

    -- ----------------------------------------------------------------
    -- 2. Para cada dia do horizonte, combina:
    --    a) Base histórica: média de consumo real (BAIXA_EXECUCAO)
    --       pelo mesmo dia da semana nos últimos p_historico dias.
    --       Fallback: média geral do período se não houver dado por
    --       dia da semana.
    --    b) Necessidade de evento: soma das necessidades MRP alocadas
    --       exatamente nesta data (não diluída).
    --    Método reportado reflete qual fonte dominou.
    -- ----------------------------------------------------------------
    FOR v_data IN
        SELECT generate_series(CURRENT_DATE, CURRENT_DATE + p_dias - 1, '1 day'::interval)::date
    LOOP
        v_semana_dia := EXTRACT(DOW FROM v_data);

        -- Base histórica por dia da semana
        SELECT COALESCE(AVG(quantidade), 0)
          INTO v_consumo_dia_semana
          FROM (
              SELECT SUM(quantidade) AS quantidade
                FROM movimentacoes_estoque
               WHERE insumo_id = p_insumo_id
                 AND tipo = 'BAIXA_EXECUCAO'
                 AND criado_em >= CURRENT_DATE - (p_historico || ' days')::INTERVAL
               GROUP BY DATE_TRUNC('day', criado_em)
               HAVING EXTRACT(DOW FROM DATE_TRUNC('day', criado_em)) = v_semana_dia
          ) consumo_dia_semana;

        -- Fallback: média geral se não há histórico para este dia da semana
        IF v_consumo_dia_semana = 0 THEN
            SELECT COALESCE(AVG(quantidade), 0)
              INTO v_consumo_dia_semana
              FROM (
                  SELECT SUM(quantidade) AS quantidade
                    FROM movimentacoes_estoque
                   WHERE insumo_id = p_insumo_id
                     AND tipo = 'BAIXA_EXECUCAO'
                     AND criado_em >= CURRENT_DATE - (p_historico || ' days')::INTERVAL
                   GROUP BY DATE_TRUNC('day', criado_em)
              ) consumo_total;
        END IF;

        -- Necessidade de evento alocada nesta data (do MRP pré-calculado)
        SELECT COALESCE(SUM(necessidade), 0)
          INTO v_necessidade_evento
          FROM _mrp_horizonte
         WHERE data_evento = v_data;

        -- Determina método predominante
        IF v_necessidade_evento > 0 AND v_consumo_dia_semana > 0 THEN
            v_metodo := 'HIBRIDO_EVENTO_HISTORICO';
        ELSIF v_necessidade_evento > 0 THEN
            v_metodo := 'MRP_EVENTO';
        ELSE
            v_metodo := 'MEDIA_HISTORICA';
        END IF;

        data_referencia     := v_data;
        quantidade_prevista := ROUND(
            COALESCE(v_consumo_dia_semana, 0) + COALESCE(v_necessidade_evento, 0),
            3
        );
        metodo := v_metodo;

        RETURN NEXT;
    END LOOP;

    RETURN;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- fn_atualizar_previsoes_consumo
-- Worker que atualiza a tabela previsoes_consumo para todos os insumos
-- ativos. Deve ser chamado periodicamente (ex.: diariamente) pelo
-- forecast_worker em Python.
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_atualizar_previsoes_consumo(
    p_dias INTEGER DEFAULT 30,
    p_historico INTEGER DEFAULT 90
)
RETURNS VOID AS $$
DECLARE
    v_insumo RECORD;
    v_previsao RECORD;
    v_versao INTEGER;
BEGIN
    FOR v_insumo IN
        SELECT id FROM insumos WHERE ativo = TRUE
    LOOP
        SELECT COALESCE(MAX(versao), 0) + 1 INTO v_versao
          FROM previsoes_consumo
         WHERE insumo_id = v_insumo.id;

        FOR v_previsao IN
            SELECT data_referencia, quantidade_prevista, metodo
              FROM fn_calcular_previsao_consumo(v_insumo.id, p_dias, p_historico)
        LOOP
            INSERT INTO previsoes_consumo (
                insumo_id,
                data_referencia,
                quantidade_prevista,
                metodo,
                versao,
                gerado_em
            ) VALUES (
                v_insumo.id,
                v_previsao.data_referencia,
                v_previsao.quantidade_prevista,
                v_previsao.metodo,
                v_versao,
                now()
            ) ON CONFLICT (insumo_id, data_referencia, versao)
            DO UPDATE SET
                quantidade_prevista = EXCLUDED.quantidade_prevista,
                metodo = EXCLUDED.metodo,
                gerado_em = now();
        END LOOP;

        DELETE FROM previsoes_consumo
         WHERE insumo_id = v_insumo.id
           AND versao < v_versao;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- fn_preencher_consumo_real
-- Atualiza a coluna quantidade_real em previsoes_consumo para datas
-- que já passaram, comparando com o consumo real registrado.
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_preencher_consumo_real()
RETURNS VOID AS $$
BEGIN
    UPDATE previsoes_consumo pc
       SET quantidade_real = (
           SELECT COALESCE(SUM(quantidade), 0)
             FROM movimentacoes_estoque
            WHERE insumo_id = pc.insumo_id
              AND tipo = 'BAIXA_EXECUCAO'
              AND criado_em::date = pc.data_referencia
       )
     WHERE pc.data_referencia < CURRENT_DATE
       AND pc.quantidade_real IS NULL;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- RAG / DOCUMENTOS (Busca Semântica)
-- =====================================================================

-- ---------------------------------------------------------------------
-- fn_buscar_documentos_similares
-- Busca documentos por similaridade de embedding usando distância
-- de cosseno. Retorna os top_k documentos mais relevantes.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_buscar_documentos_similares(
    p_embedding vector,
    p_limite INTEGER DEFAULT 5,
    p_tipo VARCHAR DEFAULT NULL,
    p_entidade_id UUID DEFAULT NULL
)
RETURNS TABLE (
    id UUID,
    titulo VARCHAR,
    conteudo TEXT,
    similaridade FLOAT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        d.id,
        d.titulo,
        d.conteudo,
        1 - (d.embedding <=> p_embedding) AS similaridade
    FROM documentos d
    WHERE d.embedding IS NOT NULL
      AND (p_tipo IS NULL OR d.tipo = p_tipo)
      AND (p_entidade_id IS NULL OR d.entidade_id = p_entidade_id)
    ORDER BY d.embedding <=> p_embedding
    LIMIT p_limite;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- (Opcional) Função para atualizar embedding de um documento
-- Útil quando o documento é criado via Python e o embedding é gerado
-- posteriormente.
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_atualizar_embedding_documento(
    p_documento_id UUID,
    p_embedding vector
)
RETURNS VOID AS $$
BEGIN
    UPDATE documentos
       SET embedding = p_embedding,
           atualizado_em = now()
     WHERE id = p_documento_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Documento % não encontrado', p_documento_id USING ERRCODE = 'P3000';
    END IF;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- (Opcional) Função auxiliar para listar tipos de perda ativos
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_listar_tipos_perda()
RETURNS TABLE (
    id UUID,
    nome VARCHAR,
    descricao TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT tp.id, tp.nome, tp.descricao
      FROM tipos_perda tp
     WHERE tp.ativo = TRUE
     ORDER BY tp.nome;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- EVENT SOURCING — Função auxiliar para reconstruir saldo de um lote
-- a partir dos eventos armazenados (replay)
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_reconstruir_estoque_lote(p_lote_id UUID)
RETURNS NUMERIC AS $$
DECLARE
    v_saldo NUMERIC := 0;
    ev RECORD;
BEGIN
    FOR ev IN
        SELECT event_type, payload
          FROM event_store
         WHERE aggregate_type = 'LOTE_INSUMO'
           AND aggregate_id = p_lote_id
         ORDER BY occurred_at
    LOOP
        IF ev.event_type = 'ESTOQUE_ENTRADA' THEN
            v_saldo := v_saldo + (ev.payload->>'quantidade')::NUMERIC;
        ELSIF ev.event_type IN ('ESTOQUE_DEBITADO', 'ESTOQUE_PERDA') THEN
            v_saldo := v_saldo - (ev.payload->>'quantidade')::NUMERIC;
        ELSIF ev.event_type = 'ESTOQUE_CREDITADO' THEN
            v_saldo := v_saldo + (ev.payload->>'quantidade')::NUMERIC;
        END IF;
    END LOOP;

    RETURN v_saldo;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- Fim do arquivo business-queries.sql
-- =====================================================================

-- =====================================================================
-- FUNÇÕES DO MÓDULO FINANCEIRO
-- =====================================================================

-- Atualiza automaticamente o status de contas vencidas
-- Pode ser chamado por um worker agendado (ex.: diariamente)
CREATE OR REPLACE FUNCTION fn_atualizar_status_contas_vencidas()
RETURNS VOID AS $$
BEGIN
    UPDATE contas_pagar
       SET status = 'ATRASADO'
     WHERE status IN ('PENDENTE', 'PAGO_PARCIAL')
       AND data_vencimento < CURRENT_DATE
       AND (data_pagamento IS NULL OR data_pagamento > data_vencimento);

    UPDATE contas_receber
       SET status = 'ATRASADO'
     WHERE status IN ('PENDENTE', 'RECEBIDO_PARCIAL')
       AND data_vencimento < CURRENT_DATE
       AND (data_recebimento IS NULL OR data_recebimento > data_vencimento);
END;
$$ LANGUAGE plpgsql;

-- Baixa uma conta a pagar (pagamento total ou parcial)
CREATE OR REPLACE FUNCTION fn_baixar_conta_pagar(
    p_conta_id UUID,
    p_valor_pago NUMERIC,
    p_data_pagamento DATE DEFAULT CURRENT_DATE,
    p_usuario_id UUID DEFAULT NULL
)
RETURNS VOID AS $$
DECLARE
    v_status_atual VARCHAR(20);
    v_valor_original NUMERIC;
    v_usuario_id UUID;
BEGIN
    -- Obtém usuário do contexto se não fornecido
    IF p_usuario_id IS NOT NULL THEN
        v_usuario_id := p_usuario_id;
    ELSE
        v_usuario_id := NULLIF(current_setting('app.usuario_id', true), '')::UUID;
    END IF;

    -- Lock na conta para evitar concorrência
    SELECT status, valor_original INTO v_status_atual, v_valor_original
      FROM contas_pagar WHERE id = p_conta_id FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Conta a pagar % não encontrada', p_conta_id USING ERRCODE = 'P4000';
    END IF;

    IF v_status_atual IN ('PAGO', 'CANCELADO') THEN
        RAISE EXCEPTION 'Conta já está com status %', v_status_atual USING ERRCODE = 'P4001';
    END IF;

    IF p_valor_pago <= 0 THEN
        RAISE EXCEPTION 'Valor pago deve ser positivo' USING ERRCODE = 'P4002';
    END IF;

    IF p_valor_pago > v_valor_original THEN
        RAISE EXCEPTION 'Valor pago (%) excede o valor original (%)', p_valor_pago, v_valor_original USING ERRCODE = 'P4003';
    END IF;

    UPDATE contas_pagar
       SET valor_pago = p_valor_pago,
           data_pagamento = p_data_pagamento,
           status = CASE
                        WHEN p_valor_pago >= v_valor_original THEN 'PAGO'
                        ELSE 'PAGO_PARCIAL'
                    END,
           atualizado_em = now(),
           criado_por = COALESCE(v_usuario_id, criado_por)
     WHERE id = p_conta_id;
END;
$$ LANGUAGE plpgsql;

-- Baixa uma conta a receber (recebimento total ou parcial)
CREATE OR REPLACE FUNCTION fn_baixar_conta_receber(
    p_conta_id UUID,
    p_valor_recebido NUMERIC,
    p_data_recebimento DATE DEFAULT CURRENT_DATE,
    p_usuario_id UUID DEFAULT NULL
)
RETURNS VOID AS $$
DECLARE
    v_status_atual VARCHAR(20);
    v_valor_original NUMERIC;
    v_usuario_id UUID;
BEGIN
    IF p_usuario_id IS NOT NULL THEN
        v_usuario_id := p_usuario_id;
    ELSE
        v_usuario_id := NULLIF(current_setting('app.usuario_id', true), '')::UUID;
    END IF;

    SELECT status, valor_original INTO v_status_atual, v_valor_original
      FROM contas_receber WHERE id = p_conta_id FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Conta a receber % não encontrada', p_conta_id USING ERRCODE = 'P4004';
    END IF;

    IF v_status_atual IN ('RECEBIDO', 'CANCELADO') THEN
        RAISE EXCEPTION 'Conta já está com status %', v_status_atual USING ERRCODE = 'P4005';
    END IF;

    IF p_valor_recebido <= 0 THEN
        RAISE EXCEPTION 'Valor recebido deve ser positivo' USING ERRCODE = 'P4006';
    END IF;

    IF p_valor_recebido > v_valor_original THEN
        RAISE EXCEPTION 'Valor recebido (%) excede o valor original (%)', p_valor_recebido, v_valor_original USING ERRCODE = 'P4007';
    END IF;

    UPDATE contas_receber
       SET valor_recebido = p_valor_recebido,
           data_recebimento = p_data_recebimento,
           status = CASE
                        WHEN p_valor_recebido >= v_valor_original THEN 'RECEBIDO'
                        ELSE 'RECEBIDO_PARCIAL'
                    END,
           atualizado_em = now(),
           criado_por = COALESCE(v_usuario_id, criado_por)
     WHERE id = p_conta_id;
END;
$$ LANGUAGE plpgsql;

-- Resumo financeiro (dashboard)
CREATE OR REPLACE FUNCTION fn_resumo_financeiro()
RETURNS TABLE (
    total_a_pagar NUMERIC,
    total_atrasado_pagar NUMERIC,
    total_a_receber NUMERIC,
    total_atrasado_receber NUMERIC,
    saldo_previsto NUMERIC
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        COALESCE((SELECT SUM(valor_original - COALESCE(valor_pago, 0)) FROM contas_pagar WHERE status IN ('PENDENTE', 'PAGO_PARCIAL')), 0) AS total_a_pagar,
        COALESCE((SELECT SUM(valor_original - COALESCE(valor_pago, 0)) FROM contas_pagar WHERE status = 'ATRASADO'), 0) AS total_atrasado_pagar,
        COALESCE((SELECT SUM(valor_original - COALESCE(valor_recebido, 0)) FROM contas_receber WHERE status IN ('PENDENTE', 'RECEBIDO_PARCIAL')), 0) AS total_a_receber,
        COALESCE((SELECT SUM(valor_original - COALESCE(valor_recebido, 0)) FROM contas_receber WHERE status = 'ATRASADO'), 0) AS total_atrasado_receber,
        COALESCE(
            (SELECT SUM(valor_original - COALESCE(valor_recebido, 0)) FROM contas_receber WHERE status IN ('PENDENTE', 'RECEBIDO_PARCIAL')) -
            (SELECT SUM(valor_original - COALESCE(valor_pago, 0)) FROM contas_pagar WHERE status IN ('PENDENTE', 'PAGO_PARCIAL'))
        , 0) AS saldo_previsto;
END;
$$ LANGUAGE plpgsql;

-- projecao_resumo_financeiro_mensal

CREATE TABLE projecao_resumo_financeiro_mensal (
    ano_mes VARCHAR(7) PRIMARY KEY, -- ex.: '2026-07'
    total_receitas_previstas NUMERIC(12,2),
    total_receitas_realizadas NUMERIC(12,2),
    total_despesas_previstas NUMERIC(12,2),
    total_despesas_realizadas NUMERIC(12,2),
    saldo_previsto NUMERIC(12,2),
    saldo_realizado NUMERIC(12,2),
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Função para popular (pode ser chamada pelo worker financeiro diariamente)
CREATE OR REPLACE FUNCTION fn_atualizar_projecao_resumo_mensal()
RETURNS VOID AS $$
BEGIN
    TRUNCATE projecao_resumo_financeiro_mensal;

    INSERT INTO projecao_resumo_financeiro_mensal (ano_mes, total_receitas_previstas, total_receitas_realizadas, total_despesas_previstas, total_despesas_realizadas, saldo_previsto, saldo_realizado)
    SELECT
        TO_CHAR(data_vencimento, 'YYYY-MM') AS ano_mes,
        COALESCE(SUM(CASE WHEN tipo = 'RECEBER' THEN valor_original ELSE 0 END), 0) AS total_receitas_previstas,
        COALESCE(SUM(CASE WHEN tipo = 'RECEBER' AND status IN ('RECEBIDO', 'RECEBIDO_PARCIAL') THEN valor_recebido ELSE 0 END), 0) AS total_receitas_realizadas,
        COALESCE(SUM(CASE WHEN tipo = 'PAGAR' THEN valor_original ELSE 0 END), 0) AS total_despesas_previstas,
        COALESCE(SUM(CASE WHEN tipo = 'PAGAR' AND status IN ('PAGO', 'PAGO_PARCIAL') THEN valor_pago ELSE 0 END), 0) AS total_despesas_realizadas,
        COALESCE(SUM(CASE WHEN tipo = 'RECEBER' THEN valor_original ELSE 0 END), 0) - 
        COALESCE(SUM(CASE WHEN tipo = 'PAGAR' THEN valor_original ELSE 0 END), 0) AS saldo_previsto,
        COALESCE(SUM(CASE WHEN tipo = 'RECEBER' AND status IN ('RECEBIDO', 'RECEBIDO_PARCIAL') THEN valor_recebido ELSE 0 END), 0) - 
        COALESCE(SUM(CASE WHEN tipo = 'PAGAR' AND status IN ('PAGO', 'PAGO_PARCIAL') THEN valor_pago ELSE 0 END), 0) AS saldo_realizado
    FROM (
        SELECT data_vencimento, valor_original, valor_recebido AS valor_pago, status, 'RECEBER' AS tipo FROM contas_receber
        UNION ALL
        SELECT data_vencimento, valor_original, valor_pago, status, 'PAGAR' AS tipo FROM contas_pagar
    ) AS todas_contas
    GROUP BY TO_CHAR(data_vencimento, 'YYYY-MM');
END;
$$ LANGUAGE plpgsql;


-- =====================================================================
-- fn_estimar_preco_insumo
-- Estimativa estatística de preço com base no histórico de aquisição.
--
-- Fonte: lotes_insumo (compras reais registradas).
-- Período: últimas p_janela_dias dias (padrão 90).
-- Critério mínimo: pelo menos p_min_compras compras no período (padrão 2).
--
-- Ponderação combinada por recência e volume:
--   w_rec_i = (janela - dias_desde_aquisicao) / SUM(janela - dias_j)
--   w_vol_i = quantidade_i / SUM(quantidade_j)
--   w_i     = SQRT(w_rec_i * w_vol_i)          -- média geométrica
--   preco_estimado = SUM(w_i * valor_i) / SUM(w_i)
--
-- Retorna NULL em todos os campos quando histórico insuficiente.
-- =====================================================================

CREATE OR REPLACE FUNCTION fn_estimar_preco_insumo(
    p_insumo_id      UUID,
    p_janela_dias    INT  DEFAULT 90,
    p_min_compras    INT  DEFAULT 2
)
RETURNS TABLE (
    preco_estimado          NUMERIC(12,4),
    preco_minimo            NUMERIC(12,4),
    preco_maximo            NUMERIC(12,4),
    num_compras             INT,
    fornecedor_mais_barato_id UUID,
    data_ultima_compra      DATE
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_num_compras INT;
BEGIN
    -- Conta compras elegíveis antes de calcular
    SELECT COUNT(*)
      INTO v_num_compras
      FROM lotes_insumo l
     WHERE l.insumo_id = p_insumo_id
       AND l.data_aquisicao >= CURRENT_DATE - p_janela_dias;

    -- Histórico insuficiente → retorna linha com todos NULLs
    IF v_num_compras < p_min_compras THEN
        RETURN QUERY SELECT
            NULL::NUMERIC(12,4),
            NULL::NUMERIC(12,4),
            NULL::NUMERIC(12,4),
            v_num_compras,
            NULL::UUID,
            NULL::DATE;
        RETURN;
    END IF;

    RETURN QUERY
    WITH compras AS (
        -- Compras elegíveis com dias desde aquisição
        SELECT
            l.valor_aquisicao,
            l.quantidade,
            l.fornecedor_id,
            l.data_aquisicao,
            (CURRENT_DATE - l.data_aquisicao)::NUMERIC AS dias
        FROM lotes_insumo l
        WHERE l.insumo_id = p_insumo_id
          AND l.data_aquisicao >= CURRENT_DATE - p_janela_dias
    ),
    totais AS (
        -- Denominadores para normalização
        SELECT
            SUM(p_janela_dias - dias)  AS soma_rec,
            SUM(quantidade)            AS soma_vol
        FROM compras
    ),
    pesos AS (
        -- Pesos individuais normalizados e combinados
        SELECT
            c.valor_aquisicao,
            c.fornecedor_id,
            c.data_aquisicao,
            -- w_rec normalizado
            CASE WHEN t.soma_rec > 0
                 THEN (p_janela_dias - c.dias) / t.soma_rec
                 ELSE 1.0 / (SELECT COUNT(*) FROM compras)
            END AS w_rec,
            -- w_vol normalizado
            CASE WHEN t.soma_vol > 0
                 THEN c.quantidade / t.soma_vol
                 ELSE 1.0 / (SELECT COUNT(*) FROM compras)
            END AS w_vol
        FROM compras c
        CROSS JOIN totais t
    ),
    pesos_combinados AS (
        -- Peso combinado = raiz quadrada do produto (média geométrica)
        SELECT
            valor_aquisicao,
            fornecedor_id,
            data_aquisicao,
            SQRT(w_rec * w_vol) AS w
        FROM pesos
    ),
    resultado AS (
        SELECT
            -- Preço estimado: média ponderada pelo peso combinado (renormalizado)
            ROUND(
                SUM(w * valor_aquisicao) / NULLIF(SUM(w), 0),
                4
            )::NUMERIC(12,4)                           AS preco_estimado,
            MIN(valor_aquisicao)::NUMERIC(12,4)        AS preco_minimo,
            MAX(valor_aquisicao)::NUMERIC(12,4)        AS preco_maximo,
            COUNT(*)::INT                               AS num_compras,
            MAX(data_aquisicao)                        AS data_ultima_compra
        FROM pesos_combinados
    ),
    fornecedor_barato AS (
        -- Fornecedor com menor preço médio no período
        SELECT fornecedor_id
          FROM compras
         WHERE fornecedor_id IS NOT NULL
         GROUP BY fornecedor_id
         ORDER BY AVG(valor_aquisicao)
         LIMIT 1
    )
    SELECT
        r.preco_estimado,
        r.preco_minimo,
        r.preco_maximo,
        r.num_compras,
        fb.fornecedor_id AS fornecedor_mais_barato_id,
        r.data_ultima_compra
    FROM resultado r
    LEFT JOIN fornecedor_barato fb ON TRUE;
END;
$$;