from __future__ import annotations

from collections.abc import Callable

from .cloud_transcriber import OpenAICloudTranscriber
from .config import Settings
from .correction import (
    ContextualReplacementRule,
    CorrectionPipeline,
    LocalContextualCorrector,
    OffCorrector,
    OllamaCorrector,
    OpenAIResponsesCorrector,
    UnavailableCorrector,
)
from .secrets import DpapiSecretStore


# Small, versioned, exact-only starter set.  These rules never use fuzzy
# matching and require an explicit same-utterance context cue.  Personal terms
# remain the responsibility of the existing confirmed dictionary.
_LOCAL_CONTEXTUAL_RULES = (
    ContextualReplacementRule(
        "гит хаб",
        "GitHub",
        ("репозиторий", "репозитории", "коммит", "пул-реквест"),
        language="ru",
    ),
    ContextualReplacementRule(
        "git hub",
        "GitHub",
        ("repository", "repositories", "commit", "pull request"),
        language="en",
    ),
    ContextualReplacementRule(
        "git hub",
        "GitHub",
        ("repositorio", "repositorios", "commit", "pull request"),
        language="es",
    ),
)


def build_correction_pipeline(
    settings: Settings,
    *,
    normalizer: Callable[[str], str] | None = None,
    secret_store: DpapiSecretStore | None = None,
) -> CorrectionPipeline:
    mode = settings.correction_mode
    if mode == "off":
        return CorrectionPipeline(OffCorrector())
    if mode == "local_basic":
        # Context is limited to this one utterance for now: no transcript is
        # retained by the provider before the app confirms insertion.
        return CorrectionPipeline(
            LocalContextualCorrector(
                _LOCAL_CONTEXTUAL_RULES,
                memory=None,
                normalizer=normalizer,
            )
        )
    if mode == "cloud_text":
        if not settings.cloud_text_consent or settings.cloud_provider != "openai":
            return CorrectionPipeline(UnavailableCorrector("cloud_text:no_consent"))
        try:
            store = secret_store or DpapiSecretStore()
            key = store.get("openai_api_key") or ""
            provider = OpenAIResponsesCorrector(
                api_key=key,
                model=settings.cloud_model,
                timeout=settings.correction_timeout_seconds,
            )
        except Exception:
            provider = UnavailableCorrector("cloud_text:not_configured")
        return CorrectionPipeline(provider)
    if mode in {"local_compact", "local_full"}:
        try:
            provider = OllamaCorrector(
                model=settings.local_model,
                timeout=max(settings.correction_timeout_seconds, 20.0),
                mode=mode,
            )
        except Exception:
            provider = UnavailableCorrector(f"{mode}:not_configured")
        return CorrectionPipeline(provider)
    return CorrectionPipeline(
        LocalContextualCorrector(
            _LOCAL_CONTEXTUAL_RULES,
            memory=None,
            normalizer=normalizer,
        )
    )


def build_cloud_transcriber(
    settings: Settings,
    *,
    secret_store: DpapiSecretStore | None = None,
) -> OpenAICloudTranscriber | None:
    if settings.transcription_mode != "cloud_transcription":
        return None
    if (
        not settings.cloud_transcription_consent
        or settings.cloud_provider != "openai"
    ):
        return None
    try:
        store = secret_store or DpapiSecretStore()
        key = store.get("openai_api_key") or ""
        return OpenAICloudTranscriber(
            api_key=key,
            model=settings.cloud_transcription_model,
            consent=True,
            timeout=settings.transcription_timeout_seconds,
        )
    except Exception:
        return None
