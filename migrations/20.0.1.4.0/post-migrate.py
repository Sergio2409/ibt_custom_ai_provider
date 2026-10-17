def migrate(cr, version):
    # Models never discovered (created by hand) had the flags off only because
    # they were read-only: turn them on so existing setups keep working.
    # Discovered Ollama models keep their flags; Ollama cannot read PDF files.
    cr.execute("""
        UPDATE ibt_ai_provider_model
           SET supports_tools = capabilities IS NULL OR supports_tools,
               supports_vision = capabilities IS NULL OR supports_vision,
               supports_pdf = capabilities IS NULL
    """)
