from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SQLite works out of the box; set to a Postgres URL in production / docker-compose.
    database_url: str = "sqlite:///./outreach.db"

    # "mock" runs everything locally with simulated carriers; "twilio" uses the real API.
    telephony_provider: str = "mock"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""

    # Seed demo data on first start so the dashboard has something to show.
    seed_demo_data: bool = True

    # --- Line health policy ---
    health_window_messages: int = 200    # score is computed over the last N outbound messages
    health_rest_threshold: float = 70.0  # below this a line is rested
    health_quarantine_threshold: float = 50.0  # below this a line is pulled from rotation
    rest_hours: int = 48
    default_daily_sms_cap: int = 150
    default_daily_call_cap: int = 100  # per line; heavy dialing is the fastest route to "Spam Likely"

    # --- Compliance (TCPA quiet hours, recipient local time) ---
    quiet_hours_start: int = 21  # no outreach at/after 9pm
    quiet_hours_end: int = 8     # no outreach before 8am
    max_touches_per_24h: int = 3  # texts + calls per phone number (FL, OK, MD limits)
    max_abandon_rate: float = 0.03  # FTC TSR, measured per campaign over 30 days

    # --- Email warm-up ---
    warmup_start_volume: int = 5
    warmup_target_volume: int = 40
    warmup_days: int = 28
    max_bounce_rate: float = 0.03
    max_complaint_rate: float = 0.001


settings = Settings()
