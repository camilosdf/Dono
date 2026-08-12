# backend/app/routes/ia.py — Sistema Dono
#
# Rotas de Inteligência Artificial:
#   - RAG: consulta com recuperação de documentos (pgvector + Ollama)
#   - OCR: processamento de notas fiscais (PDF/imagem) via ai_worker
#   - NF-e XML: parser direto de XML de Nota Fiscal Eletrônica (síncrono)
#   - Prospecção de pratos: match direto + sugestão criativa (stub LLM)
#   - Cotação online: estimativa estatística via fn_estimar_preco_insumo
#     (SQL determinístico) + explicação em linguagem natural via Ollama
#   - Importação de cotação: PDF/XML/XLSX/EML via pipeline assíncrono
#
# Princípio arquitetural (Opção B — aprovado):
#   SQL/PL/pgSQL calcula os números; Ollama apenas explica o resultado.
#   Nenhum número de preço é gerado pelo LLM.
#
# Histórico de patches:
#   H1: integração NF-e XML (síncrono, sem polling)
#   H2: importação de cotação por documento (PDF/XML/XLSX) + .eml
#   H3: fn_estimar_preco_insumo + Fase 2 _buscar_precos_externos
#   H4: correções status_job_cotacao (json.loads + autenticação) e
#       _buscar_precos_externos (import os local para OLLAMA_TIMEOUT)

import os
import uuid
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from pydantic import BaseModel

from app.database import get_pool
from app.dependencies import require_perfil
from app.errors import error_detail
from app.nfe_xml import parsear_xml_nfe, salvar_nfe_xml
from app.rag import (
    buscar_documentos_similares,
    consultar_llm,
    enfileirar_job_ocr,
    enfileirar_job_cotacao_documento,
)
from app.rate_limit import acquire_ia_slot, check_ia_rate_limit, release_ia_slot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ia")

# Configuração do Ollama (lida do ambiente uma única vez no import)
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")


# =====================================================================
# Modelos
# =====================================================================

class ConsultaRAGRequest(BaseModel):
    pergunta: str
    top_k: int = 5
    tipo_documento: Optional[str] = None
    entidade_id: Optional[str] = None


class ConsultaRAGResponse(BaseModel):
    resposta: str
    fontes: list[dict]


class ProspeccaoRequest(BaseModel):
    criterio: str = "INSUMOS_CRITICOS"   # ou "MANUAL"
    insumo_ids: list[str] | None = None
    estilo_menu: str | None = None
    dias_vencimento: int = 7


class JobOut(BaseModel):
    job_id: str
    status: str
    resultado: dict | list | None = None
    erro_motivo: str | None = None


class CotacaoOnlineRequest(BaseModel):
    insumo_ids: list[str]
    fornecedores_alvo: list[str] | None = None


# =====================================================================
# Helpers internos
# =====================================================================

async def _insumos_criticos(conn, dias: int) -> list[dict]:
    """Retorna insumos com lotes vencendo nos próximos N dias."""
    rows = await conn.fetch(
        """SELECT DISTINCT i.id AS insumo_id, i.nome
             FROM insumos i JOIN lotes_insumo l ON l.insumo_id = i.id
            WHERE l.quantidade_disponivel > 0
              AND l.data_validade IS NOT NULL
              AND l.data_validade <= CURRENT_DATE + $1::int
            ORDER BY i.nome""",
        dias,
    )
    return [dict(r) for r in rows]


async def _sugerir_pratos_criativos(
    insumos_criticos: list[dict], estilo_menu: str | None
) -> list[dict]:
    """Ponto de extensão para LLM. Sem provedor configurado, falha explicitamente."""
    raise NotImplementedError(
        "Nenhum provedor de LLM configurado neste ambiente para sugestão criativa"
    )


async def _buscar_precos_externos(
    insumo_ids: list[str], fornecedores_alvo: list[str] | None
) -> dict:
    """Estima preços via histórico de aquisição (fn_estimar_preco_insumo)
    e gera explicação em linguagem natural via Ollama local.

    Princípio arquitetural (Opção B — aprovado em H3):
      SQL/PL/pgSQL calcula (fn_estimar_preco_insumo); Ollama apenas explica.
      O LLM nunca gera números — apenas contextualiza o resultado determinístico.

    Fórmula de estimativa (implementada em fn_estimar_preco_insumo):
      w_rec_i = (janela - dias_desde_aquisicao) / SUM(janela - dias_j)
      w_vol_i = quantidade_i / SUM(quantidade_j)
      w_i     = SQRT(w_rec_i * w_vol_i)   -- média geométrica
      preco_estimado = SUM(w_i * valor_i) / SUM(w_i)

    Insumos sem histórico suficiente (<2 compras nos últimos 90 dias)
    são listados em sem_historico sem gerar cotacao.

    Args:
        insumo_ids: Lista de UUIDs dos insumos a estimar.
        fornecedores_alvo: Ignorado nesta implementação (reservado para
            futura filtragem por fornecedor preferencial).

    Returns:
        dict com estimativas, sem_historico, explicacao_ia e resumo.
    """
    import json as _json
    pool = get_pool()

    estimativas = []
    sem_historico = []

    async with pool.acquire() as conn:
        for insumo_id_str in insumo_ids:
            # Validar UUID antes de qualquer consulta
            try:
                insumo_uuid = uuid.UUID(insumo_id_str)
            except ValueError:
                sem_historico.append({
                    "insumo_id": insumo_id_str,
                    "motivo": "UUID inválido",
                })
                continue

            # Verificar se o insumo existe e está ativo
            insumo = await conn.fetchrow(
                "SELECT id, nome, unidade FROM insumos WHERE id = $1 AND ativo = TRUE",
                insumo_uuid,
            )
            if not insumo:
                sem_historico.append({
                    "insumo_id": insumo_id_str,
                    "motivo": "Insumo não encontrado",
                })
                continue

            # Chamar a função SQL determinística de estimativa
            # (mínimo 2 compras nos últimos 90 dias — parâmetros padrão)
            row = await conn.fetchrow(
                "SELECT * FROM fn_estimar_preco_insumo($1)",
                insumo_uuid,
            )

            if row["preco_estimado"] is None:
                # Histórico insuficiente — não gera cotacao
                sem_historico.append({
                    "insumo_id": insumo_id_str,
                    "nome": insumo["nome"],
                    "motivo": (
                        f"Histórico insuficiente ({row['num_compras']} compra(s) "
                        f"nos últimos 90 dias — mínimo: 2)"
                    ),
                })
                continue

            # Gravar cotação pendente de revisão humana
            # origem=IA_ONLINE distingue de cotações manuais e importadas
            fornecedor_hint = row["fornecedor_mais_barato_id"] or None
            cotacao_id = await conn.fetchval(
                """INSERT INTO cotacoes
                       (insumo_id, fornecedor_id, preco_unitario, origem, status)
                   VALUES ($1, $2, $3, 'IA_ONLINE', 'PENDENTE_REVISAO')
                   RETURNING id""",
                insumo_uuid,
                fornecedor_hint,
                row["preco_estimado"],
            )

            estimativas.append({
                "cotacao_id": str(cotacao_id),
                "insumo_id": insumo_id_str,
                "nome": insumo["nome"],
                "unidade": insumo["unidade"],
                "preco_estimado": float(row["preco_estimado"]),
                "preco_minimo": float(row["preco_minimo"]),
                "preco_maximo": float(row["preco_maximo"]),
                "num_compras": row["num_compras"],
                "fornecedor_mais_barato_id": (
                    str(row["fornecedor_mais_barato_id"])
                    if row["fornecedor_mais_barato_id"] else None
                ),
                "data_ultima_compra": (
                    row["data_ultima_compra"].isoformat()
                    if row["data_ultima_compra"] else None
                ),
            })

    # Gerar explicação via Ollama apenas quando houver estimativas
    # (sem estimativas, não há contexto para o LLM explicar)
    explicacao = None
    if estimativas:
        try:
            contexto = _json.dumps(estimativas, ensure_ascii=False, indent=2)
            prompt = f"""Você é um assistente de gestão de compras de um sistema de restaurante.
Com base nas estimativas de preço calculadas pelo sistema a partir do histórico real de compras,
gere uma explicação objetiva e útil para o gestor de compras, em português.

Dados calculados pelo sistema:
{contexto}

Instruções:
- Explique brevemente como cada preço foi estimado (média ponderada por recência e volume)
- Destaque insumos com maior variação entre mínimo e máximo (possível instabilidade de preço)
- Mencione o fornecedor mais barato quando disponível
- Seja conciso (máximo 3 parágrafos)
- NÃO invente preços ou datas — use apenas os dados fornecidos acima

Explicação:"""

            # PATCH H4: import local explícito para garantir que os.getenv
            # seja resolvido no escopo correto (evita NameError em testes)
            import os as _os
            _timeout = float(_os.getenv("OLLAMA_TIMEOUT", "120.0"))

            async with httpx.AsyncClient(timeout=_timeout) as client:
                resp = await client.post(
                    f"{OLLAMA_HOST}/api/generate",
                    json={
                        "model": OLLAMA_MODEL,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.2, "top_p": 0.9},
                    },
                )
                resp.raise_for_status()
                explicacao = resp.json().get("response", "").strip()

        except Exception as e:
            # Fallback gracioso: Ollama indisponível não impede o retorno
            # das estimativas calculadas pelo SQL
            logger.warning("Ollama indisponível para explicação de cotação: %s", str(e))
            explicacao = "Explicação indisponível (Ollama não respondeu)."

    return {
        "estimativas": estimativas,
        "sem_historico": sem_historico,
        "explicacao_ia": explicacao,
        "resumo": {
            "total_solicitados": len(insumo_ids),
            "com_estimativa": len(estimativas),
            "sem_historico": len(sem_historico),
        },
    }


# =====================================================================
# RAG — Consulta com recuperação de documentos
# =====================================================================

@router.post("/consultar", response_model=ConsultaRAGResponse)
async def consultar_rag(
    body: ConsultaRAGRequest,
    current_user: dict = Depends(require_perfil("CHEF", "GESTAO", "ADMIN")),
):
    """Consulta o assistente RAG (pgvector + Ollama).

    Busca documentos similares e gera resposta com base no contexto
    recuperado. Rate limit: 10 req/hora por usuário.
    """
    await check_ia_rate_limit(current_user["user_id"])

    entidade_uuid = uuid.UUID(body.entidade_id) if body.entidade_id else None

    try:
        documentos = await buscar_documentos_similares(
            pergunta=body.pergunta,
            top_k=body.top_k,
            tipo=body.tipo_documento,
            entidade_id=entidade_uuid,
        )
    except Exception as e:
        logger.exception("Erro ao buscar documentos similares")
        raise HTTPException(
            status_code=500,
            detail=error_detail("ERRO_INTERNO", f"Erro na busca de documentos: {e}"),
        )

    if not documentos:
        return ConsultaRAGResponse(
            resposta="Não encontrei informações relevantes nos documentos disponíveis.",
            fontes=[],
        )

    MAX_DOC_CHARS = int(os.getenv("RAG_MAX_DOC_CHARS", "2500"))
    MAX_CONTEXT_CHARS = int(os.getenv("RAG_MAX_CONTEXT_CHARS", "8000"))

    contexto = ""
    fontes_usadas = []
    for doc in documentos:
        bloco = f"Documento: {doc['titulo'] or 'Sem título'}\n{doc['conteudo'][:MAX_DOC_CHARS]}\n\n---\n"
        if len(contexto) + len(bloco) > MAX_CONTEXT_CHARS:
            break
        contexto += bloco
        fontes_usadas.append(doc)

    logger.info(
        "RAG: %d docs recuperados, %d usados no contexto, %d caracteres",
        len(documentos), len(fontes_usadas), len(contexto),
    )

    try:
        resposta = await consultar_llm(body.pergunta, contexto)
    except Exception as e:
        logger.exception("Erro ao consultar o LLM")
        raise HTTPException(
            status_code=503,
            detail=error_detail(
                "SERVICO_INDISPONIVEL",
                f"Erro ao consultar o LLM: {e}. Verifique se o Ollama está rodando.",
            ),
        )

    return ConsultaRAGResponse(
        resposta=resposta,
        fontes=[
            {
                "id": doc["id"],
                "titulo": doc["titulo"] or "Documento sem título",
                "similaridade": round(doc["similaridade"], 4),
            }
            for doc in fontes_usadas
        ],
    )


# =====================================================================
# OCR — Processamento de Notas Fiscais (PDF/imagem)
# =====================================================================

@router.post("/processar-nota", status_code=202)
async def processar_nota_fiscal(
    arquivo: UploadFile = File(...),
    current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN")),
):
    """Envia nota fiscal (PDF ou imagem) para extração via OCR.

    Processamento assíncrono — retorna job_id para polling.
    Para XML de NF-e, prefira POST /ia/processar-nfe-xml (síncrono, mais preciso).
    """
    await check_ia_rate_limit(current_user["user_id"])
    await acquire_ia_slot()
    try:
        conteudo = await arquivo.read()
        if len(conteudo) > 10 * 1024 * 1024:
            raise HTTPException(
                status_code=413,
                detail=error_detail("ARQUIVO_MUITO_GRANDE", "Arquivo excede 10MB"),
            )

        extensoes_validas = (".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp")
        nome = arquivo.filename or ""
        if not any(nome.lower().endswith(ext) for ext in extensoes_validas):
            raise HTTPException(
                status_code=400,
                detail=error_detail(
                    "FORMATO_INVALIDO",
                    f"Use: {', '.join(extensoes_validas)}. Para XML use /ia/processar-nfe-xml.",
                ),
            )

        try:
            job_id = await enfileirar_job_ocr(conteudo, uuid.UUID(current_user["user_id"]))
        except ValueError as e:
            raise HTTPException(
                status_code=422,
                detail=error_detail("DADOS_NAO_EXTRAIDOS", str(e)),
            )
        except Exception as e:
            logger.exception("Erro ao enfileirar job de OCR")
            raise HTTPException(
                status_code=500,
                detail=error_detail("ERRO_INTERNO", f"Erro ao enfileirar job: {e}"),
            )

        return {
            "job_id": str(job_id),
            "status": "pendente",
            "message": "Nota enviada para processamento. Consulte GET /ia/processar-nota/jobs/{job_id}",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao processar nota")
        raise HTTPException(
            status_code=500,
            detail=error_detail("ERRO_INTERNO", f"Erro ao processar nota: {e}"),
        )
    finally:
        await release_ia_slot()


@router.get("/processar-nota/jobs/{job_id}")
async def status_job_ocr(
    job_id: str,
    current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN")),
):
    """Consulta status de um job de OCR."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ia_jobs WHERE id = $1 AND tipo = 'OCR_NOTA'",
            uuid.UUID(job_id),
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail=error_detail("RECURSO_NAO_ENCONTRADO", "Job não encontrado"),
            )
        if row["solicitado_por"] != uuid.UUID(current_user["user_id"]):
            raise HTTPException(
                status_code=403,
                detail=error_detail("PERMISSAO_NEGADA", "Este job não pertence a você"),
            )
        return {
            "job_id": str(row["id"]),
            "status": row["status"],
            "resultado": row["resultado"],
            "erro_motivo": row["erro_motivo"],
            "criado_em": row["criado_em"],
            "concluido_em": row["concluido_em"],
        }


# =====================================================================
# Importação de Cotação — Documentos (PDF/XML/XLSX)
# =====================================================================

# Mapeamento extensão → formato para reutilização nos dois endpoints de cotação
_EXTENSAO_PARA_FORMATO_COTACAO = {".pdf": "pdf", ".xml": "xml", ".xlsx": "xlsx"}


@router.post("/importar-cotacao", status_code=202)
async def importar_cotacao_documento(
    arquivo: UploadFile = File(...),
    fornecedor_id: Optional[str] = None,
    current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN")),
):
    """Envia um documento de cotação (PDF, XML ou XLSX) para extração
    assistida por IA local.

    Diferente da NF-e (schema legal fixo), cotações não têm formato
    padronizado — a extração é feita por LLM local (Ollama) sobre o texto
    do documento. Itens associados a insumo e fornecedor existentes viram
    cotações pendentes de revisão (origem='IA_IMPORTADA'). Itens sem
    associação clara retornam como itens_pendentes, sem persistir nada.

    fornecedor_id (opcional): se o fornecedor já é conhecido, informe para
    não depender de o LLM identificá-lo corretamente no texto do documento.

    Processamento assíncrono — retorna job_id para polling em
    GET /ia/importar-cotacao/jobs/{job_id}.
    """
    await check_ia_rate_limit(current_user["user_id"])
    await acquire_ia_slot()
    try:
        nome = arquivo.filename or ""
        extensao = next(
            (ext for ext in _EXTENSAO_PARA_FORMATO_COTACAO if nome.lower().endswith(ext)),
            None,
        )
        if not extensao:
            raise HTTPException(
                status_code=400,
                detail=error_detail(
                    "FORMATO_INVALIDO",
                    f"Use um de: {', '.join(_EXTENSAO_PARA_FORMATO_COTACAO.keys())}",
                ),
            )
        formato = _EXTENSAO_PARA_FORMATO_COTACAO[extensao]

        conteudo = await arquivo.read()
        if len(conteudo) > 10 * 1024 * 1024:
            raise HTTPException(
                status_code=413,
                detail=error_detail("ARQUIVO_MUITO_GRANDE", "Arquivo excede 10MB"),
            )

        fornecedor_uuid = None
        if fornecedor_id:
            try:
                fornecedor_uuid = uuid.UUID(fornecedor_id)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=error_detail("VALIDACAO_INVALIDA", "fornecedor_id inválido"),
                )

        try:
            job_id = await enfileirar_job_cotacao_documento(
                conteudo, formato, uuid.UUID(current_user["user_id"]), fornecedor_uuid,
            )
        except Exception as e:
            logger.exception("Erro ao enfileirar job de importação de cotação")
            raise HTTPException(
                status_code=500,
                detail=error_detail("ERRO_INTERNO", f"Erro ao enfileirar job: {e}"),
            )

        return {
            "job_id": str(job_id),
            "status": "pendente",
            "message": "Documento enviado para processamento. Consulte GET /ia/importar-cotacao/jobs/{job_id}",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao importar cotação")
        raise HTTPException(
            status_code=500,
            detail=error_detail("ERRO_INTERNO", f"Erro ao importar cotação: {e}"),
        )
    finally:
        await release_ia_slot()


# =====================================================================
# Importação de Cotação — E-mail (.eml)
# =====================================================================

@router.post("/importar-cotacao/eml", status_code=202)
async def importar_cotacao_eml(
    arquivo: UploadFile = File(...),
    fornecedor_id: Optional[str] = None,
    current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN")),
):
    """Importa cotações a partir de um arquivo de e-mail (.eml).

    O usuário encaminha o e-mail do fornecedor para si mesmo, salva como
    .eml e faz upload aqui. O endpoint extrai os anexos (.pdf, .xml, .xlsx)
    e enfileira um job assíncrono para cada um, reutilizando o pipeline
    de importação de documento já existente.

    Anexos sem formato suportado são ignorados (listados em ignorados).
    E-mails sem nenhum anexo suportado retornam 400.

    fornecedor_id (opcional): repassado para todos os jobs enfileirados.

    Processamento assíncrono — retorna lista de job_ids para polling em
    GET /ia/importar-cotacao/jobs/{job_id}.
    """
    import email as _email_stdlib
    import email.policy

    await check_ia_rate_limit(current_user["user_id"])
    await acquire_ia_slot()
    try:
        nome = arquivo.filename or ""
        if not nome.lower().endswith(".eml"):
            raise HTTPException(
                status_code=400,
                detail=error_detail("FORMATO_INVALIDO", "Apenas arquivos .eml são aceitos neste endpoint"),
            )

        conteudo_eml = await arquivo.read()
        if len(conteudo_eml) > 10 * 1024 * 1024:
            raise HTTPException(
                status_code=413,
                detail=error_detail("ARQUIVO_MUITO_GRANDE", "Arquivo .eml excede 10MB"),
            )

        fornecedor_uuid = None
        if fornecedor_id:
            try:
                fornecedor_uuid = uuid.UUID(fornecedor_id)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=error_detail("VALIDACAO_INVALIDA", "fornecedor_id inválido"),
                )

        # Parsear o e-mail com a stdlib (sem dependência externa)
        msg = _email_stdlib.message_from_bytes(
            conteudo_eml,
            policy=email.policy.default,
        )

        jobs_enfileirados = []
        anexos_ignorados = []

        for parte in msg.walk():
            # Pular multipart containers e o corpo do e-mail
            if parte.get_content_maintype() == "multipart":
                continue
            if parte.get_content_disposition() not in ("attachment", "inline"):
                continue

            nome_anexo = parte.get_filename() or ""
            extensao = next(
                (ext for ext in _EXTENSAO_PARA_FORMATO_COTACAO
                 if nome_anexo.lower().endswith(ext)),
                None,
            )

            if not extensao:
                anexos_ignorados.append(nome_anexo or "(sem nome)")
                continue

            bytes_anexo = parte.get_payload(decode=True)
            if not bytes_anexo:
                anexos_ignorados.append(nome_anexo)
                continue

            formato = _EXTENSAO_PARA_FORMATO_COTACAO[extensao]
            try:
                job_id = await enfileirar_job_cotacao_documento(
                    bytes_anexo,
                    formato,
                    uuid.UUID(current_user["user_id"]),
                    fornecedor_uuid,
                )
                jobs_enfileirados.append({
                    "job_id": str(job_id),
                    "anexo": nome_anexo,
                    "formato": formato,
                    "status": "pendente",
                })
            except Exception as e:
                logger.exception("Erro ao enfileirar anexo %s do .eml", nome_anexo)
                anexos_ignorados.append(f"{nome_anexo} (erro: {e})")

        if not jobs_enfileirados:
            raise HTTPException(
                status_code=400,
                detail=error_detail(
                    "SEM_ANEXOS_SUPORTADOS",
                    f"Nenhum anexo suportado encontrado no .eml. "
                    f"Ignorados: {anexos_ignorados or ['nenhum anexo encontrado']}",
                ),
            )

        return {
            "jobs": jobs_enfileirados,
            "anexos_ignorados": anexos_ignorados,
            "resumo": {
                "total_anexos": len(jobs_enfileirados) + len(anexos_ignorados),
                "enfileirados": len(jobs_enfileirados),
                "ignorados": len(anexos_ignorados),
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao processar .eml")
        raise HTTPException(
            status_code=500,
            detail=error_detail("ERRO_INTERNO", f"Erro ao processar .eml: {e}"),
        )
    finally:
        await release_ia_slot()


@router.get("/importar-cotacao/jobs/{job_id}")
async def status_job_cotacao_documento(
    job_id: str,
    current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN")),
):
    """Consulta status de um job de importação de cotação."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ia_jobs WHERE id = $1 AND tipo = 'COTACAO_DOCUMENTO'",
            uuid.UUID(job_id),
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail=error_detail("RECURSO_NAO_ENCONTRADO", "Job não encontrado"),
            )
        if row["solicitado_por"] != uuid.UUID(current_user["user_id"]):
            raise HTTPException(
                status_code=403,
                detail=error_detail("PERMISSAO_NEGADA", "Este job não pertence a você"),
            )
        return {
            "job_id": str(row["id"]),
            "status": row["status"],
            "resultado": row["resultado"],
            "erro_motivo": row["erro_motivo"],
            "criado_em": row["criado_em"],
            "concluido_em": row["concluido_em"],
        }


# =====================================================================
# NF-e XML — Parser direto (síncrono, sem OCR, sem polling)
# =====================================================================

@router.post("/processar-nfe-xml", status_code=202)
async def processar_nfe_xml(
    arquivo: UploadFile = File(...),
    current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN")),
):
    """Processa XML de NF-e diretamente via ElementTree (sem OCR).

    Diferente de /processar-nota (OCR assíncrono), este endpoint parseia
    o XML estruturado da NF-e — campo a campo, namespace SEFAZ — e retorna
    o resultado imediatamente. Muito mais confiável para CNPJ, valores e
    itens do que OCR em PDF de DANFE.

    Fluxo:
      1. Parseia o XML (parsear_xml_nfe).
      2. Busca ou cria fornecedor pelo CNPJ do emitente.
      3. Tenta associar produtos a insumos existentes (EAN / nome similar).
      4. Cria conta a pagar com o valor total da NF-e.

    Retorna fornecedor, conta a pagar, itens associados e itens pendentes
    de associação manual.
    """
    nome = arquivo.filename or ""
    if not nome.lower().endswith(".xml"):
        raise HTTPException(
            status_code=400,
            detail=error_detail(
                "FORMATO_INVALIDO",
                "Este endpoint aceita apenas arquivos .xml de NF-e. "
                "Para PDF ou imagem, use POST /ia/processar-nota.",
            ),
        )

    conteudo = await arquivo.read()
    if len(conteudo) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=error_detail("ARQUIVO_MUITO_GRANDE", "XML excede o limite de 5MB"),
        )

    try:
        dados = parsear_xml_nfe(conteudo)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=error_detail("XML_NFE_INVALIDO", str(e)),
        )
    except Exception as e:
        logger.exception("Erro ao parsear XML de NF-e")
        raise HTTPException(
            status_code=500,
            detail=error_detail("ERRO_INTERNO", f"Erro ao processar XML: {e}"),
        )

    try:
        resultado = await salvar_nfe_xml(dados, uuid.UUID(current_user["user_id"]))
    except Exception as e:
        logger.exception("Erro ao salvar NF-e no banco")
        raise HTTPException(
            status_code=500,
            detail=error_detail("ERRO_INTERNO", f"Erro ao salvar dados da NF-e: {e}"),
        )

    return {"status": "processado", "fonte": "XML_NFE", **resultado}


@router.post("/validar-nfe-xml")
async def validar_nfe_xml(
    arquivo: UploadFile = File(...),
    current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN")),
):
    """Valida e extrai dados de XML de NF-e sem salvar no banco.

    Útil para prévia antes de confirmar o processamento.
    """
    nome = arquivo.filename or ""
    if not nome.lower().endswith(".xml"):
        raise HTTPException(
            status_code=400,
            detail=error_detail("FORMATO_INVALIDO", "Aceita apenas arquivos .xml de NF-e"),
        )

    conteudo = await arquivo.read()
    try:
        dados = parsear_xml_nfe(conteudo)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=error_detail("XML_NFE_INVALIDO", str(e)),
        )

    return {
        "valido": True,
        "identificacao": dados["identificacao"],
        "emitente": {
            "cnpj": dados["emitente"].get("cnpj"),
            "razao_social": dados["emitente"].get("razao_social"),
            "nome_fantasia": dados["emitente"].get("nome_fantasia"),
            "municipio": dados["emitente"].get("municipio"),
            "uf": dados["emitente"].get("uf"),
        },
        "total_produtos": len(dados["produtos"]),
        "produtos": dados["produtos"],
        "totais": dados["totais"],
    }


# =====================================================================
# Prospecção de Pratos
# =====================================================================

@router.get("/prospeccao-pratos/insumos-criticos")
async def insumos_criticos(
    dias: int = 7,
    _: dict = Depends(require_perfil("CHEF", "COMPRAS", "ADMIN")),
):
    """Lista insumos vencendo em até N dias — input para prospecção."""
    pool = get_pool()
    async with pool.acquire() as conn:
        return {"dias": dias, "insumos": await _insumos_criticos(conn, dias)}


@router.post("/prospeccao-pratos", status_code=202)
async def solicitar_prospeccao(
    body: ProspeccaoRequest,
    current_user: dict = Depends(require_perfil("CHEF", "ADMIN")),
):
    """Prospecta pratos executáveis com base no estoque atual.

    Match direto (pratos cadastrados que usam os insumos) é real.
    Sugestão criativa (LLM) retorna aviso enquanto não houver provedor.
    """
    await check_ia_rate_limit(current_user["user_id"])
    await acquire_ia_slot()
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            job = await conn.fetchrow(
                """INSERT INTO ia_jobs (tipo, solicitado_por, entrada)
                   VALUES ('PROSPECCAO_PRATOS', $1, $2) RETURNING id""",
                uuid.UUID(current_user["user_id"]),
                {
                    "criterio": body.criterio,
                    "insumo_ids": body.insumo_ids,
                    "estilo_menu": body.estilo_menu,
                    "dias_vencimento": body.dias_vencimento,
                },
            )

            if body.criterio == "MANUAL" and body.insumo_ids:
                ids_alvo = [uuid.UUID(i) for i in body.insumo_ids]
                alvo = await conn.fetch(
                    "SELECT id AS insumo_id, nome FROM insumos WHERE id = ANY($1::uuid[])",
                    ids_alvo,
                )
                alvo = [dict(r) for r in alvo]
            else:
                alvo = await _insumos_criticos(conn, body.dias_vencimento)

            ids_alvo = [uuid.UUID(str(a["insumo_id"])) for a in alvo]
            match_direto = []
            if ids_alvo:
                rows = await conn.fetch(
                    """SELECT DISTINCT p.id AS prato_id, p.nome, p.genero_prato
                         FROM pratos p JOIN itens_receita ir ON ir.prato_id = p.id
                        WHERE ir.insumo_id = ANY($1::uuid[]) AND p.status = 'ATIVO'""",
                    ids_alvo,
                )
                match_direto = [dict(r) for r in rows]

            resultado = {
                "insumos_considerados": alvo,
                "match_direto": match_direto,
                "sugestoes_criativas": [],
                "aviso": None,
            }
            try:
                resultado["sugestoes_criativas"] = await _sugerir_pratos_criativos(
                    alvo, body.estilo_menu
                )
            except NotImplementedError as e:
                resultado["aviso"] = str(e)

            import json as _json
            await conn.execute(
                "UPDATE ia_jobs SET status = 'concluido', resultado = $2, concluido_em = now() WHERE id = $1",
                job["id"],
                _json.dumps(resultado),
            )
            return {"job_id": str(job["id"])}
    finally:
        await release_ia_slot()


@router.get("/prospeccao-pratos/jobs/{job_id}", response_model=JobOut)
async def status_job_prospeccao(job_id: str):
    """Consulta status/resultado de um job de prospecção."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ia_jobs WHERE id = $1 AND tipo = 'PROSPECCAO_PRATOS'",
            uuid.UUID(job_id),
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail=error_detail("RECURSO_NAO_ENCONTRADO", "Job não encontrado"),
            )
        return JobOut(
            job_id=str(row["id"]),
            status=row["status"],
            resultado=row["resultado"],
            erro_motivo=row["erro_motivo"],
        )


# =====================================================================
# Cotação Online via IA (Opção B — estimativa estatística)
# =====================================================================

@router.post("/cotacoes/ia-online", status_code=202)
async def solicitar_cotacao_ia(
    body: CotacaoOnlineRequest,
    current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN")),
):
    """Solicita estimativa de preços via histórico de aquisição + Ollama.

    Para cada insumo_id informado:
      1. Chama fn_estimar_preco_insumo (SQL determinístico).
      2. Grava cotacao (origem=IA_ONLINE, status=PENDENTE_REVISAO) se
         houver histórico suficiente (>=2 compras nos últimos 90 dias).
      3. Insumos sem histórico listados em sem_historico.
      4. Ollama gera explicação textual do conjunto de estimativas.

    Resultado entra como Cotacao com status=PENDENTE_REVISAO —
    nunca aplicado automaticamente ao custo sem aprovação humana.
    """
    import json as _json
    await check_ia_rate_limit(current_user["user_id"])
    await acquire_ia_slot()
    try:
        pool = get_pool()

        # Criar o job com conexão mínima — fecha antes do processamento pesado
        # (chamada ao Ollama pode durar 30-120s; não deve reter conexão do pool)
        async with pool.acquire() as conn:
            job = await conn.fetchrow(
                """INSERT INTO ia_jobs (tipo, solicitado_por, entrada)
                   VALUES ('COTACAO_ONLINE', $1, $2) RETURNING id""",
                uuid.UUID(current_user["user_id"]),
                _json.dumps({
                    "insumo_ids": body.insumo_ids,
                    "fornecedores_alvo": body.fornecedores_alvo,
                }),
            )
        job_id = job["id"]

        # Processamento: abre novas conexões internamente via get_pool()
        try:
            resultado = await _buscar_precos_externos(
                body.insumo_ids, body.fornecedores_alvo
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE ia_jobs SET status = 'concluido', resultado = $2,
                       concluido_em = now() WHERE id = $1""",
                    job_id,
                    _json.dumps(resultado),
                )
        except Exception as e:
            logger.exception("Erro ao processar cotacao_ia job %s", job_id)
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE ia_jobs SET status = 'erro', erro_motivo = $2,
                       concluido_em = now() WHERE id = $1""",
                    job_id,
                    str(e),
                )

        return {"job_id": str(job_id)}
    finally:
        await release_ia_slot()


@router.get("/cotacoes/ia-online/jobs/{job_id}", response_model=JobOut)
async def status_job_cotacao(
    job_id: str,
    current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN")),
):
    """Consulta status de um job de cotação online.

    PATCH H4: adicionado require_perfil (antes o endpoint era público,
    retornando 404 para não autenticados em vez de 401).
    """
    import json as _json
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ia_jobs WHERE id = $1 AND tipo = 'COTACAO_ONLINE'",
            uuid.UUID(job_id),
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail=error_detail("RECURSO_NAO_ENCONTRADO", "Job não encontrado"),
            )

        # PATCH H4: resultado vem do banco como string JSON (asyncpg não
        # deserializa JSONB automaticamente em todos os contextos); fazer
        # json.loads() antes de passar para JobOut evita ValidationError.
        resultado = row["resultado"]
        if isinstance(resultado, str):
            try:
                resultado = _json.loads(resultado)
            except Exception:
                pass  # manter como string se não for JSON válido

        return JobOut(
            job_id=str(row["id"]),
            status=row["status"],
            resultado=resultado,
            erro_motivo=row["erro_motivo"],
        )
