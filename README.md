# AccessFix

**AccessFix finds accessibility bugs on any website, writes the fixes, re-tests them with axe-core to prove they work, and retries the ones that fail.**

Built for the Nebius × NVIDIA Global AI Hackathon (Coding & Agentic Engineering track). The full README with the Nemotron, Token Factory, and Tavily sections is in progress.

## How it works

```
Tavily Map → axe-core scan → Nemotron Nano triage → Tavily Search → Nemotron Super fixes
     → axe-core verify (fresh page load) → retry failures with Super → Nemotron Ultra report
```

## Run locally (mock mode, no API keys)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env          # MOCK_LLM=1 and MOCK_TAVILY=1 by default
uvicorn app.main:app --port 8000
```

Open http://localhost:8000 and click **Try the demo site**, then **Scan & Fix**.

Run the tests with `pytest`.

## License

MIT (see `LICENSE`). Vendors [axe-core](https://github.com/dequelabs/axe-core) under MPL-2.0 (`app/vendor/AXE_LICENSE.txt`).
