# Supply Chain Email Agent

A Python service that reads supplier emails, extracts lead-time commitments
("6–8 weeks ARO", "20 pcs in 5 business days, remainder in 25–30"), and writes
them to an ERP system of record through an **MCP server**.

- **`POST /emails`**: runs an email through the agent and returns a stable
  `message_id` plus the extracted commitments, with evidence quotes and a review status.
- **`GET /lead-times`**: returns the current lead time for a PO number, optionally narrowed
  to one part number.
- **`/mcp`**: the mock ERP's MCP server (streamable HTTP). The agent uses it to read PO
  context and write lead times; any other MCP client can use it too.

Under the hood, a PydanticAI agent (Claude by default, or OpenAI) reads the email and can
look things up in the ERP through read-only MCP tools. Code verifies everything the agent
proposes, and only code writes to the ERP.

This README covers setup and running. [NOTES.md](NOTES.md) covers the design: how the
pipeline works end to end, what the API returns and what is written to the ERP, the
prompt and why it's built that way, and the assumptions and limitations.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.11+ for you if needed), or
  Docker with Compose v2 (see [Run with Docker](#run-with-docker))
- An API key for **Anthropic** (default) or **OpenAI**. You only need it for `POST /emails`;
  the tests and everything else run without one.

## Setup

```bash
make install
```

This runs `uv sync --extra dev`: it creates `.venv/` and installs the app plus the dev tools.

```bash
cp .env.example .env
```

Then open `.env` and set your API key:

```dotenv
PYTHONPATH=/path/to/email-agent/src
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

```bash
make seed
```

This creates `data/app.db` and loads the mock ERP data (see [Seed data](#seed-data)).

## Quick start

```bash
make run
```

This starts the service on <http://localhost:8000> with auto-reload, for development.
Interactive API docs are at <http://localhost:8000/docs>. To run it as a long-lived
service, see [Run as a standalone service](#run-as-a-standalone-service).

### Process an email

Send one of the example emails in `tests/fixtures/emails/`. The request body is JSON:
`{"email": "<raw email text>"}`.

```bash
curl -s -X POST http://localhost:8000/emails \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-001' \
  -d '{"email": "From: NorthBay Fasteners <quotes@northbayfasteners.com>\nTo: buyer@joby.aero\nSubject: PO 4500999911 - partial delivery plan\n\nHello,\n\nWe can commit to the following:\n- PN NB-4402: 20 pcs available to ship in 5 business days.\n- Remaining 80 pcs will ship in 25-30 business days after receipt of order.\n\nPlease confirm if partial shipment is acceptable.\n\nRegards,\nNorthBay Fasteners"}' \
  | python3 -m json.tool
```

The response looks like this:

```json
{
    "message_id": "msg_8717f37cdb0b87276dd5b29f",
    "status": "needs_review",
    "reference_date": "2026-11-10",
    "po_numbers_found": [
        "4500999911"
    ],
    "part_numbers_found": [
        "NB-4402"
    ],
    "flags": [],
    "commitments": [
        {
            "po_number": "4500999911",
            "part_number": "NB-4402",
            "line_number": null,
            "written": false,
            "review_status": "needs_review",
            "is_option": false,
            "confidence": 0.85,
            "details": {
                "kind": "duration",
                "quantity": 20,
                "min_days": 5,
                "max_days": 5,
                "day_basis": "business",
                "anchor": "email_date",
                "promised_date_min": "2026-11-17",
                "promised_date_max": "2026-11-17",
                "conditions": [
                    "buyer must confirm partial shipment"
                ],
                "assumptions": [
                    "PO 4500999911 was not found in the ERP"
                ],
                "raw_text": "PN NB-4402: 20 pcs available to ship in 5 business days.",
                "evidence": [
                    "PN NB-4402: 20 pcs available to ship in 5 business days.",
                    "Please confirm if partial shipment is acceptable."
                ]
            },
            "issues": [
                "PO 4500999911 not found in ERP",
                "Condition: buyer must confirm partial shipment",
                "Assumption: PO 4500999911 was not found in the ERP"
            ]
        },
        {
            "po_number": "4500999911",
            "part_number": "NB-4402",
            "line_number": null,
            "written": false,
            "review_status": "needs_review",
            "is_option": false,
            "confidence": 0.8,
            "details": {
                "kind": "duration",
                "quantity": 80,
                "min_days": 25,
                "max_days": 30,
                "day_basis": "business",
                "anchor": "ARO",
                "promised_date_min": null,
                "promised_date_max": null,
                "conditions": [
                    "buyer must confirm partial shipment"
                ],
                "assumptions": [
                    "Assumed the remaining 80 pcs are part NB-4402 because the bullet does not name the part",
                    "PO 4500999911 was not found in the ERP",
                    "Order date unknown; promised date not computed"
                ],
                "raw_text": "Remaining 80 pcs will ship in 25-30 business days after receipt of order.",
                "evidence": [
                    "Remaining 80 pcs will ship in 25-30 business days after receipt of order."
                ]
            },
            "issues": [
                "PO 4500999911 not found in ERP",
                "Order date unknown; promised date not computed",
                "Condition: buyer must confirm partial shipment",
                "Assumption: Assumed the remaining 80 pcs are part NB-4402 because the bullet does not name the part",
                "Assumption: PO 4500999911 was not found in the ERP"
            ]
        }
    ],
    "issues": [
        "PO 4500999911 is referenced but not found in the ERP"
    ],
    "usage": {
        "model": "claude-opus-5-5",
        "duration_ms": 39878,
        "requests": 2,
        "tool_calls": 1,
        "input_tokens": 7575,
        "output_tokens": 850
    }
}
```

The response also has a `flags` list: fraud and spoofing signals such as a lookalike
sender domain, a `Reply-To` redirect or new bank details. A `high` flag blocks every write
from the email; a `medium` flag forces review. See NOTES.md, section 3a.

Email-level `status` values:

| Status | Meaning |
|---|---|
| `processed` | Commitments were written to the ERP, and nothing needs a human |
| `needs_review` | Something needs a buyer's attention: no matching PO line, a condition, an assumption, low confidence, a fraud flag |
| `no_signal` | The email has no lead-time commitment and raised no fraud flag. This is not a failure. |
| `error` | Processing failed, for example because the LLM was unavailable. Returned with HTTP 502. Safe to resubmit. |

Submitting the same email again returns the stored result without calling the LLM again.

### Read a lead time

```bash
curl -s "localhost:8000/lead-times?po_number=4500123456&part_number=JT-8831" | python3 -m json.tool
```

The response has one record per matching PO line. An empty `lead_times` list means the
line exists but no commitment has been recorded yet.

`part_number` is optional. Leave it out to get every line of the PO:

```bash
curl -s "localhost:8000/lead-times?po_number=4500123456" | python3 -m json.tool
```

If the PO doesn't exist, or has no line for the given part, you get a `404` with the reason.

### Use the ERP MCP server

The MCP endpoint is `http://localhost:8000/mcp` (streamable HTTP). It has four tools:

| Tool | What it does |
|---|---|
| `get_purchase_order` | A PO with its supplier, order date and lines |
| `find_po_lines` | Find PO lines by PO number and/or part number |
| `get_lead_time` | Current lead-time commitments for a PO, one record per line (optionally one part) |
| `write_lead_times` | Record an email's commitments for one existing PO line (idempotent per message) |


## Run as a standalone service

`make run` is for development: one process, auto-reload, localhost only. To run the
service as a long-lived process on a server or VM, use these steps instead. Run every
command from the project root, because `.env` and the default `data/app.db` path are
resolved relative to the working directory.

**1. Install the locked runtime dependencies** (no dev tools):

```bash
uv sync --frozen
```

`--frozen` installs exactly what `uv.lock` pins, without re-resolving versions. On a
development checkout this removes pytest and ruff from `.venv`; `make install` brings
them back.

**2. Configure** with `.env`, as in [Setup](#setup). Real environment variables also work
and take precedence over `.env`. Prefer `.env`, though: an `export` in one terminal
silently changes what every later command in *that* terminal uses, but not the others.

By default the database is `data/app.db`, relative to the folder you start from. To keep
it somewhere else, set `DATABASE_URL` in `.env` to an absolute path, for example
`DATABASE_URL=sqlite:////srv/sc-agent/app.db`. Note the four slashes. The folder must be
writable by the user running the service. The `Service started` log line shows which
database is in use.

**3. Create and seed the database** once, before starting the workers:

```bash
uv run --frozen python -m src.database.seed
```

**4. Start the service:**

```bash
uv run --frozen uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```

`make serve` runs the same command; you can override `HOST`, `PORT` and `WORKERS`, for
example:

```bash
make serve PORT=9000 WORKERS=4
```

The flags:
- `--host 0.0.0.0` listens on all interfaces, not just localhost.
- `--workers N` starts N processes.
- There's no `--reload`.
- If you run behind a reverse proxy (nginx, a cloud load balancer), add `--proxy-headers`
  so client IPs and the scheme are taken from `X-Forwarded-*`.

Check that it's up:

```bash
curl -s localhost:8000/health
```

Notes on running several workers:
- **MCP runs in stateless mode,** so no client session lives in a worker's memory and
  any worker (or replica behind a load balancer) can serve any MCP request. With the
  default stateful mode, a request that reached a different worker failed with an
  unknown session.
- **SQLite serializes writes.** That's fine at this scale. For real multi-instance
  deployments, point `DATABASE_URL` at a server database such as Postgres.

## Run with Docker

You need Docker with Compose v2. Create `.env` with your API key as in [Setup](#setup);
Compose passes it to the container, and it is never copied into the image.

```bash
make docker-up
```

This runs `docker compose up -d --build`: it builds the image and starts the service on
<http://localhost:8000>. Then check it:

```bash
docker compose ps
```

The container should show `healthy` after a few seconds. The examples in [Run](#run) work
unchanged against it.

```bash
make docker-logs
```

```bash
make docker-down
```

`make docker-logs` follows the logs; `make docker-down` stops the service. The database
lives on the `app-data` volume, so it survives restarts and `down`. To also delete the
data, run `docker compose down -v`.

If a local dev server already uses port 8000, choose another host port:

```bash
HOST_PORT=8001 make docker-up
```

To run the test suite inside a container (useful in CI):

```bash
make docker-test
```

This builds the `test` image, then runs the suite in a container. Every test's name and
result are printed, and the command exits with an error if any test fails. The tests run
when the container runs, not during the build, so Docker's build cache can never skip
them. To run a subset, pass a pytest command:

```bash
docker run --rm supply-chain-email-agent:test pytest -k fraud
```

What the image does:
- **Multi-stage build.** uv installs the locked runtime dependencies (`uv sync --frozen`)
  in a cached layer. The final `python:3.12-slim` image contains only that virtualenv,
  `app/` and `src/`. No dev tools, tests, docs or `.env` (see `.dockerignore`).
- **Runs as a non-root user** (`app`), with `DATABASE_URL=sqlite:////data/app.db` on the
  `/data` volume.
- **Seeds the mock ERP on every start.** Seeding is idempotent, so existing data is kept.
  Then it starts uvicorn on port 8000 with `WEB_CONCURRENCY=2` workers.
- **Has a `HEALTHCHECK`** that calls `/health`.

To use the image without Compose:

```bash
make docker-build
```

```bash
docker run -d -p 8000:8000 -e ANTHROPIC_API_KEY -v sc-agent-data:/data supply-chain-email-agent:latest
```

`-e ANTHROPIC_API_KEY` passes the key through from your shell. You can use
`--env-file .env` instead, but plain `docker run` only accepts `KEY=value` lines: it
rejects `export KEY=value`, which Compose and the app itself accept.

## Tests

```bash
make test
```

This runs the pytest suite. It needs no API key and makes no network calls:
- the pipeline, agent and eval tests run the **real** agent with its model scripted
  (PydanticAI's `FunctionModel` via `agent.override`), including real tool calls against
  the in-process MCP server; only the model's replies are fixed;
- the provider tests send real Anthropic and OpenAI requests to a mocked HTTP layer.

Each test runs against a fresh temporary database.

```bash
make lint
```

This runs ruff (lint and format check). `make format` applies the fixes.

## Evaluation

The eval harness runs 20 labeled emails through the **real** pipeline (agent, LLM, MCP
server, code gate) on a throwaway database.

```bash
make eval
```

To run only some cases, repeat them (to measure consistency) or try another model:

```bash
make eval REPEATS=3 CASES=fraud
```

```bash
uv run python -m src.evals --provider openai --model <model-name>
```

It prints a summary and writes `eval-results/<timestamp>-<model>.md`, with a per-case table
and every problem, plus a `.json` file with all the details. The command **exits with an
error if any commitment was wrongly written or wrongly accepted**, so it can gate CI. The
cases live in `src/evals/cases/`; NOTES.md section 6a covers the metrics and results.

For benchmarking beyond the unit tests, [docs/benchmark-datasets.md](docs/benchmark-datasets.md)
surveys public datasets: what's usable for each pipeline step, their licenses, and a
proposed plan for a gold benchmark.

## Configuration

All settings come from environment variables or `.env` (see `.env.example`).

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `LLM_MODEL` | `claude-opus-5-5` for Anthropic | Model name. **Required** when `LLM_PROVIDER=openai`. |
| `LLM_EFFORT` | `medium` for Anthropic; unset for OpenAI | Anthropic `effort`, or OpenAI `reasoning_effort` (reasoning models only) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | none | Provider API key |
| `DATABASE_URL` | `sqlite:///data/app.db` | SQLite database file |
| `ERP_MCP_URL` | unset | ERP MCP server the agent uses. Unset means the in-process mock ERP; set it to, for example, `http://localhost:8000/mcp` to go over HTTP. |
| `REVIEW_CONFIDENCE_THRESHOLD` | `0.8` | Commitments below this model confidence are marked `needs_review` |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `LOG_FORMAT` | `text` | `text` (human-readable) or `json` (one object per line) |
| `PYTHONPATH` | `./` | path to assignment repo |

To use OpenAI instead of Anthropic:

```dotenv
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
LLM_MODEL=<an OpenAI model that supports structured outputs>
```

## Seed data

`make seed` loads two purchase orders that match the example emails:

| PO | Supplier | Order date | Lines |
|---|---|---|---|
| 4500123456 | Acme Components (`acme-components.com`) | 2026-05-15 | 10: JT-8831 × 50; 20: JT-9002 × 25 |
| 4500999911 | NorthBay Fasteners (`northbayfasteners.com`) | 2026-05-20 | 10: NB-4402 × 100 |

Part **AP-1187** (example email 3, "RFQ / PO TBD") is deliberately **not** in the ERP.
That shows the rule that a lead time is never written without an existing PO line.

`make reset-db` drops all tables and reseeds.

## Project layout

```
app/                    FastAPI application only
  main.py               app setup; mounts the MCP server at /mcp
  middleware.py         request IDs and one log line per request
  api/                  routes: emails.py, lead_times.py, errors.py
src/
  config.py             settings
  database/             engine, tables (models.py), data access (crud.py), seed data
  erp/                  ERP service (shared business logic), MCP server, schemas, errors
  agent/                email pipeline: thread parsing, references, ERP client (MCP),
                        PydanticAI agent (read-only ERP tools, hallucination checks),
                        prompt, ERP context, post-processing, fraud checks
  evals/                eval harness: labeled cases, runner, metrics, reports
  utils/                shared helpers: dates, text, email domains, logging setup
tests/
  fixtures/emails/      the four example emails from the assignment, plus fraud cases
docs/
  benchmark-datasets.md survey of public datasets for benchmarking
Dockerfile              multi-stage image (deps, test, runtime)
docker-compose.yml      service with the .env file and a data volume
```

Dependencies only point downward: `utils` → `database` → `erp` → `agent` → `app`. The
REST routes and the MCP tools are both thin wrappers over the same `ERPService`. The agent
reaches the ERP only through MCP.
