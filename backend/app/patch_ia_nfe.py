# Patch para backend/app/routes/ia.py
# Adicionar após as importações existentes e antes do router:
#
# from app.nfe_xml import parsear_xml_nfe, salvar_nfe_xml
#
# E adicionar os endpoints abaixo ao final do arquivo.

PATCH_IMPORT = '''from app.nfe_xml import parsear_xml_nfe, salvar_nfe_xml'''

PATCH_ENDPOINTS = '''

# =====================================================================
# NF-e XML — processamento direto de XML de Nota Fiscal Eletrônica
# =====================================================================

@router.post("/processar-nfe-xml", status_code=202)
async def processar_nfe_xml(
    arquivo: UploadFile = File(...),
    current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN"))
):
    """Processa um XML de NF-e (Nota Fiscal Eletrônica) diretamente.

    Diferente do endpoint /processar-nota (que usa OCR em PDF/imagem),
    este endpoint parseia o XML estruturado da NF-e — muito mais confiável
    para campos como CNPJ, valores e itens da nota.

    O processamento é **síncrono** para XML (diferente do OCR que é assíncrono)
    porque o parsing de XML é instantâneo — não há inferência de modelo.

    Args:
        arquivo: Arquivo .xml da NF-e (nfeProc ou NFe, namespace SEFAZ).

    Returns:
        Dados extraídos + fornecedor criado/encontrado + conta a pagar criada
        + lista de itens associados a insumos + itens pendentes de associação manual.
    """
    # Valida extensão
    nome = arquivo.filename or ""
    if not nome.lower().endswith(".xml"):
        raise HTTPException(
            status_code=400,
            detail=error_detail(
                "FORMATO_INVALIDO",
                "Este endpoint aceita apenas arquivos .xml de NF-e. "
                "Para PDF ou imagem, use POST /ia/processar-nota."
            )
        )

    # Lê o arquivo
    conteudo = await arquivo.read()
    if len(conteudo) > 5 * 1024 * 1024:  # 5MB — XML de NF-e nunca passa disso
        raise HTTPException(
            status_code=413,
            detail=error_detail("ARQUIVO_MUITO_GRANDE", "XML excede o limite de 5MB")
        )

    # Parseia o XML
    try:
        dados = parsear_xml_nfe(conteudo)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=error_detail("XML_NFE_INVALIDO", str(e))
        )
    except Exception as e:
        logger.exception("Erro ao parsear XML de NF-e")
        raise HTTPException(
            status_code=500,
            detail=error_detail("ERRO_INTERNO", f"Erro ao processar XML: {str(e)}")
        )

    # Salva no banco
    try:
        resultado = await salvar_nfe_xml(dados, uuid.UUID(current_user["user_id"]))
    except Exception as e:
        logger.exception("Erro ao salvar NF-e no banco")
        raise HTTPException(
            status_code=500,
            detail=error_detail("ERRO_INTERNO", f"Erro ao salvar dados da NF-e: {str(e)}")
        )

    return {
        "status": "processado",
        "fonte": "XML_NFE",
        **resultado
    }


@router.post("/validar-nfe-xml")
async def validar_nfe_xml(
    arquivo: UploadFile = File(...),
    current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN"))
):
    """Valida e extrai dados de um XML de NF-e sem salvar no banco.

    Útil para prévia antes de confirmar o processamento.
    Retorna os dados extraídos sem criar fornecedor, lotes ou contas a pagar.

    Args:
        arquivo: Arquivo .xml da NF-e.

    Returns:
        Dados extraídos: emitente, produtos, totais, identificação.
    """
    nome = arquivo.filename or ""
    if not nome.lower().endswith(".xml"):
        raise HTTPException(
            status_code=400,
            detail=error_detail("FORMATO_INVALIDO", "Aceita apenas arquivos .xml de NF-e")
        )

    conteudo = await arquivo.read()
    try:
        dados = parsear_xml_nfe(conteudo)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=error_detail("XML_NFE_INVALIDO", str(e))
        )

    # Remove dados internos desnecessários para a prévia
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
'''
