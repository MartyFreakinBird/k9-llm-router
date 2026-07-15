from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    # Temporal layer
    granularity_ms: int = 50           # 50 = micro/HFT, 60000 = structural/daily
    symbols: List[str] = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    # Graph decay parameters
    alpha: float = 0.3                 # Order flow derivative weight
    beta: float = 0.5                  # Crowding exponential coefficient
    gamma: float = 0.1                 # Crowding exponent scale
    crowding_decay_rate: float = 0.1   # VoC → crowding update factor

    # Algorithm thresholds
    voc_stampede_threshold: float = 100.0   # VoC > this = stochastic mode
    voc_stationary_threshold: float = 10.0  # VoC < this + high KL = ARA*
    kl_crowd_threshold: float = 0.2         # KL < this = Yen's anti-crowd
    kl_stationary_threshold: float = 0.5

    # ARA* config
    ara_epsilon_start: float = 3.0
    ara_epsilon_decay: float = 0.5

    # Execution
    max_position_usd: float = 10_000.0
    stop_loss_voc: float = 50.0        # Emergency exit if VoC spikes here

    # Infrastructure
    redis_url: str = "redis://localhost:6379"
    ws_feed_url: str = "wss://stream.binance.com:9443/ws"

    # History window for metrics
    path_history_size: int = 1000

    class Config:
        env_file = ".env"


settings = Settings()
