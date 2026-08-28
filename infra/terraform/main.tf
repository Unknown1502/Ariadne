/*
 * Ariadne on Google Cloud — the minimum that supports the architecture's claims.
 *
 * The interesting part is the IAM block. Each of the four roles gets its own service
 * account with the minimum it needs, and the Verifier deliberately has no Vertex AI grant:
 * its manifest already refuses to be constructed with an LLM, and this makes the same rule
 * true at the infrastructure layer. If someone bypassed the application check, IAM would
 * still refuse the call.
 *
 * Everything is scale-to-zero and smallest-tier. The evidence ledger is tiny and experiment
 * execution is local arithmetic, so the running cost is dominated by a handful of small
 * Gemini calls per investigation.
 */

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  services = [
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "firestore.googleapis.com",
    "sqladmin.googleapis.com",
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "logging.googleapis.com",
    "cloudtrace.googleapis.com",
  ]

  agent_roles = {
    investigator = "Compiles explanations into claims. Reads lineage, writes claims."
    experimenter = "Designs and runs probes. Writes evidence, never verdicts."
    verifier     = "Deterministic verdicts. No Vertex AI grant, by design."
    governor     = "Bounded policy actions. Publishes events, writes decisions."
  }
}

resource "google_project_service" "enabled" {
  for_each           = toset(local.services)
  service            = each.value
  disable_on_destroy = false
}

# --- identity -------------------------------------------------------------------------

resource "google_service_account" "agent" {
  for_each     = local.agent_roles
  account_id   = "ariadne-${each.key}"
  display_name = "Ariadne ${title(each.key)}"
  description  = each.value
}

# Only the roles that perform semantic reasoning may call Vertex AI. The Verifier is
# absent from this map on purpose - that absence is the security control.
resource "google_project_iam_member" "vertex_user" {
  for_each = toset(["investigator", "experimenter", "governor"])
  project  = var.project_id
  role     = "roles/aiplatform.user"
  member   = "serviceAccount:${google_service_account.agent[each.value].email}"
}

resource "google_project_iam_member" "sql_client" {
  for_each = local.agent_roles
  project  = var.project_id
  role     = "roles/cloudsql.client"
  member   = "serviceAccount:${google_service_account.agent[each.key].email}"
}

resource "google_project_iam_member" "firestore_user" {
  for_each = toset(["experimenter", "governor"])
  project  = var.project_id
  role     = "roles/datastore.user"
  member   = "serviceAccount:${google_service_account.agent[each.value].email}"
}

resource "google_project_iam_member" "publisher" {
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.agent["governor"].email}"
}

# --- events ---------------------------------------------------------------------------

resource "google_pubsub_topic" "model_events" {
  name       = var.model_topic
  depends_on = [google_project_service.enabled]
}

resource "google_pubsub_topic" "dead_letter" {
  name       = var.dead_letter_topic
  depends_on = [google_project_service.enabled]
}

# At-least-once delivery with a bounded retry ladder, then dead-lettering. The worker is
# idempotent, so redelivery is safe; what must not happen is an event retried forever or
# dropped silently.
resource "google_pubsub_subscription" "worker" {
  name  = "${var.model_topic}.worker"
  topic = google_pubsub_topic.model_events.id

  ack_deadline_seconds       = 300
  message_retention_duration = "604800s" # 7 days

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = var.max_delivery_attempts
  }
}

# --- storage --------------------------------------------------------------------------

resource "google_sql_database_instance" "evidence" {
  name                = var.sql_instance_name
  database_version    = "POSTGRES_15"
  region              = var.region
  deletion_protection = false # hackathon environment

  settings {
    tier              = var.sql_tier
    availability_type = "ZONAL"
    disk_size         = 10
    disk_autoresize   = false

    backup_configuration {
      enabled = false # synthetic data; the ledger is reproducible from the demo script
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_sql_database" "ariadne" {
  name     = "ariadne"
  instance = google_sql_database_instance.evidence.name
}

resource "google_firestore_database" "runtime" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.enabled]
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-ariadne-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  lifecycle_rule {
    condition { age = 30 }
    action { type = "Delete" }
  }
}

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "ariadne"
  format        = "DOCKER"
  depends_on    = [google_project_service.enabled]
}

# --- services -------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "api" {
  name     = "ariadne-api"
  location = var.region

  template {
    service_account = google_service_account.agent["governor"].email

    scaling {
      min_instance_count = 0 # scale to zero: this is a demonstration system
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      env {
        name  = "ENABLE_GOOGLE_CLOUD"
        value = "true"
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "EVENT_BUS"
        value = "pubsub"
      }
      env {
        name  = "RUNTIME_STORE"
        value = "firestore"
      }
      env {
        name  = "LLM_PROVIDER"
        value = var.llm_provider
      }
      env {
        name  = "USE_VERTEX_AI"
        value = "true"
      }
      env {
        name  = "DATABASE_URL"
        value = "postgresql+psycopg://ariadne@/ariadne?host=/cloudsql/${google_sql_database_instance.evidence.connection_name}"
      }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.evidence.connection_name]
      }
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_billing_budget" "guardrail" {
  count           = var.billing_account == "" ? 0 : 1
  billing_account = var.billing_account
  display_name    = "ariadne-budget"

  budget_filter {
    projects = ["projects/${var.project_id}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_usd)
    }
  }

  threshold_rules { threshold_percent = 0.5 }
  threshold_rules { threshold_percent = 0.9 }
}

# --- outputs --------------------------------------------------------------------------

output "api_url" {
  value       = google_cloud_run_v2_service.api.uri
  description = "Cloud Run URL. /health and /api/v1/system are the proof endpoints."
}

output "service_accounts" {
  value       = { for role, account in google_service_account.agent : role => account.email }
  description = "One identity per cognitive role. The verifier holds no Vertex AI grant."
}

output "sql_connection_name" {
  value = google_sql_database_instance.evidence.connection_name
}
