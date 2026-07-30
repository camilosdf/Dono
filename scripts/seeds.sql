-- =====================================================================
-- Sistema Dono — Seeds de Dados Iniciais
--
-- Este arquivo contém os dados de referência (dicionários) necessários
-- para o funcionamento básico do sistema. É executado automaticamente
-- pelo docker-entrypoint-initdb.d/ após schema.sql e business-queries.sql.
--
-- ATENÇÃO: Os gêneros (generos) são seedados diretamente no schema.sql
-- (INSERT INTO generos (nome) VALUES ('ALIMENTICIO'), ('OPERACIONAL_UTENSILIO'))
-- porque são referenciados pelas categorias, que são seedadas aqui.
--
-- ATUALIZAÇÃO (Tabela de Perdas e Ajustes):
--   - Adicionada seção 4 para seed dos tipos de perda padrão,
--     utilizados para classificar movimentações de estoque do tipo
--     AJUSTE_MANUAL (perdas por validade, quebra, produção, sobras, etc.).
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Categorias (subclasses de gênero — texto original do domínio)
--    Pré-requisito: generos já populado (feito no schema.sql)
-- ---------------------------------------------------------------------
INSERT INTO categorias (nome, genero_id)
SELECT v.nome, g.id
FROM (VALUES
    -- Gênero Alimentício (Alimentos e Bebidas)
    ('Secos e Despensa',          'ALIMENTICIO'),
    ('Hortifruti',                'ALIMENTICIO'),
    ('Carnes, Aves e Peixes',     'ALIMENTICIO'),
    ('Laticínios e Frios',        'ALIMENTICIO'),
    ('Bebidas',                   'ALIMENTICIO'),
    ('Congelados',                'ALIMENTICIO'),
    -- Gênero Operacionais e Utensílios (Não-Alimentícios)
    ('Descartáveis e Embalagens', 'OPERACIONAL_UTENSILIO'),
    ('Limpeza e Higienização',    'OPERACIONAL_UTENSILIO'),
    ('Material de Salão e Bar',   'OPERACIONAL_UTENSILIO'),
    ('Energéticos',               'OPERACIONAL_UTENSILIO'),
    ('Utensílios',                'OPERACIONAL_UTENSILIO')
) AS v(nome, genero_nome)
JOIN generos g ON g.nome = v.genero_nome
ON CONFLICT (nome, genero_id) DO NOTHING;

-- ---------------------------------------------------------------------
-- 2. Estilos de Serviço (nome; descrição; dinâmica — texto original)
-- ---------------------------------------------------------------------
INSERT INTO estilos_servico (nome, descricao, dinamica) VALUES

('Franco-Americano (Buffet / Self-Service)',
 'Serviço em que os alimentos ficam expostos em balcões térmicos, quentes e frios.',
 'O próprio cliente se serve (self-service) ou funcionários da cozinha servem as porções nas rampas.'),

('À La Carte (Serviço Emprestado / Americano)',
 'O cliente escolhe o prato através de um cardápio e a comida já vem montada e decorada diretamente da cozinha.',
 'O garçom serve o prato pronto pela direita do cliente e retira os pratos usados pela esquerda.'),

('À Francesa',
 'Um dos estilos mais formais, clássicos e sofisticados do mundo, utilizado em banquetes de gala e alta gastronomia.',
 'O garçom traz a travessa da cozinha e a apresenta ao cliente pelo lado esquerdo. O próprio cliente utiliza os talheres de serviço para se servir da travessa diretamente para o seu prato.'),

('À Inglesa Direto',
 'Um estilo elegante, muito comum em casamentos, jantares corporativos e restaurantes tradicionais.',
 'O garçom traz a travessa da cozinha e serve o cliente pelo lado esquerdo, utilizando o alicate (colher e garfo na mesma mão).'),

('À Inglesa Indireto (Gueridon)',
 'Variante indireta do serviço à inglesa, com montagem do prato à vista do cliente.',
 'O garçom traz a travessa da cozinha e a coloca em uma mesa auxiliar (guéridon). Ele monta os pratos à vista do cliente e depois os serve pelo lado direito.'),

('À Russa',
 'Historicamente parecido com o serviço à inglesa indireto, mas focado na finalização visual no salão.',
 'Grandes peças de carne (como leitões, perus ou costelas) vêm inteiras da cozinha. O garçom ou o próprio chef fatia, porciona e flamba a comida no carrinho auxiliar (guéridon) na frente dos clientes antes de servir.'),

('À Família (Familiar / Compartilhado)',
 'Um estilo focado em informalidade, aconchego e interação entre as pessoas da mesa.',
 'Os pratos e travessas vêm da cozinha com grandes porções de comida e são colocados no centro da mesa. Os próprios clientes passam as travessas e servem uns aos outros.')

ON CONFLICT (nome) DO NOTHING;

-- ---------------------------------------------------------------------
-- 3. Regras de Composição por Gênero de Refeição
--    Confirmação: a segunda ocorrência de "O lanche manhã" no
--    documento original era erro de digitação — trata-se do
--    "Lanche da Tarde", já nomeado como tal abaixo.
-- ---------------------------------------------------------------------
INSERT INTO regras_composicao (genero_refeicao, genero_prato_obrigatorio) VALUES

-- Café da Manhã · 7h às 10h30
('Café da Manhã', 'Padaria'),
('Café da Manhã', 'Frios/Laticínios'),
('Café da Manhã', 'Quentes'),
('Café da Manhã', 'Bebida Quente'),
('Café da Manhã', 'Bebida Fria'),
('Café da Manhã', 'Frutas'),

-- Lanche da Manhã · 10h às 11h
('Lanche da Manhã', 'Frios/Laticínios'),
('Lanche da Manhã', 'Frutas'),

-- Almoço Executivo · 12h às 15h
('Almoço Executivo', 'Entrada'),
('Almoço Executivo', 'Prato Principal'),
('Almoço Executivo', 'Guarnição'),
('Almoço Executivo', 'Bebida Quente'),
('Almoço Executivo', 'Bebida Fria'),
('Almoço Executivo', 'Sobremesa'),

-- Lanche da Tarde · 16h às 17h
('Lanche da Tarde', 'Frios/Laticínios'),
('Lanche da Tarde', 'Frutas'),

-- Jantar · 18h às 21h30
('Jantar', 'Entrada'),
('Jantar', 'Prato Principal'),
('Jantar', 'Guarnição'),
('Jantar', 'Bebida Quente'),
('Jantar', 'Bebida Fria'),
('Jantar', 'Sobremesa'),

-- "Fine Dining" · 20h às 02h
('Fine Dining', 'Aperitivo & Couvert'),
('Fine Dining', 'Entrada'),
('Fine Dining', 'Prato Principal'),
('Fine Dining', 'Sorbet/Queijos'),
('Fine Dining', 'Sobremesa'),
('Fine Dining', 'Digestivo/Café'),

-- Coquetel · horário livre
('Coquetel', 'Frios/Laticínios'),
('Coquetel', 'Salgados Quentes e Assados'),
('Coquetel', 'Finger Food'),
('Coquetel', 'Sobremesa'),

-- Coffee Break · horário livre
('Coffee Break', 'Bebida Quente'),
('Coffee Break', 'Bebida Fria'),
('Coffee Break', 'Padaria'),
('Coffee Break', 'Frios/Laticínios'),
('Coffee Break', 'Frutas'),

-- Colação
('Colação', 'Frios/Laticínios'),
('Colação', 'Padaria'),
('Colação', 'Bebida Quente')

ON CONFLICT (genero_refeicao, genero_prato_obrigatorio) DO NOTHING;

-- ---------------------------------------------------------------------
-- 4. Tipos de Perda e Ajuste (Tabela de Perdas e Ajustes)
--    Utilizados em movimentacoes_estoque com tipo = 'AJUSTE_MANUAL'
--    para classificar a natureza da perda/ajuste.
-- ---------------------------------------------------------------------
INSERT INTO tipos_perda (nome, descricao) VALUES
    ('VALIDADE', 'Perda por vencimento do prazo de validade'),
    ('QUEBRA', 'Perda por dano físico ou quebra durante manuseio'),
    ('PRODUCAO', 'Perda ocorrida durante o processo de produção (ex.: queima, derramamento, sobra de preparo)'),
    ('SOBRA_LIMPA', 'Sobra de alimento que ainda pode ser reaproveitada ou doada (não é perda financeira total)'),
    ('SOBRA_SUJA', 'Sobra que não pode ser reaproveitada (contaminada, misturada, descartada)'),
    ('INVENTARIO', 'Ajuste decorrente de contagem de inventário (diferença entre o físico e o sistema)'),
    ('AMOSTRA', 'Retirada para testes, degustação ou controle de qualidade')
ON CONFLICT (nome) DO NOTHING;