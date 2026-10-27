"""Create an isolated JiuwenSwarm home configured for SwarmFlow (never touches ~/.jiuwenswarm)."""
import os, re, subprocess
from pathlib import Path
from ruamel.yaml import YAML


def make_home(home: Path, provider: str, model: str, venv_bin: Path) -> None:  # provider is always OpenAI
    home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "API_KEY")}  # init never sees the key
    env["JIUWENSWARM_HOME"] = str(home)  # data lands in <home>/.jiuwenswarm
    subprocess.run([str(venv_bin / "jiuwenswarm-init")], input="en\n", text=True, env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cfgdir = home / ".jiuwenswarm" / "config"
    y = YAML()
    y.preserve_quotes = True
    cfg = y.load((cfgdir / "config.yaml").read_text())
    cfg["preferred_language"] = "en"              # init defaults to zh when it cannot prompt
    cfg["setup_guide"]["enabled"] = False         # first-run onboarding distracts the Leader
    team = cfg["modes"]["team"]["jiuwen_team"]
    team["enable_swarmflow"] = True               # SwarmFlow is off by default
    team["swarmflow_budget"] = 3_000_000          # default team ceiling (chat.send may override per session)
    with open(cfgdir / "config.yaml", "w") as f:
        y.dump(cfg, f)
    envf = cfgdir / ".env"
    keep = [l for l in envf.read_text().splitlines()
            if not re.match(r"^(API_KEY|API_BASE|MODEL_NAME|MODEL_PROVIDER|ENDPOINT_PROFILE)=", l)]
    # No API_KEY line: the key is passed only in the server's process environment.
    keep += ["MODEL_PROVIDER=OpenAI", "ENDPOINT_PROFILE=openai", "API_BASE=https://api.openai.com/v1", f"MODEL_NAME={model}"]
    envf.write_text("\n".join(keep) + "\n")
