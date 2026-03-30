# Mail Ingestion Pipeline

Automatically ingests emails from monitored mailboxes in near real-time, classifies them using OpenAI (extracting sender name, email, and intent), and forwards structured JSON to Azure Event Hubs for downstream consumption.

Optionally integrates with **Azure AI Content Understanding** to extract structured fields from PDF and image attachments (ACORD forms, loss runs, certificates of insurance, etc.) before classification — so the intent extracted by the LLM is informed by the actual document content, not just the email body.

## How it works

```
New email arrives in monitored mailbox
        │
        │  Microsoft Graph pushes a change notification
        ▼
Azure Service Bus queue  (no public endpoint required)
        │
        ▼
Ingestion Worker (AKS)
  ├─ Fetches new email + attachment list via Graph delta query
  ├─ [Optional] Downloads each PDF/image attachment
  │    └─ Submits to Azure Content Understanding → structured fields
  ├─ Calls OpenAI → extracts { name, email, intent }
  │    (CU-extracted fields injected as context if available)
  └─ Publishes JSON → Azure Event Hubs
```

A **reconciler** runs hourly as a Kubernetes CronJob. It checks every monitored mailbox against live Graph subscriptions, creates any that are missing, and renews any expiring within 48 hours — so subscriptions are self-healing with no manual intervention.

### Output event shape

Every email produces one JSON message on Event Hubs.

**Without Content Understanding** (or emails with no attachments):
```json
{
  "event_id":     "550e8400-e29b-41d4-a716-446655440000",
  "extracted_at": "2026-03-13T14:23:01Z",
  "email_id":     "AAMkAGI...",
  "mailbox_id":   "broker@example.com",
  "subject":      "Re: Policy Renewal Q2",
  "received_at":  "2026-03-13T14:20:55Z",
  "sender": {
    "name":  "Jane Broker",
    "email": "jbroker@insureco.com"
  },
  "intent": "Requesting a renewal quote for the commercial property policy expiring May 1.",
  "model":  "gpt-4o-mini"
}
```

**With Content Understanding enabled** (email has PDF/image attachments):
```json
{
  "event_id":     "550e8400-e29b-41d4-a716-446655440000",
  "extracted_at": "2026-03-13T14:23:01Z",
  "email_id":     "AAMkAGI...",
  "mailbox_id":   "broker@example.com",
  "subject":      "Re: Policy Renewal Q2",
  "received_at":  "2026-03-13T14:20:55Z",
  "sender": {
    "name":  "Jane Broker",
    "email": "jbroker@insureco.com"
  },
  "intent": "Requesting renewal for commercial property policy CP-2041 expiring May 1, total insured value $4.2M.",
  "model":  "gpt-4o-mini",
  "attachments": [
    {
      "filename":     "loss_run_2025.pdf",
      "content_type": "application/pdf",
      "size_bytes":   142300,
      "analyzer_id":  "prebuilt-read",
      "fields": {
        "PolicyNumber":   {"value": "CP-2041",     "confidence": 0.98},
        "InsuredName":    {"value": "Acme Corp",   "confidence": 0.97},
        "TotalLosses":    {"value": "$18,400",     "confidence": 0.95},
        "PolicyPeriod":   {"value": "2024–2025",   "confidence": 0.96}
      },
      "markdown": "# Loss Run Report\n**Policy:** CP-2041 ..."
    },
    {
      "filename":     "photo.heic",
      "content_type": "image/heic",
      "size_bytes":   3200000,
      "skipped_reason": "unsupported_type"
    }
  ]
}
```

The intent is notably richer when CU fields are available — the LLM receives the extracted policy number, coverage amounts, and dates as context alongside the email body.

---

## Content Understanding (optional)

Attachment extraction is opt-in. Set three environment variables to enable it — the pipeline works identically without them.

| Variable | Description |
|---|---|
| `AZURE_CU_ENDPOINT` | Your Azure AI Services endpoint, e.g. `https://<resource>.cognitiveservices.azure.com` |
| `AZURE_CU_API_KEY` | API key for the resource |
| `AZURE_CU_ANALYZER_ID` | Analyzer to use (default: `prebuilt-read`). Use a custom analyzer for ACORD forms or loss runs. |

Add these to `k8s/configmap.yaml` (endpoint + analyzer ID) and `k8s/secrets-template.yaml` (API key) then redeploy.

**Supported attachment types:** PDF, JPEG, PNG, TIFF, BMP. Attachments of other types or over 10 MB are included in the event with a `skipped_reason` field and not sent to CU.

**Custom analyzers:** The `prebuilt-read` default extracts raw text and basic layout. For structured field extraction from domain-specific forms (ACORD 125, loss runs, certificates of insurance), create a custom analyzer in Azure AI Foundry and set `AZURE_CU_ANALYZER_ID` to its ID.

---

## Prerequisites

### Tools

Install the following before starting:

| Tool | Min version | Install |
|---|---|---|
| Azure CLI | 2.60 | https://learn.microsoft.com/cli/azure/install-azure-cli |
| Terraform | 1.7 | https://developer.hashicorp.com/terraform/install |
| kubectl | 1.29 | https://kubernetes.io/docs/tasks/tools/ |
| Docker | 24 | https://docs.docker.com/get-docker/ |
| Python | 3.11 | https://www.python.org/downloads/ |
| jq | any | https://jqlang.github.io/jq/download/ |

Verify everything is installed:

```bash
az --version && terraform --version && kubectl version --client && docker --version && python3 --version && jq --version
```

### Azure requirements

- An active Azure subscription
- **Global Admin or Privileged Role Admin** access in the Entra ID tenant (required for the App Registration step below)
- The mailboxes you want to monitor must have **Exchange Online licenses** — Graph `Mail.Read` does not work against personal Microsoft accounts

### OpenAI

An OpenAI API key (`sk-...`) from https://platform.openai.com/api-keys. The pipeline uses `gpt-4o-mini` by default — change `OPENAI_MODEL` in `k8s/configmap.yaml` if you want a different model.

---

## Step 1 — Create the App Registration in Entra ID

> **This step requires Global Admin or Privileged Role Admin.** It cannot be done by Terraform without elevated AAD permissions. Do this first.

1. Go to **Azure Portal → Entra ID → App registrations → New registration**
   - Name: `mail-ingestion-pipeline`
   - Supported account types: **Accounts in this organizational directory only**
   - Click **Register**

2. On the app's overview page, copy and save:
   - **Application (client) ID** → this is your `GRAPH_CLIENT_ID`
   - **Directory (tenant) ID** → this is your `GRAPH_TENANT_ID`

3. Go to **API permissions → Add a permission → Microsoft Graph → Application permissions**. Add all three:
   - `Mail.Read`
   - `Mail.Send` *(required for `scripts/send_test_emails.py` — safe to skip if you won't use the test sender)*
   - `Subscription.ReadWrite.All`

4. Click **Grant admin consent for [your tenant]** and confirm. Both permissions must show a green ✓ status.

5. Go to **Certificates & secrets → New client secret**
   - Description: `mail-ingestion-pipeline`
   - Expiry: 24 months (or per your org policy)
   - Click **Add**, then **immediately copy the secret value** — it is only shown once
   - This is your `GRAPH_CLIENT_SECRET`

---

## Step 2 — Log in to Azure

```bash
az login
az account set --subscription "<your-subscription-id>"

# Confirm you're pointed at the right subscription
az account show --query "{name:name, id:id}"
```

---

## Step 3 — Clone the repo

```bash
git clone <repo-url>
cd mail-ingestion-pipeline
```

---

## Step 4 — Provision Azure infrastructure with Terraform

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
```

Open `terraform.tfvars` and fill in the values:

```hcl
prefix   = "contoso"    # Short, lowercase, no hyphens. Used in all resource names.
location = "eastus"     # Azure region

# A random secret string you choose. Must match GRAPH_CLIENT_STATE later.
# Example: openssl rand -hex 16
graph_client_state = "replace-with-a-random-string"

openai_api_key = "sk-..."
```

Apply:

```bash
terraform init
terraform apply
```

Type `yes` when prompted. This takes approximately **8–12 minutes** (most of the time is AKS cluster provisioning).

When it completes, Terraform prints a summary of created resources. You don't need to copy anything — the deploy script reads these outputs automatically.

---

## Step 5 — Deploy to AKS

Set the environment variables from Step 1, then run the deploy script:

```bash
cd ..   # back to repo root

export GRAPH_TENANT_ID="<from step 1>"
export GRAPH_CLIENT_ID="<from step 1>"
export GRAPH_CLIENT_SECRET="<from step 1>"
export GRAPH_CLIENT_STATE="<same value you put in terraform.tfvars>"
export OPENAI_API_KEY="sk-..."

./scripts/deploy.sh
```

The script will:
- Build and push the Docker images to ACR
- Configure `kubectl` to point at the AKS cluster
- Create the Kubernetes secret from your env vars and Terraform outputs
- Deploy the ingestion worker and reconciler CronJob
- Print confirmation when done

---

## Step 6 — Onboard mailboxes

For each mailbox you want to monitor:

```bash
./scripts/register_mailbox.sh user@yourdomain.com "Display Name"
```

This immediately creates a Graph change-notification subscription for that mailbox, pointing at the Service Bus queue. Any email arriving in the inbox from this point forward will flow through the pipeline.

You can onboard multiple mailboxes:

```bash
./scripts/register_mailbox.sh broker1@example.com "Jane Broker"
./scripts/register_mailbox.sh broker2@example.com "John Smith"
```

That's it. The pipeline is live.

---

## Verifying it works

**Check pods are running:**
```bash
kubectl get pods -n mail-ingestion
```
```
NAME                                READY   STATUS    RESTARTS   AGE
ingestion-worker-7d9f8b6c4-xk2pj   1/1     Running   0          2m
```

**Stream live logs from the worker:**
```bash
kubectl logs -n mail-ingestion -l app=ingestion-worker -f
```

**Send test emails programmatically** (streams realistic insurance broker emails into the pipeline):
```bash
pip install msal httpx

python scripts/send_test_emails.py \
  --to broker@yourdomain.com \
  --from-mailbox sender@yourdomain.com \
  --delay 15 \
  --loop
```

Or send a fixed batch:
```bash
python scripts/send_test_emails.py \
  --to broker@yourdomain.com \
  --from-mailbox sender@yourdomain.com \
  --count 5
```

Within 30–60 seconds of each send you should see log lines like:
```
Delta query for broker@example.com → 1 messages
Published: email=AAMkAGI... sender=jbroker@insureco.com intent="Requesting renewal quote..."
```

**Check the reconciler ran:**
```bash
kubectl get jobs -n mail-ingestion
```

**Trigger the reconciler manually** (useful to force a subscription check without waiting for the hour):
```bash
kubectl create job --from=cronjob/subscription-reconciler reconciler-manual -n mail-ingestion
kubectl logs -n mail-ingestion -l app=subscription-reconciler -f
```

---

## Setting up CI/CD (GitHub Actions)

Skip this section if you just want the pipeline running — the deploy script above is sufficient. Set this up if you want code changes automatically deployed when you push to `main`.

### 1. Create a federated credential on the App Registration

This lets GitHub Actions authenticate to Azure without storing a client secret.

In Azure Portal → Entra ID → App registrations → `mail-ingestion-pipeline` → **Certificates & secrets → Federated credentials → Add credential**:

- Federated credential scenario: **GitHub Actions deploying Azure resources**
- Organization: your GitHub org or username
- Repository: this repo name
- Entity type: **Branch**
- Branch: `main`
- Click **Add**

### 2. Add secrets to the GitHub repository

Go to your GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**. Add each of the following:

| Secret name | Value |
|---|---|
| `AZURE_CLIENT_ID` | App registration client ID (from Step 1) |
| `AZURE_TENANT_ID` | Your Entra tenant ID (from Step 1) |
| `AZURE_SUBSCRIPTION_ID` | `az account show --query id -o tsv` |
| `ACR_NAME` | `cd infra && terraform output -raw acr_login_server \| cut -d. -f1` |
| `ACR_LOGIN_SERVER` | `cd infra && terraform output -raw acr_login_server` |
| `AKS_CLUSTER_NAME` | `cd infra && terraform output -raw aks_cluster_name` |
| `AZURE_RESOURCE_GROUP` | `cd infra && terraform output -raw resource_group_name` |

From this point, pushing to `main` will automatically build new images and roll them out to AKS.

---

## Day-2 operations

### Add a new mailbox
```bash
./scripts/register_mailbox.sh newuser@yourdomain.com "New User"
```

### Remove a mailbox
Delete the document from Cosmos DB (container: `mailboxes`, database: `mail-ingestion`) and the reconciler will stop renewing the subscription. The Graph subscription will naturally expire within 3 days.

### Subscription renewals
Fully automatic. The reconciler runs every hour and renews any subscription expiring within 48 hours. No manual action required.

### Scale the worker
The worker scales automatically between 1 and 10 replicas based on CPU. To adjust the bounds:
```bash
kubectl edit hpa ingestion-worker-hpa -n mail-ingestion
```

### Change the OpenAI model
Edit `k8s/configmap.yaml`, update `OPENAI_MODEL`, then:
```bash
kubectl apply -f k8s/configmap.yaml
kubectl rollout restart deployment/ingestion-worker -n mail-ingestion
```

### View all monitored mailboxes
In the Azure Portal → Cosmos DB → `mail-ingestion` database → `mailboxes` container → Items. Each document shows the mailbox status and its active Graph subscriptions.

---

## Infrastructure overview

All resources are provisioned in a single resource group named `rg-<prefix>`.

| Resource | Purpose |
|---|---|
| AKS cluster | Runs the ingestion worker and reconciler |
| Azure Container Registry | Stores Docker images |
| Azure Service Bus queue | Receives Graph change notifications directly |
| Azure Event Hubs | Output destination for classified email JSON |
| Cosmos DB | Tracks monitored mailboxes and subscription state |
| Storage Account | Persists Graph delta tokens across pod restarts |
| Key Vault | Stores all generated secrets for auditability |

---

## Repository layout

```
.
├── src/
│   ├── shared/
│   │   ├── graph_client.py                 # Graph API: auth, subscriptions, delta query, attachments
│   │   ├── cosmos_client.py                # Cosmos DB: mailbox + subscription tracking
│   │   └── content_understanding_client.py # Azure CU: submit bytes, poll result, extract fields
│   ├── reconciler/
│   │   ├── reconciler.py          # Hourly subscription reconciliation
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   └── ingestion_worker/
│       ├── ingestion_worker.py    # ASB consumer → delta query → OpenAI → Event Hubs
│       ├── delta_token_store.py   # Blob Storage delta token cache
│       ├── requirements.txt
│       └── Dockerfile
├── infra/                         # Terraform (all Azure infrastructure)
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── providers.tf
│   └── terraform.tfvars.example
├── k8s/                           # Kubernetes manifests
│   ├── namespace.yaml
│   ├── configmap.yaml
│   ├── secrets-template.yaml      # Reference only — never commit real values
│   ├── reconciler/
│   │   └── cronjob.yaml
│   └── ingestion-worker/
│       ├── deployment.yaml
│       ├── service.yaml
│       └── hpa.yaml
├── scripts/
│   ├── deploy.sh                  # One-shot full deploy (reads from terraform output)
│   └── register_mailbox.sh        # Onboard a mailbox
└── .github/
    └── workflows/
        ├── build-push.yml         # Build + push images on push to main
        └── deploy.yml             # Deploy to AKS after successful build
```

---

## Security notes

- Secrets are never stored in source control. `deploy.sh` creates the Kubernetes secret imperatively from environment variables and `terraform output`.
- The Service Bus queue has two separate auth rules: `graph-send` (Send only, embedded in Graph notification URLs) and `worker-listen` (Listen only, used by the ingestion worker).
- The Event Hub has separate Send and Listen auth rules.
- All generated credentials are stored in Key Vault for auditability.
- The App Registration has the minimum required permissions: `Mail.Read` + `Subscription.ReadWrite.All` (application permissions, not delegated).
