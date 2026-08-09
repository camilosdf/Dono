# backend/tests/test_importar_cotacao_eml.py — Sistema Dono
#
# Testes para o endpoint POST /ia/importar-cotacao/eml.
#
# O endpoint aceita arquivos .eml (e-mail exportado manualmente), extrai
# anexos com formato suportado (PDF, XML, XLSX) e enfileira um job
# COTACAO_DOCUMENTO para cada um via pipeline já existente.
#
# Estratégia de mock:
#   - app.routes.ia.enfileirar_job_cotacao_documento → mock (sem Redis real)
#   - O parse do .eml é real (stdlib email) — testado sem mock
#
# Fixtures do conftest utilizadas:
#   client, token_admin, token_chef, token_compras
#
# PRÉ-REQUISITO: mesmos do conftest.py (banco dono_test com schema aplicado).

import io
import uuid
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import patch

import pytest

from tests.conftest import auth_headers, err


# =====================================================================
# Helpers — criação de .eml em memória
# =====================================================================

def _criar_eml(anexos: list[tuple[str, bytes, str]]) -> bytes:
    """Cria um .eml com os anexos especificados.

    Args:
        anexos: lista de (nome_arquivo, conteudo_bytes, mime_type)

    Returns:
        bytes do arquivo .eml pronto para upload.
    """
    msg = MIMEMultipart()
    msg["Subject"] = "Cotação de Fornecedor"
    msg["From"] = "fornecedor@teste.com"
    msg["To"] = "compras@dono.com"
    msg.attach(MIMEText("Segue cotação conforme solicitado.", "plain"))

    for nome, conteudo, mime_type in anexos:
        tipo_principal, subtipo = mime_type.split("/", 1)
        part = MIMEBase(tipo_principal, subtipo)
        part.set_payload(conteudo)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=nome)
        msg.attach(part)

    return msg.as_bytes()


def _eml_sem_anexos() -> bytes:
    """Cria um .eml apenas com corpo de texto, sem nenhum anexo."""
    msg = MIMEMultipart()
    msg["Subject"] = "Cotação sem anexo"
    msg["From"] = "fornecedor@teste.com"
    msg["To"] = "compras@dono.com"
    msg.attach(MIMEText("Esqueci de anexar.", "plain"))
    return msg.as_bytes()


def _eml_com_anexo_invalido() -> bytes:
    """Cria um .eml com anexo de formato não suportado (.docx)."""
    return _criar_eml([
        ("proposta.docx", b"PK fake docx content", "application/octet-stream"),
    ])


# =====================================================================
# Testes — POST /ia/importar-cotacao/eml
# =====================================================================

@pytest.mark.asyncio
class TestImportarCotacaoEml:
    """Testa o endpoint de importação de cotação via arquivo .eml."""

    async def test_eml_com_pdf_retorna_202_e_job(self, client, token_admin):
        """EML com anexo PDF deve enfileirar 1 job e retornar 202 com
        lista de jobs e resumo."""
        eml = _criar_eml([
            ("cotacao.pdf", b"%PDF-1.4 fake", "application/pdf"),
        ])
        files = {"arquivo": ("cotacao.eml", io.BytesIO(eml), "message/rfc822")}

        with patch("app.routes.ia.enfileirar_job_cotacao_documento") as mock_enf:
            mock_enf.return_value = uuid.uuid4()
            r = await client.post(
                "/ia/importar-cotacao/eml",
                headers=auth_headers(token_admin),
                files=files,
            )

        assert r.status_code == 202
        data = r.json()
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["formato"] == "pdf"
        assert data["jobs"][0]["status"] == "pendente"
        assert data["resumo"]["enfileirados"] == 1
        assert data["resumo"]["ignorados"] == 0
        assert mock_enf.call_count == 1

    async def test_eml_com_xml_retorna_202_e_job(self, client, token_admin):
        """EML com anexo XML deve enfileirar 1 job com formato xml."""
        eml = _criar_eml([
            ("cotacao.xml", b"<cotacao><item>Arroz</item></cotacao>", "application/xml"),
        ])
        files = {"arquivo": ("cotacao.eml", io.BytesIO(eml), "message/rfc822")}

        with patch("app.routes.ia.enfileirar_job_cotacao_documento") as mock_enf:
            mock_enf.return_value = uuid.uuid4()
            r = await client.post(
                "/ia/importar-cotacao/eml",
                headers=auth_headers(token_admin),
                files=files,
            )

        assert r.status_code == 202
        assert r.json()["jobs"][0]["formato"] == "xml"
        assert mock_enf.call_count == 1

    async def test_eml_com_xlsx_retorna_202_e_job(self, client, token_admin):
        """EML com anexo XLSX deve enfileirar 1 job com formato xlsx."""
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["Produto", "Preco"])
        ws.append(["Feijao", 5.0])
        buf = io.BytesIO()
        wb.save(buf)
        xlsx_bytes = buf.getvalue()

        eml = _criar_eml([
            ("cotacao.xlsx", xlsx_bytes,
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ])
        files = {"arquivo": ("cotacao.eml", io.BytesIO(eml), "message/rfc822")}

        with patch("app.routes.ia.enfileirar_job_cotacao_documento") as mock_enf:
            mock_enf.return_value = uuid.uuid4()
            r = await client.post(
                "/ia/importar-cotacao/eml",
                headers=auth_headers(token_admin),
                files=files,
            )

        assert r.status_code == 202
        assert r.json()["jobs"][0]["formato"] == "xlsx"
        assert mock_enf.call_count == 1

    async def test_eml_com_multiplos_anexos_enfileira_todos(self, client, token_admin):
        """EML com 3 anexos suportados deve enfileirar 3 jobs,
        um por anexo, com job_ids distintos."""
        eml = _criar_eml([
            ("cotacao_a.pdf", b"%PDF-1.4 a", "application/pdf"),
            ("cotacao_b.xml", b"<cotacao/>", "application/xml"),
            ("cotacao_c.pdf", b"%PDF-1.4 c", "application/pdf"),
        ])
        files = {"arquivo": ("multi.eml", io.BytesIO(eml), "message/rfc822")}

        job_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        with patch("app.routes.ia.enfileirar_job_cotacao_documento") as mock_enf:
            mock_enf.side_effect = job_ids
            r = await client.post(
                "/ia/importar-cotacao/eml",
                headers=auth_headers(token_admin),
                files=files,
            )

        assert r.status_code == 202
        data = r.json()
        assert len(data["jobs"]) == 3
        assert data["resumo"]["enfileirados"] == 3
        assert data["resumo"]["ignorados"] == 0
        assert mock_enf.call_count == 3

        # job_ids devem ser distintos
        ids = [j["job_id"] for j in data["jobs"]]
        assert len(set(ids)) == 3

    async def test_eml_com_mistura_suportados_e_ignorados(self, client, token_admin):
        """EML com 1 PDF suportado e 1 DOCX não suportado deve enfileirar
        1 job e listar o DOCX em anexos_ignorados."""
        eml = _criar_eml([
            ("cotacao.pdf", b"%PDF-1.4", "application/pdf"),
            ("proposta.docx", b"PK fake", "application/octet-stream"),
        ])
        files = {"arquivo": ("misto.eml", io.BytesIO(eml), "message/rfc822")}

        with patch("app.routes.ia.enfileirar_job_cotacao_documento") as mock_enf:
            mock_enf.return_value = uuid.uuid4()
            r = await client.post(
                "/ia/importar-cotacao/eml",
                headers=auth_headers(token_admin),
                files=files,
            )

        assert r.status_code == 202
        data = r.json()
        assert data["resumo"]["enfileirados"] == 1
        assert data["resumo"]["ignorados"] == 1
        assert any("proposta.docx" in ig for ig in data["anexos_ignorados"])

    async def test_eml_sem_anexos_retorna_400(self, client, token_admin):
        """EML sem nenhum anexo deve retornar 400 com código
        SEM_ANEXOS_SUPORTADOS."""
        eml = _eml_sem_anexos()
        files = {"arquivo": ("vazio.eml", io.BytesIO(eml), "message/rfc822")}

        r = await client.post(
            "/ia/importar-cotacao/eml",
            headers=auth_headers(token_admin),
            files=files,
        )

        assert r.status_code == 400
        assert err(r) == "SEM_ANEXOS_SUPORTADOS"

    async def test_eml_so_com_anexo_invalido_retorna_400(self, client, token_admin):
        """EML com apenas anexos de formato não suportado deve retornar 400."""
        eml = _eml_com_anexo_invalido()
        files = {"arquivo": ("invalido.eml", io.BytesIO(eml), "message/rfc822")}

        r = await client.post(
            "/ia/importar-cotacao/eml",
            headers=auth_headers(token_admin),
            files=files,
        )

        assert r.status_code == 400
        assert err(r) == "SEM_ANEXOS_SUPORTADOS"

    async def test_formato_nao_eml_retorna_400(self, client, token_admin):
        """Upload de arquivo que não é .eml deve retornar 400 com
        código FORMATO_INVALIDO."""
        files = {"arquivo": ("cotacao.pdf", io.BytesIO(b"%PDF"), "application/pdf")}
        r = await client.post(
            "/ia/importar-cotacao/eml",
            headers=auth_headers(token_admin),
            files=files,
        )
        assert r.status_code == 400
        assert err(r) == "FORMATO_INVALIDO"

    async def test_arquivo_grande_retorna_413(self, client, token_admin):
        """Arquivo .eml acima de 10MB deve retornar 413."""
        grande = b"x" * (10 * 1024 * 1024 + 1)
        files = {"arquivo": ("grande.eml", io.BytesIO(grande), "message/rfc822")}

        r = await client.post(
            "/ia/importar-cotacao/eml",
            headers=auth_headers(token_admin),
            files=files,
        )
        assert r.status_code == 413
        assert err(r) == "ARQUIVO_MUITO_GRANDE"

    async def test_sem_token_retorna_401(self, client):
        """Request sem autenticação deve retornar 401."""
        eml = _criar_eml([("c.pdf", b"%PDF", "application/pdf")])
        files = {"arquivo": ("c.eml", io.BytesIO(eml), "message/rfc822")}
        r = await client.post("/ia/importar-cotacao/eml", files=files)
        assert r.status_code == 401

    async def test_perfil_chef_negado(self, client, token_chef):
        """Perfil CHEF não tem permissão para importar cotações via .eml."""
        eml = _criar_eml([("c.pdf", b"%PDF", "application/pdf")])
        files = {"arquivo": ("c.eml", io.BytesIO(eml), "message/rfc822")}
        r = await client.post(
            "/ia/importar-cotacao/eml",
            headers=auth_headers(token_chef),
            files=files,
        )
        assert r.status_code == 403

    async def test_fornecedor_id_invalido_retorna_400(self, client, token_admin):
        """fornecedor_id inválido (não UUID) deve retornar 400."""
        eml = _criar_eml([("c.pdf", b"%PDF", "application/pdf")])
        files = {"arquivo": ("c.eml", io.BytesIO(eml), "message/rfc822")}
        r = await client.post(
            "/ia/importar-cotacao/eml?fornecedor_id=nao-uuid",
            headers=auth_headers(token_admin),
            files=files,
        )
        assert r.status_code == 400
        assert err(r) == "VALIDACAO_INVALIDA"

    async def test_fornecedor_id_repassado_para_todos_os_jobs(
        self, client, token_admin
    ):
        """fornecedor_id válido deve ser repassado para todos os jobs
        enfileirados, não apenas o primeiro."""
        forn_id = str(uuid.uuid4())
        eml = _criar_eml([
            ("a.pdf", b"%PDF", "application/pdf"),
            ("b.xml", b"<c/>", "application/xml"),
        ])
        files = {"arquivo": ("multi.eml", io.BytesIO(eml), "message/rfc822")}

        with patch("app.routes.ia.enfileirar_job_cotacao_documento") as mock_enf:
            mock_enf.side_effect = [uuid.uuid4(), uuid.uuid4()]
            r = await client.post(
                f"/ia/importar-cotacao/eml?fornecedor_id={forn_id}",
                headers=auth_headers(token_admin),
                files=files,
            )

        assert r.status_code == 202
        assert mock_enf.call_count == 2

        # Ambas as chamadas devem ter recebido o mesmo fornecedor_uuid
        for call in mock_enf.call_args_list:
            hint = call[0][3] if len(call[0]) > 3 else call[1].get("fornecedor_hint_id")
            assert str(hint) == forn_id

    async def test_nome_arquivo_preservado_no_resultado(self, client, token_admin):
        """O nome de cada anexo deve aparecer no resultado junto ao job_id,
        para que o usuário saiba qual job corresponde a qual documento."""
        eml = _criar_eml([
            ("cotacao_julho.pdf", b"%PDF", "application/pdf"),
        ])
        files = {"arquivo": ("email.eml", io.BytesIO(eml), "message/rfc822")}

        with patch("app.routes.ia.enfileirar_job_cotacao_documento") as mock_enf:
            mock_enf.return_value = uuid.uuid4()
            r = await client.post(
                "/ia/importar-cotacao/eml",
                headers=auth_headers(token_admin),
                files=files,
            )

        assert r.status_code == 202
        assert r.json()["jobs"][0]["anexo"] == "cotacao_julho.pdf"
