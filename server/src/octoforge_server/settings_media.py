"""Vision and speech settings and core config conversion."""

from octoforge_core.config import VISION_DEEP_MAX_TOKENS, SpeechConfig, VisionConfig
from pydantic import BaseModel

DEFAULT_VISION_MODEL = "minimax-m3"
DEFAULT_VISION_DEEP_MODEL = "qwen3.5:397b"


class MediaSettings(BaseModel):
    vision_model: str = DEFAULT_VISION_MODEL
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_deep_model: str = DEFAULT_VISION_DEEP_MODEL
    stt_base_url: str = ""
    stt_api_key: str = ""
    stt_model: str = ""
    stt_language: str = ""

    def resolve_vision_base_url(self, fallback_base_url: str) -> str:
        return self.vision_base_url or fallback_base_url

    def vision_config(self, fallback_api_key: str) -> VisionConfig:
        return VisionConfig(
            api_key=self.vision_api_key or fallback_api_key,
            model=self.vision_model,
        )

    def vision_configured(self) -> bool:
        return bool(self.vision_model)

    def deep_vision_config(self, fallback_api_key: str) -> VisionConfig:
        return VisionConfig(
            api_key=self.vision_api_key or fallback_api_key,
            model=self.vision_deep_model,
            max_tokens=VISION_DEEP_MAX_TOKENS,
        )

    def deep_vision_configured(self) -> bool:
        return bool(self.vision_deep_model)

    def to_speech_config(self) -> SpeechConfig:
        return SpeechConfig(
            api_key=self.stt_api_key,
            model=self.stt_model,
            language=self.stt_language,
        )

    def speech_configured(self) -> bool:
        return bool(self.stt_model and self.stt_base_url)
