# Agent-I:X. `make dev` in one terminal, `make client` in another.

.PHONY: install dev client test eval lint deploy publish

install:
	uv sync --dev --extra lint
	cd client && npm install

# A2A JSON-RPC on :8000/a2a, assets on :8000/assets.
dev:
	uv run uvicorn app.dev_server:app --port 8000 --reload

# Lit client on :5173, proxied to the dev server.
client:
	cd client && npm run dev

test:
	uv run pytest tests/unit tests/integration

eval:
	agents-cli eval run --all

lint:
	uv run --extra lint ruff check app tests
	uv run --extra lint ruff format --check app tests
	cd client && npx tsc --noEmit

# Pinned to the agents-cli the project was scaffolded with; 1.x deploys containers and wants a Dockerfile.
# Needs PROJECT, ASSETS_BUCKET and SERVICE_ACCOUNT (objectAdmin on the bucket, signBlob on itself); GE also needs streaming on the card. See README.
deploy:
	uvx google-agents-cli@0.2.0 deploy --project $(PROJECT) --service-account $(SERVICE_ACCOUNT) --update-env-vars ASSETS_BUCKET=$(ASSETS_BUCKET),A2A_STREAMING=true,DRIVE_EXPORT=false

# A2A registration (A2UI only travels over A2A). GE_APP is the app's full resource name;
# AGENT_CARD_URL is the card URL printed by deploy. AUTH_ID is the GE authorization
# (deployment/create_ge_authorization.py); without it GE calls the agent with no token.
publish:
	agents-cli publish gemini-enterprise --registration-type a2a --agent-card-url $(AGENT_CARD_URL) \
		--gemini-enterprise-app-id $(GE_APP) --display-name "Agent-I:X" \
		--authorization-id $(AUTH_ID)
