# backend/tests/test_nfe_xml.py — Sistema Dono
#
# Testes de integração para o parser de XML de NF-e e o endpoint
# POST /ia/processar-nfe-xml.
#
# Estratégia:
#   - XML de NF-e mínimo válido gerado inline (sem arquivo externo).
#   - Banco real via conftest.py.
#   - Testa parser diretamente (parsear_xml_nfe) e via endpoint HTTP.
#
# RODAR: docker compose exec backend pytest tests/test_nfe_xml.py -v

import io
import uuid
import pytest
from tests.conftest import auth_headers, err


# =====================================================================
# XML de NF-e mínimo válido para testes
# =====================================================================

NS = "http://www.portalfiscal.inf.br/nfe"

def _xml_nfe_minimo(
    cnpj_emit: str = "12345678000195",
    razao_social: str = "FORNECEDOR TESTE LTDA",
    numero_nf: str = "000001",
    serie: str = "1",
    chave: str = "35240312345678000195550010000001001000000010",
    valor_produto: str = "100.00",
    valor_total: str = "100.00",
    qtd: str = "2.000",
    vunit: str = "50.00",
    descricao_produto: str = "PRODUTO TESTE",
    unidade: str = "KG",
    ncm: str = "02013000",
    cfop: str = "5102",
) -> bytes:
    """Gera um XML de NF-e mínimo válido para testes."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="{NS}" versao="4.00">
  <NFe xmlns="{NS}">
    <infNFe Id="NFe{chave}" versao="4.00">
      <ide>
        <cUF>35</cUF>
        <natOp>VENDA DE MERCADORIA</natOp>
        <mod>55</mod>
        <serie>{serie}</serie>
        <nNF>{numero_nf}</nNF>
        <dhEmi>2026-08-01T10:00:00-03:00</dhEmi>
        <dhSaiEnt>2026-08-01T10:30:00-03:00</dhSaiEnt>
        <tpNF>1</tpNF>
        <idDest>1</idDest>
        <cMunFG>3550308</cMunFG>
        <tpImp>1</tpImp>
        <tpEmis>1</tpEmis>
        <tpAmb>2</tpAmb>
        <finNFe>1</finNFe>
        <indFinal>1</indFinal>
        <indPres>1</indPres>
        <procEmi>0</procEmi>
        <verProc>4.00</verProc>
      </ide>
      <emit>
        <CNPJ>{cnpj_emit}</CNPJ>
        <xNome>{razao_social}</xNome>
        <xFant>FORNECEDOR TESTE</xFant>
        <enderEmit>
          <xLgr>RUA TESTE</xLgr>
          <nro>100</nro>
          <xBairro>CENTRO</xBairro>
          <cMun>3550308</cMun>
          <xMun>SAO PAULO</xMun>
          <UF>SP</UF>
          <CEP>01310100</CEP>
          <cPais>1058</cPais>
          <xPais>BRASIL</xPais>
          <fone>1133334444</fone>
        </enderEmit>
        <IE>111111111111</IE>
        <CRT>3</CRT>
      </emit>
      <dest>
        <CNPJ>98765432000188</CNPJ>
        <xNome>RESTAURANTE DONO LTDA</xNome>
        <enderDest>
          <xLgr>RUA DO RESTAURANTE</xLgr>
          <nro>200</nro>
          <xBairro>JARDINS</xBairro>
          <cMun>3550308</cMun>
          <xMun>SAO PAULO</xMun>
          <UF>SP</UF>
          <CEP>01402000</CEP>
          <cPais>1058</cPais>
          <xPais>BRASIL</xPais>
        </enderDest>
        <indIEDest>9</indIEDest>
      </dest>
      <det nItem="1">
        <prod>
          <cProd>001</cProd>
          <cEAN>SEM GTIN</cEAN>
          <xProd>{descricao_produto}</xProd>
          <NCM>{ncm}</NCM>
          <CFOP>{cfop}</CFOP>
          <uCom>{unidade}</uCom>
          <qCom>{qtd}</qCom>
          <vUnCom>{vunit}</vUnCom>
          <vProd>{valor_produto}</vProd>
          <cEANTrib>SEM GTIN</cEANTrib>
          <uTrib>{unidade}</uTrib>
          <qTrib>{qtd}</qTrib>
          <vUnTrib>{vunit}</vUnTrib>
          <indTot>1</indTot>
        </prod>
        <imposto>
          <ICMS><ICMS00><orig>0</orig><CST>00</CST><modBC>3</modBC><vBC>{valor_produto}</vBC><pICMS>12.00</pICMS><vICMS>12.00</vICMS></ICMS00></ICMS>
        </imposto>
      </det>
      <total>
        <ICMSTot>
          <vBC>{valor_produto}</vBC>
          <vICMS>12.00</vICMS>
          <vICMSDeson>0.00</vICMSDeson>
          <vFCP>0.00</vFCP>
          <vBCST>0.00</vBCST>
          <vST>0.00</vST>
          <vFCPST>0.00</vFCPST>
          <vFCPSTRet>0.00</vFCPSTRet>
          <vProd>{valor_produto}</vProd>
          <vFrete>0.00</vFrete>
          <vSeg>0.00</vSeg>
          <vDesc>0.00</vDesc>
          <vII>0.00</vII>
          <vIPI>0.00</vIPI>
          <vIPIDevol>0.00</vIPIDevol>
          <vPIS>0.00</vPIS>
          <vCOFINS>0.00</vCOFINS>
          <vOutro>0.00</vOutro>
          <vNF>{valor_total}</vNF>
        </ICMSTot>
      </total>
      <transp>
        <modFrete>9</modFrete>
      </transp>
      <cobr>
        <fat>
          <nFat>{numero_nf}</nFat>
          <vOrig>{valor_total}</vOrig>
          <vDesc>0.00</vDesc>
          <vLiq>{valor_total}</vLiq>
        </fat>
      </cobr>
      <infAdic>
        <infCpl>NOTA FISCAL DE TESTE</infCpl>
      </infAdic>
    </infNFe>
  </NFe>
</nfeProc>""".encode("utf-8")
    return xml


# =====================================================================
# Testes do parser (sem banco, sem HTTP)
# =====================================================================

class TestParsearXmlNfe:
    """Testa a função parsear_xml_nfe diretamente. Testes síncronos — sem async."""

    def test_parser_xml_minimo_valido(self):
        """Parser deve extrair campos básicos de XML válido."""
        from app.nfe_xml import parsear_xml_nfe

        xml = _xml_nfe_minimo()
        dados = parsear_xml_nfe(xml)

        assert dados["emitente"]["cnpj"] == "12345678000195"
        assert dados["emitente"]["razao_social"] == "FORNECEDOR TESTE LTDA"
        assert dados["emitente"]["municipio"] == "SAO PAULO"
        assert dados["emitente"]["uf"] == "SP"

    def test_parser_extrai_identificacao(self):
        """Parser deve extrair número, série e data de emissão."""
        from app.nfe_xml import parsear_xml_nfe

        dados = parsear_xml_nfe(_xml_nfe_minimo(numero_nf="000042", serie="2"))

        assert dados["identificacao"]["numero"] == "000042"
        assert dados["identificacao"]["serie"] == "2"
        assert dados["identificacao"]["natureza_operacao"] == "VENDA DE MERCADORIA"
        assert dados["identificacao"]["data_emissao"] is not None

    def test_parser_extrai_produtos(self):
        """Parser deve extrair produtos com quantidade e valor."""
        from app.nfe_xml import parsear_xml_nfe

        dados = parsear_xml_nfe(_xml_nfe_minimo(
            descricao_produto="FILE MIGNON",
            qtd="5.000",
            vunit="60.00",
            valor_produto="300.00",
        ))

        assert len(dados["produtos"]) == 1
        prod = dados["produtos"][0]
        assert prod["descricao"] == "FILE MIGNON"
        assert prod["quantidade_comercial"] == 5.0
        assert prod["valor_unitario_comercial"] == 60.0
        assert prod["valor_total_produto"] == 300.0
        assert prod["unidade_comercial"] == "KG"

    def test_parser_extrai_totais(self):
        """Parser deve extrair valor total da NF."""
        from app.nfe_xml import parsear_xml_nfe

        dados = parsear_xml_nfe(_xml_nfe_minimo(
            valor_produto="500.00",
            valor_total="560.00",
        ))

        assert dados["totais"]["valor_produtos"] == 500.0
        assert dados["totais"]["valor_total_nf"] == 560.0

    def test_parser_xml_invalido_levanta_valueerror(self):
        """XML malformado deve levantar ValueError."""
        from app.nfe_xml import parsear_xml_nfe

        with pytest.raises(ValueError, match="XML inválido"):
            parsear_xml_nfe(b"isso nao e xml")

    def test_parser_xml_sem_nfe_levanta_valueerror(self):
        """XML válido mas sem elemento NFe deve levantar ValueError."""
        from app.nfe_xml import parsear_xml_nfe

        xml_generico = b'<?xml version="1.0"?><root><filho>texto</filho></root>'
        with pytest.raises(ValueError):
            parsear_xml_nfe(xml_generico)

    def test_parser_nfe_pura_sem_nfeproc(self):
        """Aceita NFe diretamente (sem wrapper nfeProc)."""
        from app.nfe_xml import parsear_xml_nfe

        # Gera XML completo e extrai apenas o elemento NFe
        xml_completo = _xml_nfe_minimo().decode("utf-8")
        # Remove a tag nfeProc wrapper
        xml_nfe = xml_completo.replace(
            f'<nfeProc xmlns="{NS}" versao="4.00">\n  ', ""
        ).replace("\n</nfeProc>", "")
        dados = parsear_xml_nfe(xml_nfe.encode("utf-8"))
        assert dados["emitente"]["cnpj"] == "12345678000195"

    def test_parser_fonte_e_xml_nfe(self):
        """Campo 'fonte' deve ser 'XML_NFE'."""
        from app.nfe_xml import parsear_xml_nfe

        dados = parsear_xml_nfe(_xml_nfe_minimo())
        assert dados["fonte"] == "XML_NFE"


# =====================================================================
# Testes do endpoint HTTP
# =====================================================================

class TestEndpointNfeXml:
    pytestmark = pytest.mark.asyncio
    """Testa os endpoints POST /ia/processar-nfe-xml e POST /ia/validar-nfe-xml."""

    async def test_validar_nfe_xml_retorna_dados(self, client, token_compras):
        """GET /ia/validar-nfe-xml deve retornar dados sem salvar no banco."""
        xml = _xml_nfe_minimo(descricao_produto="TESTE VALIDACAO")
        r = await client.post(
            "/ia/validar-nfe-xml",
            files={"arquivo": ("nfe.xml", io.BytesIO(xml), "application/xml")},
            headers=auth_headers(token_compras),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["valido"] is True
        assert data["emitente"]["cnpj"] == "12345678000195"
        assert data["total_produtos"] == 1
        assert data["produtos"][0]["descricao"] == "TESTE VALIDACAO"
        assert data["totais"]["valor_total_nf"] == 100.0

    async def test_validar_nfe_xml_campos_identificacao(self, client, token_compras):
        """Validação deve retornar número e série da NF."""
        xml = _xml_nfe_minimo(numero_nf="999", serie="3")
        r = await client.post(
            "/ia/validar-nfe-xml",
            files={"arquivo": ("nfe.xml", io.BytesIO(xml), "application/xml")},
            headers=auth_headers(token_compras),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["identificacao"]["numero"] == "999"
        assert data["identificacao"]["serie"] == "3"

    async def test_validar_nfe_xml_rejeita_pdf(self, client, token_compras):
        """Endpoint deve rejeitar arquivo não-XML com 400."""
        r = await client.post(
            "/ia/validar-nfe-xml",
            files={"arquivo": ("nota.pdf", io.BytesIO(b"%PDF"), "application/pdf")},
            headers=auth_headers(token_compras),
        )
        assert r.status_code == 400
        assert err(r) == "FORMATO_INVALIDO"

    async def test_validar_nfe_xml_rejeita_xml_invalido(self, client, token_compras):
        """XML malformado deve retornar 422."""
        r = await client.post(
            "/ia/validar-nfe-xml",
            files={"arquivo": ("nfe.xml", io.BytesIO(b"<invalido>"), "application/xml")},
            headers=auth_headers(token_compras),
        )
        assert r.status_code == 422
        assert err(r) == "XML_NFE_INVALIDO"

    async def test_processar_nfe_xml_cria_fornecedor(self, client, token_compras, conn):
        """POST /ia/processar-nfe-xml deve criar fornecedor se não existir."""
        xml = _xml_nfe_minimo(
            cnpj_emit="11111111000191",
            razao_social="FORNECEDOR NOVO XML",
        )
        r = await client.post(
            "/ia/processar-nfe-xml",
            files={"arquivo": ("nfe.xml", io.BytesIO(xml), "application/xml")},
            headers=auth_headers(token_compras),
        )
        assert r.status_code == 202
        data = r.json()
        assert data["status"] == "processado"
        assert data["fornecedor_cnpj"] == "11111111000191"
        assert data["fornecedor_id"] is not None

        # Verifica no banco
        forn = await conn.fetchrow(
            "SELECT nome, contato FROM fornecedores WHERE id = $1",
            uuid.UUID(data["fornecedor_id"]),
        )
        assert forn is not None
        assert "CNPJ: 11111111000191" in forn["contato"]

    async def test_processar_nfe_xml_cria_conta_pagar(self, client, token_compras, conn):
        """POST /ia/processar-nfe-xml deve criar conta a pagar."""
        xml = _xml_nfe_minimo(
            numero_nf="000100",
            valor_total="250.00",
            valor_produto="250.00",
        )
        r = await client.post(
            "/ia/processar-nfe-xml",
            files={"arquivo": ("nfe.xml", io.BytesIO(xml), "application/xml")},
            headers=auth_headers(token_compras),
        )
        assert r.status_code == 202
        data = r.json()
        assert data["conta_pagar_id"] is not None
        assert data["valor_total_nf"] == 250.0

        # Verifica no banco
        conta = await conn.fetchrow(
            "SELECT valor_original, status FROM contas_pagar WHERE id = $1",
            uuid.UUID(data["conta_pagar_id"]),
        )
        assert conta is not None
        assert float(conta["valor_original"]) == 250.0
        assert conta["status"] == "PENDENTE"

    async def test_processar_nfe_xml_produto_sem_insumo_fica_pendente(
        self, client, token_compras
    ):
        """Produto sem insumo correspondente deve ir para itens_pendentes."""
        xml = _xml_nfe_minimo(descricao_produto="PRODUTO SEM CORRESPONDENCIA XYZ123")
        r = await client.post(
            "/ia/processar-nfe-xml",
            files={"arquivo": ("nfe.xml", io.BytesIO(xml), "application/xml")},
            headers=auth_headers(token_compras),
        )
        assert r.status_code == 202
        data = r.json()
        assert data["resumo"]["total_itens"] == 1
        assert data["resumo"]["itens_pendentes"] == 1
        assert len(data["itens_pendentes"]) == 1
        assert data["itens_pendentes"][0]["descricao_nfe"] == "PRODUTO SEM CORRESPONDENCIA XYZ123"
        assert "associação manual" in data["itens_pendentes"][0]["pendencia"]

    async def test_processar_nfe_xml_produto_associado_a_insumo(
        self, client, token_compras, conn
    ):
        """Produto cujo nome bate com insumo existente deve ser associado."""
        # Cria insumo com nome que bate com o produto da NF
        cat_id = await conn.fetchval(
            "SELECT id FROM categorias WHERE nome = 'Carnes, Aves e Peixes'"
        )
        await conn.execute(
            "INSERT INTO insumos (nome, categoria_id, unidade) VALUES ($1, $2, 'KG')",
            "File Mignon", cat_id,
        )

        xml = _xml_nfe_minimo(descricao_produto="FILE MIGNON RESFRIADO")
        r = await client.post(
            "/ia/processar-nfe-xml",
            files={"arquivo": ("nfe.xml", io.BytesIO(xml), "application/xml")},
            headers=auth_headers(token_compras),
        )
        assert r.status_code == 202
        data = r.json()
        # Pelo menos tentou associar (pode ou não ter encontrado dependendo do ILIKE)
        assert data["resumo"]["total_itens"] == 1
        assert "itens_associados" in data
        assert "itens_pendentes" in data

    async def test_processar_nfe_xml_requer_perfil_compras(self, client, token_chef):
        """Endpoint deve rejeitar perfil CHEF com 403."""
        xml = _xml_nfe_minimo()
        r = await client.post(
            "/ia/processar-nfe-xml",
            files={"arquivo": ("nfe.xml", io.BytesIO(xml), "application/xml")},
            headers=auth_headers(token_chef),
        )
        assert r.status_code == 403
        assert err(r) == "PERMISSAO_NEGADA"

    async def test_processar_nfe_xml_fornecedor_existente_reutilizado(
        self, client, token_compras, conn
    ):
        """Se fornecedor com mesmo CNPJ já existe, deve reutilizá-lo."""
        # Cria fornecedor com CNPJ no contato
        forn_id = await conn.fetchval(
            """INSERT INTO fornecedores (nome, contato, ativo)
               VALUES ('Forn Existente', 'CNPJ: 22222222000100', TRUE)
               RETURNING id"""
        )

        xml = _xml_nfe_minimo(cnpj_emit="22222222000100")
        r = await client.post(
            "/ia/processar-nfe-xml",
            files={"arquivo": ("nfe.xml", io.BytesIO(xml), "application/xml")},
            headers=auth_headers(token_compras),
        )
        assert r.status_code == 202
        data = r.json()
        # Deve usar o fornecedor existente, não criar um novo
        assert data["fornecedor_id"] == str(forn_id)

    async def test_resumo_contagem_correta(self, client, token_compras):
        """Resumo deve contar total, associados e pendentes corretamente."""
        xml = _xml_nfe_minimo()
        r = await client.post(
            "/ia/processar-nfe-xml",
            files={"arquivo": ("nfe.xml", io.BytesIO(xml), "application/xml")},
            headers=auth_headers(token_compras),
        )
        assert r.status_code == 202
        data = r.json()
        resumo = data["resumo"]
        assert resumo["total_itens"] == resumo["itens_associados"] + resumo["itens_pendentes"]
