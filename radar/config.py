from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    db_path: Path
    transcript_api_key: str
    llm_provider: str
    llm_api_key: str
    llm_model: str
    sync_token: str
    poll_minutes: int
    process_after_hours: float
    daily_video_limit: int
    listen_host: str
    listen_port: int
    channels_file: Path = Path("channels.txt")

    @classmethod
    def from_env(cls) -> "Config":
        provider = os.getenv("LLM_PROVIDER", "openai").lower()
        if provider not in {"openai", "deepseek"}:
            raise ValueError("LLM_PROVIDER must be openai or deepseek")
        return cls(
            db_path=Path(os.getenv("RADAR_DB", "data/radar.sqlite3")).expanduser(),
            transcript_api_key=os.getenv("TRANSCRIPT_API_KEY", ""),
            llm_provider=provider,
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_model=os.getenv("LLM_MODEL", ""),
            sync_token=os.getenv("RADAR_SYNC_TOKEN", ""),
            poll_minutes=max(5, int(os.getenv("RADAR_POLL_MINUTES", "15"))),
            process_after_hours=max(0, float(os.getenv("RADAR_PROCESS_AFTER_HOURS", "3"))),
            daily_video_limit=max(1, int(os.getenv("RADAR_DAILY_VIDEO_LIMIT", "20"))),
            listen_host=os.getenv("RADAR_LISTEN_HOST", "127.0.0.1"),
            listen_port=int(os.getenv("RADAR_LISTEN_PORT", "8765")),
            channels_file=Path(os.getenv("RADAR_CHANNELS_FILE", "channels.txt")),
        )

    def require_worker_keys(self) -> None:
        missing = [name for name, value in (
            ("TRANSCRIPT_API_KEY", self.transcript_api_key),
            ("LLM_API_KEY", self.llm_api_key),
            ("LLM_MODEL", self.llm_model),
        ) if not value]
        if missing:
            raise ValueError("Missing worker configuration: " + ", ".join(missing))

