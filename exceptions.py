"""
epistylion.exceptions
~~~~~~~~~~~~~~~~~~~~~

Eccezioni personalizzate per il sistema epistylion.

Classi disponibili
------------------
AuthenticationError
    Raised quando una richiesta LLM fallisce con errore 401 (Unauthorized).
    Contiene dettagli sull'errore originale e suggerimenti per la risoluzione.

RateLimitError
    Raised quando un provider LLM restituisce errore di rate limiting (429).

ModelNotFoundError
    Raised quando il modello specificato non esiste sul server LLM.
"""

from __future__ import annotations


class AuthenticationError(Exception):
    """
    Eccezione sollevata quando l'API LLM risponde con 401 Unauthorized.
    
    Questo può accadere per diversi motivi:
    - API key mancante o non valida
    - Token scaduto o revocato
    - Provider richiede autenticazione ma nessuna chiave è configurata
    
    Attributes
    ----------
    message : str
        Messaggio di errore descrittivo.
    provider : str | None
        Nome del provider (es. "openrouter", "inclusionai") se identificabile.
    suggestion : str
        Suggerimento per risolvere il problema.
    """
    
    def __init__(self, message: str, provider: str | None = None, suggestion: str | None = None):
        self.message = message
        self.provider = provider
        self.suggestion = suggestion or "Verifica la configurazione LLM_API_KEY nel file .env"
        
        # Costruisci messaggio completo
        full_message = f"[Authentication Error] {message}"
        if provider:
            full_message += f" (provider: {provider})"
        if self.suggestion:
            full_message += f"\nSuggerimento: {self.suggestion}"
        
        super().__init__(full_message)


class RateLimitError(Exception):
    """
    Eccezione sollevata quando il provider LLM applica rate limiting (429 Too Many Requests).
    
    Attributes
    ----------
    message : str
        Messaggio di errore.
    retry_after : int | None
        Tempo in secondi prima di riprovare (se fornito dal provider).
    """
    
    def __init__(self, message: str, retry_after: int | None = None):
        self.message = message
        self.retry_after = retry_after
        
        full_message = f"[Rate Limit Error] {message}"
        if retry_after:
            full_message += f" - Riprova dopo {retry_after} secondi"
        
        super().__init__(full_message)


class ModelNotFoundError(Exception):
    """
    Eccezione sollevata quando il modello specificato non esiste sul server LLM.
    
    Attributes
    ----------
    model : str
        Nome del modello richiesto.
    available_models : list[str] | None
        Lista dei modelli disponibili (se fornita dal provider).
    """
    
    def __init__(self, model: str, available_models: list[str] | None = None):
        self.model = model
        self.available_models = available_models
        
        message = f"Modello '{model}' non trovato sul server LLM"
        if available_models:
            message += f"\nModelli disponibili: {', '.join(available_models)}"
        
        super().__init__(message)
