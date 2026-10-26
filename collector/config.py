from decimal import Decimal
from pathlib import Path
from .validation import check, strict_loads


def load_config(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError('CONFIG_LIMIT')
    config = strict_loads(path.read_bytes())
    check('config', config)
    if Decimal(config['per_run_limit_usd']) > Decimal(config['pilot_limit_usd']):
        raise ValueError('BUDGET_CONFIGURATION')
    return config
