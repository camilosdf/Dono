-- =====================================================================
-- Sistema Dono — Schema PostgreSQL
-- Versão híbrida (ver arquitetura-sistema-restaurante.md)
-- =====================================================================

-- ---------------------------------------------------------------------
-- Extensões
-- ---------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "vector";    -- para embeddings (RAG) - Fase 7

-- ---------------------------------------------------------------------
-- 0. Usuários e RBAC (§4.7)
-- ---------------------------------------------------------------------
CREATE TABLE usuarios (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nome            VARCHAR(200) NOT NULL,
    email           VARCHAR(200) NOT NULL UNIQUE,
    senha_hash      VARCHAR(255) NOT NULL,   -- hash argon2id (via passlib), nunca texto puro
    perfil          VARCHAR(20) NOT NULL
                        CHECK (perfil IN ('CHEF', 'COMPRAS', 'ADMIN', 'GESTAO')),
    ativo           BOOLEAN NOT NULL DEFAULT TRUE,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE refresh_tokens (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    usuario_id      UUID NOT NULL REFERENCES usuarios(id),
    token_hash      VARCHAR(128) NOT NULL UNIQUE,   -- sha256 do token
    familia_id      UUID NOT NULL,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expira_em       TIMESTAMPTZ NOT NULL,
    revogado        BOOLEAN NOT NULL DEFAULT FALSE,
    substituido_por UUID REFERENCES refresh_tokens(id)
);

CREATE INDEX idx_refresh_tokens_usuario ON refresh_tokens(usuario_id) WHERE revogado = FALSE;
CREATE INDEX idx_refresh_tokens_familia ON refresh_tokens(familia_id);

-- ---------------------------------------------------------------------
-- 1. Gêneros e Categorias (§2.1)
-- ---------------------------------------------------------------------
CREATE TABLE generos (
    id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nome    VARCHAR(30) NOT NULL UNIQUE
                CHECK (nome IN ('ALIMENTICIO', 'OPERACIONAL_UTENSILIO'))
);

CREATE TABLE categorias (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nome            VARCHAR(100) NOT NULL,
    genero_id       UUID NOT NULL REFERENCES generos(id),
    UNIQUE (nome, genero_id)
);

-- ---------------------------------------------------------------------
-- 2. Fornecedores
-- ---------------------------------------------------------------------
CREATE TABLE fornecedores (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nome                    VARCHAR(200) NOT NULL,
    contato                 VARCHAR(200),
    prazo_entrega_medio_dias INTEGER,
    condicoes_pagamento     VARCHAR(200),
    avaliacao               NUMERIC(3,2) CHECK (avaliacao BETWEEN 0 AND 5),
    ativo                   BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE fornecedores_categorias (
    fornecedor_id   UUID NOT NULL REFERENCES fornecedores(id) ON DELETE CASCADE,
    categoria_id    UUID NOT NULL REFERENCES categorias(id),
    PRIMARY KEY (fornecedor_id, categoria_id)
);

-- ---------------------------------------------------------------------
-- 3. Insumos e Lotes (§2.1)
-- ---------------------------------------------------------------------
CREATE TABLE insumos (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nome                    VARCHAR(200) NOT NULL,
    categoria_id            UUID NOT NULL REFERENCES categorias(id),
    unidade                 VARCHAR(2) NOT NULL
                                CHECK (unidade IN ('KG', 'L', 'M', 'PC')),
    apresentacao            VARCHAR(30),
    marcas_aceitaveis       TEXT[],
    localizacao_estoque     VARCHAR(100),
    consumivel              BOOLEAN NOT NULL DEFAULT TRUE,
    custo_medio_ponderado   NUMERIC(12,4) NOT NULL DEFAULT 0,
    ativo                   BOOLEAN NOT NULL DEFAULT TRUE,
    criado_em               TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizado_em           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_insumos_categoria ON insumos(categoria_id);

CREATE TABLE lotes_insumo (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    insumo_id               UUID NOT NULL REFERENCES insumos(id),
    fornecedor_id           UUID REFERENCES fornecedores(id),
    valor_aquisicao         NUMERIC(12,4) NOT NULL,
    data_aquisicao          DATE NOT NULL,
    data_validade           DATE,
    quantidade              NUMERIC(12,3) NOT NULL,
    quantidade_disponivel   NUMERIC(12,3) NOT NULL,
    CHECK (quantidade_disponivel >= 0 AND quantidade_disponivel <= quantidade)
);

CREATE INDEX idx_lotes_fefo ON lotes_insumo (insumo_id, data_validade)
    WHERE quantidade_disponivel > 0;

CREATE TABLE cotacoes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    insumo_id           UUID NOT NULL REFERENCES insumos(id),
    fornecedor_id       UUID REFERENCES fornecedores(id),
    preco_unitario      NUMERIC(12,4) NOT NULL,
    data_cotacao        DATE NOT NULL DEFAULT CURRENT_DATE,
    validade_cotacao    DATE,
    origem              VARCHAR(10) NOT NULL CHECK (origem IN ('MANUAL', 'IA_ONLINE')),
    status              VARCHAR(20) NOT NULL DEFAULT 'PENDENTE_REVISAO'
                            CHECK (status IN ('PENDENTE_REVISAO', 'APROVADA', 'REJEITADA')),
    aprovado_por        UUID REFERENCES usuarios(id),
    criado_em           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_cotacoes_insumo ON cotacoes(insumo_id, data_cotacao DESC);

-- ---------------------------------------------------------------------
-- 4. Pratos e Fichas Técnicas (§2.2)
-- ---------------------------------------------------------------------
CREATE TABLE pratos (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nome                        VARCHAR(200) NOT NULL,
    genero_prato                VARCHAR(50) NOT NULL,
    tempo_preparo_min           INTEGER,
    rendimento_base_porcoes     NUMERIC(8,2) NOT NULL,
    tamanho_porcao_g            NUMERIC(8,2),
    modo_preparo                JSONB,
    instrucoes_apresentacao     TEXT,
    equipamentos_utilizados     TEXT[],
    temperatura_servico         VARCHAR(50),
    armazenamento_faixa_temp    VARCHAR(50),
    armazenamento_tempo_max_h   INTEGER,
    margem_desperdicio_pct      NUMERIC(5,2) NOT NULL DEFAULT 0,
    custo_embalagem             NUMERIC(12,4) NOT NULL DEFAULT 0,
    preco_venda_praticado       NUMERIC(12,4),
    origem                      VARCHAR(15) NOT NULL DEFAULT 'MANUAL'
                                    CHECK (origem IN ('MANUAL', 'IA_RASCUNHO')),
    status                      VARCHAR(20) NOT NULL DEFAULT 'ATIVO'
                                    CHECK (status IN ('ATIVO', 'PENDENTE_APROVACAO', 'INATIVO')),
    criado_em                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizado_em               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE itens_receita (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prato_id                UUID NOT NULL REFERENCES pratos(id) ON DELETE CASCADE,
    insumo_id               UUID NOT NULL REFERENCES insumos(id) ON DELETE RESTRICT,
    tipo                    VARCHAR(20) NOT NULL
                                CHECK (tipo IN ('ALIMENTICIO', 'OPERACIONAL', 'UTENSILIO')),
    peso_bruto              NUMERIC(12,4) NOT NULL,
    fator_correcao          NUMERIC(6,3) NOT NULL DEFAULT 1.0,
    peso_liquido            NUMERIC(12,4) GENERATED ALWAYS AS (peso_bruto / NULLIF(fator_correcao, 0)) STORED,
    custo_unitario_registrado NUMERIC(12,4) NOT NULL,
    custo_total_calculado    NUMERIC(12,4) GENERATED ALWAYS AS (peso_bruto * custo_unitario_registrado) STORED,
    UNIQUE (prato_id, insumo_id)
);

CREATE INDEX idx_itens_receita_prato ON itens_receita(prato_id);
CREATE INDEX idx_itens_receita_insumo ON itens_receita(insumo_id);

-- ---------------------------------------------------------------------
-- 5. Regras de composição por gênero de refeição (§2.5)
-- ---------------------------------------------------------------------
CREATE TABLE regras_composicao (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    genero_refeicao         VARCHAR(50) NOT NULL,
    genero_prato_obrigatorio VARCHAR(50) NOT NULL,
    obrigatorio             BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (genero_refeicao, genero_prato_obrigatorio)
);

-- ---------------------------------------------------------------------
-- 6. Refeições (§2.3)
-- ---------------------------------------------------------------------
CREATE TABLE refeicoes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    genero_refeicao     VARCHAR(50) NOT NULL,
    data                DATE NOT NULL,
    horario_inicio      TIME NOT NULL,
    horario_fim         TIME NOT NULL,
    qtd_pessoas         INTEGER NOT NULL CHECK (qtd_pessoas > 0),
    status              VARCHAR(20) NOT NULL DEFAULT 'PLANEJADA'
                            CHECK (status IN ('PLANEJADA', 'CONFIRMADA', 'EXECUTADA', 'SERVIDA', 'CANCELADA'))
);

CREATE TABLE itens_refeicao (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    refeicao_id             UUID NOT NULL REFERENCES refeicoes(id) ON DELETE CASCADE,
    prato_id                UUID NOT NULL REFERENCES pratos(id) ON DELETE RESTRICT,
    categoria_composicao    VARCHAR(50) NOT NULL,
    custo_snapshot          NUMERIC(12,4),
    UNIQUE (refeicao_id, prato_id)
);

-- ---------------------------------------------------------------------
-- 6b. Movimentações de estoque, perdas, auditoria
-- ---------------------------------------------------------------------
CREATE TABLE tipos_perda (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nome            VARCHAR(30) NOT NULL UNIQUE,
    descricao       TEXT,
    ativo           BOOLEAN NOT NULL DEFAULT TRUE,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE movimentacoes_estoque (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lote_insumo_id  UUID NOT NULL REFERENCES lotes_insumo(id),
    insumo_id       UUID NOT NULL REFERENCES insumos(id),
    refeicao_id     UUID REFERENCES refeicoes(id),
    quantidade      NUMERIC(12,3) NOT NULL CHECK (quantidade > 0),
    tipo            VARCHAR(20) NOT NULL DEFAULT 'BAIXA_EXECUCAO'
                        CHECK (tipo IN ('BAIXA_EXECUCAO', 'ESTORNO_CANCELAMENTO', 'AJUSTE_MANUAL')),
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    usuario_id      UUID REFERENCES usuarios(id),
    ip_origem       VARCHAR(45),
    user_agent      TEXT,
    tipo_perda_id   UUID REFERENCES tipos_perda(id),
    observacao      TEXT
);

CREATE INDEX idx_movimentacoes_insumo   ON movimentacoes_estoque(insumo_id, criado_em);
CREATE INDEX idx_movimentacoes_refeicao ON movimentacoes_estoque(refeicao_id);
CREATE INDEX idx_movimentacoes_usuario  ON movimentacoes_estoque(usuario_id);
CREATE INDEX idx_movimentacoes_tipo_perda ON movimentacoes_estoque(tipo_perda_id) WHERE tipo_perda_id IS NOT NULL;

-- ---------------------------------------------------------------------
-- 7. Menus / Eventos (§2.4)
-- ---------------------------------------------------------------------
CREATE TABLE estilos_servico (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nome            VARCHAR(50) NOT NULL UNIQUE,
    descricao       TEXT,
    dinamica        TEXT
);

CREATE TABLE menus (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nome_evento         VARCHAR(200) NOT NULL,
    data_criacao        DATE NOT NULL DEFAULT CURRENT_DATE,
    estilo_servico_id   UUID NOT NULL REFERENCES estilos_servico(id),
    data_inicio         DATE NOT NULL,
    horario_inicio      TIME NOT NULL,
    data_fim            DATE NOT NULL,
    horario_fim         TIME NOT NULL,
    local_servico       VARCHAR(200),
    status              VARCHAR(20) NOT NULL DEFAULT 'PLANEJADO'
                            CHECK (status IN ('PLANEJADO', 'CONFIRMADO', 'REALIZADO', 'CANCELADO'))
);

CREATE TABLE itens_menu (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    menu_id             UUID NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
    refeicao_id         UUID NOT NULL REFERENCES refeicoes(id) ON DELETE RESTRICT,
    ordem_cronologica   INTEGER NOT NULL,
    custo_snapshot      NUMERIC(12,4),
    UNIQUE (menu_id, ordem_cronologica)
);

-- ---------------------------------------------------------------------
-- 8. Classificação ABC materializada (§3)
-- ---------------------------------------------------------------------
CREATE TABLE classificacoes_abc (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    escopo_tipo         VARCHAR(20) NOT NULL
                            CHECK (escopo_tipo IN ('INSUMO_GENERO', 'PRATO', 'REFEICAO', 'MENU')),
    escopo_id_pai       UUID NOT NULL,
    item_id             UUID NOT NULL,
    custo               NUMERIC(12,4) NOT NULL,
    percentual_acumulado NUMERIC(5,2) NOT NULL,
    classe              CHAR(1) NOT NULL CHECK (classe IN ('A', 'B', 'C')),
    atualizado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (escopo_tipo, escopo_id_pai, item_id)
);

CREATE INDEX idx_abc_item ON classificacoes_abc(item_id, atualizado_em DESC);

-- ---------------------------------------------------------------------
-- 9. Outbox de eventos de domínio + resiliência + auditoria
-- ---------------------------------------------------------------------
CREATE TABLE eventos_dominio (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tipo            VARCHAR(50) NOT NULL,
    payload         JSONB NOT NULL,
    processado      BOOLEAN NOT NULL DEFAULT FALSE,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    processado_em   TIMESTAMPTZ,
    tentativas      INTEGER NOT NULL DEFAULT 0,
    ultimo_erro     TEXT,
    bloqueado_em    TIMESTAMPTZ,
    usuario_id      UUID REFERENCES usuarios(id),
    ip_origem       VARCHAR(45),
    user_agent      TEXT
);

CREATE INDEX idx_eventos_pendentes ON eventos_dominio(criado_em) WHERE processado = FALSE;
CREATE INDEX idx_eventos_com_erro ON eventos_dominio(bloqueado_em) WHERE bloqueado_em IS NOT NULL;
CREATE INDEX idx_eventos_usuario ON eventos_dominio(usuario_id);

-- ---------------------------------------------------------------------
-- 9b. Jobs assíncronos de IA (§4.6 / §8 da API)
-- ---------------------------------------------------------------------
CREATE TABLE ia_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tipo            VARCHAR(30) NOT NULL CHECK (tipo IN ('COTACAO_ONLINE', 'PROSPECCAO_PRATOS', 'OCR_NOTA', 'RAG_CONSULTA')),
    status          VARCHAR(20) NOT NULL DEFAULT 'pendente'
                        CHECK (status IN ('pendente', 'processando', 'concluido', 'erro')),
    solicitado_por  UUID NOT NULL REFERENCES usuarios(id),
    entrada         JSONB NOT NULL,
    resultado       JSONB,
    erro_motivo     TEXT,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    concluido_em    TIMESTAMPTZ
);

-- =====================================================================
-- 10. PREVISÃO DE CONSUMO (Fase 4)
--     Armazena previsões de consumo de insumos calculadas com base
--     em médias históricas e menus futuros.
-- =====================================================================
CREATE TABLE previsoes_consumo (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    insumo_id               UUID NOT NULL REFERENCES insumos(id) ON DELETE CASCADE,
    data_referencia         DATE NOT NULL,
    quantidade_prevista     NUMERIC(12,3) NOT NULL,
    quantidade_real         NUMERIC(12,3),
    metodo                  VARCHAR(30) DEFAULT 'MEDIA_MOVEL',
    gerado_em               TIMESTAMPTZ NOT NULL DEFAULT now(),
    versao                  INTEGER NOT NULL DEFAULT 1,
    UNIQUE (insumo_id, data_referencia, versao)
);

CREATE INDEX idx_previsoes_insumo_data ON previsoes_consumo(insumo_id, data_referencia);
CREATE INDEX idx_previsoes_data_referencia ON previsoes_consumo(data_referencia);
CREATE INDEX idx_previsoes_metodo ON previsoes_consumo(metodo);

-- =====================================================================
-- 11. RAG / DOCUMENTOS PARA IA (Fase 7)
--     Tabela para armazenar documentos (fichas técnicas, POPs,
--     legislação, notas fiscais) com seus embeddings para busca
--     por similaridade (RAG).
-- =====================================================================
CREATE TABLE documentos (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    titulo          VARCHAR(200),
    conteudo        TEXT NOT NULL,
    tipo            VARCHAR(30) CHECK (tipo IN ('FICHA_TECNICA', 'POP', 'LEGISLACAO', 'NOTA_FISCAL', 'OUTRO')),
    entidade_id     UUID,
    metadados       JSONB,
    embedding       vector(384),        -- dimensão fixa para o modelo all-MiniLM-L6-v2
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_documentos_embedding ON documentos
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE INDEX idx_documentos_tipo ON documentos(tipo);
CREATE INDEX idx_documentos_entidade ON documentos(entidade_id) WHERE entidade_id IS NOT NULL;

-- =====================================================================
-- 12. Trigger: NOTIFY no outbox — acorda o worker imediatamente
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_notificar_evento_pendente()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('evento_novo', NEW.id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_eventos_dominio_notify
AFTER INSERT ON eventos_dominio
FOR EACH ROW EXECUTE FUNCTION fn_notificar_evento_pendente();

-- =====================================================================
-- 13. Função auxiliar para definir o contexto de auditoria
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_set_audit_context(
    p_usuario_id UUID,
    p_ip VARCHAR(45),
    p_user_agent TEXT
) RETURNS VOID AS $$
BEGIN
    PERFORM set_config('app.usuario_id', p_usuario_id::text, false);
    PERFORM set_config('app.ip_origem', p_ip, false);
    PERFORM set_config('app.user_agent', p_user_agent, false);
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- 14. Triggers para snapshot de custos (Refeição/Menu)
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_snapshot_custo_refeicao()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'CONFIRMADA' AND OLD.status <> 'CONFIRMADA' THEN
        UPDATE itens_refeicao ir
           SET custo_snapshot = (
                SELECT COALESCE(SUM(ic.custo_total_calculado), 0) * (1 + p.margem_desperdicio_pct / 100.0)
                       / NULLIF(p.rendimento_base_porcoes, 0)
                  FROM itens_receita ic
                  JOIN pratos p ON p.id = ic.prato_id
                 WHERE ic.prato_id = ir.prato_id
                 GROUP BY p.margem_desperdicio_pct, p.rendimento_base_porcoes
           )
         WHERE ir.refeicao_id = NEW.id;

        PERFORM fn_recalcular_abc_refeicao(NEW.id);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_refeicao_confirmada
AFTER UPDATE OF status ON refeicoes
FOR EACH ROW EXECUTE FUNCTION fn_snapshot_custo_refeicao();

CREATE OR REPLACE FUNCTION fn_snapshot_custo_menu()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'CONFIRMADO' AND OLD.status <> 'CONFIRMADO' THEN
        UPDATE itens_menu im
           SET custo_snapshot = (
                SELECT SUM(ir.custo_snapshot * r.qtd_pessoas)
                  FROM itens_refeicao ir
                  JOIN refeicoes r ON r.id = ir.refeicao_id
                 WHERE ir.refeicao_id = im.refeicao_id
           )
         WHERE im.menu_id = NEW.id;

        PERFORM fn_recalcular_abc_menu(NEW.id);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_menu_confirmado
AFTER UPDATE OF status ON menus
FOR EACH ROW EXECUTE FUNCTION fn_snapshot_custo_menu();

-- =====================================================================
-- 15. Trigger: novo lote de insumo atualiza custo médio e dispara evento
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_atualizar_custo_medio_insumo()
RETURNS TRIGGER AS $$
DECLARE
    v_custo_medio NUMERIC(12,4);
    v_usuario_id UUID;
    v_ip VARCHAR(45);
    v_user_agent TEXT;
BEGIN
    v_usuario_id := NULLIF(current_setting('app.usuario_id', true), '')::UUID;
    v_ip := current_setting('app.ip_origem', true);
    v_user_agent := current_setting('app.user_agent', true);

    SELECT COALESCE(SUM(valor_aquisicao * quantidade) / NULLIF(SUM(quantidade), 0), 0)
      INTO v_custo_medio
      FROM lotes_insumo
     WHERE insumo_id = NEW.insumo_id;

    UPDATE insumos
       SET custo_medio_ponderado = v_custo_medio,
           atualizado_em = now()
     WHERE id = NEW.insumo_id;

    INSERT INTO eventos_dominio (tipo, payload, usuario_id, ip_origem, user_agent)
    VALUES ('PrecoAtualizado', jsonb_build_object('insumo_id', NEW.insumo_id),
            v_usuario_id, v_ip, v_user_agent);

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_lote_insumo_custo
AFTER INSERT ON lotes_insumo
FOR EACH ROW EXECUTE FUNCTION fn_atualizar_custo_medio_insumo();

-- =====================================================================
-- 16. Seeds iniciais
-- =====================================================================
INSERT INTO generos (nome) VALUES ('ALIMENTICIO'), ('OPERACIONAL_UTENSILIO')
ON CONFLICT (nome) DO NOTHING;

INSERT INTO tipos_perda (nome, descricao) VALUES
    ('VALIDADE', 'Perda por vencimento do prazo de validade'),
    ('QUEBRA', 'Perda por dano físico ou quebra durante manuseio'),
    ('PRODUCAO', 'Perda ocorrida durante o processo de produção'),
    ('SOBRA_LIMPA', 'Sobra que ainda pode ser reaproveitada ou doada'),
    ('SOBRA_SUJA', 'Sobra que não pode ser reaproveitada'),
    ('INVENTARIO', 'Ajuste decorrente de contagem de inventário'),
    ('AMOSTRA', 'Retirada para testes, degustação ou controle de qualidade')
ON CONFLICT (nome) DO NOTHING;

-- =====================================================================
-- 17. Event Store (para Event Sourcing)
--     Tabela genérica para armazenar todos os eventos de domínio.
--     Os eventos são imutáveis e representam fatos consumados.
-- =====================================================================

CREATE TABLE event_store (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aggregate_type  VARCHAR(50) NOT NULL,   -- ex.: 'LOTE_INSUMO', 'INSUMO', 'REFEICAO'
    aggregate_id    UUID NOT NULL,          -- ID da entidade afetada (lote, insumo, refeição)
    event_type      VARCHAR(50) NOT NULL,   -- ex.: 'ESTOQUE_DEBITADO', 'ESTOQUE_CREDITADO', 'PERDA_REGISTRADA'
    payload         JSONB NOT NULL,         -- detalhes específicos do evento
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    usuario_id      UUID REFERENCES usuarios(id),
    ip_origem       VARCHAR(45),
    user_agent      TEXT,
    version         INTEGER NOT NULL DEFAULT 1  -- número de sequência para o aggregate
);

CREATE INDEX idx_event_store_aggregate ON event_store(aggregate_type, aggregate_id, occurred_at);
CREATE INDEX idx_event_store_type ON event_store(event_type);

-- Função auxiliar para inserir eventos de forma consistente
CREATE OR REPLACE FUNCTION fn_registrar_evento(
    p_aggregate_type VARCHAR,
    p_aggregate_id UUID,
    p_event_type VARCHAR,
    p_payload JSONB
)
RETURNS UUID AS $$
DECLARE
    v_usuario_id UUID;
    v_ip VARCHAR(45);
    v_user_agent TEXT;
    v_version INTEGER;
    v_event_id UUID;
BEGIN
    -- Obtém contexto de auditoria (configurado pelo middleware)
    v_usuario_id := NULLIF(current_setting('app.usuario_id', true), '')::UUID;
    v_ip := current_setting('app.ip_origem', true);
    v_user_agent := current_setting('app.user_agent', true);

    -- Calcula a próxima versão para o aggregate
    SELECT COALESCE(MAX(version), 0) + 1 INTO v_version
      FROM event_store
     WHERE aggregate_type = p_aggregate_type
       AND aggregate_id = p_aggregate_id;

    INSERT INTO event_store (
        aggregate_type,
        aggregate_id,
        event_type,
        payload,
        occurred_at,
        usuario_id,
        ip_origem,
        user_agent,
        version
    ) VALUES (
        p_aggregate_type,
        p_aggregate_id,
        p_event_type,
        p_payload,
        now(),
        v_usuario_id,
        v_ip,
        v_user_agent,
        v_version
    ) RETURNING id INTO v_event_id;

    RETURN v_event_id;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- 18. Módulo Financeiro – Contas a Pagar e Contas a Receber
-- =====================================================================

CREATE TABLE contas_pagar (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fornecedor_id       UUID NOT NULL REFERENCES fornecedores(id),
    descricao           VARCHAR(300) NOT NULL,
    valor_original      NUMERIC(12,2) NOT NULL CHECK (valor_original >= 0),
    valor_pago          NUMERIC(12,2) CHECK (valor_pago >= 0),
    data_vencimento     DATE NOT NULL,
    data_pagamento      DATE,
    status              VARCHAR(20) NOT NULL DEFAULT 'PENDENTE'
                            CHECK (status IN ('PENDENTE', 'PAGO_PARCIAL', 'PAGO', 'CANCELADO', 'ATRASADO')),
    tipo_despesa        VARCHAR(50), -- COMPRA_INSUMO, SERVICO, IMPOSTO, OUTRO
    observacao          TEXT,
    lote_insumo_id      UUID REFERENCES lotes_insumo(id), -- vinculo com a compra
    criado_em           TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    criado_por          UUID REFERENCES usuarios(id)
);

CREATE INDEX idx_contas_pagar_fornecedor ON contas_pagar(fornecedor_id);
CREATE INDEX idx_contas_pagar_vencimento ON contas_pagar(data_vencimento) WHERE status IN ('PENDENTE', 'ATRASADO');
CREATE INDEX idx_contas_pagar_lote ON contas_pagar(lote_insumo_id) WHERE lote_insumo_id IS NOT NULL;

CREATE TABLE contas_receber (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    menu_id             UUID REFERENCES menus(id),       -- quando vinculado a um evento realizado
    cliente_nome        VARCHAR(200),
    descricao           VARCHAR(300) NOT NULL,
    valor_original      NUMERIC(12,2) NOT NULL CHECK (valor_original >= 0),
    valor_recebido      NUMERIC(12,2) CHECK (valor_recebido >= 0),
    data_vencimento     DATE NOT NULL,
    data_recebimento    DATE,
    status              VARCHAR(20) NOT NULL DEFAULT 'PENDENTE'
                            CHECK (status IN ('PENDENTE', 'RECEBIDO_PARCIAL', 'RECEBIDO', 'CANCELADO', 'ATRASADO')),
    observacao          TEXT,
    criado_em           TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    criado_por          UUID REFERENCES usuarios(id)
);

CREATE INDEX idx_contas_receber_menu ON contas_receber(menu_id);
CREATE INDEX idx_contas_receber_vencimento ON contas_receber(data_vencimento) WHERE status IN ('PENDENTE', 'ATRASADO');

-- ---------------------------------------------------------------------
-- Trigger para atualizar 'atualizado_em' automaticamente
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_atualizar_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.atualizado_em = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_contas_pagar_updated_at
BEFORE UPDATE ON contas_pagar
FOR EACH ROW EXECUTE FUNCTION fn_atualizar_updated_at();

CREATE TRIGGER trg_contas_receber_updated_at
BEFORE UPDATE ON contas_receber
FOR EACH ROW EXECUTE FUNCTION fn_atualizar_updated_at();

-- =====================================================================
-- Tabela projecao_estoque_atual
-- =====================================================================
-- 1. Tabela de projeção
CREATE TABLE projecao_estoque_atual (
    lote_insumo_id UUID PRIMARY KEY REFERENCES lotes_insumo(id),
    insumo_id UUID NOT NULL REFERENCES insumos(id),
    saldo_atual NUMERIC(12,3) NOT NULL DEFAULT 0,
    ultima_atualizacao TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_projecao_estoque_insumo ON projecao_estoque_atual(insumo_id);

-- 2. Função para reconstruir a projeção (replay total ou por lote)
CREATE OR REPLACE FUNCTION fn_reconstruir_projecao_estoque(p_lote_id UUID DEFAULT NULL)
RETURNS VOID AS $$
DECLARE
    v_lote RECORD;
BEGIN
    IF p_lote_id IS NOT NULL THEN
        -- Atualiza apenas um lote específico
        INSERT INTO projecao_estoque_atual (lote_insumo_id, insumo_id, saldo_atual, ultima_atualizacao)
        SELECT 
            l.id,
            l.insumo_id,
            fn_reconstruir_estoque_lote(l.id),
            now()
        FROM lotes_insumo l
        WHERE l.id = p_lote_id
        ON CONFLICT (lote_insumo_id) DO UPDATE
        SET saldo_atual = EXCLUDED.saldo_atual,
            ultima_atualizacao = EXCLUDED.ultima_atualizacao;
    ELSE
        -- Reconstroi todos os lotes
        TRUNCATE projecao_estoque_atual;
        INSERT INTO projecao_estoque_atual (lote_insumo_id, insumo_id, saldo_atual, ultima_atualizacao)
        SELECT 
            l.id,
            l.insumo_id,
            fn_reconstruir_estoque_lote(l.id),
            now()
        FROM lotes_insumo l;
    END IF;
END;
$$ LANGUAGE plpgsql;

-- 3. Trigger para atualizar a projeção automaticamente quando um evento é inserido no event_store
-- (Opcional: fazer de forma síncrona ou via worker)
CREATE OR REPLACE FUNCTION fn_trg_atualizar_projecao_estoque()
RETURNS TRIGGER AS $$
BEGIN
    -- Só atualiza se for evento de estoque
    IF NEW.aggregate_type = 'LOTE_INSUMO' THEN
        -- Dispara a reconstrução apenas para o lote afetado (barato)
        PERFORM fn_reconstruir_projecao_estoque(NEW.aggregate_id);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_event_store_projecao_estoque
AFTER INSERT ON event_store
FOR EACH ROW EXECUTE FUNCTION fn_trg_atualizar_projecao_estoque();