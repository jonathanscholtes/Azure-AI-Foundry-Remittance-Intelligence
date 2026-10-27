# Developer Guide

> Back to the [README](../README.md)

For developers who change the code: how to run the tests, and how to run the Function app, API or UI on your machine **against a deployed environment**. The solution has no offline mode: Content Understanding, the Foundry agents and Service Bus are Azure services, so [deploy an environment](deployment_steps.md) first.

## Prerequisites

- Python 3.11+, Azure Functions Core Tools v4, Azurite, Node 20
- A deployed environment, and `az login` as a user with data-plane access to it (the deploying user has it)

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r apps/remittance-processor/requirements.txt
pip install -r apps/api/requirements.txt
```

## Run the Tests

Unit tests need no Azure access. Run them before every change you deploy (or pass `-SkipTests $false` to `deploy.ps1`).

```powershell
Push-Location apps/remittance-processor; python -m unittest discover -s tests; Pop-Location   # Function app
python -m unittest discover -s apps/api/tests                                                   # API
Push-Location apps/ui; npm install; npm run build; Pop-Location                                  # UI build
```

To measure accuracy on real messages whose expected output a person has checked, see [Golden-Set Evaluation](evaluation.md).

## Run the Function App Locally

The Function host runs on your machine; Durable Functions state uses Azurite, and everything else is the deployed environment.

```powershell
Copy-Item apps/remittance-processor/local.settings.example.json apps/remittance-processor/local.settings.json
python scripts/setup_local_storage.py
az login
Push-Location apps/remittance-processor; func start; Pop-Location
```

In `local.settings.json` (git-ignored), set at least `CONTENT_UNDERSTANDING_ENDPOINT`, `ServiceBusConnection__fullyQualifiedNamespace` and `FOUNDRY_PROJECT_ENDPOINT` from the deployment's outputs, plus your `RECEIVING_ORG_ALIASES` and `PRO_*` settings. Without the agent settings, column mapping falls back to header rules, sources are not classified, failing runs stay in review without a second attempt and payer issues go to manual review.

> Work arrives only through the Service Bus intake queue. Dropping a file into storage does nothing: submit it through the API (below), `scripts/Test-Remittance.ps1` or `test/load/load_test.py`. Stop the deployed Function app while you test, or both will pick up messages from the same queue.

## Run the API and UI Locally

The API reads the deployed storage account with your `az login` identity. In a private deployment you need the VPN to reach the storage private endpoints.

```powershell
# Terminal 1 - API (FastAPI)
$env:STORAGE_BLOB_ENDPOINT  = "https://<storage-account>.blob.core.windows.net/"
$env:STORAGE_TABLE_ENDPOINT = "https://<storage-account>.table.core.windows.net/"
$env:SERVICE_BUS_FQDN       = "<namespace>.servicebus.windows.net"
Push-Location apps/api; uvicorn app.main:app --reload --port 8000; Pop-Location

# Terminal 2 - UI (React); Vite proxies /api to http://localhost:8000
Push-Location apps/ui; npm install; npm run dev; Pop-Location
```

## Change the Dashboard

The dashboard is generated: edit `observability/dashboard.py`, not the JSON, then run `python observability/dashboard.py` and redeploy the infrastructure phase. See [observability/README.md](../observability/README.md).
