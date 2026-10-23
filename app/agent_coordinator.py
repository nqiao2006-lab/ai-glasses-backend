"""Reserved coordination boundary. No actions are generated or executed here."""

from pydantic import BaseModel

from app.vision_service import VisionObservation


class CoordinationContext(BaseModel):
    # Model observations are claims, not independently verified ground truth.
    vision_observation: VisionObservation | None = None
    # Only explicitly supplied frontend transcription belongs here.
    explicit_user_intent: str | None = None

    def suggestions_only(self) -> list[str]:
        """No implicit authorization, even when the model suggests an action."""
        return self.vision_observation.suggested_assistance if self.vision_observation else []
