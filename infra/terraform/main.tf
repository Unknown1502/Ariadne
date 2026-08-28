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
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # Application Default Credentials carry whatever quota project was last set globally on
  # this machine, which is not necessarily this deployment's project. Without this, an API
  # that checks quota against the caller's ADC default (billingbudgets did) fails with a
  # confusing 403 even though the caller has full access to var.project_id.
  billing_project       = var.project_id
  user_project_override = true
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
    "cloudresourcemanager.googleapis.com",
    "billingbudgets.googleapis.com",
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

# The Cloud Run service runs as this same identity (see service_account on
# google_cloud_run_v2_service.api below) and is the one whose lifespan starts the
# streaming-pull subscriber. Publish-only access let it queue MODEL_VERSION_DEPLOYED events
# successfully - the POST endpoint returned 200 every time - while the subscriber silently
# had no permission to pull them back. Nothing crashed, nothing logged an error the request
# path could see, and the messages sat in the subscription indefinitely: the headline demo
# claim, "the worker wakes up with no one clicking Analyze," was demonstrably false in the
# actual deployment while every part of it that a health check or an HTTP 200 could see
# looked correct. Found only by triggering a real event against the real deployment and
# checking whether an investigation actually appeared.
resource "google_project_iam_member" "subscriber" {
  project = var.project_id
  role    = "roles/pubsub.subscriber"
  member  = "serviceAccount:${google_service_account.agent["governor"].email}"
}

# Cloud Build's default service account holds only roles/cloudbuild.builds.builder out of
# the box - the modern, deliberately narrower default. That role does not include pushing
# to Artifact Registry or deploying Cloud Run, which infra/cloudbuild/cloudbuild.yaml's own
# push and deploy-api steps both need. Without these, `gcloud builds submit` gets through
# the build step and fails on push with a permission error - invisible until a real build
# is actually submitted, which is exactly how this gap was found.
data "google_project" "current" {}

resource "google_project_iam_member" "cloudbuild_artifact_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
}

resource "google_project_iam_member" "cloudbuild_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
}

# `gcloud run deploy` also needs to act as the service account the Cloud Run revision will
# run as (the governor identity, per google_cloud_run_v2_service.api below).
resource "google_service_account_iam_member" "cloudbuild_act_as_governor" {
  service_account_id = google_service_account.agent["governor"].name
  role                = "roles/iam.serviceAccountUser"
  member              = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
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

# The Cloud Run env var below references user "ariadne" with no password. Without these two
# resources that user does not exist, and the deployed API would fail every database call -
# a gap invisible until an actual deployment tried to connect, which is exactly the class of
# defect this project's own hostile-review discipline exists to catch before it ships.
resource "random_password" "sql_ariadne" {
  length  = 32
  special = false # simplest safe value to carry through a URL query string unescaped
}

resource "google_sql_user" "ariadne" {
  name     = "ariadne"
  instance = google_sql_database_instance.evidence.name
  password = random_password.sql_ariadne.result
}

resource "google_secret_manager_secret" "database_url" {
  secret_id = "ariadne-database-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_version" "database_url" {
  secret = google_secret_manager_secret.database_url.id
  secret_data = "postgresql+psycopg://ariadne:${random_password.sql_ariadne.result}@/ariadne?host=/cloudsql/${google_sql_database_instance.evidence.connection_name}"
}

# Only the identity that runs the Cloud Run service needs to read this.
resource "google_secret_manager_secret_iam_member" "database_url_access" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent["governor"].email}"
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
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.evidence.connection_name]
      }
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_secret_manager_secret_version.database_url,
    google_secret_manager_secret_iam_member.database_url_access,
  ]
}

# `gcloud run deploy --allow-unauthenticated`, run from inside infra/cloudbuild/cloudbuild.yaml
# under the Cloud Build service account, silently did not add this binding - no error, no
# warning, just an IAM policy with zero bindings on the deployed service and every request
# answered with a 403 from Google's front end rather than the app. The identical command
# with an owner identity applied instantly, which places the gap specifically at
# `run.services.setIamPolicy` under the Cloud Build service account rather than at an org
# policy or anything about the service itself. Declaring the binding here removes the
# CLI flag's silent-failure mode entirely: Terraform either sets this policy or reports why
# it could not, and a `terraform plan` shows a missing binding as a pending change instead
# of a page that looks identical whether the service is public or not.
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# The investigation console. Holds no state of its own and reaches Firestore, Cloud SQL, and
# Pub/Sub only indirectly, through the API - so unlike the api service it runs as the
# project's default compute identity rather than one of the four cognitive-role service
# accounts, and needs no Vertex AI, Cloud SQL, Firestore, or Pub/Sub grants at all.
resource "google_cloud_run_v2_service" "console" {
  name     = "ariadne-console"
  location = var.region

  template {
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.console_image

      resources {
        limits = {
          cpu    = "1"
          # Cloud Run's default CPU-always-allocated mode refuses under 512Mi.
          memory = "512Mi"
        }
      }
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_cloud_run_v2_service_iam_member" "console_public_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.console.name
  role     = "roles/run.invoker"
  member   = "allUsers"
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

output "console_url" {
  value       = google_cloud_run_v2_service.console.uri
  description = "The investigation console. Proxies /api and /health to api_url."
}

output "service_accounts" {
  value       = { for role, account in google_service_account.agent : role => account.email }
  description = "One identity per cognitive role. The verifier holds no Vertex AI grant."
}

output "sql_connection_name" {
  value = google_sql_database_instance.evidence.connection_name
}
