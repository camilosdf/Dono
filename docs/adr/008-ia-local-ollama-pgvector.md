# ADR 008 — IA local via Ollama + PgVector, sem dependência de nuvem

**Status:** Aceito  
**Data:** 2026-07  
**Contexto:** Sistema Dono — módulo de Inteligência Artificial

---

## Contexto

O sistema precisa de dois recursos de IA: cotação online assistida e prospecção de pratos. Surgiu a oportunidade de adicionar RAG (documentos internos) e OCR de notas fiscais. A decisão é onde hospedar os modelos.

## Decisão

Toda inferência de IA usa modelos **locais** hospedados via Ollama, sem dependência de APIs de nuvem (OpenAI, Gemini, Anthropic). Embeddings para RAG são armazenados no próprio PostgreSQL via extensão **PgVector** — sem vector DB externo.

## Justificativa

Dados gastronômicos (fichas técnicas, custos, fornecedores, margens) são informação sensível de negócio. Enviar esses dados para APIs de nuvem cria dependência de terceiros, custos variáveis por token e risco de privacidade.

PgVector no mesmo Postgres elimina uma peça de infraestrutura: os embeddings ficam na mesma transação que os dados que referenciam, com backup e replicação automáticos junto com o resto do banco. Para o volume de documentos de um restaurante, PgVector é mais do que suficiente — Chroma ou Weaviate seriam overengineering.

Ollama permite trocar de modelo sem alterar código — o `app/rag.py` e `app/ai_worker.py` consultam o endpoint local e são agnósticos ao modelo específico.

## Consequências

- `app/rag.py` usa `sentence-transformers` para embeddings (lazy loading) e consulta Ollama via HTTP local.
- `app/ocr.py` usa Tesseract como motor primário (leve, sem GPU) e PaddleOCR como fallback (mais preciso para documentos estruturados). PaddleOCR tem fallback gracioso: se não inicializar (OOM, biblioteca ausente), retorna `None` e o sistema degrada para Tesseract apenas.
- `app/ai_worker.py` processa jobs de IA com `FOR UPDATE SKIP LOCKED` — mesma robustez do worker de outbox.
- Cotações de IA nunca são aplicadas automaticamente — entram com `status = PENDENTE_REVISAO` e exigem aprovação humana antes de afetar o custo.
- Rascunhos de prato gerados pela IA entram com `status = PENDENTE_APROVACAO` — o chef revisa antes de ativar.

## Alternativas rejeitadas

**APIs de nuvem (OpenAI/Gemini):** dependência externa, custo variável por token, risco de privacidade com dados de negócio. Rejeitado.

**Vector DB externo (Chroma/Weaviate):** infraestrutura adicional sem benefício para o volume atual. PgVector no Postgres já instalado é suficiente. Rejeitado.

**IA aplicada automaticamente sem revisão humana:** cotações afetam custo real; rascunhos de prato entram na produção. Humano no loop é obrigatório para decisões que mudam dados financeiros ou operacionais. Rejeitado.
