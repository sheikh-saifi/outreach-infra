from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SQLite works out of the box; set to a Postgres URL in production / docker-compose.
    database_url: str = "sqlite:///./outreach.db"

    # "mock" runs everything locally with simulated carriers; "twilio" uses the real API.
    telephony_provider: str = "mock"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""

    # "mock" simulates sending; "smtp" sends for real through the SMTP relay below.
    email_provider: str = "mock"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""

    # Used to sign unsubscribe links and to authenticate the inbound-email webhook.
    secret_key: str = "dev-secret-change-me"
    webhook_secret: str = ""
    public_base_url: str = "http://localhost:8000"
    # CAN-SPAM requires a valid physical postal address in every commercial email.
    company_name: str = "Acme Homes LLC"
    company_address: str = "1200 Main St, Suite 400, Dallas, TX 75201"

    # Background jobs (dispatcher, health sweeps, domain checks). Disabled in tests.
    enable_scheduler: bool = True
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

    # --- Pacing: bursts look like bots to carriers and mailbox providers ---
    send_jitter_minutes: int = 120   # spread scheduled sends randomly instead of all at 8:00:00
    sms_min_gap_seconds: int = 20    # per line, automated texts
    email_min_gap_minutes: int = 6   # per mailbox, campaign emails (max ~10/hour)

    # --- Email warm-up ---
    warmup_start_volume: int = 5
    warmup_target_volume: int = 40
    warmup_days: int = 28
    max_bounce_rate: float = 0.03
    max_complaint_rate: float = 0.001
    cold_start_day: int = 14          # no campaign email from a mailbox before warm-up day 14
    email_send_start: int = 8         # campaign email only 8am-6pm, Mon-Fri, recipient local time
    email_send_end: int = 18
    domain_max_bounce_rate: float = 0.05  # domain-wide (all mailboxes) 7-day limits


settings = Settings()
