# backend/tests/test_fn_estimar_preco.py — Sistema Dono
#
# Testes para fn_estimar_preco_insumo (business-queries.sql).
#
# A função é determinística — SQL/PL/pgSQL calcula, Ollama apenas explica
# (Fase 2). Estes testes validam as propriedades matemáticas da fórmula
# antes de qualquer integração com IA.
#
# Fórmula (resumo):
#   w_rec_i = (janela - dias_desde_aquisicao) / SUM(janela - dias_j)
#   w_vol_i = quantidade_i / SUM(quantidade_j)
#   w_i     = SQRT(w_rec_i * w_vol_i)   -- média geométrica
#   preco_estimado = SUM(w_i * valor_i) / SUM(w_i)
#
# Propriedades testadas:
#   P1. Histórico insuficiente (<2 compras) → preco_estimado=NULL, num_compras correto
#   P2. Resultado sempre entre min e max
#   P3. Compra mais recente + maior volume → estimativa puxada para cima
#   P4. Compra fora da janela não participa
#   P5. Janela e mínimo de compras parametrizáveis
#   P6. Determinismo: mesmo conjunto → mesmo resultado
#   P7. Fornecedor mais barato identificado corretamente
#   P8. Data da última compra correta
#
# Fixtures: conn (conexão direta, sem pool global)
# Isolamento: cada teste insere e limpa seus próprios dados via conn.

import uuid
import pytest


# =====================================================================
# Helper — setup de insumo com lotes controlados
# =====================================================================

async def _setup_insumo(conn) -> uuid.UUID:
    """Cria um insumo temporário usando a primeira categoria disponível."""
    cat_id = await conn.fetchval("SELECT id FROM categorias LIMIT 1")
    nome = f"_Estimativa_{uuid.uuid4().hex[:8]}"
    insumo_id = await conn.fetchval(
        """INSERT INTO insumos (nome, categoria_id, unidade, ativo)
           VALUES ($1, $2, 'KG', TRUE) RETURNING id""",
        nome, cat_id,
    )
    return insumo_id


async def _inserir_lote(conn, insumo_id, valor, dias_atras, quantidade=100.0, fornecedor_id=None):
    """Insere um lote com data relativa a CURRENT_DATE."""
    from datetime import date, timedelta
    data_aquisicao = date.today() - timedelta(days=dias_atras)
    await conn.execute(
        """INSERT INTO lotes_insumo
               (insumo_id, fornecedor_id, valor_aquisicao, data_aquisicao,
                quantidade, quantidade_disponivel)
           VALUES ($1, $2, $3, $4, $5, $5)""",
        insumo_id, fornecedor_id, valor, data_aquisicao, quantidade,
    )


async def _estimar(conn, insumo_id, janela=90, min_compras=2):
    """Chama fn_estimar_preco_insumo e retorna o resultado como dict."""
    row = await conn.fetchrow(
        "SELECT * FROM fn_estimar_preco_insumo($1, $2, $3)",
        insumo_id, janela, min_compras,
    )
    return dict(row)


# =====================================================================
# P1 — Histórico insuficiente
# =====================================================================

@pytest.mark.asyncio
class TestHistoricoInsuficiente:
    """Valida retorno NULL quando não há compras suficientes."""

    async def test_zero_compras_retorna_null(self, conn):
        """Sem nenhuma compra, preco_estimado deve ser NULL e num_compras=0."""
        insumo_id = await _setup_insumo(conn)
        r = await _estimar(conn, insumo_id)

        assert r["preco_estimado"] is None
        assert r["preco_minimo"] is None
        assert r["preco_maximo"] is None
        assert r["num_compras"] == 0
        assert r["fornecedor_mais_barato_id"] is None
        assert r["data_ultima_compra"] is None

    async def test_uma_compra_retorna_null(self, conn):
        """Com apenas 1 compra, preco_estimado deve ser NULL (mínimo=2)."""
        insumo_id = await _setup_insumo(conn)
        await _inserir_lote(conn, insumo_id, valor=10.0, dias_atras=5)

        r = await _estimar(conn, insumo_id)

        assert r["preco_estimado"] is None
        assert r["num_compras"] == 1  # conta a compra mas não estima

    async def test_minimo_customizado_uma_compra_suficiente(self, conn):
        """Com min_compras=1, uma única compra deve ser suficiente para estimar."""
        insumo_id = await _setup_insumo(conn)
        await _inserir_lote(conn, insumo_id, valor=15.0, dias_atras=10)

        r = await _estimar(conn, insumo_id, min_compras=1)

        assert r["preco_estimado"] is not None
        assert float(r["preco_estimado"]) == pytest.approx(15.0, abs=0.01)
        assert r["num_compras"] == 1


# =====================================================================
# P2 — Resultado entre min e max
# =====================================================================

@pytest.mark.asyncio
class TestResultadoEntreMinEMax:
    """Média ponderada convexa deve sempre produzir resultado dentro do intervalo."""

    async def test_estimado_entre_min_e_max_dois_precos(self, conn):
        """Com compras de R$10 e R$20, estimativa deve estar em (10, 20)."""
        insumo_id = await _setup_insumo(conn)
        await _inserir_lote(conn, insumo_id, valor=10.0, dias_atras=30, quantidade=100)
        await _inserir_lote(conn, insumo_id, valor=20.0, dias_atras=5,  quantidade=100)

        r = await _estimar(conn, insumo_id)

        assert r["preco_estimado"] is not None
        assert float(r["preco_minimo"]) == pytest.approx(10.0, abs=0.001)
        assert float(r["preco_maximo"]) == pytest.approx(20.0, abs=0.001)
        assert float(r["preco_estimado"]) > float(r["preco_minimo"])
        assert float(r["preco_estimado"]) < float(r["preco_maximo"])

    async def test_estimado_entre_min_e_max_multiplos_precos(self, conn):
        """Com 5 compras em faixas diferentes, estimativa deve estar no intervalo."""
        insumo_id = await _setup_insumo(conn)
        dados = [
            (5.00,  dias, 50.0)
            for dias in (80, 60, 40, 20, 5)
        ] + [(50.0, 3, 200.0)]

        for valor, dias, qtd in dados:
            await _inserir_lote(conn, insumo_id, valor=valor, dias_atras=dias, quantidade=qtd)

        r = await _estimar(conn, insumo_id)

        assert float(r["preco_estimado"]) >= float(r["preco_minimo"])
        assert float(r["preco_estimado"]) <= float(r["preco_maximo"])


# =====================================================================
# P3 — Ponderação: recente + maior volume pesa mais
# =====================================================================

@pytest.mark.asyncio
class TestPonderacao:
    """Valida que recência e volume influenciam a estimativa na direção correta."""

    async def test_compra_recente_maior_preco_puxa_estimativa_para_cima(self, conn):
        """Compra recente de maior valor deve puxar a estimativa acima da média simples.
        Media simples = (10+20)/2 = 15. Estimativa ponderada deve ser > 15."""
        insumo_id = await _setup_insumo(conn)
        await _inserir_lote(conn, insumo_id, valor=10.0, dias_atras=5,  quantidade=200)
        await _inserir_lote(conn, insumo_id, valor=20.0, dias_atras=1,  quantidade=200)

        r = await _estimar(conn, insumo_id)

        media_simples = 15.0
        assert float(r["preco_estimado"]) > media_simples

    async def test_compra_maior_volume_maior_preco_puxa_estimativa_para_cima(self, conn):
        """Compra de maior volume (mesmo se mesma idade) deve ter maior peso.
        Compra barata: 100kg. Compra cara: 300kg. Estimativa deve ser > média simples."""
        insumo_id = await _setup_insumo(conn)
        await _inserir_lote(conn, insumo_id, valor=10.0, dias_atras=10, quantidade=100)
        await _inserir_lote(conn, insumo_id, valor=20.0, dias_atras=10, quantidade=300)

        r = await _estimar(conn, insumo_id)

        media_simples = 15.0
        assert float(r["preco_estimado"]) > media_simples

    async def test_compras_identicas_retornam_proprio_preco(self, conn):
        """Duas compras com mesmo preço, mesma quantidade e mesma data devem
        retornar exatamente esse preço como estimativa."""
        insumo_id = await _setup_insumo(conn)
        await _inserir_lote(conn, insumo_id, valor=12.50, dias_atras=10, quantidade=100)
        await _inserir_lote(conn, insumo_id, valor=12.50, dias_atras=10, quantidade=100)

        r = await _estimar(conn, insumo_id)

        assert float(r["preco_estimado"]) == pytest.approx(12.50, abs=0.001)


# =====================================================================
# P4 — Compra fora da janela não participa
# =====================================================================

@pytest.mark.asyncio
class TestJanelaTemporal:

    async def test_compra_alem_da_janela_nao_participa(self, conn):
        """Compra de 91 dias atrás (janela padrão=90) não deve entrar no cálculo."""
        insumo_id = await _setup_insumo(conn)
        await _inserir_lote(conn, insumo_id, valor=10.0, dias_atras=5,   quantidade=100)
        await _inserir_lote(conn, insumo_id, valor=20.0, dias_atras=1,   quantidade=200)
        # Esta compra não deve participar
        await _inserir_lote(conn, insumo_id, valor=1.0,  dias_atras=91,  quantidade=9999)

        r = await _estimar(conn, insumo_id)

        # num_compras deve ser 2, não 3
        assert r["num_compras"] == 2
        # Estimativa não deve ser puxada para 1.00
        assert float(r["preco_estimado"]) > 5.0

    async def test_compra_exatamente_na_janela_participa(self, conn):
        """Compra de exatamente 90 dias atrás (== p_janela_dias) deve participar."""
        insumo_id = await _setup_insumo(conn)
        await _inserir_lote(conn, insumo_id, valor=10.0, dias_atras=90, quantidade=100)
        await _inserir_lote(conn, insumo_id, valor=20.0, dias_atras=1,  quantidade=100)

        r = await _estimar(conn, insumo_id)

        assert r["num_compras"] == 2
        assert r["preco_estimado"] is not None


# =====================================================================
# P5 — Parâmetros customizáveis
# =====================================================================

@pytest.mark.asyncio
class TestParametros:

    async def test_janela_customizada_30_dias(self, conn):
        """Com janela de 30 dias, compra de 31 dias atrás não deve participar."""
        insumo_id = await _setup_insumo(conn)
        await _inserir_lote(conn, insumo_id, valor=10.0, dias_atras=31, quantidade=100)
        await _inserir_lote(conn, insumo_id, valor=20.0, dias_atras=5,  quantidade=100)

        # Com janela=30, só a compra de 5 dias participa → insuficiente (min=2)
        r = await _estimar(conn, insumo_id, janela=30, min_compras=2)
        assert r["preco_estimado"] is None
        assert r["num_compras"] == 1

        # Com janela=90 (padrão), ambas participam
        r2 = await _estimar(conn, insumo_id, janela=90, min_compras=2)
        assert r2["preco_estimado"] is not None
        assert r2["num_compras"] == 2

    async def test_min_compras_3_insuficiente_com_2(self, conn):
        """Com min_compras=3, 2 compras devem retornar NULL."""
        insumo_id = await _setup_insumo(conn)
        await _inserir_lote(conn, insumo_id, valor=10.0, dias_atras=10, quantidade=100)
        await _inserir_lote(conn, insumo_id, valor=20.0, dias_atras=5,  quantidade=100)

        r = await _estimar(conn, insumo_id, min_compras=3)

        assert r["preco_estimado"] is None
        assert r["num_compras"] == 2


# =====================================================================
# P6 — Determinismo
# =====================================================================

@pytest.mark.asyncio
class TestDeterminismo:

    async def test_mesmo_conjunto_mesmo_resultado(self, conn):
        """Chamar a função duas vezes com o mesmo conjunto deve produzir
        exatamente o mesmo resultado (sem aleatoriedade)."""
        insumo_id = await _setup_insumo(conn)
        await _inserir_lote(conn, insumo_id, valor=10.0, dias_atras=20, quantidade=150)
        await _inserir_lote(conn, insumo_id, valor=15.0, dias_atras=10, quantidade=100)
        await _inserir_lote(conn, insumo_id, valor=25.0, dias_atras=2,  quantidade=200)

        r1 = await _estimar(conn, insumo_id)
        r2 = await _estimar(conn, insumo_id)

        assert r1["preco_estimado"] == r2["preco_estimado"]
        assert r1["num_compras"] == r2["num_compras"]


# =====================================================================
# P7 — Fornecedor mais barato
# =====================================================================

@pytest.mark.asyncio
class TestFornecedorMaisBarato:

    async def test_fornecedor_mais_barato_identificado(self, conn):
        """Fornecedor com menor preço médio no período deve ser retornado."""
        insumo_id = await _setup_insumo(conn)

        forn_barato = await conn.fetchval(
            "INSERT INTO fornecedores (nome, ativo) VALUES ('Forn Barato', TRUE) RETURNING id"
        )
        forn_caro = await conn.fetchval(
            "INSERT INTO fornecedores (nome, ativo) VALUES ('Forn Caro', TRUE) RETURNING id"
        )

        await _inserir_lote(conn, insumo_id, valor=8.0,  dias_atras=10, quantidade=100, fornecedor_id=forn_barato)
        await _inserir_lote(conn, insumo_id, valor=25.0, dias_atras=5,  quantidade=100, fornecedor_id=forn_caro)

        r = await _estimar(conn, insumo_id)

        assert r["fornecedor_mais_barato_id"] == forn_barato

    async def test_sem_fornecedor_retorna_null(self, conn):
        """Lotes sem fornecedor_id devem retornar NULL em fornecedor_mais_barato_id."""
        insumo_id = await _setup_insumo(conn)
        await _inserir_lote(conn, insumo_id, valor=10.0, dias_atras=10, quantidade=100)
        await _inserir_lote(conn, insumo_id, valor=20.0, dias_atras=5,  quantidade=100)

        r = await _estimar(conn, insumo_id)

        assert r["fornecedor_mais_barato_id"] is None


# =====================================================================
# P8 — Data da última compra
# =====================================================================

@pytest.mark.asyncio
class TestDataUltimaCompra:

    async def test_data_ultima_compra_correta(self, conn):
        """data_ultima_compra deve ser a data da compra mais recente no período."""
        from datetime import date, timedelta

        insumo_id = await _setup_insumo(conn)
        await _inserir_lote(conn, insumo_id, valor=10.0, dias_atras=30, quantidade=100)
        await _inserir_lote(conn, insumo_id, valor=15.0, dias_atras=5,  quantidade=100)
        await _inserir_lote(conn, insumo_id, valor=20.0, dias_atras=1,  quantidade=100)

        r = await _estimar(conn, insumo_id)

        data_esperada = date.today() - timedelta(days=1)
        assert r["data_ultima_compra"] == data_esperada
